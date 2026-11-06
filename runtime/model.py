"""模型层：Backend 抽象 + 三种后端（确定性脚本 / 确定性伪随机 / OpenAI 兼容）。

Backend.generate() 返回 ModelReply（模型文本 + token 用量 + 调用耗时）。
用量与耗时是「一次模型调用」的观测，只能在模型层产生；loop.py 仍保持
同步纯逻辑，不读时钟、不写文件、不调网络，只把观测值塞进 span。

ScriptedBackend 与 SeededBackend 都不产生真实观测，三个数值恒为 0，
这是「逐字节一致」的前提；OpenAIBackend 下这些是真实值，只进 trace / 结果行，
不参与判分，也不承诺逐字节可复现（报告口径会如实标注）。

SeededBackend 是 W3 之前的测量链路校准器：以任务脚本为「意图动作」，
按 SHA256 派生的固定 RNG 注入可复现故障，使同一任务 k 次结果可能不同。
它没有语言理解能力，注入的是人为噪声，不是模型能力波动。

JSON 解析与重试属于运行时护栏，由 runtime/loop.py 负责，不放在这里。
OpenAIBackend 用标准库 urllib 直接调用 OpenAI 兼容的 HTTP 接口，
因此连可选后端都不需要第三方 SDK，全程零依赖。

W4 前置：真实模型路径需要两件确定性后端不需要的东西——
(1) 工具清单与 trial 初始领域状态必须写进 system prompt，否则模型只读到
    用户话术，连 customer_id / 参数名都无从得知；(2) loop 用 role=tool
    记工具观察，而 chat completions 的 tool role 必须带 tool_call_id，
    本项目不用原生 tool_calls 协议，需在发出前转成 user 文本。
两件事都只在 OpenAIBackend 内发生，ScriptedBackend / SeededBackend 的
调用方与产物完全不变。

W4-b：OutputFaultBackend 把 SeededBackend 那一套输出级故障注入接到 openai
后端上——同一份 fault_rng / pick_fault / fault_text、同样的四类故障与
(seed, task_id, trial, cursor) 派生 RNG，只在「模型输出」这一层替换文本。
它与 SeededBackend 有一处本质差异：拒绝之后**不硬编码重发干净动作**，
而是照常调用底层真实模型、仅跳过本次注入，让模型自己对 [GUARD-REJECT]
作出响应。这正是 W4-b 要测的 H1，不能由 harness 代答。
默认 fault_per_mille=0 时完全不包装，首轮 W4 的行为与 run_id 逐字节不变。

W4-c：OutputFaultBackend 另把**被替换掉的干净动作**（注入前 reply.text）记进
``clean_actions``，由 eval 层写到独立 side-car 目录，供离线判定「拒绝后
重发的是不是原本那一个动作」。留档是**事后观测**：clean_text 绝不写进
messages / system prompt / 拒绝文本，运行时也绝不读它来构造响应；否则
「重发同一个动作」又会变成 harness 代答。既有 trace 8 字段、结果行、
判分、run_id 一律不变。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Sequence

from env import tools
from runtime import guard

SYSTEM_PROMPT = (
    "你是一个零售业务 Agent。每次回复只能输出一个 JSON 对象，不要输出多余文字。"
    '调用工具：{"tool": "<工具名>", "args": {...}}；'
    '结束本轮：{"final": "<对用户说的话>"}。'
    "工具名必须来自可用工具列表，args 必须包含该工具要求的全部字段。"
)

# 护栏协议的权威定义在 runtime/guard.py；这里 re-export 保持既有导入路径，
# 避免测试与调用方因常量搬家而失效。运行时的 RETRY_HINT / 工具报错不触发纠正。
GUARD_REJECT_PREFIX = guard.GUARD_REJECT_PREFIX

# loop 内部用 role=tool 记录工具观察；转成 chat completions 消息时统一加这个
# 前缀，避免没有 tool_call_id 的 tool role 被服务端拒绝。前缀只出现在发往
# 真实模型的 HTTP payload 里，不写进 trace / 结果行，判分链路不受影响。
TOOL_RESULT_PREFIX = "[TOOL-RESULT]"

# 会写坏领域库状态的动作，duplicate_call 故障只重放这些。
# 与护栏 / 判分器共用 env/tools.WRITE_TOOLS，避免三处清单漂移。
_SEEDED_WRITE_TOOLS = tools.WRITE_TOOLS

FAULT_BAD_JSON = "bad_json"
FAULT_BAD_ARGS = "bad_args"
FAULT_WRONG_TOOL = "wrong_tool"
FAULT_DUPLICATE_CALL = "duplicate_call"
SEEDED_FAULTS = (
    FAULT_BAD_JSON,
    FAULT_BAD_ARGS,
    FAULT_WRONG_TOOL,
    FAULT_DUPLICATE_CALL,
)


@dataclass(frozen=True)
class ModelReply:
    """一次模型调用的完整产出。

    tokens 取 prompt + completion，与服务商 usage 口径一致。
    latency_ms 取整毫秒，避免浮点进入产物（真实模型路径也不留浮点）。
    """

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Backend(Protocol):
    def generate(self, messages: list[dict[str, str]]) -> ModelReply: ...


@dataclass(frozen=True)
class BackendConfig:
    name: str = "scripted"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    # 输出级故障注入参数：seeded 后端恒生效；openai 后端仅在非零时启用
    # OutputFaultBackend（W4-b）。scripted 后端不读；openai 默认 0 时
    # 完全不包装，既有 run_id / 归档逐字节不变。
    seed: int = 0
    fault_per_mille: int = 0
    # 仅 openai 后端生效：附加到基础 SYSTEM_PROMPT 之后的确定性上下文
    # （工具清单 + 本 trial 的初始领域库状态）。空字符串 = 不附加。
    system_prompt: str = ""


def build_tool_context(state: Mapping[str, Any]) -> str:
    """把工具清单与 trial 初始领域库状态序列化成确定性的 system prompt 片段。

    只被 OpenAIBackend 消费：确定性后端按任务脚本走，不需要也不读取它。
    工具 schema 以 env/tools.TOOL_SCHEMAS 为唯一事实来源，状态由
    env/db.dump_state 给出，json.dumps 固定 sort_keys / 紧凑分隔符，
    因此同任务同 trial 的文本逐字节一致，不引入时间 / 随机 / 机器信息。
    """
    lines = [
        "可用工具（每次回复只能输出一个 JSON 对象，且只能调用其中一个；"
        "args 必须包含该工具要求的全部字段）：",
    ]
    for name in sorted(tools.TOOL_SCHEMAS):
        required = ", ".join(tools.TOOL_SCHEMAS[name]["required"])
        lines.append(f"- {name}：必需参数 {required}")
    lines.append("")
    lines.append("本轮开始时的领域库状态（工具调用会改变它，这里只是起点）：")
    lines.append(
        json.dumps(
            state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    return "\n".join(lines)


class ScriptedBackend:
    """按任务预置脚本逐次返回文本，无网络、无随机、零成本。

    脚本用尽后重复返回最后一项：正常脚本以 final 结尾（不会走到用尽），
    以工具调用结尾的脚本则自然形成循环，直到 MAX_STEPS 触发。
    """

    def __init__(self, script: Sequence[Any]):
        # 任务集里正常动作写成对象（可读），坏 JSON 直接写成字符串（用于制造输出格式失败）。
        self._script = [
            item
            if isinstance(item, str)
            else json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in script
        ]
        self._cursor = 0

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        if not self._script:
            return ModelReply(text="")
        if self._cursor >= len(self._script):
            return ModelReply(text=self._script[-1])
        text = self._script[self._cursor]
        self._cursor += 1
        return ModelReply(text=text)


def _as_action(text: str) -> dict[str, Any] | None:
    """把脚本项解析成动作；脚本里故意写坏的 JSON 会返回 None。"""
    try:
        action = json.loads(text)
    except (ValueError, TypeError):
        return None
    return action if isinstance(action, dict) else None


def fault_rng(seed: int, task_id: str, trial: int, cursor: int) -> random.Random:
    """输出级故障注入的确定性 PRNG：W3 的 SeededBackend 与 W4-b 的
    OutputFaultBackend 共用同一份派生式，保证「同 seed 同 trial 同 cursor」
    得到同一串判定，跨进程 / 跨机器可复现。不用内置 hash / 时间 / UUID。
    """
    key = f"{seed}|{task_id}|{trial}|{cursor}"
    bits = int.from_bytes(
        hashlib.sha256(key.encode("utf-8")).digest(), "big"
    )
    return random.Random(bits)


def pick_fault(
    seed: int,
    task_id: str,
    trial: int,
    cursor: int,
    fault_per_mille: int,
    faulted: set[int],
) -> str | None:
    """每个 cursor 独立判定；None 表示本次输出保持干净。W3 / W4-b 共用。

    ``faulted`` 保证每个 cursor 至多注入一次；故障类别在 SEEDED_FAULTS 上
    均匀取样，与 W3 的全部留档口径一致。
    """
    if cursor in faulted:
        return None
    rng = fault_rng(seed, task_id, trial, cursor)
    if rng.randrange(1000) >= fault_per_mille:
        return None
    return SEEDED_FAULTS[rng.randrange(len(SEEDED_FAULTS))]


def fault_text(
    kind: str, action: dict[str, Any], issued_writes: Sequence[str]
) -> str:
    """把一次干净的工具动作改写成四类输出级故障；W3 / W4-b 共用同一份实现。

    duplicate_call 在还没有可重放的写动作时退化成 bad_args，与 W3 一致。
    """
    if kind == FAULT_BAD_JSON:
        return "{not-json"
    if kind == FAULT_WRONG_TOOL:
        return json.dumps(
            {"tool": "__missing_tool__", "args": action.get("args", {})},
            ensure_ascii=False,
            sort_keys=True,
        )
    if kind == FAULT_DUPLICATE_CALL and issued_writes:
        return issued_writes[-1]
    # FAULT_BAD_ARGS，以及开局还没有写动作时的 duplicate_call 退化
    return json.dumps(
        {"tool": action["tool"], "args": {}},
        ensure_ascii=False,
        sort_keys=True,
    )


class SeededBackend:
    """确定性伪随机模拟器：以脚本为意图动作，按固定 seed 注入可复现故障。

    RNG 由 ``(seed, task_id, trial, cursor)`` 经 SHA256 显式派生，不用内置
    ``hash()`` / 时间 / UUID / 全局 random，因此跨进程、跨机器、跨 Python
    小版本都能复现同一串结果。

    注入时只替换本次返回的文本，cursor 恒前进 1 步，且每个 cursor 至多
    注入一次；故障不会吃光 MAX_STEPS，也不会让后端卡在原地。被替换掉的
    干净动作存为 ``_pending_correct``：只有紧邻的下一次 generate 且 messages
    末条以 ``GUARD_REJECT_PREFIX`` 开头时才重发，形成 W3「拦下 → 改对」链路。
    纠正机会只活一次，避免历史干净动作被后续无关的拦截误触发。

    任务脚本里固有的 6 类缺陷（坏 JSON、错工具、缺参数、超退等）原样保留：
    解析不成动作对象的脚本项不参与注入。ModelReply 三个观测值恒 0。
    """

    def __init__(
        self,
        config: BackendConfig,
        script: Sequence[Any],
        *,
        task_id: str = "",
        trial: int = 0,
        force_fault: str | None = None,
    ):
        if force_fault is not None and force_fault not in SEEDED_FAULTS:
            raise ValueError(f"未知故障类型：{force_fault}")
        self._config = config
        self._script = [
            item
            if isinstance(item, str)
            else json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in script
        ]
        self._task_id = task_id
        self._trial = trial
        self._force_fault = force_fault
        self._cursor = 0
        self._faulted: set[int] = set()
        self._pending_correct: str | None = None
        self._issued_writes: list[str] = []

    def _rng(self, cursor: int) -> random.Random:
        return fault_rng(self._config.seed, self._task_id, self._trial, cursor)

    def _pick_fault(self, cursor: int) -> str | None:
        """委托共享判定；force_fault 是 W3 单变量实验的调试开关。"""
        if self._force_fault is not None:
            return None if cursor in self._faulted else self._force_fault
        return pick_fault(
            self._config.seed,
            self._task_id,
            self._trial,
            cursor,
            self._config.fault_per_mille,
            self._faulted,
        )

    def _fault_text(self, kind: str, action: dict[str, Any]) -> str:
        return fault_text(kind, action, self._issued_writes)

    def _record_write(self, action: dict[str, Any], clean: str) -> None:
        if action.get("tool") in _SEEDED_WRITE_TOOLS:
            self._issued_writes.append(clean)

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        # 护栏拦下后重发被替换的干净动作；只认末条，且纠正机会只在紧邻的
        # 下一次 generate 有效。否则一次 wrong_tool 故障残留下来的干净动作，
        # 会被后面某次无关的护栏拦截误触发，让 A/B 结果失去可解释性。
        pending = self._pending_correct
        self._pending_correct = None
        if (
            pending is not None
            and messages
            and messages[-1].get("content", "").startswith(GUARD_REJECT_PREFIX)
        ):
            return ModelReply(text=pending)

        if not self._script:
            return ModelReply(text="")
        if self._cursor >= len(self._script):
            # 与 ScriptedBackend 完全一致：耗尽后重复末项，且不再注入故障
            return ModelReply(text=self._script[-1])

        clean = self._script[self._cursor]
        cursor = self._cursor
        self._cursor += 1
        action = _as_action(clean)
        # 只替换正常的工具调用；final 与脚本固有坏 JSON 保持原样
        if action is None or "tool" not in action:
            return ModelReply(text=clean)

        kind = self._pick_fault(cursor)
        if kind is None:
            self._record_write(action, clean)
            return ModelReply(text=clean)

        self._faulted.add(cursor)
        self._pending_correct = clean
        return ModelReply(text=self._fault_text(kind, action))


class OutputFaultBackend:
    """W4-b：把 W3 的输出级故障注入接到任意 Backend 上（实际用 OpenAIBackend）。

    与 SeededBackend 共用 ``fault_rng`` / ``pick_fault`` / ``fault_text`` 与
    ``SEEDED_FAULTS``，因此「是否注入、注入哪一类、坏文本长什么样」与 W3
    完全同源。差异只有两处，且都是 W4-b 成立的前提：

    1. 干净动作来自**底层真实模型当次输出**，不是任务脚本；
    2. 收到 ``[GUARD-REJECT]`` 后**绝不硬编码重发** ``_pending_correct``——
       仍然调用底层真实模型，只跳过本次注入。模型是否读懂拒绝消息必须由
       它自己的下一次输出回答，harness 不能代答。

    注入后紧邻的下一次 generate 一律不注入（``_skip_next``），既给模型留出
    响应拒绝的空间，也避免连续故障吃光 ``MAX_STEPS`` / 拦截预算。
    ``ModelReply`` 的 token / latency 观测原样保留，只替换 ``text``，
    因此预算闸门与结果行的用量口径不受影响。

    W4-c 的 ``clean_actions`` 是每次注入时追加的一条**只读留档**：记下注入前
    的 ``reply.text``（模型自己当次生成的动作）与替换后的坏文本。它只被
    eval 层在 trial 结束后写到独立 side-car 文件，供离线探针把「被拒的那次
    输出」配回「原本那一个动作」。运行时不读、不回灌、不改变任何一次
    generate 的返回值——这是防代答红线的代码级落点。
    """

    def __init__(
        self,
        inner: Backend,
        *,
        seed: int,
        fault_per_mille: int,
        task_id: str = "",
        trial: int = 0,
    ):
        self._inner = inner
        self._seed = seed
        self._fault_per_mille = fault_per_mille
        self._task_id = task_id
        self._trial = trial
        self._cursor = 0
        self._faulted: set[int] = set()
        self._skip_next = False
        # 只留可观测的注入记录，供离线装置校准读数；不进 trace / 结果行。
        self.injected: list[tuple[int, str]] = []
        # W4-c：被替换掉的干净动作留档（事后只读证据），不进 messages。
        self.clean_actions: list[dict[str, Any]] = []

    @property
    def trial_id(self) -> str:
        """与 runner / trace 同一套标识；缺 task_id 时留空，不编造。"""
        return f"{self._task_id}-t{self._trial}" if self._task_id else ""

    @property
    def inner(self) -> Backend:
        return self._inner

    @staticmethod
    def _last_successful_write(
        messages: Sequence[Mapping[str, str]],
    ) -> str | None:
        """找最近一条**确实成功执行过**的写动作原文。

        ``duplicate_call`` 必须重放一个已成功的写动作，才会被
        ``idempotency`` 护栏的 ``executed_write_keys`` 命中；拿一个从未
        执行过的动作去重放只会再写一次库，制造不出拒绝。tool 观察是
        loop 用 ``tools.call`` 的返回值序列化的，这里只认 ``ok=True``。
        """
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "tool":
                continue
            try:
                observation = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                continue
            if not (
                isinstance(observation, dict) and observation.get("ok") is True
            ):
                continue
            for back in range(index - 1, -1, -1):
                if messages[back].get("role") != "assistant":
                    continue
                action = _as_action(messages[back].get("content", ""))
                if action and action.get("tool") in _SEEDED_WRITE_TOOLS:
                    return messages[back]["content"]
                break
        return None

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        reply = self._inner.generate(messages)
        cursor = self._cursor
        self._cursor += 1

        # 拒绝之后的这一次输出就是 H1 的测量对象：无论上一步是否注入，
        # 都不允许再扰动它。
        rejecting = bool(messages) and messages[-1].get("content", "").startswith(
            GUARD_REJECT_PREFIX
        )
        if rejecting or self._skip_next:
            self._skip_next = False
            return reply

        action = _as_action(reply.text)
        # 与 W3 一致：final、本来就解析不出的输出不参与注入。
        if action is None or "tool" not in action:
            return reply

        kind = pick_fault(
            self._seed,
            self._task_id,
            self._trial,
            cursor,
            self._fault_per_mille,
            self._faulted,
        )
        if kind is None:
            return reply

        self._faulted.add(cursor)
        self._skip_next = True
        writes: list[str] = []
        previous = self._last_successful_write(messages)
        if previous is not None:
            writes.append(previous)
        text = fault_text(kind, action, writes)
        self.injected.append((cursor, kind))
        self.clean_actions.append(
            {
                "task_id": self._task_id,
                "trial": self._trial,
                "trial_id": self.trial_id,
                "cursor": cursor,
                "fault_kind": kind,
                "clean_text": reply.text,
                "injected_text": text,
            }
        )
        return replace(reply, text=text)


def _wire_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """把 harness 内部消息转成 OpenAI 兼容 chat completions 可接受的形式。

    loop 用 ``role=tool`` 记录观察，但 chat completions 的 tool role 必须
    携带 ``tool_call_id``；本项目用纯文本 JSON 动作、不走原生 tool_calls，
    直接发会收到 400。转成 user 文本、加可读前缀，语义不变。
    guard 拒绝消息本来就是 role=user，原样保留。
    """
    wired: list[dict[str, str]] = []
    for message in messages:
        role, content = message["role"], message["content"]
        if role == "tool":
            wired.append(
                {"role": "user", "content": f"{TOOL_RESULT_PREFIX} {content}"}
            )
        else:
            wired.append({"role": role, "content": content})
    return wired


class OpenAIBackend:
    """OpenAI 兼容 chat completions 后端；opener 可注入以便离线单测。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        opener: Any = None,
        system_prompt: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._opener = opener if opener is not None else urllib.request.urlopen
        # 附加上下文为空时严格退回 W2 的原始提示，既有留档与单测不变
        self._system_prompt = (
            f"{SYSTEM_PROMPT}\n\n{system_prompt}" if system_prompt else SYSTEM_PROMPT
        )

    @property
    def system_prompt(self) -> str:
        """暴露最终提示，便于离线断言真实模型能看到工具清单与领域状态。"""
        return self._system_prompt

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                *_wire_messages(messages),
            ],
            "temperature": 0,
        }
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        started = time.perf_counter()
        with self._opener(request) as response:
            body = json.loads(response.read().decode("utf-8"))
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        usage = body.get("usage") or {}
        return ModelReply(
            text=body["choices"][0]["message"]["content"],
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
        )


