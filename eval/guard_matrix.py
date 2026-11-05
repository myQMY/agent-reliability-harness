"""W3 护栏 2×2 全因子汇总：主效应、交互效应与诚实的口径说明。

不重新跑批、不修改任何留档，只读四组 ``experiments/runs/<run_id>/`` 下的
``result_rows.jsonl`` 与对应的实验配置，生成一份 Markdown 矩阵报告。
四组共用同一后端、同一 seed、同一任务集，唯一自变量是 ``[guards].enabled``：

                     idempotency OFF        idempotency ON
  schema OFF         guard_ab_off           guard_ab_idem
  schema ON          guard_ab_on            guard_ab_both

``run_id`` 由配置摘要确定性生成，不硬编码；报告里不写时间、绝对路径或
机器信息，因此同一批留档重跑本模块，产物逐字节一致。

主效应取「开 / 关两组均值之差」，交互取双重差分 (difference-in-differences)：
``(both - idem) - (schema - off)``。这些是描述性数字，不是显著性检验。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from eval import analyze, config as exp_config
from eval import trace

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = REPO_ROOT / "experiments" / "runs"
DEFAULT_OUTPUT = REPO_ROOT / "experiments" / "w3_guard_matrix.md"

# (格子名, schema_validation 开关, idempotency 开关, 配置文件相对路径)
CELLS = (
    ("off", 0, 0, "experiments/guard_ab_off.toml"),
    ("schema", 1, 0, "experiments/guard_ab_on.toml"),
    ("idem", 0, 1, "experiments/guard_ab_idem.toml"),
    ("both", 1, 1, "experiments/guard_ab_both.toml"),
)

# 进入主效应表的指标；(表头, 键, 小数位, 是否百分比式指标)
EFFECT_METRICS = (
    ("pass¹", "pass1", 2),
    ("pass^k", "passk", 2),
    ("invalid_calls", "invalid_calls", 2),
    ("平均步数", "avg_steps", 2),
)


def effects_of(values: dict[str, float]) -> dict[str, float]:
    """从四格数值算两条主效应与一个交互效应；纯函数，便于单测。

    主效应 = 该因子 ON 的两格均值 − OFF 的两格均值；
    交互 = idem ON 时 schema 的增量 − idem OFF 时 schema 的增量。
    """
    schema_on = (values["schema"] + values["both"]) / 2
    schema_off = (values["off"] + values["idem"]) / 2
    idem_on = (values["idem"] + values["both"]) / 2
    idem_off = (values["off"] + values["schema"]) / 2

    def _zero_noise(value: float) -> float:
        # 双重差分常算出 ~1e-17 的浮点残差，显示成 "-0.00" 会被误读成
        # 负效应；只把纯数值噪声归零，不做任何四舍五入或结果修饰。
        return 0.0 if abs(value) < 1e-9 else value

    return {
        "schema": _zero_noise(schema_on - schema_off),
        "idempotency": _zero_noise(idem_on - idem_off),
        "interaction": _zero_noise(
            (values["both"] - values["idem"])
            - (values["schema"] - values["off"])
        ),
    }


def build_matrix(
    runs_root: Path | None = None, *, config_root: Path | None = None
) -> dict[str, Any]:
    """读四组留档，返回可渲染的矩阵数据；缺任一格直接报错，不静默补零。"""
    root = Path(runs_root) if runs_root is not None else DEFAULT_RUNS_DIR
    cfg_root = Path(config_root) if config_root is not None else REPO_ROOT
    cells: dict[str, dict[str, Any]] = {}
    for name, schema_on, idem_on, rel_config in CELLS:
        cfg = exp_config.load_config(cfg_root / rel_config)
        rid = exp_config.run_id(cfg)
        rows = trace.read_jsonl(root / rid / "result_rows.jsonl")
        summary = analyze.summarize(rows, cfg.k)
        cells[name] = {
            "run_id": rid,
            "config": rel_config,
            "schema_on": schema_on,
            "idem_on": idem_on,
            "guards": cfg.guards,
            "k": cfg.k,
            "backend": cfg.backend,
            "seed": cfg.seed,
            "fault_per_mille": cfg.fault_per_mille,
            "summary": summary,
        }
    return {"cells": cells}


def _values(cells: dict[str, dict[str, Any]], key: str) -> dict[str, float]:
    return {name: cell["summary"][key] for name, cell in cells.items()}


def render_matrix(matrix: dict[str, Any]) -> str:
    """渲染 Markdown；数值全部来自传入的留档数据，措辞不含未经验证的结论。"""
    cells = matrix["cells"]
    off, schema, idem, both = (cells[name] for name in ("off", "schema", "idem", "both"))
    k = off["k"]

    lines = [
        "# W3 护栏 2×2 全因子结果",
        "",
        "> 由 `python -m eval.guard_matrix` 读取四组留档如实生成；本模块不重跑评测、"
        "不修改任何留档，也不含生成时间。",
        "> 四组共用同一后端、同一任务集、同一 seed，唯一自变量是 "
        "`[guards].enabled`。`run_id` 由配置摘要确定性生成，可逐格回溯生效配置。",
        "",
        "## 因子设计",
        "",
        "| 格子 | schema_validation | idempotency | run_id | pass¹ | "
        f"pass^{k} | invalid_calls | 平均步数 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in ("off", "schema", "idem", "both"):
        cell = cells[name]
        s = cell["summary"]
        lines.append(
            f"| {name} | {'ON' if cell['schema_on'] else 'OFF'} | "
            f"{'ON' if cell['idem_on'] else 'OFF'} | `{cell['run_id']}` | "
            f"{s['pass1']:.2f} | {s['passk']:.2f} | {s['invalid_calls']} | "
            f"{s['avg_steps']:.2f} |"
        )
    lines += [
        "",
        "## 主效应与交互效应",
        "",
        "> 主效应 = 该因子 ON 的两格均值 − OFF 的两格均值；交互 = "
        "`(both − idem) − (schema − off)`。以下是**描述性差值**，"
        "不是显著性检验，也没有置信区间。",
        "",
        "| 指标 | schema 主效应 | idempotency 主效应 | 交互（双重差分） |",
        "| --- | --- | --- | --- |",
    ]
    for label, key, precision in EFFECT_METRICS:
        effects = effects_of(_values(cells, key))
        lines.append(
            f"| {label} | {effects['schema']:+.{precision}f} | "
            f"{effects['idempotency']:+.{precision}f} | "
            f"{effects['interaction']:+.{precision}f} |"
        )

    s_off, s_idem = off["summary"], idem["summary"]
    pv_off = s_off["failure_counts"].get("policy_violation", 0)
    pv_idem = s_idem["failure_counts"].get("policy_violation", 0)
    lines += [
        "",
        "## 怎么读这张表",
        "",
        f"- `idempotency` 单开（OFF → idem）：pass^{k} "
        f"{s_off['passk']:.2f} → {s_idem['passk']:.2f}，"
        f"`invalid_calls` {s_off['invalid_calls']} → {s_idem['invalid_calls']}，"
        f"`policy_violation` 失败 trial {pv_off} → {pv_idem}。"
        "它拦下的是「工具层**不报错**、但终态被重复写坏」的调用，"
        "这类修复本来就不会反映在无效调用数上；**论效果只看 pass^k**，"
        "中间指标（上升、下降或不变）都不能当代理。",
        "- 两条单变量边是 `off → schema` 与 `off → idem`：`both` 格一次改了"
        "两项，**不能**拿它相对 OFF 的 A/B 表当单变量因果证据，它只用于算交互。",
        "- 交互接近 0 只说明在**本 seed、本任务集、本故障注入率**下两条护栏的"
        "效果可加；换任务集或换模型后可能不成立，不推广为一般结论。",
        "",
        "## 口径与限制（读表前先看）",
        "",
        "- 四组后端都是 `SeededBackend`：确定性伪随机模拟器，**没有语言理解能力**，"
        "注入的是人为噪声（坏 JSON / 错参数 / 错工具 / 重复写）。这些数字"
        "**不构成任何真实模型能力、成本或延迟结论**，`tokens` / `latency_ms` 恒为 0。",
        "- 每格 10 个任务 × k 次（共 "
        f"{off['summary']['trial_count']} 个 trial），样本很小；"
        "pass^k 是「同一任务 k 次全通过」的任务占比，差一格就是 0.10。",
        "- 判分是终态逐字段 diff + 策略检查 + 必要告知；护栏产生的 `role=guard` "
        "span 不进 grader，**开启护栏不改变判分口径**。",
        "- `invalid_calls` 只统计工具层报错。`invalid_calls` 下降不等于成功率提升"
        "（坏参数被拦下后任务仍可能失败），反过来，成功率提升也不要求它下降。",
        "- 幂等键由运行时从 `(tool, args)` 经 SHA256 确定性派生，只在单个 trial "
        "的内存里去重；**没有持久化、不承诺跨进程幂等**，也不覆盖模型自带幂等键的场景。",
        "",
        "## 数据来源与复现",
        "",
        "| 格子 | 生效配置 | 结果行 |",
        "| --- | --- | --- |",
    ]
    for name in ("off", "schema", "idem", "both"):
        cell = cells[name]
        rel = f"experiments/runs/{cell['run_id']}"
        lines.append(
            f"| {name} | `{cell['config']}` | "
            f"`{rel}/config.json` · `{rel}/result_rows.jsonl` |"
        )
    lines += [
        "",
        "```",
        "python -m eval.guard_matrix",
        "python -m unittest discover -s tests -v",
        "```",
        "",
        "逐格复现命令与完整的单变量 A/B 表见各留档目录下的 `report.md`。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.guard_matrix",
        description="汇总 W3 四组护栏留档，生成 2×2 主效应 / 交互效应报告",
    )
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    text = render_matrix(build_matrix(args.runs_dir))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(text.encode("utf-8"))
    print(f"矩阵报告：{output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
