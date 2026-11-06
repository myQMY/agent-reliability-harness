"""W3 归因对照：把「提升来自 harness 而非换模型」做成可复现实验。

动机：W3 的四条护栏全部在*同一个* ``SeededBackend``、同一个
``fault_per_mille``（下称 fpm）上做 A/B，证明的是「加护栏会涨」。项目最核心的
主张却更强——提升来自包围模型的那一层工程，而不是换一个更强的模型。此前
这组对照从没做过：没有第二条可版本化的「模型能力」轴。

本模块把 fpm 当作**模型输出可靠性轴**，与护栏开关组成 2×8 全因子：

- 纵向（同护栏、只改 fpm）：等价于「换一个输出故障更少的模型」；
- 横向（同 fpm、只改 ``[guards].enabled``）：等价于「不换模型，只加 harness」。

两条边各自都是单变量，但**同时改两格的格子不能直接对比**；这是因子实验，
不是 A/B。默认 30 个 seed、任务集、k 全部复用 ``eval/robustness.py`` 的版本化
常量，CLI 只允许缩小 seed 集做自检，不允许覆盖任务集或 k，避免混入第三个自变量。

为什么这条轴是干净的：``SeededBackend._rng`` 由 ``(seed, task_id, trial,
cursor)`` 经 SHA256 派生，**不含 fpm**；判定是 ``rng.randrange(1000) >= fpm``。
因此每个 cursor 有一个固定的 0..999 阈值，fpm 降低只会*删掉*一部分故障，
不会改变其余故障的类型或位置——故障集合严格嵌套。``fault_thresholds`` 把
这一点从后端同源算出，供报告自证、供测试钉死。

纪律：
- **不新增护栏、不加配置字段、不改判分口径、不修改任何既有留档**。
  既有 8 个 ``run_id`` / 归档报告 / 冻结基线一字节未动。
- 不读时钟、不读环境变量、不用全局随机源、零第三方依赖；跑批只写临时
  目录，仓库只增加 ``w3_attribution.jsonl`` 与 ``w3_attribution.md``。
- ``--from-jsonl`` 只读证据重渲染；同命令两次产物逐字节一致。
- 报告是**描述性统计、不是显著性检验**：不给置信区间与 p 值。

最重要的边界（报告里逐条展开，不许省）：fpm 只模拟*输出级*故障率，
不代表模型的推理 / 理解 / 规划能力；``fpm=0`` 不是「完美模型」，脚本
固有缺陷仍在；因此本实验**不替代 W4 的真实模型对照**，也不构成任何真实
模型能力 / 成本 / 延迟结论。接真实模型仍需先定 API 预算。

用法：
    python -m eval.attribution                    # 2×8 全因子 × 30 seed
    python -m eval.attribution --from-jsonl        # 只读证据重渲染
    python -m eval.attribution --seeds 11,1 --out-dir <dir>   # 小规模自检
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any, Sequence

from eval import analyze, ceiling, robustness, runner
from eval.config import resolve_input
from runtime import guard
from runtime.model import BackendConfig, SeededBackend

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "experiments"
EVIDENCE_NAME = "w3_attribution.jsonl"
REPORT_NAME = "w3_attribution.md"
EFFECT_EPSILON = 1e-9

# 版本化的模型轴，单位 ‰。0 = 无注入故障；60 = W3 全部实验的参考点。
# 档位是人为选定的诊断刻度、不是随机抽样，不随运行变化。
FPM_AXIS: tuple[int, ...] = (0, 10, 20, 30, 40, 60, 80, 100)
REFERENCE_FPM = robustness.DEFAULT_FAULT_PER_MILLE
ZERO_FPM = 0
# 正式跑批的 seed 集：直接复用多种子稳健性检验的版本化常量，保证两条
# 证据链上的随机流可比；CLI 只允许缩小它做自检。
DEFAULT_SEEDS = robustness.DEFAULT_SEEDS

CONTROL = "off"
TREATMENT = "all4"
ALL4 = (
    guard.GUARD_SCHEMA_VALIDATION,
    guard.GUARD_IDEMPOTENCY,
    guard.GUARD_TOOL_RETRY,
    guard.GUARD_JSON_RECOVERY,
)
CELLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CONTROL, ()),
    (TREATMENT, ALL4),
)
CELL_NAMES = tuple(name for name, _ in CELLS)

# 嵌套性验证用的固定随机流集合：覆盖全部 10 个任务 × 3 个 trial × 12 个
# cursor = 360 个阈值位点，与后端同源、不参与评测结果。取多条流是为了让
# 低 fpm 档也有可观测的故障位点，避免单条流在低档全空。
THRESHOLD_SEED = 11
THRESHOLD_TASK_IDS = tuple(f"T{index:02d}" for index in range(1, 11))
THRESHOLD_TRIALS = (1, 2, 3)
THRESHOLD_CURSORS = tuple(range(12))

Record = dict[str, Any]


def collect(
    seeds: Sequence[int],
    *,
    tasks_path: Path,
    k: int = robustness.DEFAULT_K,
    fpm_axis: Sequence[int] = FPM_AXIS,
) -> list[Record]:
    """2×len(fpm_axis) 格 × 每个 seed 重跑，返回紧凑结构化证据。

    只取 ``runner.run`` 返回的 rows 做汇总，不读任何跑批落盘文件；临时目录里
    各格反复覆盖同名文件也不影响结果，且产物完全确定。
    """
    if k < 1:
        raise ValueError("k 必须 >= 1")
    axis = tuple(int(value) for value in fpm_axis)
    if not axis or any(not 0 <= value <= 1000 for value in axis):
        raise ValueError("fpm 轴必须非空且落在 0..1000")
    if len(set(axis)) != len(axis):
        raise ValueError("fpm 轴不得包含重复档位")

    records: list[Record] = []
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for fpm in axis:
            for seed in seeds:
                for cell, guards in CELLS:
                    rows = runner.run(
                        runner.RunConfig(
                            tasks_path=tasks_path,
                            k=k,
                            backend=BackendConfig(
                                name="seeded",
                                seed=seed,
                                fault_per_mille=fpm,
                            ),
                            results_dir=scratch / "results",
                            traces_dir=scratch / "traces",
                            guards=guards,
                        )
                    )
                    summary = analyze.summarize(rows, k)
                    records.append(
                        {
                            "seed": int(seed),
                            "fpm": fpm,
                            "cell": cell,
                            "guards": list(guards),
                            "pass1": summary["pass1"],
                            "passk": summary["passk"],
                            "passk_hits": summary["passk_hits"],
                            "task_count": summary["task_count"],
                            "trial_count": summary["trial_count"],
                            "invalid_calls": summary["invalid_calls"],
                            "total_steps": summary["total_steps"],
                            "failure_counts": dict(
                                sorted(summary["failure_counts"].items())
                            ),
                            "per_task": [
                                {
                                    "task_id": task["task_id"],
                                    "sequence": task["sequence"],
                                    "all_pass": task["all_pass"],
                                    "failure_type": task["failure_type"],
                                }
                                for task in summary["per_task"]
                            ],
                        }
                    )
    return records


def fpm_values(records: Sequence[Record]) -> tuple[int, ...]:
    return tuple(sorted({int(record["fpm"]) for record in records}))


def seed_values(records: Sequence[Record]) -> tuple[int, ...]:
    return tuple(sorted({int(record["seed"]) for record in records}))


def cell_records(
    records: Sequence[Record], cell: str, fpm: int
) -> list[Record]:
    return [
        record
        for record in records
        if record["cell"] == cell and int(record["fpm"]) == fpm
    ]


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("证据里没有格子，拒绝静默补零")
    return sum(values) / len(values)


def cell_mean(records: Sequence[Record], cell: str, fpm: int) -> float:
    return _mean([float(row["passk"]) for row in cell_records(records, cell, fpm)])


def axis_rows(records: Sequence[Record]) -> list[dict[str, Any]]:
    """每个 fpm 档位一行的曲线数据；任一格缺失直接报错。"""
    result: list[dict[str, Any]] = []
    for fpm in fpm_values(records):
        row: dict[str, Any] = {"fpm": fpm}
        for cell in CELL_NAMES:
            rows = cell_records(records, cell, fpm)
            if not rows:
                raise ValueError(f"fpm={fpm} 缺少格子 {cell}，拒绝静默补零")
            values = [float(item["passk"]) for item in rows]
            row[f"{cell}_passk"] = _mean(values)
            row[f"{cell}_min"] = min(values)
            row[f"{cell}_max"] = max(values)
            row[f"{cell}_pass1"] = _mean(
                [float(item["pass1"]) for item in rows]
            )
            row[f"{cell}_count"] = len(rows)
        row["delta"] = row[f"{TREATMENT}_passk"] - row[f"{CONTROL}_passk"]
        result.append(row)
    return result


def axis_row(records: Sequence[Record], fpm: int) -> dict[str, Any]:
    matches = [row for row in axis_rows(records) if row["fpm"] == fpm]
    if not matches:
        raise ValueError(f"证据里没有 fpm={fpm}，拒绝静默补零")
    return matches[0]


def equivalent_fpm(
    records: Sequence[Record], target: float, cell: str = CONTROL
) -> dict[str, Any]:
    """只靠「把模型变可靠」达到 ``target``，最少需要把 fpm 降到多少。

    在 ``cell`` 的跨 seed 均值曲线上取**满足 >= target 的最大 fpm**（即最省
    的模型升级）。若连 fpm=0 都够不到，返回 ``reachable=False`` 与曲线上界、
    缺口；调用方必须据此如实说明「不可达」，不得编造一个等效值。
    """
    if cell not in CELL_NAMES:
        raise ValueError(f"未知格子 {cell}")
    rows = axis_rows(records)
    key = f"{cell}_passk"
    candidates = [row for row in rows if row[key] + EFFECT_EPSILON >= target]
    if candidates:
        chosen = max(candidates, key=lambda row: row["fpm"])
        return {
            "reachable": True,
            "target": target,
            "required_fpm": chosen["fpm"],
            "value": chosen[key],
            "cell": cell,
        }
    # 不可达时给出整条曲线的真实最大值（不预设它一定在 fpm=0）。
    best = max(rows, key=lambda row: row[key])
    return {
        "reachable": False,
        "target": target,
        "best_fpm": best["fpm"],
        "best_value": best[key],
        "gap": target - best[key],
        "cell": cell,
    }


def lift_decomposition(
    records: Sequence[Record], fpm: int = REFERENCE_FPM
) -> dict[str, Any]:
    """把「加护栏」的收益拆成「等价消除注入故障」与「除此之外」。

    当且仅当护栏曲线平坦（``all4@ref == all4@0``）时，恒等式
    ``harness_path == model_path + (all4@0 - off@0)`` 成立：加护栏 = 消除全部
    注入故障 + 额外修好一个脚本固有缺陷。平坦与否由证据判定，不预设。
    """
    ref = axis_row(records, fpm)
    zero = axis_row(records, ZERO_FPM)
    baseline = ref[f"{CONTROL}_passk"]
    harness = ref[f"{TREATMENT}_passk"]
    model_only = zero[f"{CONTROL}_passk"]
    all4_zero = zero[f"{TREATMENT}_passk"]
    harness_path = harness - baseline
    model_path = model_only - baseline
    intrinsic_extra = all4_zero - model_only
    return {
        "fpm": fpm,
        "baseline": baseline,
        "all4_at_reference": harness,
        "off_at_zero": model_only,
        "all4_at_zero": all4_zero,
        "model_path": model_path,
        "harness_path": harness_path,
        "intrinsic_extra": intrinsic_extra,
        "unreachable_by_model": harness - model_only,
        "model_share": (
            model_path / harness_path
            if abs(harness_path) > EFFECT_EPSILON
            else None
        ),
        "flat_identity_holds": (
            abs(harness - all4_zero) < EFFECT_EPSILON
            and abs(harness_path - model_path - intrinsic_extra)
            < EFFECT_EPSILON
        ),
    }


def per_seed_delta(
    records: Sequence[Record], fpm: int
) -> list[tuple[int, float]]:
    """同一 fpm 上逐个 seed 的 Δpass^k（all4 - off）；缺格直接报错。"""
    by_seed: dict[int, dict[str, float]] = {}
    for record in cell_records(records, CONTROL, fpm) + cell_records(
        records, TREATMENT, fpm
    ):
        by_seed.setdefault(int(record["seed"]), {})[record["cell"]] = float(
            record["passk"]
        )
    result: list[tuple[int, float]] = []
    for seed in sorted(by_seed):
        cells = by_seed[seed]
        missing = [name for name in CELL_NAMES if name not in cells]
        if missing:
            raise ValueError(
                f"fpm={fpm} seed={seed} 缺少格子 {missing}，拒绝静默补零"
            )
        result.append((seed, cells[TREATMENT] - cells[CONTROL]))
    return result


def fault_thresholds(
    seed: int = THRESHOLD_SEED,
    task_ids: Sequence[str] = THRESHOLD_TASK_IDS,
    trials: Sequence[int] = THRESHOLD_TRIALS,
    cursors: Sequence[int] = THRESHOLD_CURSORS,
) -> dict[str, int]:
    """从后端同源的 RNG 取出每个「随机流位点」的固定故障阈值（0..999）。

    ``_pick_fault`` 判定 ``rng.randrange(1000) >= fpm`` 时输出干净动作，因此
    ``threshold < fpm`` 即该位点会注入故障。阈值不含 fpm，故降 fpm 只删
    故障、不改其余故障类型——模型轴因此是严格嵌套的单变量轴。键形如
    ``"T01:1:6"``（task_id : trial : cursor），不会把不同随机流混在一起，
    也不用 ``|`` 以免报告里的 Markdown 表格被拆列。
    """
    result: dict[str, int] = {}
    for task_id in task_ids:
        for trial in trials:
            backend = SeededBackend(
                BackendConfig(name="seeded", seed=seed),
                (),
                task_id=task_id,
                trial=trial,
            )
            for cursor in cursors:
                key = f"{task_id}:{trial}:{int(cursor)}"
                result[key] = backend._rng(int(cursor)).randrange(1000)
    return result


def nested_fault_counts(
    thresholds: dict[str, int], fpm_axis: Sequence[int] = FPM_AXIS
) -> list[dict[str, Any]]:
    """每档 fpm 的故障位点数与集合，用于自证嵌套性（不参与评测）。"""
    result: list[dict[str, Any]] = []
    for fpm in fpm_axis:
        affected = tuple(
            sorted(key for key, value in thresholds.items() if value < fpm)
        )
        result.append({"fpm": fpm, "count": len(affected), "keys": affected})
    return result


def is_nested(nested: Sequence[dict[str, Any]]) -> bool:
    """低 fpm 的故障集合必须是高 fpm 的子集，且计数单调不减。"""
    ordered = sorted(nested, key=lambda row: row["fpm"])
    for previous, current in zip(ordered, ordered[1:]):
        if current["count"] < previous["count"]:
            return False
        if not set(previous["keys"]).issubset(set(current["keys"])):
            return False
    return True


def intrinsic_task_count(records: Sequence[Record]) -> int:
    """从「零注入故障 + 四条护栏全开」格自动推导恒失败任务数，不硬编码 ID。

    口径与 ``ceiling.unrecoverable_tasks`` 一致：在所有 seed 上都非 k 次全过
    的任务只能是脚本固有缺陷，模型轴与护栏轴都够不到。
    """
    rows = cell_records(records, TREATMENT, ZERO_FPM)
    if not rows:
        raise ValueError("证据里没有零故障全开格，拒绝静默补零")
    by_task: dict[str, list[bool]] = {}
    for row in rows:
        for item in row["per_task"]:
            by_task.setdefault(str(item["task_id"]), []).append(
                bool(item["all_pass"])
            )
    return sum(not any(values) for values in by_task.values())


def is_monotone_nonincreasing(records: Sequence[Record], cell: str) -> bool:
    """曲线随 fpm 增大是否单调不增（只作如实描述，不预设成立）。"""
    rows = axis_rows(records)
    values = [row[f"{cell}_passk"] for row in rows]
    return all(
        right <= left + EFFECT_EPSILON
        for left, right in zip(values, values[1:])
    )


def render_report(records: Sequence[Record]) -> str:
    """从证据渲染报告；所有数字与结论措辞都由数据推导，不写死判断。"""
    if not records:
        raise ValueError("证据为空，拒绝渲染")
    seeds = seed_values(records)
    fpms = fpm_values(records)
    rows = axis_rows(records)
    zero = axis_row(records, ZERO_FPM)
    decomp = lift_decomposition(records)
    equivalent = equivalent_fpm(
        records, decomp["all4_at_reference"], CONTROL
    )
    deltas = per_seed_delta(records, REFERENCE_FPM)
    signs = ceiling.sign_counts(deltas)
    threshold_rows = nested_fault_counts(fault_thresholds())
    nested = is_nested(threshold_rows)
    task_count = int(records[0]["task_count"])
    step = 1 / task_count
    flat = all(
        abs(row[f"{TREATMENT}_passk"] - decomp["all4_at_zero"])
        < EFFECT_EPSILON
        for row in rows
    )
    strongest_fpm = min(fpms)
    weakest_fpm = max(fpms)
    off_ceiling = zero[f"{CONTROL}_passk"]
    harness_level = decomp["all4_at_reference"]
    intrinsic = intrinsic_task_count(records)
    share_text = (
        f"{decomp['model_share'] * 100:.1f}%"
        if decomp["model_share"] is not None
        else "不可计算"
    )
    first_delta = rows[0]["delta"]
    last_delta = rows[-1]["delta"]

    lines = [
        "# W3 归因对照：提升来自 harness，还是换一个更强的模型？",
        "",
        f"> 本报告全部数字来自确定性模拟器 `SeededBackend`，{len(seeds)} 个"
        "版本化 seed，零第三方依赖、零 API 成本。**它不是真实模型实验**，"
        "口径边界见最后一节。",
        "",
        "## 一句话结论",
        "",
        f"在 W3 的参考点 `fpm={REFERENCE_FPM}` 上，护栏全关的 `pass^k` 是 "
        f"{decomp['baseline']:.4f}；**不换模型、只开四条护栏**得到 "
        f"{harness_level:.4f}（+{decomp['harness_path']:.4f}）；"
        f"而把模型的输出故障率一路降到 `{ZERO_FPM}`（`fpm=0`）也只能得到 "
        f"{off_ceiling:.4f}（+{decomp['model_path']:.4f}）。",
        "",
        (
            f"**只靠降低模型故障率无法达到 `{harness_level:.4f}`**："
            f"`{CONTROL}` 整条曲线的上界只有 "
            f"{equivalent['best_value']:.4f}（在 fpm={equivalent['best_fpm']}），"
            f"缺口 {equivalent['gap']:.4f}。换模型路径最多兑现 "
            f"{share_text} 的 harness 收益。"
            if not equivalent["reachable"]
            else
            f"在 `{CONTROL}` 曲线上达到 {harness_level:.4f} 需要把 fpm 降到 "
            f"{equivalent['required_fpm']}（该档实测 {equivalent['value']:.4f}）。"
        ),
        "",
        "## 实验设计",
        "",
        f"2 × {len(fpms)} 全因子：模型轴 `fpm ∈ "
        f"{{{', '.join(str(value) for value in fpms)}}}` × 护栏轴 "
        f"`{{{CONTROL}, {TREATMENT}}}` × {len(seeds)} 个 seed，"
        f"共 {len(records)} 行证据。",
        "",
        "| 项 | 取值 |",
        "| --- | --- |",
        f"| 后端 | `SeededBackend`（确定性），任务集 `{robustness.DEFAULT_TASKS}` |",
        f"| k | {robustness.DEFAULT_K}（主指标 `pass^k`） |",
        f"| seed 集 | 直接复用 `eval/robustness.py` 的版本化常量，本次 "
        f"{len(seeds)} 个整数，人为选定、非随机抽样 |",
        f"| 模型轴 | `[seeded].fault_per_mille`，只改输出级故障率 |",
        f"| 护栏轴 | `[guards].enabled`：`{CONTROL}` = 空；`{TREATMENT}` = "
        f"{', '.join(f'`{name}`' for name in ALL4)} |",
        "| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1 |",
        "",
        "**这是因子实验，不是 A/B**：表内每条横向边（同 fpm、只改护栏）与"
        "纵向边（同护栏、只改 fpm）各自是单变量；**同时改两格的格子不能直接"
        "对比作因果结论**。W3 的单变量 A/B 证据仍在 `w3_robustness.md` 与"
        " `w3_tool_retry_sweep.md` / `w3_json_recovery_sweep.md`。",
        "",
        "### 为什么模型轴是干净的",
        "",
        "`SeededBackend._rng` 由 `(seed, task_id, trial, cursor)` 经 SHA256 "
        "派生，**不含 fpm**；`_pick_fault` 判定 `rng.randrange(1000) >= fpm` "
        "时输出干净动作。所以每个随机流位点有一个固定的 0..999 阈值，降 fpm "
        "只会删掉一部分故障，不会改变其余故障的类型或位置。下表取 "
        f"{len(THRESHOLD_TASK_IDS)} 个任务 × {len(THRESHOLD_TRIALS)} 个 trial "
        f"× {len(THRESHOLD_CURSORS)} 个 cursor = "
        f"{len(THRESHOLD_TASK_IDS) * len(THRESHOLD_TRIALS) * len(THRESHOLD_CURSORS)} "
        f"个位点（seed={THRESHOLD_SEED}），用后端同源的 RNG 实测：",
        "",
        "| fpm | 会注入故障的位点数 | 较上一档新增 | 位点示例（前 3 个） |",
        "| --- | --- | --- | --- |",
    ]
    for index, row in enumerate(threshold_rows):
        added = (
            row["count"] - threshold_rows[index - 1]["count"]
            if index
            else row["count"]
        )
        sample = ", ".join(row["keys"][:3]) or "（空）"
        lines.append(
            f"| {row['fpm']} | {row['count']} | +{added} | {sample} |"
        )
    lines += [
        "",
        f"低 fpm 的故障位点集合是否为高 fpm 的子集、计数是否单调不减：**"
        f"{'是' if nested else '否'}**。因此「降 fpm」在本模拟器里严格等价于"
        "「同一模型少犯输出级错误」，是单变量轴；它**不**等价下文口径边界里"
        "列出的模型推理 / 理解能力。",
        "",
        "## 两条曲线",
        "",
        "| fpm（‰） | "
        f"`{CONTROL}` `pass^k` | `{CONTROL}` min~max | `{TREATMENT}` `pass^k` | "
        f"`{TREATMENT}` min~max | Δ（护栏收益） |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['fpm']} | {row[f'{CONTROL}_passk']:.4f} | "
            f"{row[f'{CONTROL}_min']:.2f}~{row[f'{CONTROL}_max']:.2f} | "
            f"{row[f'{TREATMENT}_passk']:.4f} | "
            f"{row[f'{TREATMENT}_min']:.2f}~{row[f'{TREATMENT}_max']:.2f} | "
            f"{row['delta']:+.4f} |"
        )
    lines += [
        "",
        f"- **`{CONTROL}` 曲线随模型变可靠而上升，但有天花板**："
        f"fpm 从 {weakest_fpm} 降到 {strongest_fpm}，`pass^k` 从 "
        f"{rows[-1][f'{CONTROL}_passk']:.4f} 变为 {off_ceiling:.4f}；"
        f"按 fpm 从低到高是否单调不增：**"
        f"{'是' if is_monotone_nonincreasing(records, CONTROL) else '否'}**。"
        f"这个上界由 {intrinsic} 条脚本固有缺陷任务锁死，"
        "清单见 [可达上限分析](w3_ceiling.md)。",
        f"- **`{TREATMENT}` 曲线"
        f"{'全程平坦' if flat else '并非全程平坦'}**："
        f"各档 `pass^k` "
        + (
            f"均为 {decomp['all4_at_zero']:.4f}，说明四条护栏把注入故障完全"
            "吸收，模型轴不再影响结果。"
            if flat
            else "存在波动，不能说「护栏对注入故障完全免疫」，详见上表。"
        ),
        f"- **护栏收益随模型变强而"
        f"{'收窄但不归零' if first_delta > EFFECT_EPSILON else '收窄且归零'}**："
        f"fpm={weakest_fpm} 时 Δ={last_delta:+.4f}，"
        f"fpm={strongest_fpm} 时 Δ={first_delta:+.4f}。"
        + (
            "后者是护栏在「零注入故障」下仍修好的脚本固有缺陷（T03 重复写），"
            "不在模型轴的作用范围内。"
            if first_delta > EFFECT_EPSILON
            else "零故障下护栏不再产生任务级差异。"
        ),
        "",
        "## 收益拆分",
        "",
        "| 路径 | 起点 → 终点 | Δ`pass^k` |",
        "| --- | --- | --- |",
        f"| 只换模型（降故障率） | `{CONTROL}`@{REFERENCE_FPM} "
        f"{decomp['baseline']:.4f} → `{CONTROL}`@0 {decomp['off_at_zero']:.4f} | "
        f"{decomp['model_path']:+.4f} |",
        f"| 只加 harness | `{CONTROL}`@{REFERENCE_FPM} "
        f"{decomp['baseline']:.4f} → `{TREATMENT}`@{REFERENCE_FPM} "
        f"{decomp['all4_at_reference']:.4f} | "
        f"{decomp['harness_path']:+.4f} |",
        f"| 零故障下护栏的残余收益 | `{CONTROL}`@0 "
        f"{decomp['off_at_zero']:.4f} → `{TREATMENT}`@0 "
        f"{decomp['all4_at_zero']:.4f} | "
        f"{decomp['intrinsic_extra']:+.4f} |",
        f"| 换模型路径够不到的部分 | `{TREATMENT}`@{REFERENCE_FPM} − "
        f"`{CONTROL}`@0 | {decomp['unreachable_by_model']:+.4f} |",
        "",
        (
            "因为护栏曲线平坦，恒等式 "
            "`harness_path = model_path + (all4@0 − off@0)` 成立（实测 "
            f"**{'是' if decomp['flat_identity_holds'] else '否'}**）："
            "加护栏的收益可以完整拆成「等价消除全部注入故障」+「额外修好一个"
            "脚本固有缺陷」。"
            if decomp["flat_identity_holds"]
            else "护栏曲线不平坦，收益拆分不满足该恒等式，不能把两者简单相加。"
        ),
        "",
        f"**必须点破的一点**：`{decomp['all4_at_reference']:.2f} > "
        f"{decomp['off_at_zero']:.2f}` 的那 {decomp['intrinsic_extra']:.2f} "
        "来自 `idempotency` 修了 T03 脚本里固有的重复退款"
        "（W3 已钉死，见 [w3_ceiling.md](w3_ceiling.md)）。"
        "它不是「harness 普遍强于更强模型」的证明——真实模型完全可能自己不重复"
        "退款。这里能成立的只有一句：*在本模拟器的这条模型轴上*，降低故障率"
        "够不到加护栏的水平，因为这条轴不动脚本固有缺陷。",
        "",
        "## 逐 seed 的横向效应（参考点）",
        "",
        f"`fpm={REFERENCE_FPM}` 上逐个 seed 的 Δ`pass^k`（`{TREATMENT}` − "
        f"`{CONTROL}`）：**正 {signs['positive']} / 零 "
        f"{signs['zero']} / 负 {signs['negative']}**，均值 "
        f"{signs['mean']:+.4f}。",
        "",
        "| seed | Δ`pass^k` |",
        "| --- | --- |",
    ]
    for seed, delta in deltas:
        lines.append(f"| {seed} | {delta:+.2f} |")
    lines += [
        "",
        f"这是描述性统计、**不是显著性检验**：没有置信区间与 p 值，"
        f"{len(seeds)} 个 seed 是人为选定的整数、不是随机抽样，"
        "符号分布不能当概率估计。",
        "",
        "## 口径边界（不得省略）",
        "",
        "1. **fpm 不是「模型能力」的全部**。它只控制四类*输出级*故障"
        "（坏 JSON / 错参数 / 错工具 / 重复写）的注入率，不模拟推理、语言理解、"
        "工具规划、多轮追问或长上下文能力。把降 fpm 叫作「换更强的模型」是"
        "本实验内部的类比，**不能外推到任何真实模型**。",
        f"2. **`fpm=0` 不是「完美模型」**。从证据自动推导的 {intrinsic} 条"
        "恒失败任务（脚本固有缺陷：坏 JSON、缺参数、错工具、无 final、超退、"
        "少说必要告知）原样保留，所以 "
        f"`{CONTROL}` 曲线被 fixture 锁死在 {off_ceiling:.4f}。"
        "这个天花板是任务集属性，不是模型属性；清单见 "
        "[w3_ceiling.md](w3_ceiling.md)。",
        "3. **不替代 W4**。真实模型是否在 `[GUARD-REJECT]` 下改对、以及成本 / "
        "延迟帕累托，都没在这里测；W4 接真实模型前仍需先定 API 预算。"
        "本报告只把对照设计与口径先在零成本模拟器上跑通。",
        f"4. **分辨率仍是 {step:.2f}**。`pass^k = passk_hits / "
        f"{task_count}`，所有均值都落在 1/{task_count} 的格点组合上，"
        f"表里的 {decomp['model_path']:+.4f} / "
        f"{decomp['harness_path']:+.4f} 是 {len(seeds)} 个 seed 的平均，"
        "**不是连续效应量**；见 [w3_resolution.md](w3_resolution.md)。",
        "5. **因子实验不是 A/B**。跨 fpm 又跨护栏的两格对比不作因果解读；"
        "W3 的单变量因果证据仍以 `w3_robustness.md` 与各 `*_sweep.md` 为准。",
        "6. 全程是本地后端的确定性（无网络、无时钟、无全局随机），"
        "token / 延迟恒 0，不构成任何成本或延迟结论；`invalid_calls` 同样"
        "不是成功率的代理指标。",
        "",
        "## 数据来源与复现",
        "",
        f"每行一档 fpm、一个 seed、一格的完整指标（seed / fpm / cell / guards / "
        f"pass¹ / pass^k / passk_hits / task_count / trial_count / 逐任务 0/1 "
        f"序列）在 `{EVIDENCE_NAME}`；既有单变量 A/B 留档在 "
        "`experiments/runs/<run_id>/`。",
        "",
        "```",
        "python -m eval.attribution",
        "python -m eval.attribution --from-jsonl",
        "python -m unittest discover -s tests -v",
        "```",
        "",
        "本模块不新增护栏、不加配置字段、不改判分口径、不修改任何既有留档；"
        "跑批只写共用临时目录，不读时钟 / 环境变量、不用全局随机、零第三方依赖，"
        "同命令两次产物逐字节一致。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.attribution",
        description="W3 归因对照：模型能力轴 × 护栏轴的 2×8 全因子实验",
    )
    parser.add_argument(
        "--seeds", default=None,
        help="逗号分隔的整数；默认使用 eval/robustness.py 里版本化的 seed 集",
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
        records = robustness.load_records(
            evidence_path.read_text(encoding="utf-8")
        )
    else:
        seeds = (
            DEFAULT_SEEDS
            if args.seeds is None
            else robustness.parse_seeds(args.seeds)
        )
        records = collect(
            seeds,
            tasks_path=resolve_input(robustness.DEFAULT_TASKS),
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(
            robustness.dumps_records(records).encode("utf-8")
        )

    text = render_report(records)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(text.encode("utf-8"))

    decomp = lift_decomposition(records)
    print(
        f"fpm 档位：{list(fpm_values(records))}，seed 数："
        f"{len(seed_values(records))}，证据行：{len(records)}"
    )
    for row in axis_rows(records):
        print(
            f"  fpm={row['fpm']:3d}: off {row[f'{CONTROL}_passk']:.4f} → "
            f"all4 {row[f'{TREATMENT}_passk']:.4f} (Δ{row['delta']:+.4f})"
        )
    equivalent = equivalent_fpm(
        records, decomp["all4_at_reference"], CONTROL
    )
    print(
        f"  等效模型提升："
        + (
            f"不可达（上界 {equivalent['best_value']:.4f}，缺口 "
            f"{equivalent['gap']:.4f}）"
            if not equivalent["reachable"]
            else f"需把 fpm 降到 {equivalent['required_fpm']}"
        )
    )
    print(f"证据：{evidence_path}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
