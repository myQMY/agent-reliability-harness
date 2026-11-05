"""运行时护栏：步数预算、JSON 重试，以及 W3 起逐项加入的可开关护栏。

护栏的开关来自实验配置（eval/config.py），默认全部关闭：这是 W3 单变量
对照实验的前提——OFF 组必须与 W1 冻结基线逐字节等价，任何护栏都不能
悄悄改变默认行为。

W3 第一条护栏 ``schema_validation`` 只做参数 schema 校验，规则与工具层
共用 env/tools.validate_args；工具名合法性、业务策略都不归它管，避免
一次实验里混入多个自变量。它与工具层报错的区别在**位置**：工具层报错
会让这次调用的动作直接丢失，护栏则在工具执行前拦下、把拒绝原因写进
messages，让后端有机会重发正确调用——这才是 harness 可测量的价值。

token / 延迟观测不在这里：它们由 Backend.generate 返回的 ModelReply 提供，
ScriptedBackend / SeededBackend 恒为 0，OpenAIBackend 为真实值。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from env import tools

MAX_STEPS = 12
MAX_JSON_RETRIES = 2

# 护栏拦下后写进 messages 的固定前缀。SeededBackend 只认末条以此开头的消息，
# 才会重发被替换掉的干净意图动作，形成「拦下 → 改对」的可测量链路。
GUARD_REJECT_PREFIX = "[GUARD-REJECT]"

GUARD_SCHEMA_VALIDATION = "schema_validation"

# 已实现的护栏白名单；配置里出现其他名字直接报错，杜绝「写了但没接上」。
GUARD_NAMES = (GUARD_SCHEMA_VALIDATION,)

# 同一 trial 内最多拦截次数。脚本后端不响应拒绝消息，若没有这个上限，
# 连续的坏参数动作会被反复拦下直到吃光 MAX_STEPS；超过则放行给工具层。
MAX_GUARD_REJECTIONS = 3


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


def rejection_message(name: str, args: Any) -> str | None:
    """schema_validation 的判定入口：返回拒绝文本，None 表示放行。

    只查参数 schema；未知工具名放行给工具层判 wrong_tool，保证这条护栏
    严格是单变量（它修不了 bad_json / wrong_tool / duplicate_call / 策略违规）。
    """
    code = tools.validate_args(name, args)
    if code is None:
        return None
    required = ", ".join(tools.TOOL_SCHEMAS.get(name, {}).get("required", ()))
    return (
        f"{GUARD_REJECT_PREFIX} 工具 {name} 的参数未通过 schema 校验（{code}）；"
        f"必需字段：{required or '无'}。请重新输出这一个完整的工具调用。"
    )
