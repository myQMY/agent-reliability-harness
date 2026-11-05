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
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

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
    # 仅 SeededBackend 使用；scripted / openai 下恒为 0，不影响任何既有行为
    seed: int = 0
    fault_per_mille: int = 0


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
        key = f"{self._config.seed}|{self._task_id}|{self._trial}|{cursor}"
        bits = int.from_bytes(
            hashlib.sha256(key.encode("utf-8")).digest(), "big"
        )
        return random.Random(bits)

    def _pick_fault(self, cursor: int) -> str | None:
        """每个 cursor 独立判定；返回 None 表示本次输出干净意图动作。"""
        if cursor in self._faulted:
            return None
        if self._force_fault is not None:
            return self._force_fault
        rng = self._rng(cursor)
        if rng.randrange(1000) >= self._config.fault_per_mille:
            return None
        return SEEDED_FAULTS[rng.randrange(len(SEEDED_FAULTS))]

    def _fault_text(self, kind: str, action: dict[str, Any]) -> str:
        if kind == FAULT_BAD_JSON:
            return "{not-json"
        if kind == FAULT_WRONG_TOOL:
            return json.dumps(
                {"tool": "__missing_tool__", "args": action.get("args", {})},
                ensure_ascii=False,
                sort_keys=True,
            )
        if kind == FAULT_DUPLICATE_CALL and self._issued_writes:
            return self._issued_writes[-1]
        # FAULT_BAD_ARGS，以及开局还没有写动作时的 duplicate_call 退化
        return json.dumps(
            {"tool": action["tool"], "args": {}},
            ensure_ascii=False,
            sort_keys=True,
        )

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


class OpenAIBackend:
    """OpenAI 兼容 chat completions 后端；opener 可注入以便离线单测。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        opener: Any = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._opener = opener if opener is not None else urllib.request.urlopen

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        payload = {
            "model": self._model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
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
        return OpenAIBackend(config.base_url, config.api_key, config.model)
    raise ValueError(f"未知后端：{config.name}")


def config_from_env(env: dict[str, str] | None = None) -> BackendConfig:
    source = os.environ if env is None else env
    return BackendConfig(
        name="openai",
        base_url=source.get("LLM_BASE_URL", ""),
        api_key=source.get("LLM_API_KEY", ""),
        model=source.get("LLM_MODEL", ""),
    )
