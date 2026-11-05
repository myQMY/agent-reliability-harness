"""实验配置：唯一事实来源，决定 run_id 与所有生效参数。

只读标准库 tomllib，零第三方依赖。这里有一条硬纪律：
配置里的每个字段都必须真实生效，不允许「写了但没接上」的占位项；
run_id 由配置摘要确定性生成，不含时间戳 / 随机值 / 机器路径，
保证「同配置 → 同 run_id → 同报告」，也保证不同配置落到不同目录。

摘要兼容约定：W1 冻结留档的 run_id 由原有 7 个字段算出，新增配置字段
不得改变它。fingerprint 与 config_snapshot 统一走 _core()：seed /
fault_per_mille 只在 backend=seeded 时并入，guards 只在非空时并入。
因此「全部关闭」的配置摘要逐字节等于 W1 / W2.5，开启任一护栏才分叉出
新 run_id——这正是 W3 单变量 A/B 需要的性质。
"""
from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from runtime.guard import GUARD_NAMES

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "experiments" / "config.toml"

_SECTION_FIELDS: dict[str, set[str]] = {
    "experiment": {"name", "tasks", "k", "backend", "compare"},
    "guards": {"max_steps", "max_json_retries", "enabled"},
    "seeded": {"seed", "fault_per_mille"},
}

# W1 冻结基线配置摘要的 7 个原始字段；任何情况下都保持原样
_CORE_FIELDS = (
    "name",
    "tasks",
    "k",
    "backend",
    "max_steps",
    "max_json_retries",
    "compare",
)


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
    # 仅 backend == "seeded" 时生效；scripted / openai 下忽略
    seed: int = 0
    fault_per_mille: int = 0
    # W3 起的可开关运行时护栏；空元组 = 全部关闭（OFF 组），与 W1 行为逐字等价
    guards: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.name or any(
            not (char.isalnum() or char in "-_") for char in self.name
        ):
            raise ValueError("experiment.name 只能含字母、数字、- 或 _（用作目录名）")
        if self.k < 1:
            raise ValueError("experiment.k 必须 >= 1")
        if self.backend not in ("scripted", "openai", "seeded"):
            raise ValueError("experiment.backend 只能是 scripted / openai / seeded")
        if self.max_steps < 1:
            raise ValueError("guards.max_steps 必须 >= 1")
        if self.max_json_retries < 0:
            raise ValueError("guards.max_json_retries 必须 >= 0")
        if self.seed < 0:
            raise ValueError("[seeded].seed 必须 >= 0")
        if not 0 <= self.fault_per_mille <= 1000:
            raise ValueError("[seeded].fault_per_mille 必须落在 0..1000")
        unknown_guards = [name for name in self.guards if name not in GUARD_NAMES]
        if unknown_guards:
            raise ValueError(
                f"未知护栏 {unknown_guards}；已实现：{list(GUARD_NAMES)}"
            )
        if len(set(self.guards)) != len(self.guards):
            raise ValueError("[guards].enabled 不得包含重复护栏名")
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
    seeded = raw.get("seeded", {})
    enabled = guards.get("enabled", ())
    if not isinstance(enabled, (list, tuple)) or any(
        not isinstance(item, str) for item in enabled
    ):
        raise ValueError("[guards].enabled 必须是护栏名数组")
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
        seed=seeded.get("seed", _DEFAULTS.seed),
        fault_per_mille=seeded.get(
            "fault_per_mille", _DEFAULTS.fault_per_mille
        ),
        guards=tuple(enabled),
    )
    config.validate()
    return config


def with_overrides(config: ExperimentConfig, **changes: Any) -> ExperimentConfig:
    """CLI 显式传入的参数覆盖配置；None 表示「未指定」，不覆盖。"""
    updates = {key: value for key, value in changes.items() if value is not None}
    result = replace(config, **updates)
    result.validate()
    return result


def _core(config: ExperimentConfig) -> dict[str, Any]:
    """进入 run_id 摘要与 config.json 快照的生效配置。

    非 seeded 后端下 seed / fault_per_mille 不生效，因此不入摘要——
    这让默认 scripted 配置的摘要逐字节等于 W1 冻结留档的那一份。
    """
    payload: dict[str, Any] = {
        field: getattr(config, field) for field in _CORE_FIELDS
    }
    if config.backend == "seeded":
        payload["seed"] = config.seed
        payload["fault_per_mille"] = config.fault_per_mille
    # 护栏开关是 W3 的实验自变量：开启后必须落到新 run_id，
    # 关闭时完全不进摘要，保证两个既有留档的 run_id / config.json 逐字节不变。
    if config.guards:
        payload["guards"] = list(config.guards)
    return payload


def fingerprint(config: ExperimentConfig) -> str:
    payload = json.dumps(
        _core(config),
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
    return {"run_id": rid, "config": _core(config)}
