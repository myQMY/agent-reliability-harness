"""MVP 仅有的两条护栏：MAX_STEPS 与 JSON 解析失败重试 2 次。

其余护栏（参数校验、幂等、上下文压缩、预算、审批、退避）按设计文档 §7
留给 W3 作为单变量对照实验的自变量，这里一律不加。
"""
from __future__ import annotations

from dataclasses import dataclass

MAX_STEPS = 12
MAX_JSON_RETRIES = 2

# ScriptedBackend 不产生真实 token 与耗时，统一记为占位常量。
# 这是「逐字节一致」验收项的一部分，不代表真实模型开销。
SCRIPTED_TOKENS = 0
SCRIPTED_LATENCY_MS = 0


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
