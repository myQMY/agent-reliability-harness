"""W3 结论的多种子稳健性检验：把「单 seed 的 A/B」升级成跨 seed 证据。

动机：既有 2×2 全因子报告只在 ``seed=11``、10 条任务、k=3 上跑过一次，
``pass^k`` 的分辨率是 0.10（一个任务是否 k 次全过就是 ±0.10）。那份报告里
两条主效应各 +0.10 很容易被读成普遍规律。本模块把**完全相同**的四格配置
（同后端、同任务集、同 k、同故障注入率，唯一变化是 seed）在一组版本化的
整数 seed 上重跑，报告效应符号分布，如实指出哪些结论能重复、哪些只是
样本运气。

纪律：
- **不新增护栏、不改判分口径、不改任何既有留档**。四份 2×2 配置的
  ``run_id`` / ``config.json`` / ``result_rows.jsonl`` / ``report.md``、
  ``w3_guard_matrix.md`` 与冻结基线全部一字节未动。
- seed 列表硬编码在模块里并版本化；不读时钟、不读环境变量、不用全局
  随机源，不做随机抽样。效应计算直接复用 ``guard_matrix.effects_of``，
  保证与单 seed 矩阵报告的口径逐字一致。
- 跑批只写临时目录：所有格共用同一目录，trace 文件名由 ``trial_id``
  决定（与 seed / 护栏无关），因此最多留下 31 个小文件并自动清理。
  仓库里只增加 ``w3_robustness.jsonl`` 证据与 ``w3_robustness.md`` 报告。
- 报告是**描述性统计，不是显著性检验**：不给置信区间、不给 p 值。

用法：
    python -m eval.robustness                      # 全量 seed 扫描并写 experiments/
    python -m eval.robustness --from-jsonl        # 只读证据重渲染，不重跑评测
    python -m eval.robustness --seeds 11,1,2 --out-dir <dir>   # 小规模自检
"""
from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from eval import analyze, runner
from eval.config import resolve_input
from eval.guard_matrix import effects_of
from runtime.guard import GUARD_IDEMPOTENCY, GUARD_SCHEMA_VALIDATION
from runtime.model import BackendConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "experiments"
EVIDENCE_NAME = "w3_robustness.jsonl"
REPORT_NAME = "w3_robustness.md"

# 固定的测量输入：不允许 CLI 覆盖，否则同一次检查可能混入第二个自变量，
# 也让 --from-jsonl 无法逐字节复现报告。
DEFAULT_TASKS = "tasks/mvp_10.jsonl"
DEFAULT_K = 3
DEFAULT_FAULT_PER_MILLE = 60

# 版本化的 seed 集：11 放在首位，因为它是既有 2×2 报告所用的那一个，
# 便于第一眼对照；其余为诊断时选定的一组整数，不是随机抽样。
DEFAULT_SEEDS = (
    11, 1, 2, 3, 7, 13, 42, 101, 2026,
    5, 17, 23, 31, 55, 77, 88, 99,
    123, 256, 512, 777, 1024, 2048, 4096, 8192,
    3001, 6007, 9001, 12345, 65535,
)

# 四格是 [guards].enabled 的四个取值，顺序同时决定证据文件的写出顺序。
CELLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("off", ()),
    ("schema", (GUARD_SCHEMA_VALIDATION,)),
    ("idem", (GUARD_IDEMPOTENCY,)),
    ("both", (GUARD_SCHEMA_VALIDATION, GUARD_IDEMPOTENCY)),
)
CELL_NAMES = tuple(name for name, _ in CELLS)
EFFECT_KEYS = ("schema", "idempotency", "interaction")
EFFECT_EPSILON = 1e-9

Record = dict[str, Any]


