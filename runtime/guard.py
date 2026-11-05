"""MVP 仅有的两条护栏：MAX_STEPS 与 JSON 解析失败重试 2 次。

其余护栏（参数校验、幂等、上下文压缩、预算、审批、退避）按设计文档 §7
留给 W3 作为单变量对照实验的自变量，这里一律不加。

token / 延迟观测不在这里：它们由 Backend.generate 返回的 ModelReply 提供，
ScriptedBackend 恒为 0（确定性），OpenAIBackend 为真实值。护栏与观测分离，
避免有人为了「看起来有成本数据」而往产物里塞假数字。
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_STEPS = 12
MAX_JSON_RETRIES = 2


@dataclass
class StepBudget:
    """整个 trial 共享一份步数预算，跨多轮对话累计。"""

    max_steps: int = MAX_STEPS
    used: int = 0

    def take(self) -> bool:
        if self.used >= self.max_steps:
            return False
        self.used += 1
        return True
