"""终态判分：DB 逐字段 diff + 策略遵循 + 必要告知，输出 0/1 与失败类型。

纯函数：不写文件、不调模型、不读时钟。判分口径见 docs/mvp-design.md §2 / README。

归因不再靠聚合后的 errors 列表猜，而是沿 trace 按 step 顺序找**第一个
可判定偏离点**：工具层报错、JSON 重试提示、造成策略违规的写操作、终止异常。
`failure_type` 仍是单标签主归因（便于分布统计），并列原因放在 `reasons`，
偏离点的位置与类型放在 `first_divergence`；判不出时如实记为 `unknown`。
"""
from __future__ import annotations

from typing import Any, Callable

from env import db
from runtime.loop import (
    RETRY_HINT,
    TERMINATION_FINAL,
    TERMINATION_JSON_FAILURE,
    TERMINATION_MAX_STEPS,
)

FAILURE_TYPES = (
    "intent_misread",
    "wrong_tool",
    "bad_args",
    "policy_violation",
    "loop_or_timeout",
    "output_format",
    "unknown",
)
NO_FAILURE = "none"

WRITE_TOOLS = frozenset({"refund_order", "cancel_order", "notify_customer"})

CheckResult = tuple[bool, str]
Divergence = dict[str, Any] | None


def _no_over_refund(state: db.State) -> bool:
    return all(row["refunded_cents"] <= row["total_cents"] for row in state["orders"])


def _no_refund_after_cancel(state: db.State) -> bool:
    return all(
        not (row["status"] == "cancelled" and row["refunded_cents"] > 0)
        for row in state["orders"]
    )


def _single_refund_per_order(state: db.State) -> bool:
    keys = [
        row["ref_key"]
        for row in state["audit_log"]
        if row["action"] == "refund"
    ]
    return len(keys) == len(set(keys))


def _notify_after_refund(state: db.State) -> bool:
    logs = state["audit_log"]
    messages = [row["message"] for row in logs if row["action"] == "notify"]
    refund_keys = [
        row["ref_key"] for row in logs if row["action"] == "refund"
    ]
    return all(any(key in message for message in messages) for key in refund_keys)


POLICY_CHECKERS: dict[str, Callable[[db.State], bool]] = {
    "no_over_refund": _no_over_refund,
    "no_refund_after_cancel": _no_refund_after_cancel,
    "single_refund_per_order": _single_refund_per_order,
    "notify_after_refund": _notify_after_refund,
}


def trace_events(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 span 流压成按 step 排序的可归因事件；不做任何判分。"""
    events: list[dict[str, Any]] = []
    for span in spans:
        role = span.get("role")
        observation = span.get("observation")
        step = int(span.get("step", 0))
        if role == "tool" and isinstance(observation, dict):
            error = observation.get("error")
            tool = str(span.get("tool") or "")
            if error in ("wrong_tool", "bad_args"):
                events.append({"step": step, "kind": error, "detail": tool})
            elif tool in WRITE_TOOLS:
                events.append({"step": step, "kind": "state_write", "detail": tool})
        elif role == "user" and isinstance(observation, str) and observation.startswith(
            RETRY_HINT
        ):
            events.append(
                {"step": step, "kind": "output_format", "detail": "json_retry"}
            )
        elif role == "assistant":
            events.append({"step": step, "kind": "assistant", "detail": ""})
    events.sort(key=lambda event: event["step"])
    return events


def _attribute(
    state_mismatch: bool,
    policy_failures: list[str],
    missing: list[str],
    termination: str,
    events: list[dict[str, Any]],
    errors: list[str],
) -> tuple[str, Divergence, str]:
    """返回 (主归因, 偏离点证据, 证据来源)。优先级即偏离点的时间顺序。"""
    last = events[-1] if events else None

    if termination == TERMINATION_JSON_FAILURE:
        retries = [event for event in events if event["kind"] == "output_format"]
        return "output_format", (retries[-1] if retries else last), "trace"
    if termination == TERMINATION_MAX_STEPS:
        # 超时无法定位到单个错误动作，用耗尽预算的最后一步作锚点
        return "loop_or_timeout", last, "trace"
    if policy_failures:
        writes = [event for event in events if event["kind"] == "state_write"]
        return "policy_violation", (writes[0] if writes else None), "trace"
    # 按 step 顺序取第一个工具层错误，而不是按类型优先级挑：
    # 一个 trial 里若先后出现多种错误，主归因必须是最早偏离的那一步
    hit = next(
        (event for event in events if event["kind"] in ("wrong_tool", "bad_args")),
        None,
    )
    if hit is not None:
        return hit["kind"], hit, "trace"
    if not events and errors:
        # 调用方没给 trace 时降级到聚合 errors，并在 reasons 里如实标注
        for kind in ("wrong_tool", "bad_args"):
            if kind in errors:
                return kind, None, "errors_fallback"
    if state_mismatch:
        return "unknown", None, "none"
    if missing:
        # 终态正确、只是没说该说的话：属于理解 / 表达偏差，有明确证据
        return "intent_misread", None, "none"
    return "unknown", None, "none"


def grade(
    state: db.State,
    task: dict[str, Any],
    run_info: dict[str, Any],
) -> dict[str, Any]:
    """返回 passed / failure_type / reasons / first_divergence；不产生任何副作用。"""
    termination = run_info["termination"]
    errors: list[str] = list(run_info["errors"])
    final_text: str = run_info["final_text"]
    spans: list[dict[str, Any]] = list(run_info.get("spans", ()))

    target = db.normalize_target(task["target_db"], task.get("initial_db"))
    state_mismatch = state != target

    policy_failures: list[str] = []
    for name in task.get("policy_checks", ()):
        checker = POLICY_CHECKERS.get(name)
        if checker is None or not checker(state):
            policy_failures.append(name)

    missing = [
        token
        for token in task.get("required_disclosures", ())
        if token not in final_text
    ]

    reasons: list[str] = []
    if state_mismatch:
        reasons.append("terminal_state_mismatch")
    if policy_failures:
        reasons.append("policy:" + ",".join(policy_failures))
    if missing:
        reasons.append("missing_disclosures:" + ",".join(missing))
    if termination != TERMINATION_FINAL:
        reasons.append("termination:" + termination)

    passed = (
        not state_mismatch
        and not policy_failures
        and not missing
        and termination == TERMINATION_FINAL
    )
    if passed:
        return {
            "passed": 1,
            "failure_type": NO_FAILURE,
            "reasons": [],
            "first_divergence": None,
        }

    events = trace_events(spans)
    failure_type, divergence, evidence = _attribute(
        state_mismatch, policy_failures, missing, termination, events, errors
    )
    reasons.append("attribution:" + failure_type)
    reasons.append("evidence:" + evidence)
    return {
        "passed": 0,
        "failure_type": failure_type,
        "reasons": sorted(reasons),
        "first_divergence": divergence,
    }