def build_backend(
    config: BackendConfig,
    script: Sequence[Any],
    *,
    task_id: str = "",
    trial: int = 0,
) -> Backend:
    if config.name == "scripted":
        return ScriptedBackend(script)
    if config.name == "seeded":
        return SeededBackend(config, script, task_id=task_id, trial=trial)
    if config.name == "openai":
        if not (config.base_url and config.api_key and config.model):
            raise ValueError(
                "OpenAIBackend 需要 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL"
            )
        backend: Backend = OpenAIBackend(
            config.base_url,
            config.api_key,
            config.model,
            system_prompt=config.system_prompt,
        )
        # fpm=0 时完全不包装：首轮 W4 的调用路径、trace 与 run_id 逐字节不变。
        if config.fault_per_mille:
            backend = OutputFaultBackend(
                backend,
                seed=config.seed,
                fault_per_mille=config.fault_per_mille,
                task_id=task_id,
                trial=trial,
            )
        return backend
    raise ValueError(f"未知后端：{config.name}")


def config_from_env(env: dict[str, str] | None = None) -> BackendConfig:
    source = os.environ if env is None else env
    return BackendConfig(
        name="openai",
        base_url=source.get("LLM_BASE_URL", ""),
        api_key=source.get("LLM_API_KEY", ""),
        model=source.get("LLM_MODEL", ""),
    )
