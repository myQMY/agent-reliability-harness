"""模型层：Backend 抽象 + 确定性脚本后端 + OpenAI 兼容后端。

Backend.generate() 只负责返回模型原始文本；JSON 解析与重试属于运行时护栏，
由 runtime/loop.py 负责，不放在这里。

OpenAIBackend 用标准库 urllib 直接调用 OpenAI 兼容的 HTTP 接口，
因此连可选后端都不需要第三方 SDK，全程零依赖。
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

SYSTEM_PROMPT = (
    "你是一个零售业务 Agent。每次回复只能输出一个 JSON 对象，不要输出多余文字。"
    '调用工具：{"tool": "<工具名>", "args": {...}}；'
    '结束本轮：{"final": "<对用户说的话>"}。'
    "工具名必须来自可用工具列表，args 必须包含该工具要求的全部字段。"
)


class Backend(Protocol):
    def generate(self, messages: list[dict[str, str]]) -> str: ...


@dataclass(frozen=True)
class BackendConfig:
    name: str = "scripted"
    base_url: str = ""
    api_key: str = ""
    model: str = ""


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

    def generate(self, messages: list[dict[str, str]]) -> str:
        if not self._script:
            return ""
        if self._cursor >= len(self._script):
            return self._script[-1]
        text = self._script[self._cursor]
        self._cursor += 1
        return text


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

    def generate(self, messages: list[dict[str, str]]) -> str:
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
        with self._opener(request) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]


def build_backend(config: BackendConfig, script: Sequence[str]) -> Backend:
    if config.name == "scripted":
        return ScriptedBackend(script)
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