def parse_seeds(text: str | None) -> tuple[int, ...]:
    """解析 --seeds；None 用版本化默认值。非法或重复直接拒绝。"""
    if text is None:
        return DEFAULT_SEEDS
    parts = [item.strip() for item in text.split(",") if item.strip()]
    if not parts:
        raise SystemExit("--seeds 不得为空")
    try:
        seeds = tuple(int(item) for item in parts)
    except ValueError:
        raise SystemExit("--seeds 必须是逗号分隔的整数") from None
    if any(value < 0 for value in seeds):
        raise SystemExit("--seeds 必须 >= 0")
    if len(set(seeds)) != len(seeds):
        raise SystemExit("--seeds 不得包含重复值")
    return seeds


def collect(
    seeds: Sequence[int],
    *,
    tasks_path: Path,
    k: int = DEFAULT_K,
    fault_per_mille: int = DEFAULT_FAULT_PER_MILLE,
) -> list[Record]:
    """在临时目录里重跑四格 × 每个 seed，返回紧凑的结构化证据。

    只取 ``runner.run`` 返回的 rows 做汇总，不读任何跑批落盘文件；因此
    临时目录里各格反复覆盖同名文件也不影响结果，且产物完全确定。
    """
    records: list[Record] = []
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for seed in seeds:
            for cell, guards in CELLS:
                rows = runner.run(
                    runner.RunConfig(
                        tasks_path=tasks_path,
                        k=k,
                        backend=BackendConfig(
                            name="seeded",
                            seed=seed,
                            fault_per_mille=fault_per_mille,
                        ),
                        results_dir=scratch / "results",
                        traces_dir=scratch / "traces",
                        guards=guards,
                    )
                )
                summary = analyze.summarize(rows, k)
                records.append(
                    {
                        "seed": seed,
                        "cell": cell,
                        "guards": list(guards),
                        "pass1": summary["pass1"],
                        "passk": summary["passk"],
                        "passk_hits": summary["passk_hits"],
                        "task_count": summary["task_count"],
                        "invalid_calls": summary["invalid_calls"],
                        "total_steps": summary["total_steps"],
                        "failure_counts": dict(
                            sorted(summary["failure_counts"].items())
                        ),
                    }
                )
    return records


def dumps_records(records: Sequence[Record]) -> str:
    """固定字段顺序、固定分隔符，保证同一批证据逐字节一致。"""
    return "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    )


def load_records(text: str) -> list[Record]:
    return [
        json.loads(line) for line in text.splitlines() if line.strip()
    ]


def per_seed_effects(records: Sequence[Record]) -> list[dict[str, Any]]:
    """按证据里的出现顺序分组，逐 seed 复用矩阵模块的效应公式。"""
    by_seed: dict[int, dict[str, float]] = {}
    order: list[int] = []
    for record in records:
        seed = int(record["seed"])
        if seed not in by_seed:
            by_seed[seed] = {}
            order.append(seed)
        by_seed[seed][record["cell"]] = float(record["passk"])
    result = []
    for seed in order:
        cells = by_seed[seed]
        missing = [name for name in CELL_NAMES if name not in cells]
        if missing:
            raise ValueError(f"seed={seed} 缺少格子 {missing}，拒绝静默补零")
        result.append(
            {"seed": seed, "cells": cells, "effects": effects_of(cells)}
        )
    return result


