"""规则用户模拟器：按任务预置话术逐轮发话，不调模型。

多轮对话由 runner 驱动：每发一句，就把同一份 messages / 环境 / 步数预算
交给 runtime.loop.run_turn 继续跑，因此跨轮状态与副作用都被保留。
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Sequence


class RuleUserSimulator:
    def __init__(self, utterances: Sequence[str]):
        if not utterances:
            raise ValueError("任务至少需要一句用户话术")
        self._utterances = list(utterances)

    @property
    def turn_count(self) -> int:
        return len(self._utterances)

    def turns(self) -> Iterator[str]:
        yield from self._utterances
