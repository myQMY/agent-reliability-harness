"""终态判分：DB 逐字段 diff + 策略遵循 + 必要告知，输出 0/1 与失败类型。

纯函数：不写文件、不调模型、不读时钟。判分口径见 docs/mvp-design.md §2 / README。
failure_type 使用固定枚举，保证归因分布可复算。
"""
from __future__ import annotations

from typing import Any, Callable

from env import db
from runtime.loop import (
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
)
NO_FAILURE = "none"

CheckResult = tuple[bool, str]


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


def grade(
    state: db.State,
    task: dict[str, Any],
    run_info: dict[str, Any],
) -> dict[str, Any]:
    """返回 passed / failure_type / reasons；不产生任何副作用。"""
    termination = run_info["termination"]
    errors: list[str] = list(run_info["errors"])
    final_text: str = run_info["final_text"]

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
        return {"passed": 1, "failure_type": NO_FAILURE, "reasons": []}

    if termination == TERMINATION_JSON_FAILURE:
        failure_type = "output_format"
    elif termination == TERMINATION_MAX_STEPS:
        failure_type = "loop_or_timeout"
    elif policy_failures:
        failure_type = "policy_violation"
    elif state_mismatch and "wrong_tool" in errors:
        failure_type = "wrong_tool"
    elif state_mismatch and "bad_args" in errors:
        failure_type = "bad_args"
    else:
        failure_type = "intent_misread"

    return {
        "passed": 0,
        "failure_type": failure_type,
        "reasons": reasons,
    }