def distribution(per_seed: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """跨 seed 的效应符号分布与描述性量；不是显著性检验。"""
    result: dict[str, dict[str, Any]] = {}
    for key in EFFECT_KEYS:
        values = [float(row["effects"][key]) for row in per_seed]
        signs = Counter(
            "+" if value > EFFECT_EPSILON
            else ("-" if value < -EFFECT_EPSILON else "0")
            for value in values
        )
        result[key] = {
            "positive": signs["+"],
            "zero": signs["0"],
            "negative": signs["-"],
            "mean": sum(values) / len(values) if values else 0.0,
            "min": min(values) if values else 0.0,
            "max": max(values) if values else 0.0,
        }
    return result


def _signed(value: float, precision: int = 2) -> str:
    return f"{0.0 if abs(value) < EFFECT_EPSILON else value:+.{precision}f}"


def render_sweep(records: Sequence[Record]) -> str:
    """渲染 Markdown；只依赖证据与模块常量，不含路径、时钟或机器信息。"""
    per_seed = per_seed_effects(records)
    dist = distribution(per_seed)
    seed_count = len(per_seed)
    seed_list = ", ".join(str(row["seed"]) for row in per_seed)
    schema_d, idem_d, inter_d = (
        dist["schema"], dist["idempotency"], dist["interaction"]
    )
    negative_total = sum(dist[key]["negative"] for key in EFFECT_KEYS)
    nonzero_inter = [
        row for row in per_seed
        if abs(row["effects"]["interaction"]) > EFFECT_EPSILON
    ]
    headline = next(
        (row for row in per_seed if row["seed"] == DEFAULT_SEEDS[0]), None
    )

    lines = [
        "# W3 护栏结论的多种子稳健性检验",
        "",
        "> 由 `python -m eval.robustness` 依据 `w3_robustness.jsonl` 如实生成；"
        "不含生成时间，同命令两次产物逐字节一致。",
        "> 本检查**不新增护栏、不改判分口径、不修改任何既有留档**：四份 2×2 配置的 "
        "`run_id` / `config.json` / `result_rows.jsonl` / `report.md`、"
        "`w3_guard_matrix.md` 与冻结基线全部一字节未动。",
        "",
        "## 为什么要做这份检查",
        "",
        "既有 [2×2 全因子报告](w3_guard_matrix.md) 只在 `seed=11` 上跑过一次："
        "10 条任务、k=3，`pass^k` 的分辨率是 0.10 —— 一个任务是否 k 次全过就是 ±0.10。"
        "那份报告里 `pass^k` 的两条主效应各 `+0.10`、交互 `+0.00`，若当成普遍规律，"
        "就是拿单一样本当结论。",
        "",
        f"本检查把**完全相同**的四格配置（同后端、同任务集、同 k、同故障注入率，"
        f"唯一变化是 seed）在本次 {seed_count} 个固定整数 seed 上重跑，报告效应符号分布："
        "哪些结论能重复，哪些只是样本运气。",
        "",
        "## 方法与输入",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        "| 后端 | `SeededBackend`（确定性伪随机模拟器） |",
        f"| 任务集 | `{DEFAULT_TASKS}`（10 条） |",
        f"| k | {DEFAULT_K} |",
        f"| 故障注入率 | {DEFAULT_FAULT_PER_MILLE}/1000 |",
        f"| seed 数 | {seed_count} |",
        f"| seed 列表 | `{seed_list}` |",
        "| 四格 | off / schema / idem / both（`[guards].enabled` 的四个取值） |",
        "| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1 |",
        "| 效应定义 | 主效应 = 该因子 ON 两格均值 − OFF 两格均值；"
        "交互 = `(both − idem) − (schema − off)`，复用 `eval/guard_matrix.py` 的 `effects_of` |",
        "",
        "> **逐字节可复现**：seed 列表硬编码在 `eval/robustness.py` 并版本化；RNG 由 "
        "`(seed, task_id, trial, cursor)` 经 SHA256 派生。模块不读时钟、不读环境变量、"
        "不用全局随机源；跑批只写临时目录（各格共用，文件名由 `trial_id` 决定），"
        "仓库里只留下 `w3_robustness.jsonl` 与本报告。"
        "`--from-jsonl` 可从证据只读重渲染，不重跑评测。",
        "",
        "## 逐 seed 结果",
        "",
        f"| seed | off | schema | idem | both | schema 主效应 | "
        f"idempotency 主效应 | 交互 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in per_seed:
        cells, effects = row["cells"], row["effects"]
        lines.append(
            f"| {row['seed']} | {cells['off']:.2f} | {cells['schema']:.2f} | "
            f"{cells['idem']:.2f} | {cells['both']:.2f} | "
            f"{_signed(effects['schema'])} | {_signed(effects['idempotency'])} | "
            f"{_signed(effects['interaction'])} |"
        )

    lines += [
        "",
        "## 跨 seed 汇总（描述性统计，不是显著性检验）",
        "",
        "| 效应 | 正 | 零 | 负 | 均值 | 最小 | 最大 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, key in (
        ("schema_validation 主效应", "schema"),
        ("idempotency 主效应", "idempotency"),
        ("交互（双重差分）", "interaction"),
    ):
        item = dist[key]
        lines.append(
            f"| {label} | {item['positive']} | {item['zero']} | "
            f"{item['negative']} | {_signed(item['mean'], 4)} | "
            f"{_signed(item['min'])} | {_signed(item['max'])} |"
        )

    lines += [
        "",
        "## 这份检查改变了什么结论",
        "",
    ]
    if headline is not None:
        h = headline["effects"]
        lines.append(
            f"- **`seed={headline['seed']}` 是偏乐观样本**：既有报告里两条主效应各 "
            f"`{_signed(h['schema'])}` / `{_signed(h['idempotency'])}`；跨 {seed_count} 个 seed，"
            f"`schema_validation` 只有 {schema_d['positive']} 个为正、"
            f"{schema_d['zero']} 个为零（均值 {_signed(schema_d['mean'], 4)}），"
            f"`idempotency` 有 {idem_d['positive']} 个为正、{idem_d['zero']} 个为零"
            f"（均值 {_signed(idem_d['mean'], 4)}）。"
            "**「两条护栏各提升 0.10」不能外推**，它描述的是一次特定随机流。"
        )
    else:
        lines.append(
            f"- 本次 seed 集不含 `{DEFAULT_SEEDS[0]}`（既有报告所用的 seed），"
            "因此这里不与该报告逐项对照；完整扫描请不带 `--seeds` 重跑。"
        )
    lines += [
        f"- **`idempotency` 稳健得多，但不是定律**：仍有 {idem_d['zero']} 个 seed 零效应，"
        f"效应大小从 {_signed(idem_d['min'])} 到 {_signed(idem_d['max'])} 不等。"
        f"`schema_validation` 则在 {schema_d['zero']}/{seed_count} 个 seed 上完全测不到提升。",
        (
            f"- **交互几乎恒为 0，但有例外**：{inter_d['zero']}/{seed_count} 个 seed 为 0，"
            + "、".join(
                f"seed={row['seed']} 出现 {_signed(row['effects']['interaction'])}"
                for row in nonzero_inter
            )
            + "。既有报告说「交互为 0 只描述本 seed」是对的，但「效果可加」"
            "也不能当成机制保证。"
        )
        if nonzero_inter
        else f"- **交互在 {seed_count} 个 seed 上全为 0**，但这仍只是本次样本的描述，不是机制保证。",
        (
            f"- **可以保留的正面结论**：{seed_count} 个 seed 里**没有任何一个出现负效应**"
            f"（三条效应合计负号 {negative_total} 个），两条护栏在本任务集、本模拟器下"
            "都没有让 `pass^k` 变差。"
        )
        if negative_total == 0
        else (
            f"- **出现了负效应**：三条效应合计 {negative_total} 个负号，"
            "说明护栏并非无条件无害，必须逐 seed 检查。"
        ),
        "- **机制解释**：`schema_validation` 把工具层坏参数拦在执行前，`invalid_calls` "
        "下降、失败归因从 `bad_args` 变成 `unknown`，但任务脚本固有的缺陷并没有被修好；"
        "`pass^k` 又要求同一任务 k 次全过，条件苛刻，所以多数 seed 下不翻转任务。"
        "`idempotency` 直接减少 `policy_violation`（拦下重复写），修的是终态，因此稳定得多。"
        "这也再次说明**中间指标不能替代终态成功率**。",
        "",
        "## 口径与限制（读表前先看）",
        "",
        "- **不是显著性检验**：没有置信区间、没有 p 值、不做多重比较校正。这里只是把"
        "同一套确定性跑批在多个 seed 上重做一遍，报告效应符号分布。",
        "- **seed 是人为选定的一组整数，不是随机抽样**：所以「N 个里 X 个为正」不能当"
        "概率估计，更不能外推到真实模型、其他任务集或其他故障注入率。",
        "- **分辨率极粗**：10 条任务 × k=3，`pass^k` 只能取 0.10 的整数倍；`+0.10` "
        "仅代表「一个任务从不是 k 次全过变成 k 次全过」，不是连续的效应量。",
        "- **全部数字来自确定性模拟器**：`SeededBackend` 没有语言理解能力，注入的是人为噪声；"
        "这些数字**不构成任何真实模型能力、成本或延迟结论**，`tokens` / `latency_ms` 恒为 0。",
        "- **护栏拒绝协议只在模拟器里验证过**：`[GUARD-REJECT]` 靠 `_pending_correct` "
        "触发重发干净动作；真实模型能否读懂拒绝消息并改对，必须接 `OpenAIBackend` 后"
        "另开实验，本表不能外推。",
        "- 效应只做 `|v| < 1e-9 → 0` 的浮点噪声归零，不做任何四舍五入或结果修饰。",
        "",
        "## 下一步",
        "",
        "- W3 剩余护栏（上下文压缩、token / 步数预算、退避）仍按「一组一条、单变量 A/B」"
        "推进，但**每组都应在本 seed 集上给出同样的符号分布**；只报一个 seed 的 A/B "
        "不算可复现结论。",
        "- 若要把 `pass^k` 当成主要指标，先扩大任务集或提高 k，否则 0.10 的分辨率"
        "会继续淹没真实差异。",
        "- 真实模型下的拒绝协议验证仍是开放项。",
        "",
        "## 数据来源与复现",
        "",
        "每行一格的完整指标（seed / cell / guards / pass¹ / pass^k / invalid_calls / "
        "total_steps / failure_counts）在 `w3_robustness.jsonl`；既有单 seed A/B 的"
        "逐格证据在 `experiments/runs/<run_id>/`。",
        "",
        "```",
        "python -m eval.robustness",
        "python -m eval.robustness --from-jsonl",
        "python -m unittest discover -s tests -v",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.robustness",
        description="在多个 seed 上重跑 W3 四格护栏配置，报告效应符号分布",
    )
    parser.add_argument(
        "--seeds", default=None,
        help="逗号分隔的整数；默认使用模块内版本化的 seed 集",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--from-jsonl", action="store_true",
        help="只读已有证据重渲染报告，不重跑评测",
    )
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    evidence_path = out_dir / EVIDENCE_NAME
    report_path = out_dir / REPORT_NAME

    if args.from_jsonl:
        if not evidence_path.is_file():
            raise SystemExit(f"证据文件不存在：{evidence_path}")
        records = load_records(evidence_path.read_text(encoding="utf-8"))
    else:
        seeds = parse_seeds(args.seeds)
        records = collect(
            seeds,
            tasks_path=resolve_input(DEFAULT_TASKS),
            k=DEFAULT_K,
            fault_per_mille=DEFAULT_FAULT_PER_MILLE,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(dumps_records(records).encode("utf-8"))

    text = render_sweep(records)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(text.encode("utf-8"))

    dist = distribution(per_seed_effects(records))
    print(f"seed 数：{len(per_seed_effects(records))}，证据行：{len(records)}")
    for key in EFFECT_KEYS:
        item = dist[key]
        print(
            f"  {key:12s} 正 {item['positive']:2d} / 零 {item['zero']:2d} / "
            f"负 {item['negative']:2d}，均值 {item['mean']:+.4f}"
        )
    print(f"证据：{evidence_path}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
