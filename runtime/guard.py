"""运行时护栏：步数预算、JSON 重试，以及 W3 起逐项加入的可开关护栏。

护栏的开关来自实验配置（eval/config.py），默认全部关闭：这是 W3 单变量
对照实验的前提——OFF 组必须与 W1 冻结基线逐字节等价，任何护栏都不能
悄悄改变默认行为。四条护栏共用同一个拦截预算与 ``GUARD_REJECT_PREFIX``
协议，判定位置在 loop.py 里固定，保证结果确定、可归因：

- ``schema_validation`` / ``idempotency``：工具执行**之前**（顺序固定）。
- ``tool_retry``：工具层报错**之后**。
- ``json_recovery``：模型输出 JSON 解析失败**之后**、进入工具层之前。

各条职责：
- ``schema_validation``：只做参数 schema 校验，规则与工具层共用
  env/tools.validate_args；工具名合法性、业务策略都不归它管。
- ``idempotency``：为写工具调用从 ``(tool, args)`` 确定性派生幂等键，
  在同一 trial 内对**已成功执行**的重复写拦截。键只在内存里、只在本次
  trial 有效，不承诺持久化幂等；未知工具 / 读工具 / 失败调用都不记账。
- ``tool_retry``：工具层报错后触发重发。它工作在**失败之后**：如实保留
  报错的 tool span 与 ``errors``，只额外追加一条拒绝消息让后端重发干净
  动作。这是路线表里「退避」在本同步确定性 harness 里的落地形态——
  固定次数重试预算，不做真实 sleep、不引入指数延迟，否则会破坏逐字节复现。
- ``json_recovery``：W1 的 JSON 重试只给出 ``RETRY_HINT`` 提示，被替换掉的
  那个动作会被直接跳过；本护栏在**保留原提示证据**的基础上，额外追加一条
  ``[GUARD-REJECT]`` 消息，让后端重发那一个动作。它覆盖 ``bad_json`` 注入
  故障——四类注入故障里最后一条没有护栏覆盖的类别。脚本里固有的连续坏
  JSON 没有可重发的干净动作，依然失败，护栏不越界。

前两条护栏与工具层报错的区别都在**位置**：工具层报错会让这次调用的动作
直接丢失，执行前护栏则拦下并把拒绝原因写进 messages，让后端有机会重发
正确调用——这才是 harness 可测量的价值。后两条把同一个协议延伸到失败之后。

token / 延迟观测不在这里：它们由 Backend.generate 返回的 ModelReply 提供，
ScriptedBackend / SeededBackend 恒为 0，OpenAIBackend 为真实值。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from env import tools

MAX_STEPS = 12
MAX_JSON_RETRIES = 2

# 护栏拦下后写进 messages 的固定前缀。SeededBackend 只认末条以此开头的消息，
# 才会重发被替换掉的干净意图动作，形成「拦下 → 改对」的可测量链路。
GUARD_REJECT_PREFIX = "[GUARD-REJECT]"

GUARD_SCHEMA_VALIDATION = "schema_validation"
GUARD_IDEMPOTENCY = "idempotency"
GUARD_TOOL_RETRY = "tool_retry"
GUARD_JSON_RECOVERY = "json_recovery"

# 已实现的护栏白名单；配置里出现其他名字直接报错，杜绝「写了但没接上」。
# 顺序同时是报告里 --guard 的输出顺序，不要随意调整。四条护栏在 loop.py
# 里的判定位置不同，互不争抢同一个分支。
GUARD_NAMES = (
    GUARD_SCHEMA_VALIDATION,
    GUARD_IDEMPOTENCY,
    GUARD_TOOL_RETRY,
    GUARD_JSON_RECOVERY,
)

# 工具层可能返回的错误；这些错误都在 _validate 阶段被拦下、没有写入状态，
# 因此重发干净动作是安全的。业务策略违规不在这里（工具层故意不检查策略）。
RETRYABLE_TOOL_ERRORS = (tools.WRONG_TOOL, tools.BAD_ARGS)

# parse_action 的两类格式失败都可以尝试恢复：注入的 bad_json 属于
# invalid_json；脚本固有的坏 JSON / 缺字段也会尝试，但没有干净动作可重发
# 时会自然收敛（连续 3 次后 TERMINATION_JSON_FAILURE），护栏不越界。
RECOVERABLE_FORMAT_REASONS = ("invalid_json", "missing_fields")

# 同一 turn 内最多拦截 / 重试次数（四条护栏共享）。脚本后端不响应拒绝消息，
# 若没有这个上限，连续的坏参数或坏 JSON 动作会被反复拦下直到吃光 MAX_STEPS。
MAX_GUARD_REJECTIONS = 3

# 幂等键长度：只用于同 trial 去重与 trace 追溯，取 16 位十六进制足够。
IDEMPOTENCY_KEY_LENGTH = 16


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


@dataclass
class GuardState:
    """一次 trial 内跨 turn 共享的幂等键集合。

    只在 ``idempotency`` 开启时由 runner 创建；默认路径与只开
    schema_validation 时它是 None，因此既有留档的产物逐字节不变。
    集合不进任何 span / 结果行，纯粹是运行时的去重状态。
    """

    executed_write_keys: set[str] = field(default_factory=set)


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


def idempotency_key(name: str, args: Any) -> str:
    """从 ``(tool, args)`` 确定性派生幂等键，不含时间 / 随机 / 机器信息。

    用 SHA256 而非 ``hash()``，保证跨进程、跨机器一致；键只在本次 trial
    的内存里用于去重，不是持久化幂等存储，也不承诺跨进程生效。
    ``default=str`` 只是防御性兜底：动作来自 JSON，正常路径不会触发。
    """
    payload = json.dumps(
        {"tool": name, "args": args},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[
        :IDEMPOTENCY_KEY_LENGTH
    ]


def idempotency_rejection(
    name: str, args: Any, state: GuardState | None
) -> str | None:
    """idempotency 的判定入口：重复的成功写调用返回拒绝文本，None 放行。

    只管 ``tools.WRITE_TOOLS`` 里的写动作：读工具重复调用是合法的信息查询，
    拦它反而会破坏任务；参数 schema / 工具名合法性归别的层，避免混自变量。
    """
    if state is None or name not in tools.WRITE_TOOLS:
        return None
    key = idempotency_key(name, args)
    if key not in state.executed_write_keys:
        return None
    return (
        f"{GUARD_REJECT_PREFIX} 工具 {name} 的这次写调用在本轮已成功执行过"
        f"（幂等键 {key}），重复执行会造成重复退款 / 重复通知。"
        "请跳过该动作，直接输出下一个工具调用或 final。"
    )


def record_write(name: str, args: Any, state: GuardState | None) -> None:
    """工具层**成功执行**后才记账；报错的调用本来就没改变状态，不算重复。"""
    if state is not None and name in tools.WRITE_TOOLS:
        state.executed_write_keys.add(idempotency_key(name, args))


def tool_retry_message(name: str, error: str) -> str:
    """tool_retry 的拒绝文本：工具层报错之后要求后端重发完整调用。

    只由 ``RETRYABLE_TOOL_ERRORS`` 里的错误触发。文本里明确写「没有写入状态」，
    因为工具层在 ``_validate`` 阶段就返回了，重发不会造成重复副作用；
    这也让真实模型能理解为什么需要重发，而不是把报错当成任务失败。
    """
    return (
        f"{GUARD_REJECT_PREFIX} 工具 {name} 的调用在工具层失败（{error}），"
        "这次调用没有写入任何状态。请重新输出这一个完整的工具调用，"
        "工具名与参数都按任务要求写对。"
    )


def json_recovery_hint(reason: str) -> str:
    """json_recovery 的拒绝文本：格式失败后要求后端重发被跳过的那一个动作。

    与 ``tool_retry_message`` 一样以 ``GUARD_REJECT_PREFIX`` 开头，才能触发
    SeededBackend 的 ``_pending_correct`` 重发。明确写「还没有执行」，是因为
    W1 的 JSON 重试会把该动作直接跳过，而注入故障替换掉的其实是一个干净动作。
    """
    return (
        f"{GUARD_REJECT_PREFIX} 上一次输出不是合法的单个 JSON 对象（{reason}），"
        "被跳过的那一个动作还没有执行。请重新输出这一个完整的工具调用或 final，"
        "不要跳过它。"
    )
