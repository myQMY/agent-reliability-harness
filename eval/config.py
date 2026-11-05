"""实验配置：唯一事实来源，决定 run_id 与所有生效参数。

只读标准库 tomllib，零第三方依赖。这里有一条硬纪律：
配置里的每个字段都必须真实生效，不允许「写了但没接上」的占位项；
run_id 由配置摘要确定性生成，不含时间戳 / 随机值 / 机器路径，
保证「同配置 → 同 run_id → 同报告」，也保证不同配置落到不同目录。
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "experiments" / "config.toml"

_SECTION_FIELDS: dict[str, set[str]] = {
    "experiment": {"name", "tasks", "k", "backend", "compare"},
    "guards": {"max_steps", "max_json_retries"},
}


@dataclass(frozen=True)
class ExperimentConfig:
    """一次实验的全部输入。字段增减必须同步 config.toml 与守门测试。"""

    name: str = "baseline_scripted"
    tasks: str = "tasks/mvp_10.jsonl"
    k: int = 3
    backend: str = "scripted"
    max_steps: int = 12
    max_json_retries: int = 2
    compare: str = "experiments/baseline/result_rows.jsonl"

    def validate(self) -> None:
        if not self.name or any(
            not (char.isalnum() or char in "-_") for char in self.name
        ):
            raise ValueError("experiment.name 只能含字母、数字、- 或 _（用作目录名）")
        if self.k < 1:
            raise ValueError("experiment.k 必须 >= 1")
        if self.backend not in ("scripted", "openai"):
            raise ValueError("experiment.backend 只能是 scripted 或 openai")
        if self.max_steps < 1:
            raise ValueError("guards.max_steps 必须 >= 1")
        if self.max_json_retries < 0:
            raise ValueError("guards.max_json_retries 必须 >= 0")
        if not self.tasks:
            raise ValueError("tasks 不得为空（compare 允许留空以关闭对照）")


_DEFAULTS = ExperimentConfig()


def load_config(path: Path | None = None) -> ExperimentConfig:
    """读取 TOML；未知字段直接报错，避免拼写错误被静默忽略。"""
    source = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with source.open("rb") as handle:
        raw = tomllib.load(handle)

    for section, keys in raw.items():
        allowed = _SECTION_FIELDS.get(section)
        if allowed is None:
            raise ValueError(f"未知配置段：[{section}]")
        unknown = set(keys) - allowed
        if unknown:
            raise ValueError(f"[{section}] 含未知字段：{sorted(unknown)}")

    experiment = raw.get("experiment", {})
    guards = raw.get("guards", {})
    config = ExperimentConfig(
        name=experiment.get("name", _DEFAULTS.name),
        tasks=experiment.get("tasks", _DEFAULTS.tasks),
        k=experiment.get("k", _DEFAULTS.k),
        backend=experiment.get("backend", _DEFAULTS.backend),
        compare=experiment.get("compare", _DEFAULTS.compare),
        max_steps=guards.get("max_steps", _DEFAULTS.max_steps),
        max_json_retries=guards.get(
            "max_json_retries", _DEFAULTS.max_json_retries
        ),
    )
    config.validate()
    return config


def with_overrides(config: ExperimentConfig, **changes: Any) -> ExperimentConfig:
    """CLI 显式传入的参数覆盖配置；None 表示「未指定」，不覆盖。"""
    updates = {key: value for key, value in changes.items() if value is not None}
    result = replace(config, **updates)
    result.validate()
    return result


def fingerprint(config: ExperimentConfig) -> str:
    payload = json.dumps(
        asdict(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def run_id(config: ExperimentConfig) -> str:
    """确定性 run_id：同配置恒定，不同配置必不同，不含任何非确定值。"""
    return f"{config.name}-{fingerprint(config)}"


def resolve_input(path_str: str) -> Path:
    """配置里的输入路径按 CWD 解析；CWD 不合适时回退到仓库根。"""
    path = Path(path_str)
    if path.is_absolute():
        return path
    from_cwd = Path.cwd() / path
    return from_cwd if from_cwd.exists() else REPO_ROOT / path


def config_snapshot(config: ExperimentConfig, rid: str) -> dict[str, Any]:
    """写入 run 目录的生效配置证据，与报告同目录版本化入库。"""
    return {"run_id": rid, "config": asdict(config)}
