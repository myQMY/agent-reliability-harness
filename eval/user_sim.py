"""规则用户模拟器：按任务预置话术逐轮发话，支持按上一轮结果选话术。

不调模型，所有分支都是确定性字符串规则。多轮对话由 runner 驱动：
每发一句，就把同一份 messages / 环境 / 步数预算交给 runtime.loop.run_turn
继续跑，因此跨轮状态与副作用都被保留。

条件话术解决的是「Agent 反问时预置话术语义错乱」：真实模型可能先问
「请问退哪笔订单」，此时写死的第二句「那帮我退 3000 分吧」就不成立。
规则按上一轮 final 文本 / 调用过的工具 / 终止原因命中，命中就替换该轮话术。

规则是任务集的可选字段，解析时校验字段与条件，避免「写了但没生效」。
默认任务集里的规则在 ScriptedBackend 下不命中，因此话术选择与 W1 完全一致，
冻结基线保持可比；接真实模型或 W4 扩任务集时才发挥区分度。
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_WHEN_KEYS = {"final_contains_any", "tool_called", "termination"}
_RULE_KEYS = {"turn", "text", "when"}


@dataclass(frozen=True)
class TurnContext:
    """上一轮对话结束时的可观测上下文，用于决定下一句说什么。"""

    last_final: str = ""
    last_termination: str = ""
    last_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConditionalUtterance:
    """替换规则：turn 是 1-based 轮次；when 中指定的条件需全部满足。"""

    turn: int
    text: str
    final_contains_any: tuple[str, ...] = ()
    tool_called: str = ""
    termination: str = ""

    def matches(self, context: TurnContext) -> bool:
        conditions = (
            bool(self.final_contains_any) or bool(self.tool_called) or bool(self.termination)
        )
        if not conditions:
            return False
        if self.final_contains_any and not any(
            token in context.last_final for token in self.final_contains_any
        ):
            return False
        if self.tool_called and self.tool_called not in context.last_tools:
            return False
        if self.termination and self.termination != context.last_termination:
            return False
        return True


def parse_rules(
    raw: Sequence[Mapping[str, Any]],
) -> tuple[ConditionalUtterance, ...]:
    """把任务集里的 utterance_rules 解析成规则；未知字段 / 无效条件直接报错。"""
    rules: list[ConditionalUtterance] = []
    for item in raw:
        unknown = set(item) - _RULE_KEYS
        if unknown:
            raise ValueError(f"utterance_rules 含未知字段：{sorted(unknown)}")
        when = item.get("when", {})
        if not isinstance(when, Mapping):
            raise ValueError("utterance_rules.when 必须是对象")
        unknown_when = set(when) - _WHEN_KEYS
        if unknown_when:
            raise ValueError(f"utterance_rules.when 含未知字段：{sorted(unknown_when)}")

        turn, text = item.get("turn"), item.get("text")
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 2:
            raise ValueError("utterance_rules.turn 必须是 >= 2 的整数（第 1 轮没有上文）")
        if not isinstance(text, str) or not text:
            raise ValueError("utterance_rules.text 必须是非空字符串")

        contains = when.get("final_contains_any", ())
        if not isinstance(contains, (list, tuple)) or not all(
            isinstance(token, str) and token for token in contains
        ):
            raise ValueError("final_contains_any 必须是非空字符串列表")
        rule = ConditionalUtterance(
            turn=turn,
            text=text,
            final_contains_any=tuple(contains),
            tool_called=str(when.get("tool_called") or ""),
            termination=str(when.get("termination") or ""),
        )
        if not (
            rule.final_contains_any or rule.tool_called or rule.termination
        ):
            raise ValueError("utterance_rules.when 至少要有一个条件，否则规则恒不生效")
        rules.append(rule)
    return tuple(rules)


class RuleUserSimulator:
    def __init__(
        self,
        utterances: Sequence[str],
        rules: Sequence[Mapping[str, Any]] = (),
    ):
        if not utterances:
            raise ValueError("任务至少需要一句用户话术")
        self._utterances = list(utterances)
        self._rules = parse_rules(rules)

    @property
    def turn_count(self) -> int:
        return len(self._utterances)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def utterance_for(self, turn: int, context: TurnContext | None = None) -> str:
        """按 1-based 轮次返回话术；命中首条规则则替换预置话术。"""
        if not 1 <= turn <= self.turn_count:
            raise ValueError(f"轮次 {turn} 超出 1..{self.turn_count}")
        ctx = context if context is not None else TurnContext()
        for rule in self._rules:
            if rule.turn == turn and rule.matches(ctx):
                return rule.text
        return self._utterances[turn - 1]

    def turns(self) -> Iterator[str]:
        """无条件模式：空上下文不命中任何规则，逐轮回放预置话术。"""
        for turn in range(1, self.turn_count + 1):
            yield self.utterance_for(turn, TurnContext())
