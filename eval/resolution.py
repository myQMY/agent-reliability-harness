"""W3 测量仪器审计：``pass^k`` 的分辨率从哪来，又吞掉了多少真实修复。

动机：W3 护栏阶段收口时，[可达上限分析](w3_ceiling.md) 指出四类注入故障
已被四条护栏完全恢复，剩余 gap 是 7 个脚本固有缺陷任务；但多种子 A/B 里
大量 seed 仍是零效应。本模块把零效应拆成**两类分别计量**：护栏在这个 seed
上压根没修好任何 trial，与修好了 trial 却被「k 次全过」门槛吞掉。实测表明
前者才是多数，后者真实存在但属少数——**两者都不能省略，否则就是对数据的
选择性解读**。同时纠正一处沿用了四份报告的口径错误：「扩大任务集或提高 k
能提高 ``pass^k`` 分辨率」。

分辨率的数学性质（见 ``eval/analyze.py`` 的 ``summarize``）：

    passk = passk_hits / task_count

``passk_hits`` 是「k 次全过」的任务个数，取值只能是 0..N 的整数，所以
``pass^k`` 只能落在 ``{0, 1/N, 2/N, ..., 1}`` 这 N+1 个格点上。**N 是任务数，
与 k 无关**。提高 k 不改变任何一个格点，只会同时改变判定难度、可翻转空间
与掩蔽率，让读数漂移；它不是分辨率旋钮。

三个必须说清楚的边界：
1. **本模块不引入新指标、不改判分口径、不换尺子**。k=3 的 ``pass^k`` 仍是
   所有既有 A/B / 稳健性 / 可达上限结论的主指标；k=5 / k=8 只是诊断扫描。
2. **历史留档不回溯修改**。`w3_robustness.md` / `w3_*_sweep.md` /
   `w3_ceiling.md` 里「提高 k」那句话按实验记录纪律保留原文，本报告显式
   标注它已被纠正；活文档（README / tech-stack）直接改。
3. 报告是描述性统计，**不是显著性检验**；全部数字来自确定性模拟器，
   不构成真实模型能力 / 成本 / 延迟结论。

纪律：不读时钟、不读环境变量、不用全局随机源、零第三方依赖；跑批只写共用
临时目录，仓库里只留下证据 JSONL 与 Markdown 报告；``--from-jsonl`` 只读
证据重渲染，同命令两次产物逐字节一致。

用法：
    python -m eval.resolution                           # 全量 3 档 k × 30 seed × 2 格
    python -m eval.resolution --from-jsonl               # 只读证据重渲染
    python -m eval.resolution --seeds 11,1,2 --out-dir <dir>   # 小规模自检
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any, Sequence

from eval import analyze, robustness, runner
from eval.config import resolve_input
from runtime import guard
from runtime.model import BackendConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "experiments"
EVIDENCE_NAME = "w3_resolution.jsonl"
REPORT_NAME = "w3_resolution.md"

# k=3 是既有全部实验的主指标；5 / 8 只用来验证「提高 k 不改变分辨率」。
DEFAULT_K_VALUES = (3, 5, 8)
CONTROL = "off"
# 单开一条护栏，代表「效应弱、在 30-seed 稳健性检验里 25/30 零效应」的那一类；
# 四条全开代表可达上限。两条对照并排，才能区分「护栏没干活」与「尺子量不出」。
SINGLE = "schema"
TREATMENT = "all4"
CELLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CONTROL, ()),
    (SINGLE, (guard.GUARD_SCHEMA_VALIDATION,)),
    (
        TREATMENT,
        (
            guard.GUARD_SCHEMA_VALIDATION,
            guard.GUARD_IDEMPOTENCY,
            guard.GUARD_TOOL_RETRY,
            guard.GUARD_JSON_RECOVERY,
        ),
    ),
)
CELL_NAMES = tuple(name for name, _ in CELLS)
TREATMENTS = (SINGLE, TREATMENT)
EFFECT_EPSILON = 1e-9

Record = dict[str, Any]


def resolution(task_count: int) -> float:
    """``pass^k`` 的最小可分辨步进 = 1 / 任务数，与 k 无关。

    来自 ``analyze.summarize``：``passk = passk_hits / task_count``，而
    ``passk_hits`` 只能是 0..N 的整数。k 决定「hits 有多难拿到」，不决定
    「相邻两个 hits 相差多少」。
    """
    if task_count <= 0:
        raise ValueError("任务数必须为正")
    return 1.0 / task_count


def passk_grid(task_count: int) -> tuple[float, ...]:
    """``pass^k`` 全部可能取值；调用 :func:`resolution` 保证口径单一。"""
    step = resolution(task_count)
    return tuple(hits * step for hits in range(task_count + 1))


def is_on_grid(value: float, task_count: int) -> bool:
    return any(
        abs(value - point) < EFFECT_EPSILON for point in passk_grid(task_count)
    )


def count_passes(sequences: Sequence[str]) -> int:
    """从逐任务 0/1 序列统计通过的 trial 总数；序列长度不一致直接报错。"""
    if not sequences:
        return 0
    length = len(sequences[0])
    if any(len(seq) != length for seq in sequences):
        raise ValueError("同一格内各任务的序列长度必须一致")
    if any(char not in "01" for seq in sequences for char in seq):
        raise ValueError("序列只能由 0 / 1 组成")
    return sum(seq.count("1") for seq in sequences)


def k_values(records: Sequence[Record]) -> tuple[int, ...]:
    """从证据推导 k 档位，使 --from-jsonl / 自定义子集都能正确渲染。"""
    return tuple(sorted({int(record["k"]) for record in records}))


def seed_values(records: Sequence[Record]) -> tuple[int, ...]:
    return tuple(sorted({int(record["seed"]) for record in records}))


def _paired(
    records: Sequence[Record], k: int, treatment: str = TREATMENT
) -> list[dict[str, Any]]:
    """按 seed 取出 OFF 与指定实验格，并校验逐任务序列可逐位比较。

    三格必须同时齐全才允许计算：``schema`` 格缺失会让「弱效应护栏」的
    漏斗悄悄消失，报告就有选择性呈现的风险。
    """
    if treatment not in TREATMENTS:
        raise ValueError(f"未知实验格：{treatment}；可用：{list(TREATMENTS)}")
    by_seed: dict[int, dict[str, Record]] = {}
    for record in records:
        if int(record["k"]) != k:
            continue
        by_seed.setdefault(int(record["seed"]), {})[str(record["cell"])] = record
    result: list[dict[str, Any]] = []
    for seed in sorted(by_seed):
        cells = by_seed[seed]
        missing = [name for name in CELL_NAMES if name not in cells]
        if missing:
            raise ValueError(
                f"k={k} seed={seed} 缺少格子 {missing}，拒绝静默补零"
            )
        off = {
            item["task_id"]: str(item["sequence"])
            for item in cells[CONTROL]["per_task"]
        }
        on = {
            item["task_id"]: str(item["sequence"])
            for item in cells[treatment]["per_task"]
        }
        if set(off) != set(on):
            raise ValueError(f"k={k} seed={seed} OFF 与实验格的任务集合不一致")
        task_count = int(cells[CONTROL]["task_count"])
        if len(off) != task_count:
            raise ValueError(f"k={k} seed={seed} 任务数与 task_count 不符")
        if any(len(seq) != k for seq in (*off.values(), *on.values())):
            raise ValueError(f"k={k} seed={seed} 序列长度与 k 不符")
        result.append(
            {
                "seed": seed,
                "treatment": treatment,
                "off": off,
                "on": on,
                "off_passk": float(cells[CONTROL]["passk"]),
                "on_passk": float(cells[treatment]["passk"]),
                "task_count": task_count,
            }
        )
    return result


def task_flags(records: Sequence[Record], k: int) -> dict[str, dict[str, bool]]:
    """从证据推导每个任务在全部 seed × 三格下的可翻转性，不硬编码任务 ID。

    ``always_fail`` 即「固有缺陷」：任何护栏组合、任何 seed 下都 k 次全不过，
    对 ``pass^k`` 贡献恒定，不携带信息。``variable`` 才是真正能产生效应阶梯
    的任务。
    """
    paired = _paired(records, k)
    rows = [record for record in records if int(record["k"]) == k]
    if not rows or not paired:
        raise ValueError(f"证据里没有 k={k} 的完整三格配对")
    flags: dict[str, list[bool]] = {}
    for record in rows:
        for item in record["per_task"]:
            seq = str(item["sequence"])
            if len(seq) != k:
                raise ValueError(f"k={k} 的序列长度与 k 不符")
            flags.setdefault(str(item["task_id"]), []).append(
                seq.count("1") == k
            )
    return {
        task_id: {
            "always_pass": all(values),
            "always_fail": not any(values),
            "variable": any(values) and not all(values),
        }
        for task_id, values in sorted(flags.items())
    }


def effective_range(records: Sequence[Record], k: int) -> dict[str, Any]:
    """这把尺子实际可用的档位数与动态范围。

    N 个任务里只有 ``variable`` 个可能翻转，所以 ``pass^k`` 实际只能取
    ``floor .. floor + variable`` 共 ``variable + 1`` 档——比名义上的 N+1 档
    少得多。这是 W3 大量零效应的结构性原因。
    """
    flags = task_flags(records, k)
    task_count = len(flags)
    fixed_pass = sum(item["always_pass"] for item in flags.values())
    fixed_fail = sum(item["always_fail"] for item in flags.values())
    variable = sum(item["variable"] for item in flags.values())
    step = resolution(task_count)
    return {
        "k": k,
        "task_count": task_count,
        "step": step,
        "grid_size": task_count + 1,
        "fixed_pass": fixed_pass,
        "fixed_fail": fixed_fail,
        "variable": variable,
        "levels": variable + 1,
        "floor": fixed_pass * step,
        "ceiling": (fixed_pass + variable) * step,
    }


def funnel(
    records: Sequence[Record], k: int, treatment: str = TREATMENT
) -> list[dict[str, Any]]:
    """逐 seed 的「trial 级修复 → 任务级翻转」漏斗。

    ``repaired_trials``：OFF 失败、实验格通过的单次 trial 数（护栏确实修好了）。
    ``flipped_tasks``：从「不是 k 次全过」变成「k 次全过」的任务数，即
    ``Δpass^k × N``。``masked`` 表示修好了 trial 却一个任务都没翻转——
    **这些 seed 在 A/B 表里显示为零效应，但护栏并非没起作用**。
    """
    result: list[dict[str, Any]] = []
    for pair in _paired(records, k, treatment):
        repaired = regressed = flipped = 0
        for task_id in sorted(pair["off"]):
            off_seq, on_seq = pair["off"][task_id], pair["on"][task_id]
            repaired += sum(a == "0" and b == "1" for a, b in zip(off_seq, on_seq))
            regressed += sum(a == "1" and b == "0" for a, b in zip(off_seq, on_seq))
            off_all = off_seq.count("1") == k
            on_all = on_seq.count("1") == k
            flipped += int(not off_all and on_all)
        delta = pair["on_passk"] - pair["off_passk"]
        if abs(delta - flipped / pair["task_count"]) > EFFECT_EPSILON:
            raise ValueError(
                f"k={k} seed={pair['seed']} 任务级翻转数与 Δpass^k 不一致"
            )
        result.append(
            {
                "seed": pair["seed"],
                "treatment": treatment,
                "repaired_trials": repaired,
                "regressed_trials": regressed,
                "flipped_tasks": flipped,
                "masked": repaired > 0 and flipped == 0,
                "delta": delta,
            }
        )
    return result


def funnel_summary(
    records: Sequence[Record], k: int, treatment: str
) -> dict[str, Any]:
    """一档 k、一条对照的漏斗汇总；描述性统计，不是显著性检验。"""
    rows = funnel(records, k, treatment)
    if not rows:
        raise ValueError(f"k={k} / {treatment} 没有逐 seed 漏斗证据")
    return {
        "k": k,
        "treatment": treatment,
        "seed_count": len(rows),
        "repaired_trials": sum(row["repaired_trials"] for row in rows),
        "regressed_trials": sum(row["regressed_trials"] for row in rows),
        "flipped_tasks": sum(row["flipped_tasks"] for row in rows),
        "masked_seeds": [row["seed"] for row in rows if row["masked"]],
        "zero_effect_seeds": [
            row["seed"] for row in rows
            if abs(row["delta"]) < EFFECT_EPSILON
        ],
        "delta_mean": sum(row["delta"] for row in rows) / len(rows),
    }


def k_summary(records: Sequence[Record], k: int) -> dict[str, Any]:
    """一档 k 的汇总；所有取值都从证据读，不做任何推算填充。"""
    rows = [record for record in records if int(record["k"]) == k]
    if not rows:
        raise ValueError(f"证据里没有 k={k} 的记录")
    task_count = int(rows[0]["task_count"])
    step = resolution(task_count)
    paired = _paired(records, k)
    rows_funnel = funnel(records, k)
    off_values = sorted(
        {round(float(record["passk"]), 10) for record in rows
         if record["cell"] == CONTROL}
    )
    return {
        "k": k,
        "task_count": task_count,
        "step": step,
        "seed_count": len(paired),
        "trial_count": int(rows[0]["trial_count"]),
        "pass1_mean": sum(
            float(record["pass1"]) for record in rows
            if record["cell"] == CONTROL
        ) / sum(1 for record in rows if record["cell"] == CONTROL),
        "passk_mean_off": sum(pair["off_passk"] for pair in paired) / len(paired),
        "passk_mean_all4": sum(pair["on_passk"] for pair in paired) / len(paired),
        "off_values": off_values,
        "off_on_grid": all(is_on_grid(value, task_count) for value in off_values),
        "repaired_trials": sum(row["repaired_trials"] for row in rows_funnel),
        "regressed_trials": sum(row["regressed_trials"] for row in rows_funnel),
        "flipped_tasks": sum(row["flipped_tasks"] for row in rows_funnel),
        "masked_seeds": [row["seed"] for row in rows_funnel if row["masked"]],
        "range": effective_range(records, k),
    }


def collect(
    seeds: Sequence[int],
    *,
    tasks_path: Path,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
    fault_per_mille: int = robustness.DEFAULT_FAULT_PER_MILLE,
) -> list[Record]:
    """多档 k × 每个 seed × 三格（off / schema / all4）重跑，返回紧凑结构化证据。

    只取 ``runner.run`` 返回的 rows 做汇总，不读任何跑批落盘文件；各格反复
    覆盖同名 trace 也不影响结果。证据里带逐任务 0/1 序列，使
    ``--from-jsonl`` 能在完全离线的情况下重算整条漏斗。
    """
    if any(k <= 0 for k in k_values):
        raise ValueError("k 必须为正整数")
    records: list[Record] = []
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for k in k_values:
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
                            "k": k,
                            "seed": seed,
                            "cell": cell,
                            "guards": list(guards),
                            "pass1": summary["pass1"],
                            "passk": summary["passk"],
                            "passk_hits": summary["passk_hits"],
                            "task_count": summary["task_count"],
                            "trial_count": summary["trial_count"],
                            "per_task": [
                                {
                                    "task_id": item["task_id"],
                                    "sequence": item["sequence"],
                                    "all_pass": item["all_pass"],
                                }
                                for item in summary["per_task"]
                            ],
                        }
                    )
    return records


def _signed(value: float, precision: int = 2) -> str:
    return f"{0.0 if abs(value) < EFFECT_EPSILON else value:+.{precision}f}"


def render_report(records: Sequence[Record]) -> str:
    """渲染 Markdown；只依赖证据与模块常量，不含路径、时钟或机器信息。"""
    ks = k_values(records)
    seeds = seed_values(records)
    summaries = {k: k_summary(records, k) for k in ks}
    primary = summaries[ks[0]]
    task_count = primary["task_count"]
    step = resolution(task_count)
    full_sweep = (
        tuple(seeds) == tuple(sorted(robustness.DEFAULT_SEEDS))
        and tuple(ks) == DEFAULT_K_VALUES
    )
    seed_note = (
        "（与 `w3_robustness.md` **完全相同**的一组整数）"
        if full_sweep
        else "（本次为自定义子集，完整扫描请不带 `--seeds` 重跑）"
    )
    grid = passk_grid(task_count)
    ranges = {k: effective_range(records, k) for k in ks}
    funnels = {
        (k, treatment): funnel(records, k, treatment)
        for k in ks
        for treatment in TREATMENTS
    }
    fsum = {
        (k, treatment): funnel_summary(records, k, treatment)
        for k in ks
        for treatment in TREATMENTS
    }
    weak = fsum[(ks[0], SINGLE)]
    strong = fsum[(ks[0], TREATMENT)]
    weak_last = fsum[(ks[-1], SINGLE)]
    masked_all = [
        seed for seed in seeds
        if all(
            any(row["seed"] == seed and row["masked"]
                for row in funnels[(k, SINGLE)])
            for k in ks
        )
    ]
    negative_seeds = [
        (k, treatment, row["seed"])
        for k in ks
        for treatment in TREATMENTS
        for row in funnels[(k, treatment)]
        if row["delta"] < -EFFECT_EPSILON
    ]
    all4_means = ", ".join(
        f"k={k} {summaries[k]['passk_mean_all4']:.4f}" for k in ks
    )

    lines = [
        "# W3 测量仪器审计：`pass^k` 的分辨率与效应掩蔽",
        "",
        f"> 由 `python -m eval.resolution` 依据 `{EVIDENCE_NAME}` 如实生成；"
        "不含生成时间，同命令两次产物逐字节一致。",
        "> 本模块**不引入新指标、不改判分口径、不加配置字段、不修改任何既有留档**："
        "既有 `run_id` / 归档报告 / 矩阵 / 稳健性 / A-B 扫描 / 可达上限分析与"
        "冻结基线一字节未动。k=5 / k=8 只是诊断扫描，**主指标仍是 k=3 的 `pass^k`**。",
        "",
        "## 结论先行",
        "",
        f"- **分辨率只由任务数决定，与 k 无关**。`analyze.summarize` 里 "
        f"`passk = passk_hits / task_count`，`passk_hits` 只能是 0..{task_count} 的整数，"
        f"因此 `pass^k` 只能落在 {len(grid)} 个格点上，最小步进恒为 {step:.2f}。"
        f"{len(ks)} 档 k（{', '.join(str(k) for k in ks)}）的实测取值全部落在这组格点上"
        f"（见下表），**没有任何一档 k 变出更细的刻度**。",
        f"- **提高 k 不改变分辨率，只让读数漂移**。`pass¹` 与 k 无关（只取 trial=1）；"
        f"OFF 平均 `pass^k` 从 k={ks[0]} 的 {primary['passk_mean_off']:.4f} "
        f"{'降到' if primary['passk_mean_off'] - summaries[ks[-1]]['passk_mean_off'] > EFFECT_EPSILON else '变为'} "
        f"k={ks[-1]} 的 {summaries[ks[-1]]['passk_mean_off']:.4f}（判定更严苛），"
        f"但 `schema` 单开的任务级翻转从 {weak['flipped_tasks']} 增到 "
        f"{weak_last['flipped_tasks']}、零效应 seed 从 "
        f"{len(weak['zero_effect_seeds'])} 降到 {len(weak_last['zero_effect_seeds'])} —— "
        "基线降低反而释放了更多可翻转任务；"
        f"同时被掩蔽的 seed 从 {len(weak['masked_seeds'])} 增到 "
        f"{len(weak_last['masked_seeds'])}。**k 同时改变难度、可翻转空间与掩蔽率，"
        "却一档刻度都没变细**；拿它当「提高分辨率」的手段是错的。",
        f"- **这把尺子实际只有 {primary['range']['levels']} 档可用**。"
        f"{task_count} 个任务里 {primary['range']['fixed_fail']} 个在任何 seed、"
        "任何护栏组合下都 k 次全不过（脚本固有缺陷），贡献恒定不携带信息；"
        f"真正可能翻转的只有 {primary['range']['variable']} 个任务，"
        f"所以 `pass^k` 的动态范围实际是 {primary['range']['floor']:.2f} ~ "
        f"{primary['range']['ceiling']:.2f}。名义上的 {len(grid)} 个格点里，"
        "大部分根本不可达。",
        f"- **效应掩蔽真实存在，但不是零效应的主因**。k={ks[0]} 的 `off → schema` "
        f"有 {len(weak['zero_effect_seeds'])} 个零效应 seed，其中只有 "
        f"{len(weak['masked_seeds'])} 个是「修好了 trial 却被 k 次全过门槛吞掉」"
        f"（该对照共修好 {weak['repaired_trials']} 个 trial、翻转 "
        f"{weak['flipped_tasks']} 个任务）；其余 "
        f"{len(weak['zero_effect_seeds']) - len(weak['masked_seeds'])} 个是护栏在这个 "
        "seed 上根本没修好任何 trial —— 注入故障没落到它能修的位置。"
        "**不能把 W3 的零效应一律解释成「尺子太钝」**，那和把它们一律读成"
        "「护栏没用」同样是过度解读。",
        f"- **组合效应够大时，掩蔽与回归一起消失**：同档 k 下 `off → all4` 修好 "
        f"{strong['repaired_trials']} 个 trial、翻转 {strong['flipped_tasks']} 个任务、"
        f"掩蔽 {len(strong['masked_seeds'])} 个 seed、零效应 "
        f"{len(strong['zero_effect_seeds'])} 个；{len(ks)} 档 k 的 all4 平均 `pass^k` 为 "
        f"`{all4_means}`，都等于可达上限。这是 [可达上限分析](w3_ceiling.md) "
        "「单变量 A/B 会低估互补护栏」的定量机制。",
        f"- **四条全开零回归；单开有 trial 级变差，但无任务级负效应**。"
        f"`off → all4` 在全部 {len(ks)} 档 k 上的 trial 级 1→0 变差为 "
        f"{sum(fsum[(k, TREATMENT)]['regressed_trials'] for k in ks)} 次；"
        f"`off → schema` 合计 {sum(fsum[(k, SINGLE)]['regressed_trials'] for k in ks)} 次，"
        f"而逐 seed Δ`pass^k` 的负值共 {len(negative_seeds)} 个 —— 这些变差都落在"
        "「本来就不是 k 次全过」的任务里。这是「trial 级中间指标不是任务级结果"
        "代理」的又一个方向：下降、持平、上升，以及这里的 1→0 变差，"
        "都不能替代 `pass^k`。",
        "",
        "## 方法与输入",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        "| 后端 | `SeededBackend`（确定性伪随机模拟器） |",
        f"| 任务集 | `{robustness.DEFAULT_TASKS}`（{task_count} 条） |",
        f"| k 档位 | {', '.join(f'`{k}`' for k in ks)}（k={robustness.DEFAULT_K} 是既有全部实验的主指标） |",
        f"| 故障注入率 | {robustness.DEFAULT_FAULT_PER_MILLE}/1000 |",
        f"| seed 数 | {len(seeds)}{seed_note} |",
        "| 三格 | `off`（护栏全关）/ `schema`（只开 `schema_validation`）/ "
        "`all4`（四条护栏全开） |",
        "| 两条对照 | `off → schema`（弱效应）/ `off → all4`（可达上限）；"
        "**不是新的 A/B 结论**，只是同一批数据的漏斗重算 |",
        "| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1 |",
        "| 效应漏斗 | `repaired_trials` = OFF 失败且实验格通过的单次 trial 数；"
        "`flipped_tasks` = 「不是 k 次全过」→「k 次全过」的任务数（= Δ`pass^k` × N） |",
        "| 掩蔽 | `repaired_trials > 0` 且 `flipped_tasks = 0` 的 seed |",
        "",
        "选 `schema_validation` 单开作弱效应代表，是因为 [多种子稳健性检验]"
        "(w3_robustness.md) 里它的**四格主效应**在 30 个 seed 上只有 5 个为正、"
        "25 个为零；`all4` 则给出「效应足够大时」的另一极。**口径提醒**：那份报告"
        "算的是 2×2 主效应（开 / 关两组均值之差），本报告的 `off → schema` 是"
        "单变量 Δ，两者数值不必相同，不能逐项对齐。两条对照共用同一批 seed、"
        "同一任务集、同一故障注入率，唯一自变量仍是 `[guards].enabled`。",
        "",
        "> **逐字节可复现**：seed 集、任务集、k 档位、故障率、三格定义全部硬编码"
        "并版本化；RNG 由 `(seed, task_id, trial, cursor)` 经 SHA256 派生。模块不读"
        "时钟、不读环境变量、不用全局随机源；跑批只写共用临时目录，仓库里只留下 "
        f"{EVIDENCE_NAME} 与本报告。`--from-jsonl` 可从证据只读重渲染，不重跑评测。",
        "",
        "## 分辨率的数学性质（先定口径，再看数据）",
        "",
        "设任务数为 N、某任务 k 次全过记 1 分，则 `pass^k = (k 次全过的任务数) / N`。"
        "分子是 0..N 的整数，与 k 无关；k 只影响「分子能拿到多少」。"
        "下表左半是这一定义直接推出的格点，右半是本次实测：",
        "",
        "| k | N | 最小步进 1/N | 名义格点数 | 实测 OFF 取值 | 是否全部落在格点上 | "
        "平均 `pass¹` | 平均 `pass^k`（OFF / all4） |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for k in ks:
        item = summaries[k]
        values = ", ".join(f"{value:.2f}" for value in item["off_values"])
        lines.append(
            f"| {k} | {item['task_count']} | {item['step']:.2f} | "
            f"{len(grid)} | {values} | {'是' if item['off_on_grid'] else '否'} | "
            f"{item['pass1_mean']:.4f} | {item['passk_mean_off']:.4f} / "
            f"{item['passk_mean_all4']:.4f} |"
        )
    lines += [
        "",
        f"`pass¹` 在这 {len(ks)} 档 k 下相同，因为 `summarize` 只取每个任务的 trial=1；"
        "同一 seed 的 trial=1 随机流不随 k 变化。这恰好说明 **k 只增加重复次数，"
        "不增加独立任务数** —— 而分辨率的分母是后者。",
        "",
        "按 `1/N` 直接推算的口径值（**下表是公式推算，不是实测结果**，用于说明"
        "唯一能提高分辨率的途径）：",
        "",
        "| 独立任务数 N | 最小步进 1/N |",
        "| --- | --- |",
    ]
    for n in (10, 20, 30, 50, 100):
        lines.append(f"| {n} | {resolution(n):.4f} |")
    lines += [
        "",
        "**只有增加独立任务数才能变细**。「把固有缺陷分层标注」如果理解为「把死任务"
        "从分母里剔除」，N 反而变小、步进变粗；它的价值是让报告说清楚哪些失败"
        "不可恢复，属于归因分层，**不是提高分辨率的手段**，也不能替换主指标。"
        "要真正变细，得新增一批结构相同、可被运行时恢复的独立任务，并作为新的"
        "版本化任务集另开一组实验（不能悄悄改既有任务集，否则既有留档失去可比性）。",
        "",
        "## 有效动态范围：为什么 +0.10 已经是很大的效应",
        "",
        "| k | 恒通过 | 恒失败（固有缺陷） | 可翻转 | 实际可用档位数 | 实际动态范围 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for k in ks:
        item = ranges[k]
        lines.append(
            f"| {k} | {item['fixed_pass']} | {item['fixed_fail']} | "
            f"{item['variable']} | {item['levels']} | "
            f"{item['floor']:.2f} ~ {item['ceiling']:.2f} |"
        )
    lines += [
        "",
        "「恒失败」清单由证据自动推导（在所有 seed × 三格里都非 k 次全过），"
        "**不硬编码任务 ID**；改任务集后本表会自动更新。它们与 [可达上限分析]"
        "(w3_ceiling.md) 的结论一致：剩余 gap 是任务集设计里的固有缺陷，"
        "不该由护栏越界修好。",
        "",
        "## 效应漏斗：trial 级修复到任务级翻转的漏损",
        "",
        "| k | 对照 | seed 数 | 修好的 trial | 任务级翻转 | 翻转 / 修复 | "
        "平均 Δ`pass^k` | 被掩蔽的 seed | 零效应 seed | 变差 trial |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for k in ks:
        for treatment in TREATMENTS:
            item = fsum[(k, treatment)]
            ratio = (
                item["flipped_tasks"] / item["repaired_trials"]
                if item["repaired_trials"]
                else 0.0
            )
            lines.append(
                f"| {k} | `off → {treatment}` | {item['seed_count']} | "
                f"{item['repaired_trials']} | {item['flipped_tasks']} | "
                f"{ratio:.4f} | {_signed(item['delta_mean'], 4)} | "
                f"{len(item['masked_seeds'])} | "
                f"{len(item['zero_effect_seeds'])} | "
                f"{item['regressed_trials']} |"
            )
    header = [
        f"k={k} {treatment}"
        for k in ks
        for treatment in TREATMENTS
    ]
    lines += [
        "",
        "逐 seed 明细（每格「修复 trial / 翻转任务」，`*` 表示该 seed 被掩蔽）：",
        "",
        "| seed | " + " | ".join(header) + " |",
        "| --- | " + " | ".join("---" for _ in header) + " |",
    ]
    indexed = {
        (k, treatment): {row["seed"]: row for row in funnels[(k, treatment)]}
        for k in ks
        for treatment in TREATMENTS
    }
    for seed in seeds:
        cells = []
        for k in ks:
            for treatment in TREATMENTS:
                row = indexed[(k, treatment)].get(seed)
                if row is None:
                    cells.append("—")
                else:
                    mark = "*" if row["masked"] else ""
                    cells.append(
                        f"{row['repaired_trials']} / {row['flipped_tasks']}{mark}"
                    )
        lines.append(f"| {seed} | " + " | ".join(cells) + " |")
    lines += [
        "",
        f"在全部 {len(ks)} 档 k 上、单开 `schema_validation` 都被掩蔽的 seed："
        f"`{', '.join(str(s) for s in masked_all) or '无'}`。",
        "",
        "读法：一个任务在 OFF 里有 1 次失败，护栏修好了它，但如果这个任务还有"
        "别的失败（或另外几次 trial 有别的注入故障），`pass^k` 仍然是 0。"
        "护栏确实改了 trace 与终态，分数却不动。**这不是护栏无效，是二值化"
        "的「k 次全过」把连续改善压成了稀疏阶跃**。",
        "",
        f"两类零效应必须分开读：k={ks[0]} 的 {len(weak['zero_effect_seeds'])} 个 "
        f"`off → schema` 零效应 seed 里，"
        f"{len(weak['zero_effect_seeds']) - len(weak['masked_seeds'])} 个是护栏没修好"
        f"任何 trial（故障注入分布所致），只有 {len(weak['masked_seeds'])} 个是修好了"
        "却被门槛吞掉。掩蔽随 k 变严而放大（见上表），但它始终不是主因。所以既"
        "**不能把 W3 的零效应一律读成「护栏没用」**，也**不能反过来一律解释成"
        "「只是尺子太钝」**——后者同样是选择性解读。四条全开之所以逐 seed 都达到"
        "上限，是因为它一次修好四类故障、效应足够大，掩蔽与 trial 级回归同时归零；"
        "这与「单变量 A/B 会低估互补护栏」是同一件事的两面。",
        "",
        "## 纠正一处沿用多份报告的口径错误",
        "",
        "旧文案（出现在 `w3_robustness.md`、`w3_tool_retry_sweep.md`、"
        "`w3_json_recovery_sweep.md`、`w3_ceiling.md` 及对应渲染模块）：",
        "",
        "> 若要把 `pass^k` 当成主要指标，先扩大任务集或提高 k，否则 0.10 的"
        "分辨率会继续淹没真实差异。",
        "",
        "其中「扩大任务集」正确，「**提高 k**」错误。本报告的实测显示："
        f"k 从 {ks[0]} 提到 {ks[-1]}，最小步进始终 {step:.2f}、格点集合始终是同一组 "
        f"{len(grid)} 个值；变的是读数 —— OFF 平均 `pass^k` 从 "
        f"{primary['passk_mean_off']:.4f} 漂到 "
        f"{summaries[ks[-1]]['passk_mean_off']:.4f}、单开 `schema` 被掩蔽的 seed 从 "
        f"{len(weak['masked_seeds'])} 个变成 {len(weak_last['masked_seeds'])} 个。"
        "k 是**难度旋钮**，不是分辨率旋钮；想让刻度变细，只能增加独立任务数。",
        "",
        "**历史留档按实验记录纪律保留原文，不回溯改字**：归档报告是当时跑批的"
        "证据，事后静默改写会破坏可追溯性。活文档 `README.md` 与 "
        "`docs/tech-stack.md` 已直接改成正确表述，并由守门测试钉死；"
        "若需要引用旧报告的结论，请以本报告这节为准。",
        "",
        "## 口径与限制",
        "",
        "- **不是显著性检验**：没有置信区间、没有 p 值、不做多重比较校正；"
        "只是把同一套确定性跑批在多档 k / 多个 seed 上重做一遍。",
        "- **seed 是人为选定的一组整数，不是随机抽样**：「N 个里 X 个被掩蔽」"
        "不能当概率估计，更不能外推到真实模型、其他任务集或故障注入率。",
        "- **k 档位变化是诊断，不是新的主指标**：既有全部 A/B / 稳健性 / "
        "可达上限结论仍以 k=3 为准，本表不替换、不重算、不修正任何一个。",
        "- **两类零效应分开计量**：`masked` 只统计「修好了 trial 却零翻转」的 "
        "seed；护栏未修好任何 trial 的零效应不计入掩蔽。二者在 A/B 表里都显示为 "
        "Δ`pass^k` = 0，但机制不同，合并统计或只报其一都会误导。",
        "- **trial 级 1→0 变差不是任务级负效应**：它只表示单次执行结果翻转；"
        f"本审计逐 seed Δ`pass^k` 的负值共 {len(negative_seeds)} 个，另行列示，"
        "不与 trial 级变差混用。",
        f"- **全部数字来自确定性模拟器**：`SeededBackend` 没有语言理解能力，"
        "注入的是人为噪声；`tokens` / `latency_ms` 恒为 0，**不构成任何真实模型"
        "能力、成本或延迟结论**。",
        "- **护栏拒绝协议只在模拟器里验证过**：`[GUARD-REJECT]` 靠 "
        "`_pending_correct` 触发重发干净动作；真实模型能否读懂并改对，"
        "必须接 `OpenAIBackend` 后另开实验。",
        "- 效应只做 `|v| < 1e-9 → 0` 的浮点噪声归零，不做结果修饰；"
        "翻转数与 Δ`pass^k` 在每档 k 上逐 seed 校验一致，不一致直接报错。",
        "",
        "## 下一步",
        "",
        "- 真正提高分辨率要新增**独立任务**（新版本化任务集，另开实验组），"
        "并把固有缺陷作为归因分层单独报告，而不是改 k 或悄悄剔除死任务。",
        "- W4 接真实模型后，`pass^k` 才有真实的能力波动与成本 / 延迟观测；"
        "同时验证 `[GUARD-REJECT]` 能否被读懂。本次审计的结论只在"
        "确定性模拟器与当前任务集上成立。",
        "- 前端 demo 仍后置。",
        "",
        "## 数据来源与复现",
        "",
        f"每行一档 k、一个 seed、一格的完整指标（k / seed / cell / guards / "
        f"pass¹ / pass^k / passk_hits / task_count / trial_count / 逐任务 0/1 "
        f"序列）在 `{EVIDENCE_NAME}`；既有单变量 A/B 留档在 "
        "`experiments/runs/<run_id>/`。",
        "",
        "```",
        "python -m eval.resolution",
        "python -m eval.resolution --from-jsonl",
        "python -m unittest discover -s tests -v",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.resolution",
        description="W3 测量仪器审计：pass^k 分辨率、有效动态范围与效应漏斗",
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
        seeds = robustness.parse_seeds(args.seeds)
        records = collect(
            seeds,
            tasks_path=resolve_input(robustness.DEFAULT_TASKS),
            k_values=DEFAULT_K_VALUES,
            fault_per_mille=robustness.DEFAULT_FAULT_PER_MILLE,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(
            robustness.dumps_records(records).encode("utf-8")
        )

    text = render_report(records)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(text.encode("utf-8"))

    print(f"seed 数：{len(seed_values(records))}，k 档位：{list(k_values(records))}，"
          f"证据行：{len(records)}")
    for k in k_values(records):
        item = k_summary(records, k)
        print(
            f"  k={k}: 步进 {item['step']:.2f}，OFF pass^k {item['passk_mean_off']:.4f}"
            f" → all4 {item['passk_mean_all4']:.4f}"
        )
        for treatment in TREATMENTS:
            summary = funnel_summary(records, k, treatment)
            print(
                f"    off→{treatment}: 修复 trial {summary['repaired_trials']} / "
                f"翻转任务 {summary['flipped_tasks']}，"
                f"掩蔽 {len(summary['masked_seeds'])} 个 seed，"
                f"零效应 {len(summary['zero_effect_seeds'])} 个"
            )
    print(f"证据：{evidence_path}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
