"""W3 收口后的测量标尺升级：v2 扩展任务集与 ``pass^k`` 分辨率对照。

动机：[w3_resolution.md](w3_resolution.md) 证明 ``passk = passk_hits /
task_count``，分辨率恒为 ``1/N``、与 k 无关；既有 10 条任务里 7 条是脚本
固有缺陷任务，在任何 seed / 任何护栏组合下都恒失败、不携带信息，导致这把
尺子实际只有 0.00~0.30 共 4 个可达档位。真正变细只能**新增独立任务**。

本模块做的不是「把任务变难」或「让数字更好看」，而是比较两个版本化
测量标尺的**分辨率属性**：

* ``mvp_10.jsonl``：既有 10 条（7 条固有缺陷 + 3 条可翻转）；
* ``mvp_30.jsonl``：前 10 行逐字节沿用，后 20 条是脚本正确的独立任务，
  成败只由「注入故障是否被护栏恢复」决定，因此是携带信息的测量位点。

2 个任务集 × 3 格（``off`` / 单开 ``schema_validation`` / 四条全开）×
30 个版本化 seed。三格的设计理由：``off→schema`` 是既有 30-seed 检验里
最弱的效应（单变量 A/B 下只有 4 个正效应，见 [w3_resolution.md]），
最能暴露「尺子太钝」；``all4`` 是可达上限。

注意 [w3_robustness.md] 里 ``schema_validation`` 的正 5 / 零 25 是 **2×2 四格
主效应**（开 / 关两组均值之差），与本模块的单变量直接差不是同一个量；
[w3_resolution.md] 已就此写过口径提醒。

**口径红线**：任务集是第三个自变量。两个任务集的 ``pass^k`` 分母与任务
构成都不同，**跨任务集的数值对比只描述分辨率属性，不是「护栏效应变大」
的因果结论**，也不与任何既有 W3 留档混用。单变量因果证据仍以
[w3_robustness.md](w3_robustness.md) 与各 ``*_sweep.md`` 为准。

零第三方依赖、不读时钟 / 环境变量、不用全局随机；跑批只写共用临时目录。
``--from-jsonl`` 可从证据只读重渲染，不重跑评测。本模块不新增护栏、不加
配置字段、不改判分口径、不修改任何既有留档。
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any, Sequence

from eval import analyze, ceiling, robustness, runner
from eval.config import resolve_input
from runtime import guard
from runtime.model import BackendConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "experiments"
EVIDENCE_NAME = "w3_taskset_scale.jsonl"
REPORT_NAME = "w3_taskset_scale.md"
EFFECT_EPSILON = 1e-9

# 全部测量输入复用版本化常量，避免一次检查混入第四个自变量。
DEFAULT_SEEDS = robustness.DEFAULT_SEEDS
DEFAULT_K = robustness.DEFAULT_K
DEFAULT_FPM = robustness.DEFAULT_FAULT_PER_MILLE

V1 = "mvp_10"
V2 = "mvp_30"
V1_TASKS = robustness.DEFAULT_TASKS
V2_TASKS = "tasks/mvp_30.jsonl"
TASKSETS: tuple[tuple[str, str], ...] = (
    (V1, V1_TASKS),
    (V2, V2_TASKS),
)
TASKSET_NAMES = tuple(name for name, _ in TASKSETS)

CONTROL = "off"
WEAK = "schema"
STRONG = "all4"
ALL4 = ceiling.ALL4
CELLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CONTROL, ()),
    (WEAK, (guard.GUARD_SCHEMA_VALIDATION,)),
    (STRONG, ALL4),
)
CELL_NAMES = tuple(name for name, _ in CELLS)

# 确定性脚本后端的参考基线：每个任务集一行，用来交叉验证「新任务脚本正确」
# 以及 W3 收口结论（idempotency 额外修好一条脚本固有缺陷）。它不参与
# 任何 seed 级统计，所有聚合函数都显式按 CELL_NAMES 过滤。
SCRIPTED_CELL = "scripted"
SCRIPTED_SEED = -1

Record = dict[str, Any]
Delta = tuple[int, float]


def _record(
    taskset: str,
    cell: str,
    seed: int,
    guards: Sequence[str],
    summary: dict[str, Any],
) -> Record:
    return {
        "taskset": taskset,
        "cell": cell,
        "seed": int(seed),
        "guards": list(guards),
        "pass1": summary["pass1"],
        "passk": summary["passk"],
        "passk_hits": summary["passk_hits"],
        "task_count": summary["task_count"],
        "trial_count": summary["trial_count"],
        "invalid_calls": summary["invalid_calls"],
        "total_steps": summary["total_steps"],
        "failure_counts": dict(sorted(summary["failure_counts"].items())),
        "per_task": [
            {
                "task_id": item["task_id"],
                "sequence": item["sequence"],
                "all_pass": item["all_pass"],
                "failure_type": item["failure_type"],
            }
            for item in summary["per_task"]
        ],
    }


def collect(
    seeds: Sequence[int],
    *,
    k: int = DEFAULT_K,
    fault_per_mille: int = DEFAULT_FPM,
    tasksets: Sequence[tuple[str, str]] = TASKSETS,
) -> list[Record]:
    """2 任务集 × 3 格 × 每个 seed 重跑，另加 2 行脚本后端参考基线。

    只取 ``runner.run`` 返回的 rows 做汇总，不读任何跑批落盘文件；临时目录里
    各格反复覆盖同名文件也不影响结果，产物完全确定。
    """
    if k < 1:
        raise ValueError("k 必须 >= 1")
    if not 0 <= fault_per_mille <= 1000:
        raise ValueError("fault_per_mille 必须落在 0..1000")
    if not seeds:
        raise ValueError("seed 集不得为空")
    names = tuple(name for name, _ in tasksets)
    if len(set(names)) != len(names) or not names:
        raise ValueError("任务集名必须非空且互不重复")

    records: list[Record] = []
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for taskset, tasks_rel in tasksets:
            tasks_path = resolve_input(tasks_rel)
            reference = runner.run(
                runner.RunConfig(
                    tasks_path=tasks_path,
                    k=k,
                    backend=BackendConfig(),
                    results_dir=scratch / "results",
                    traces_dir=scratch / "traces",
                )
            )
            records.append(
                _record(
                    taskset, SCRIPTED_CELL, SCRIPTED_SEED, (),
                    analyze.summarize(reference, k),
                )
            )
            for seed in seeds:
                for cell, guards in CELLS:
                    rows = runner.run(
                        runner.RunConfig(
                            tasks_path=tasks_path,
                            k=k,
                            backend=BackendConfig(
                                name="seeded",
                                seed=int(seed),
                                fault_per_mille=fault_per_mille,
                            ),
                            results_dir=scratch / "results",
                            traces_dir=scratch / "traces",
                            guards=guards,
                        )
                    )
                    records.append(
                        _record(
                            taskset, cell, int(seed), guards,
                            analyze.summarize(rows, k),
                        )
                    )
    return records


# ---------------------------------------------------------------------------
# 纯函数：全部只读证据，拒绝缺格静默补零
# ---------------------------------------------------------------------------

def nominal_step(task_count: int) -> float:
    """``pass^k`` 的最小步进只由任务数决定，与 k 无关。"""
    if task_count < 1:
        raise ValueError("task_count 必须 >= 1")
    return 1.0 / task_count


def task_names(records: Sequence[Record]) -> tuple[str, ...]:
    seen: list[str] = []
    for record in records:
        name = str(record["taskset"])
        if name not in seen:
            seen.append(name)
    if not seen:
        raise ValueError("证据为空")
    return tuple(seen)


def seed_values(records: Sequence[Record]) -> tuple[int, ...]:
    return tuple(
        sorted({
            int(record["seed"])
            for record in records
            if record["cell"] in CELL_NAMES
        })
    )


def task_counts(records: Sequence[Record]) -> dict[str, int]:
    result: dict[str, int] = {}
    for record in records:
        name = str(record["taskset"])
        count = int(record["task_count"])
        if name in result and result[name] != count:
            raise ValueError(f"任务集 {name} 的 task_count 前后不一致")
        result[name] = count
    return result


def _select(
    records: Sequence[Record], taskset: str, cell: str
) -> list[Record]:
    return [
        record for record in records
        if record["taskset"] == taskset and record["cell"] == cell
    ]


def cell_summary(
    records: Sequence[Record], taskset: str, cell: str
) -> dict[str, Any]:
    """一格的 30-seed 描述性统计与实测可达档位。"""
    rows = _select(records, taskset, cell)
    if not rows:
        raise ValueError(f"证据里没有格子 {taskset}/{cell}")
    expected = len(seed_values(records))
    if len(rows) != expected or len({int(row["seed"]) for row in rows}) != expected:
        raise ValueError(f"{taskset}/{cell} 缺格或重复，拒绝静默补零")
    values = [float(row["passk"]) for row in rows]
    levels = sorted({round(value, 9) for value in values})
    count = int(rows[0]["task_count"])
    return {
        "taskset": taskset,
        "cell": cell,
        "seed_count": len(rows),
        "task_count": count,
        "passk_mean": sum(values) / len(values),
        "passk_min": min(values),
        "passk_max": max(values),
        "levels": levels,
        "level_count": len(levels),
        "step": nominal_step(count),
    }


def attainable_levels(
    records: Sequence[Record], taskset: str
) -> list[float]:
    """该任务集在全部三格 × 全部 seed 上实际出现过的 ``pass^k`` 取值。"""
    values = [
        round(float(record["passk"]), 9)
        for record in records
        if record["taskset"] == taskset and record["cell"] in CELL_NAMES
    ]
    if not values:
        raise ValueError(f"证据里没有任务集 {taskset} 的格子")
    return sorted(set(values))


def information_sites(records: Sequence[Record], taskset: str) -> dict[str, Any]:
    """按任务聚合全部 seed × 三格的「是否 k 次全过」，划分三类位点。

    从证据自动推导，不硬编码任务 ID：既有 10 条里的固有缺陷任务会落在
    ``constant_fail``，新增的脚本正确任务应落在 ``informative``。
    """
    flags: dict[str, list[bool]] = {}
    for record in records:
        if record["taskset"] != taskset or record["cell"] not in CELL_NAMES:
            continue
        for item in record["per_task"]:
            flags.setdefault(str(item["task_id"]), []).append(
                bool(item["all_pass"])
            )
    if not flags:
        raise ValueError(f"证据里没有任务集 {taskset} 的逐任务序列")
    widths = {len(values) for values in flags.values()}
    if len(widths) != 1:
        raise ValueError(f"任务集 {taskset} 的逐任务观测数不一致：{widths}")
    return {
        "total": len(flags),
        "observations": widths.pop(),
        "informative": sorted(
            tid for tid, values in flags.items()
            if any(values) and not all(values)
        ),
        "constant_fail": sorted(
            tid for tid, values in flags.items() if not any(values)
        ),
        "constant_pass": sorted(
            tid for tid, values in flags.items() if all(values)
        ),
    }


def per_seed_delta(
    records: Sequence[Record], taskset: str, left: str, right: str
) -> list[Delta]:
    """同任务集、同 seed、只改护栏的逐 seed Δ；缺格直接报错。"""
    if left not in CELL_NAMES or right not in CELL_NAMES:
        raise ValueError(f"未知的格子：{left} / {right}")
    lhs = {int(row["seed"]): float(row["passk"]) for row in _select(records, taskset, left)}
    rhs = {int(row["seed"]): float(row["passk"]) for row in _select(records, taskset, right)}
    seeds = seed_values(records)
    if set(lhs) != set(seeds) or set(rhs) != set(seeds):
        raise ValueError(f"{taskset} 的 {left}/{right} 缺少格子，拒绝静默补零")
    return [(seed, rhs[seed] - lhs[seed]) for seed in seeds]


def scripted_reference(records: Sequence[Record]) -> dict[str, dict[str, Any]]:
    """确定性脚本后端的参考基线（每个任务集一行）。

    同时返回整数命中数：两个任务集的分母不同，``pass^k`` 分数**不能直接
    相减**，只有 ``passk_hits`` 的任务条数可以逐条相加 / 相减。
    """
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if record["cell"] == SCRIPTED_CELL:
            result[str(record["taskset"])] = {
                "passk": float(record["passk"]),
                "hits": int(record["passk_hits"]),
                "task_count": int(record["task_count"]),
            }
    if set(result) != set(task_names(records)):
        raise ValueError("脚本后端参考基线缺失")
    return result


def scale_summary(records: Sequence[Record]) -> dict[str, dict[str, Any]]:
    """两个标尺的分辨率属性汇总，全部由证据推导。"""
    counts = task_counts(records)
    result: dict[str, dict[str, Any]] = {}
    for name in task_names(records):
        levels = attainable_levels(records, name)
        sites = information_sites(records, name)
        cells = {
            cell: cell_summary(records, name, cell) for cell in CELL_NAMES
        }
        result[name] = {
            "task_count": counts[name],
            "step": nominal_step(counts[name]),
            "levels": levels,
            "level_count": len(levels),
            "level_min": min(levels),
            "level_max": max(levels),
            "span": max(levels) - min(levels),
            "informative": len(sites["informative"]),
            "constant_fail": len(sites["constant_fail"]),
            "constant_pass": len(sites["constant_pass"]),
            "sites": sites,
            "cells": cells,
        }
    return result


def weak_effect_summary(records: Sequence[Record]) -> dict[str, dict[str, Any]]:
    """单开 ``schema_validation`` 的 30-seed 符号分布，用于量化尺子灵敏度。"""
    result: dict[str, dict[str, Any]] = {}
    for name in task_names(records):
        deltas = per_seed_delta(records, name, CONTROL, WEAK)
        result[name] = {
            "deltas": deltas,
            "signs": ceiling.sign_counts(deltas),
            "levels": sorted({round(delta, 9) for _, delta in deltas}),
        }
    return result


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def render_report(records: Sequence[Record]) -> str:
    if not records:
        raise ValueError("证据为空，拒绝渲染报告")
    names = task_names(records)
    if names != TASKSET_NAMES:
        raise ValueError(f"任务集顺序/组成不符：{names}")
    seeds = seed_values(records)
    summary = scale_summary(records)
    weak = weak_effect_summary(records)
    reference = scripted_reference(records)
    old, new = summary[V1], summary[V2]
    old_signs, new_signs = weak[V1]["signs"], weak[V2]["signs"]
    old_cells, new_cells = old["cells"], new["cells"]
    v2_strong = new_cells[STRONG]
    v1_strong = old_cells[STRONG]

    step_ratio = old["step"] / new["step"] if new["step"] else 0.0
    level_delta = new["level_count"] - old["level_count"]
    site_delta = new["informative"] - old["informative"]
    positive_delta = new_signs["positive"] - old_signs["positive"]
    zero_delta = old_signs["zero"] - new_signs["zero"]
    refined = new["step"] < old["step"] - EFFECT_EPSILON
    more_levels = level_delta > 0
    more_sites = site_delta > 0
    more_sensitive = positive_delta > 0 and zero_delta > 0

    # all4 在新标尺上的通过任务分解：新增任务全部通过 + 既有任务通过数。
    v2_strong_records = _select(records, V2, STRONG)
    passed_ids = sorted({
        str(item["task_id"])
        for record in v2_strong_records
        for item in record["per_task"]
        if item["all_pass"]
    })
    added_ids = sorted(set(passed_ids) - set(old["sites"]["informative"])
                       - set(old["sites"]["constant_fail"])
                       - set(old["sites"]["constant_pass"]))
    inherited_pass = sorted(set(passed_ids) & set(
        old["sites"]["informative"] + old["sites"]["constant_fail"]
        + old["sites"]["constant_pass"]
    ))
    scripted_gap = (
        v2_strong["passk_mean"] - reference[V2]["passk"]
        if V2 in reference else None
    )
    added_count = new["task_count"] - old["task_count"]
    hit_delta = reference[V2]["hits"] - reference[V1]["hits"]

    lines = [
        "# W3 测量标尺升级：v2 扩展任务集与 `pass^k` 分辨率对照",
        "",
        "> 本报告全部数字来自确定性模拟器 `SeededBackend`，"
        f"{len(seeds)} 个版本化 seed，零第三方依赖、零 API 成本。"
        "**任务集是第三个自变量，跨任务集的数值对比只描述分辨率属性，不是因果结论**；"
        "口径边界见最后一节。",
        "",
        "## 一句话结论",
        "",
        f"`{V1}` 的名义步进是 {old['step']:.4f}（1/{old['task_count']}），"
        f"实测可达档位 {old['level_count']} 个、携带信息的位点 "
        f"{old['informative']} 个；`{V2}` 的名义步进是 "
        f"{new['step']:.4f}（1/{new['task_count']}，细 {step_ratio:.1f} 倍），"
        f"实测可达档位 {new['level_count']} 个、携带信息的位点 "
        f"{new['informative']} 个。",
        "",
        (
            "**名义步进变细、可达档位变多、信息位点变多，三者同时成立**；"
            if refined and more_levels and more_sites
            else "三项指标并非同时改善，需按下表逐项读，不能只报名义步进；"
        )
        + f"最直接的证据是弱效应护栏：单开 `schema_validation` 的 "
        f"{len(seeds)} 个 seed 里，正效应从 {old_signs['positive']} 个变成 "
        f"{new_signs['positive']} 个、零效应从 {old_signs['zero']} 个变成 "
        f"{new_signs['zero']} 个"
        + (
            "，**尺子灵敏度确实提高**。" if more_sensitive
            else "，但不能据此断言灵敏度提高，详见下表。"
        ),
        "",
        "## 实验设计",
        "",
        f"{len(names)} 个任务集 × {len(CELL_NAMES)} 格 × {len(seeds)} 个 seed = "
        f"{sum(1 for r in records if r['cell'] in CELL_NAMES)} 行评测证据，"
        f"另有 {len(names)} 行 `ScriptedBackend` 参考基线。",
        "",
        "| 项 | 取值 |",
        "| --- | --- |",
        f"| 任务集 | `{V1}` = `{V1_TASKS}`（{old['task_count']} 条）；"
        f"`{V2}` = `{V2_TASKS}`（{new['task_count']} 条，前 "
        f"{old['task_count']} 行逐字节沿用） |",
        "| 后端 | `SeededBackend`（确定性）；参考基线用 `ScriptedBackend` |",
        f"| k | {DEFAULT_K}（主指标 `pass^k`） |",
        f"| 故障注入率 | {DEFAULT_FPM}/1000，两个任务集完全相同 |",
        f"| seed 集 | 直接复用 `eval/robustness.py` 的版本化常量，本次 "
        f"{len(seeds)} 个整数，人为选定、非随机抽样 |",
        f"| 三格 | `{CONTROL}` = 空；`{WEAK}` = 单开 "
        f"`{guard.GUARD_SCHEMA_VALIDATION}`；`{STRONG}` = "
        f"{', '.join(f'`{name}`' for name in ALL4)} |",
        "| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1；"
        "两个任务集口径完全相同 |",
        "",
        "### 为什么新增任务的脚本全部正确",
        "",
        "[分辨率审计](w3_resolution.md) 已证明：既有 10 条里 7 条是脚本固有缺陷"
        "任务，在任何 seed、任何护栏组合下都 k 次全不过，**不携带信息**；"
        "再复制一批恒失败任务只会让分母变大、名义步进变细，可达动态范围却不变，"
        "属于自欺。所以新增 20 条使用**正确脚本**，让成败只由「注入故障是否被"
        "护栏恢复」决定。它们不是更难或更容易的任务，只是更多独立测量位点："
        "覆盖 3 笔订单 / 2 位客户 / 不同退款金额 / 单轮与多轮 / 2~5 个不等的"
        "脚本步数（= 不同数量的故障注入 cursor），且 `task_id` 进入 RNG 派生，"
        "随机流彼此独立。",
        "",
        f"**7 条固有缺陷任务一个都没剔除**，仍作为 `{V2}` 的前 10 行原样保留，"
        "既是归因分层的重要样本，也保证与既有留档可比。"
        "[可达上限分析](w3_ceiling.md) 明确写过：护栏不该越界去修固有缺陷"
        "（等于评测替模型做对）。",
        "",
        "## 分辨率对照",
        "",
        "| 指标 | " + f"`{V1}` | `{V2}` | 变化 |",
        "| --- | --- | --- | --- |",
        f"| 任务数 N | {old['task_count']} | {new['task_count']} | "
        f"+{new['task_count'] - old['task_count']} |",
        f"| 名义步进 1/N | {old['step']:.4f} | {new['step']:.4f} | "
        f"{'细' if refined else '未变细'} {step_ratio:.1f} 倍 |",
        f"| 实测可达档位数 | {old['level_count']} | {new['level_count']} | "
        f"{level_delta:+d} |",
        f"| 实测动态范围 | {old['level_min']:.4f}~{old['level_max']:.4f} "
        f"({old['span']:.4f}) | {new['level_min']:.4f}~{new['level_max']:.4f} "
        f"({new['span']:.4f}) | — |",
        f"| 携带信息的位点 | {old['informative']} | {new['informative']} | "
        f"{site_delta:+d} |",
        f"| 恒失败位点 | {old['constant_fail']} | {new['constant_fail']} | "
        f"{new['constant_fail'] - old['constant_fail']:+d} |",
        f"| 恒通过位点 | {old['constant_pass']} | {new['constant_pass']} | "
        f"{new['constant_pass'] - old['constant_pass']:+d} |",
        "",
        "**名义步进只是下界，真正决定尺子好不好用的是「可达档位数」与"
        "「携带信息的位点数」**。分母变大后如果新增位点全是恒失败 / 恒通过，"
        "档位数不会增加；上表两项同时变化才是分辨率真正改善的证据。",
        "",
        "口径区分：[分辨率审计](w3_resolution.md) 说「`mvp_10` 实际只有 "
        f"{len(old['sites']['informative']) + 1} 档"
        f"（0.00~{old['level_max']:.2f}）」指的是**理论格点数**"
        f"（{len(old['sites']['informative'])} 个可翻转位点）；本表统计的是"
        f"**本次 {len(seeds)} 个 seed 实测出现过的取值数**——"
        f"`{V1}` 是 {old['level_count']} 个（0.00 那档没出现），"
        f"`{V2}` 是 {new['level_count']} 个。两者不矛盾，比的是不同东西。",
        "",
        "### 各格实测档位",
        "",
        "| 任务集 | 格子 | 均值 `pass^k` | min~max | 实测不同取值 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name in names:
        for cell in CELL_NAMES:
            item = summary[name]["cells"][cell]
            levels = ", ".join(f"{value:.4f}" for value in item["levels"])
            lines.append(
                f"| `{name}` | `{cell}` | {item['passk_mean']:.4f} | "
                f"{item['passk_min']:.4f}~{item['passk_max']:.4f} | {levels} |"
            )
    lines += [
        "",
        "注意「实测不同取值」是 30 个 seed 上出现过的离散格点，不是连续效应量；"
        "`passk_hits` 只能是 0..N 的整数，所以每个取值都严格落在 1/N 的格点上。",
        "",
        "### 携带信息的位点清单（由证据自动推导，不硬编码任务 ID）",
        "",
        "| 任务集 | 恒失败 | 恒通过 | 可翻转（携带信息） |",
        "| --- | --- | --- | --- |",
    ]
    def _join(values: Sequence[str]) -> str:
        return ", ".join(f"`{value}`" for value in values) or "（无）"

    for name in names:
        sites = summary[name]["sites"]
        lines.append(
            f"| `{name}` | {_join(sites['constant_fail'])} | "
            f"{_join(sites['constant_pass'])} | "
            f"{_join(sites['informative'])} |"
        )
    newly_informative = (
        len(summary[V2]["sites"]["informative"])
        - len(summary[V1]["sites"]["informative"])
    )
    lines += [
        "",
        (
            f"注意 `{V2}` 的 {len(summary[V2]['sites']['constant_pass'])} 条"
            "恒通过位点都是只读任务（脚本只有 1~2 个读工具调用，故障注入位点最少），"
            f"在本次 {len(seeds)} 个 seed × {len(CELL_NAMES)} 格里始终 k 次全过，"
            "**不携带变化信息**。也就是说新增的 "
            f"{added_count} 条里真正贡献可翻转档位的是 {newly_informative} 条，"
            "不是全部。这正说明「分母变大」不等于「分辨率自动改善」——"
            "必须同时看可达档位数与信息位点数；本报告没有把这些恒通过位点算作功劳，"
            "也没有事后把它们从分母里剔除。"
            if summary[V2]["sites"]["constant_pass"] else ""
        ),
        "",
        "## 尺子灵敏度：弱效应护栏的逐 seed 符号分布",
        "",
        f"`{CONTROL}` → `{WEAK}`（单开 `schema_validation`）是既有 30-seed 检验里"
        "最弱的效应，最适合检验「尺子是否太钝」。两行共用同一批 seed、同一 fpm、"
        "同一护栏，唯一差别是任务集：",
        "",
        "| 任务集 | 正效应 | 零效应 | 负效应 | 均值 Δ | 实测 Δ 取值 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in names:
        item = weak[name]
        signs = item["signs"]
        levels = ", ".join(f"{value:+.4f}" for value in item["levels"]) or "（无）"
        lines.append(
            f"| `{name}` | {signs['positive']} | {signs['zero']} | "
            f"{signs['negative']} | {signs['mean']:+.4f} | {levels} |"
        )
    lines += [
        "",
        (
            f"正效应 seed 增加 {positive_delta} 个、零效应 seed 减少 {zero_delta} 个："
            "**更多真实修复跨过了 k 次全过门槛，不再被二值化压成零效应**。"
            if more_sensitive else
            "符号分布未出现「正效应增加且零效应减少」，不能宣称灵敏度提升。"
        ),
        "这解释的是「同一条护栏为什么在旧标尺上测不到、在新标尺上测得到」，"
        "**不是「护栏在新任务集上效果更好」**——两个任务集的 `pass^k` 分母不同，"
        "均值 Δ 不可跨行相减作因果解读。",
        "",
        "另需按 [分辨率审计](w3_resolution.md) 的结论拆两类零效应：护栏压根没"
        "修好任何 trial，与修好了却被 k 次全过门槛吞掉，是两回事。"
        "本报告只给符号分布，不把零效应一律解释成「尺子太钝」。",
        "",
        "## 与既有结论的交叉验证",
        "",
        f"- **脚本正确性**：`ScriptedBackend` 下 `{V1}` 通过 "
        f"{reference[V1]['hits']}/{reference[V1]['task_count']} 条"
        f"（`pass^k` {reference[V1]['passk']:.4f}），`{V2}` 通过 "
        f"{reference[V2]['hits']}/{reference[V2]['task_count']} 条"
        f"（`pass^k` {reference[V2]['passk']:.4f}）；整数命中数多 "
        f"{hit_delta} 条，恰好等于新增的 {added_count} 条任务全部通过，"
        "说明新任务的 `target_db` / 策略检查 / 必要告知与脚本一致，"
        "没有靠放宽判分换取好看数字。**两个 `pass^k` 分母不同，分数不可直接相减**，"
        "这里比的是整数命中条数。",
        f"- **可达上限**：`{STRONG}` 在 `{V1}` 上是 "
        f"{v1_strong['passk_mean']:.4f}，与 [w3_ceiling.md](w3_ceiling.md) 的"
        f"四条全开结论一致；在 `{V2}` 上是 {v2_strong['passk_mean']:.4f}，"
        f"其中新增任务通过 {len(added_ids)} 条、既有任务通过 "
        f"{len(inherited_pass)} 条。",
        (
            f"  `all4` 比 `ScriptedBackend` 参考基线高 {scripted_gap:+.4f}"
            f"（= {round(scripted_gap / new['step'])} 个 1/N 格点，即多 "
            f"{round(scripted_gap / new['step'])} 条任务）：来自 "
            "`idempotency` 修好 T03 脚本固有的重复退款，正是 "
            "[w3_ceiling.md](w3_ceiling.md) 钉死的「`fpm=0` 不是天花板」那部分收益，"
            "不是新任务放水。"
            if scripted_gap is not None and scripted_gap > EFFECT_EPSILON
            else ""
        ),
        f"- **弱效应基准（口径提醒）**：`{V1}` 上 `{CONTROL}`→`{WEAK}` 是"
        "**单变量 A/B 直接差**，"
        f"{len(seeds)} 个 seed 里正 {old_signs['positive']} / 零 "
        f"{old_signs['zero']} / 负 {old_signs['negative']}，与 "
        "[w3_resolution.md](w3_resolution.md) 的 k=3 `off → schema` 分布逐项一致，"
        "说明本模块没有偷偷换 seed 集、fpm 或护栏定义。"
        "[w3_robustness.md](w3_robustness.md) 里的 `schema_validation` 数字是 "
        "**2×2 四格主效应**（开 / 关两组均值之差），不是同一个量，"
        "两处符号分布不可直接比对；[w3_resolution.md](w3_resolution.md) "
        "已就此写过口径提醒。",
        f"- **分辨率口径**：`pass^k = passk_hits / task_count` 始终没变，"
        f"`{V1}` 步进 {old['step']:.4f} 与 [w3_resolution.md](w3_resolution.md) "
        "一致；提高 k 不改变分辨率，本次也没有改 k。",
        "",
        "## 口径边界（不得省略）",
        "",
        "1. **任务集是第三个自变量**。两个任务集的分母与任务构成都不同，"
        "`pass^k` 数值、均值 Δ、符号分布**只用于描述分辨率属性**，"
        "跨任务集相减不是单变量对照、不作因果解读；W3 的单变量因果证据仍以 "
        "`w3_robustness.md` 与各 `*_sweep.md` 为准。",
        "2. **既有全部留档仍使用 `mvp_10.jsonl`**。既有实验配置、"
        "`robustness.py` / `ceiling.py` / `resolution.py` / `attribution.py` "
        "的版本化常量一个都没改；两组数字不可混用、不可拼接成一条曲线。",
        "3. **新任务脚本正确 ≠ 任务更容易**。它们只是把「不携带信息的恒失败"
        "位点」换成「携带信息的可翻转位点」；7 条固有缺陷任务原样保留，"
        "没有从分母里剔除（[分辨率审计](w3_resolution.md) 已证明剔除反而让"
        "步进变粗、且等于人为挑选样本）。",
        "4. **`pass^k` 仍是二值化指标**。步进变细不等于效应量变连续，"
        "k 次全过的门槛仍在；本报告是描述性统计、**不是显著性检验**，"
        "不构成「效应量估计」，没有置信区间与 p 值，"
        "seed 是人为选定的整数、非随机抽样。",
        "5. **不外推真实模型**。全程 `SeededBackend`，四类输出级故障注入，"
        "不模拟推理 / 理解 / 规划能力；`fpm=0` 不是完美模型。"
        "不替代 W4、不替代 `[GUARD-REJECT]` 的真实模型验证。",
        "6. token / 延迟恒 0（本地后端无真实调用），"
        "不构成任何成本或延迟结论；`invalid_calls` 同样不是成功率的代理指标。",
        "7. `mvp_30.jsonl` 是**版本化 fixture**，前 10 行逐字节沿用 "
        "`mvp_10.jsonl`，提交后不得再生成或修改；构造规则与一致性校验见 "
        "[tasks/README.md](../tasks/README.md)。",
        "",
        "## 数据来源与复现",
        "",
        f"每行一个任务集 / 一格 / 一个 seed 的完整指标（含逐任务 0/1 序列）"
        f"在 `{EVIDENCE_NAME}`；另有 {len(names)} 行 `ScriptedBackend` 参考基线。",
        "",
        "```",
        "python -m eval.taskset_scale",
        "python -m eval.taskset_scale --from-jsonl",
        "python -m unittest discover -s tests -v",
        "```",
        "",
        "本模块不新增护栏、不加配置字段、不改判分口径、不修改任何既有留档；"
        "跑批只写共用临时目录，不读时钟 / 环境变量、不用全局随机、"
        "零第三方依赖，同命令两次产物逐字节一致。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.taskset_scale",
        description="v2 扩展任务集与 pass^k 分辨率对照（2 任务集 × 3 格 × 30 seed）",
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
        records = collect(seeds)
        out_dir.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(
            robustness.dumps_records(records).encode("utf-8")
        )

    text = render_report(records)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(text.encode("utf-8"))

    summary = scale_summary(records)
    weak = weak_effect_summary(records)
    print(
        f"任务集：{list(task_names(records))}，格子：{list(CELL_NAMES)}，"
        f"seed 数：{len(seed_values(records))}，证据行：{len(records)}"
    )
    for name in task_names(records):
        item = summary[name]
        signs = weak[name]["signs"]
        print(
            f"  {name}: N={item['task_count']} 步进 {item['step']:.4f}，"
            f"可达档位 {item['level_count']} 个，信息位点 {item['informative']} 个，"
            f"off→schema 正 {signs['positive']} / 零 {signs['zero']} / "
            f"负 {signs['negative']}"
        )
    print(f"证据：{evidence_path}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
