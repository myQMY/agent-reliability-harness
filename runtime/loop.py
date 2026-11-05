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
) -> TurnResult:
    """跑一轮对话，直到模型给出 final 或触发护栏。

    max_json_retries / guards 由实验配置注入，默认值与 W1 冻结基线一致：
    不传时行为完全不变，护栏全部关闭。

    护栏拦截只发生在工具执行之前：拦下时不调 tools.call（不写库、不计入
    errors），而是把 ``GUARD_REJECT_PREFIX`` 消息追加进 messages，让后端
    有机会重发正确调用。guard span 只作证据，grader 不消费这个 role，
    因此判分口径不因开启护栏而改变。
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
        if (
            guard.GUARD_SCHEMA_VALIDATION in guards
            and guard_rejections < guard.MAX_GUARD_REJECTIONS
        ):
            rejection = guard.rejection_message(name, args)
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
