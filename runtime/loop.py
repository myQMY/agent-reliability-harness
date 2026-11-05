"""最小 Agent 运行时：while 循环 + 结构化工具调用。

loop 保持同步纯逻辑：不写文件、不读时钟、不调网络。
token / 延迟观测由 Backend.generate 的 ModelReply 带进来，只挂在
assistant span 上；loop 自己不测量任何东西。
每个事件产出一条内存 span，由 eval 层统一序列化并落盘，
这样 W5 做并发时循环与判分逻辑都不用改。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from env import tools
from runtime import guard
from runtime.model import Backend

TERMINATION_FINAL = "final"
TERMINATION_MAX_STEPS = "max_steps"
TERMINATION_JSON_FAILURE = "json_failure"

RETRY_HINT = "上一次输出不是合法的单个 JSON 对象，请只输出一个 JSON 对象。"


@dataclass
class StepCounter:
    """trace span 的全局序号，跨多轮对话连续递增。"""

    value: int = 0

    def next(self) -> int:
        self.value += 1
        return self.value


@dataclass
class TurnResult:
    final_text: str = ""
    termination: str = ""
    spans: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def make_span(
    trial_id: str,
    step: int,
    role: str,
    tool: str | None = None,
    args: Any = None,
    observation: Any = None,
    *,
    tokens: int = 0,
    latency_ms: int = 0,
) -> dict[str, Any]:
    """设计文档 §6 的 8 个字段一律补齐；确定性序列化由 eval/trace.py 负责。

    只有 assistant span 携带模型观测；其余 span 用默认 0，
    这样结果行的 sum(tokens) 恰好等于本次 trial 的模型总用量。
    """
    return {
        "trial_id": trial_id,
        "step": step,
        "role": role,
        "tool": tool,
        "args": args,
        "observation": observation,
        "tokens": tokens,
        "latency_ms": latency_ms,
    }


def parse_action(raw: str) -> tuple[dict[str, Any] | None, str]:
    """解析模型输出；缺字段与非法 JSON 都算输出格式失败。"""
    try:
        action = json.loads(raw)
    except (ValueError, TypeError):
        return None, "invalid_json"
    if not isinstance(action, dict):
        return None, "invalid_json"
    if "final" in action and isinstance(action["final"], str):
        return action, ""
    if (
        "tool" in action
        and isinstance(action["tool"], str)
        and isinstance(action.get("args", {}), dict)
    ):
        return action, ""
    return None, "missing_fields"


def run_turn(
    messages: list[dict[str, str]],
    backend: Backend,
    conn: sqlite3.Connection,
    *,
    trial_id: str,
    counter: StepCounter,
    budget: guard.StepBudget,
    max_json_retries: int = guard.MAX_JSON_RETRIES,
    guards: tuple[str, ...] = (),
    guard_state: guard.GuardState | None = None,
) -> TurnResult:
    """跑一轮对话，直到模型给出 final 或触发护栏。

    max_json_retries / guards 由实验配置注入，默认值与 W1 冻结基线一致：
    不传时行为完全不变，护栏全部关闭。``guard_state`` 是 idempotency 护栏
    专用的跨 turn 状态，由 runner 在一次 trial 里创建并复用；不传则为 None，
    该护栏不生效，因此只开 schema_validation 的既有留档逐字节不变。

    前两条护栏在工具执行**之前**拦截：拦下时不调 tools.call（不写库、不计入
    errors），而是把 ``GUARD_REJECT_PREFIX`` 消息追加进 messages，让后端
    有机会重发正确调用。第三条 ``tool_retry`` 在工具层**报错之后**工作：
    如实保留 tool span 与 errors，只额外追加一条拒绝消息触发重发，因此
    ``invalid_calls`` 的口径不因开关而改变。第四条 ``json_recovery`` 在
    JSON 解析失败之后工作：**保留 W1 的 RETRY_HINT 证据**，只额外追加一条
    拒绝消息，让后端重发被跳过的那一个动作，覆盖 ``bad_json`` 注入故障。
    guard span 只作证据，grader 不消费这个 role，因此判分口径不因开启
    护栏而改变。四条护栏共用同一拦截预算；前两条在工具执行前按固定顺序
    判定，后两条各占一个独立分支，结果不依赖集合遍历顺序。
    """
    result = TurnResult()
    consecutive_format_errors = 0
    guard_rejections = 0
    while True:
        if not budget.take():
            result.termination = TERMINATION_MAX_STEPS
            return result

        reply = backend.generate(messages)
        raw = reply.text
        messages.append({"role": "assistant", "content": raw})
        result.spans.append(
            make_span(
                trial_id,
                counter.next(),
                "assistant",
                observation=raw,
                tokens=reply.tokens,
                latency_ms=reply.latency_ms,
            )
        )

        action, reason = parse_action(raw)
        if action is None:
            consecutive_format_errors += 1
            messages.append({"role": "user", "content": RETRY_HINT})
            result.spans.append(
                make_span(
                    trial_id,
                    counter.next(),
                    "user",
                    observation=f"{RETRY_HINT} ({reason})",
                )
            )
            # 第四条护栏：W1 的 JSON 重试只提示，被替换掉的动作会被跳过。
            # 保留上面的 RETRY_HINT user span（grader 据此归因 output_format），
            # 只额外追加 guard span 与一条 [GUARD-REJECT] 消息触发重发。
            # 固有的连续坏 JSON 没有干净动作可重发，会自然收敛到
            # TERMINATION_JSON_FAILURE，护栏不越界去修它。
            if (
                guard.GUARD_JSON_RECOVERY in guards
                and reason in guard.RECOVERABLE_FORMAT_REASONS
                and guard_rejections < guard.MAX_GUARD_REJECTIONS
            ):
                recovery = guard.json_recovery_hint(reason)
                guard_rejections += 1
                result.spans.append(
                    make_span(
                        trial_id,
                        counter.next(),
                        "guard",
                        observation=recovery,
                    )
                )
                messages.append({"role": "user", "content": recovery})
            if consecutive_format_errors > max_json_retries:
                result.termination = TERMINATION_JSON_FAILURE
                return result
            continue

        consecutive_format_errors = 0
        if "final" in action:
            result.final_text = action["final"]
            result.termination = TERMINATION_FINAL
            return result

        name = action["tool"]
        args = action.get("args", {})
        rejection = None
        if guard_rejections < guard.MAX_GUARD_REJECTIONS:
            if guard.GUARD_SCHEMA_VALIDATION in guards:
                rejection = guard.rejection_message(name, args)
            if (
                rejection is None
                and guard.GUARD_IDEMPOTENCY in guards
                and guard_state is not None
            ):
                rejection = guard.idempotency_rejection(name, args, guard_state)
        if rejection is not None:
            guard_rejections += 1
            result.spans.append(
                make_span(
                    trial_id,
                    counter.next(),
                    "guard",
                    tool=name,
                    args=args,
                    observation=rejection,
                )
            )
            messages.append({"role": "user", "content": rejection})
            continue
        observation, error = tools.call(name, args, conn)
        if error is None:
            guard.record_write(name, args, guard_state)
        result.spans.append(
            make_span(
                trial_id,
                counter.next(),
                "tool",
                tool=name,
                args=args,
                observation=observation,
            )
        )
        if error is not None:
            result.errors.append(error)
        messages.append(
            {
                "role": "tool",
                "content": json.dumps(
                    observation, ensure_ascii=False, sort_keys=True
                ),
            }
        )
        # 第三条护栏：失败之后请求重发。tool span / errors / tool 观察全部
        # 保留，trace 因此能完整还原「工具报错 → 护栏要求重发 → 重发成功」。
        if (
            error is not None
            and error in guard.RETRYABLE_TOOL_ERRORS
            and guard.GUARD_TOOL_RETRY in guards
            and guard_rejections < guard.MAX_GUARD_REJECTIONS
        ):
            retry = guard.tool_retry_message(name, error)
            guard_rejections += 1
            result.spans.append(
                make_span(
                    trial_id,
                    counter.next(),
                    "guard",
                    tool=name,
                    args=args,
                    observation=retry,
                )
            )
            messages.append({"role": "user", "content": retry})
