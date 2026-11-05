"""W3 可达上限分析：四条护栏是否已覆盖全部「可恢复」故障。

动机：W3 前三轮分别验证了单变量 A/B、2×2 全因子、30-seed 稳健性与第三/四条
护栏，但一直没回答一个更根本的问题——**这台测量仪器上，护栏最多能拿到多少
分，现在还差多少，差的那些该不该由护栏修**。本模块用九格诊断回答它：

- 把故障注入率降到 0（``fpm=0``）得到「没有任何注入故障」的参照；
- 把四条护栏全开与「无故障 + 全开」逐 seed 对照，判断注入故障是否已被
  完全恢复；
- 从「无故障 + 全开」自动推导**不可恢复的任务**（脚本固有缺陷），不硬编码
  任务 ID，改任务集后报告会自动更新。

两个必须钉死的口径陷阱：
1. ``fpm=0`` **不是天花板**。它只关掉了注入故障，任务脚本里固有的重复写
   仍在；``idempotency`` 的去重机制天然能修它，所以护栏全开可以**超过**
   「无故障 + 无护栏」。拿 ``fpm=0`` 当分母算「达成率」会得到 >100%。
2. 剩余的 gap **不是护栏不够强**，而是任务集设计里的固有缺陷（坏 JSON、
   缺参数、错工具、无 final、超退、少说必要告知）。让护栏去修它们，等于
   让评测替模型把任务做对，是作弊而不是可靠性提升。

纪律：**零侵入**——不新增护栏、不改判分口径、不加配置字段、不修改任何既有
留档。``fpm=0`` 的两格是**诊断参照，不是对照组**，不与 OFF 组做因果对比；
报告是描述性统计，不是显著性检验。不读时钟、不读环境变量、不用全局随机源，
跑批只写共用临时目录；``--from-jsonl`` 只读证据重渲染，同命令两次逐字节一致。

用法：
    python -m eval.ceiling                          # 全量九格 × 30 seed
    python -m eval.ceiling --from-jsonl             # 只读证据重渲染
    python -m eval.ceiling --seeds 11,1,2 --out-dir <dir>   # 小规模自检
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
EVIDENCE_NAME = "w3_ceiling.jsonl"
REPORT_NAME = "w3_ceiling.md"
EFFECT_EPSILON = 1e-9

S = guard.GUARD_SCHEMA_VALIDATION
I = guard.GUARD_IDEMPOTENCY
R = guard.GUARD_TOOL_RETRY
J = guard.GUARD_JSON_RECOVERY
ALL3 = (S, I, R)
ALL4 = (S, I, R, J)
FAULT = robustness.DEFAULT_FAULT_PER_MILLE

# (格子名, guards, fault_per_mille, 用途)。前七格是同一测量输入下只差护栏开关
# 的 W3 实验；后两格把故障注入率改成 0，仅作诊断参照，不参与因果对照。
CELLS: tuple[tuple[str, tuple[str, ...], int, str], ...] = (
    ("off", (), FAULT, "W3 对照组：护栏全关"),
    ("schema", (S,), FAULT, "单开 `schema_validation`"),
    ("idempotency", (I,), FAULT, "单开 `idempotency`"),
    ("tool_retry", (R,), FAULT, "单开 `tool_retry`"),
    ("json_recovery", (J,), FAULT, "单开 `json_recovery`"),
    ("all3", ALL3, FAULT, "前三条护栏全开"),
    ("all4", ALL4, FAULT, "四条护栏全开"),
    ("no_fault", (), 0, "诊断参照：关掉注入故障，护栏也全关"),
    ("no_fault_all4", ALL4, 0, "诊断参照：关掉注入故障 + 四条护栏全开"),
)
CELL_NAMES = tuple(cell[0] for cell in CELLS)

Record = dict[str, Any]
Delta = tuple[int, float]


def collect(
    seeds: Sequence[int],
    *,
    tasks_path: Path,
    k: int = robustness.DEFAULT_K,
) -> list[Record]:
    """九格 × 每个 seed 重跑，返回紧凑结构化证据；不读任何落盘产物。"""
    records: list[Record] = []
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for seed in seeds:
            for cell, guards, fpm, _ in CELLS:
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
                        "seed": seed,
                        "cell": cell,
                        "guards": list(guards),
                        "fault_per_mille": fpm,
                        "pass1": summary["pass1"],
                        "passk": summary["passk"],
                        "passk_hits": summary["passk_hits"],
                        "task_count": summary["task_count"],
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


def cell_records(records: Sequence[Record], cell: str) -> list[Record]:
    return [record for record in records if record["cell"] == cell]


def cell_summary(records: Sequence[Record], cell: str) -> dict[str, Any]:
    """一格的跨 seed 描述性汇总；缺格直接报错，不静默补零。"""
    rows = cell_records(records, cell)
    if not rows:
        raise ValueError(f"证据里没有格子 {cell}，拒绝静默补零")
    passk = [float(row["passk"]) for row in rows]
    pass1 = [float(row["pass1"]) for row in rows]
    return {
        "count": len(rows),
        "passk_mean": sum(passk) / len(passk),
        "passk_min": min(passk),
        "passk_max": max(passk),
        "pass1_mean": sum(pass1) / len(pass1),
        "invalid_calls": sum(int(row["invalid_calls"]) for row in rows),
        "total_steps": sum(int(row["total_steps"]) for row in rows),
    }


def per_seed_delta(
    records: Sequence[Record], control: str, treatment: str
) -> list[Delta]:
    """逐 seed 的 Δpass^k；缺任一格直接报错。"""
    by_seed: dict[int, dict[str, float]] = {}
    for record in records:
        if record["cell"] in (control, treatment):
            by_seed.setdefault(int(record["seed"]), {})[record["cell"]] = float(
                record["passk"]
            )
    result: list[Delta] = []
    for seed in sorted(by_seed):
        cells = by_seed[seed]
        missing = [name for name in (control, treatment) if name not in cells]
        if missing:
            raise ValueError(f"seed={seed} 缺少格子 {missing}，拒绝静默补零")
        result.append((seed, cells[treatment] - cells[control]))
    return result


def sign_counts(deltas: Sequence[Delta]) -> dict[str, Any]:
    """Δ 的符号分布与均值；描述性统计，不是显著性检验。"""
    positive = sum(delta > EFFECT_EPSILON for _, delta in deltas)
    negative = sum(delta < -EFFECT_EPSILON for _, delta in deltas)
    return {
        "positive": positive,
        "zero": len(deltas) - positive - negative,
        "negative": negative,
        "mean": (
            sum(delta for _, delta in deltas) / len(deltas) if deltas else 0.0
        ),
    }


def equal_seed_count(
    records: Sequence[Record], left: str, right: str
) -> tuple[int, int, list[int]]:
    """两格逐 seed 相等的 (相等数, 总数, 不等的 seed 列表)。"""
    deltas = per_seed_delta(records, left, right)
    mismatching = [
        seed for seed, delta in deltas if abs(delta) > EFFECT_EPSILON
    ]
    return len(deltas) - len(mismatching), len(deltas), mismatching


def unrecoverable_tasks(records: Sequence[Record]) -> list[dict[str, str]]:
    """从「无故障 + 全开」自动推导在所有 seed 上都 k 次全不过的任务。

    不硬编码任务 ID：这些任务在任何护栏组合、任何 seed 下都失败，只能是
    任务脚本自带的固有缺陷。归因列取自 ``no_fault`` / ``no_fault_all4`` 的
    首个 seed，用于说明缺陷类型，不参与任何判分。
    """
    upper = cell_records(records, "no_fault_all4")
    flat = cell_records(records, "no_fault")
    if not upper or not flat:
        raise ValueError("缺少诊断参照格子，无法推导不可恢复任务")
    flags: dict[str, list[bool]] = {}
    for record in upper:
        for task in record["per_task"]:
            flags.setdefault(task["task_id"], []).append(bool(task["all_pass"]))
    plain = {task["task_id"]: task["failure_type"] for task in flat[0]["per_task"]}
    guarded = {
        task["task_id"]: task["failure_type"] for task in upper[0]["per_task"]
    }
    return [
        {
            "task_id": task_id,
            "no_fault_failure": plain.get(task_id, "unknown"),
            "all4_failure": guarded.get(task_id, "unknown"),
        }
        for task_id, results in sorted(flags.items())
        if not any(results)
    ]


def _signed(value: float, precision: int = 2) -> str:
    return f"{0.0 if abs(value) < EFFECT_EPSILON else value:+.{precision}f}"


def render_report(records: Sequence[Record]) -> str:
    """渲染 Markdown；只依赖证据与模块常量，不含路径、时钟或机器信息。"""
    summaries = {cell: cell_summary(records, cell) for cell in CELL_NAMES}
    seeds = sorted({int(record["seed"]) for record in records})
    full_sweep = tuple(seeds) == tuple(sorted(robustness.DEFAULT_SEEDS))
    seed_note = (
        "（与 `w3_robustness.md` **完全相同**的一组整数）"
        if full_sweep
        else "（本次为自定义子集，完整扫描请不带 `--seeds` 重跑）"
    )
    seed_list = ", ".join(str(seed) for seed in seeds)

    single = sign_counts(per_seed_delta(records, "off", "json_recovery"))
    all3_to_all4 = per_seed_delta(records, "all3", "all4")
    all4_vs_upper, all4_total, all4_mismatch = equal_seed_count(
        records, "all4", "no_fault_all4"
    )
    all3_vs_upper, all3_total, _ = equal_seed_count(
        records, "all3", "no_fault_all4"
    )
    lifted = [seed for seed, delta in all3_to_all4 if delta > EFFECT_EPSILON]
    harmed = [seed for seed, delta in all3_to_all4 if delta < -EFFECT_EPSILON]
    intrinsic = unrecoverable_tasks(records)
    task_count = int(cell_records(records, "off")[0]["task_count"])
    ceiling = summaries["all4"]["passk_mean"]
    no_fault = summaries["no_fault"]["passk_mean"]
    plain_guard = summaries["off"]["passk_mean"]

    lines = [
        "# W3 可达上限分析：护栏阶段为什么到此收口",
        "",
        f"> 由 `python -m eval.ceiling` 依据 `{EVIDENCE_NAME}` 如实生成；"
        "不含生成时间，同命令两次产物逐字节一致。",
        "> 本模块**不新增护栏、不改判分口径、不加配置字段、不修改任何既有留档**："
        "全部既有 `run_id` / 归档报告 / 矩阵 / 稳健性 / A-B 扫描产物与冻结基线"
        "一字节未动。",
        "",
        "## 结论先行",
        "",
        f"- **四条护栏全开 == 关掉注入故障 + 四条护栏全开**："
        f"`{all4_vs_upper}/{all4_total}` 个 seed 上 `pass^k` 逐 seed 相等。"
        "四类注入故障已被运行时完全恢复，当前任务集 / 判分口径下的护栏收益已到顶。",
        f"- **单变量 A/B 会低估这类护栏**：`json_recovery` 单开的 Δ`pass^k` 分布是 "
        f"正 {single['positive']} / 零 {single['zero']} / 负 {single['negative']}"
        f"（均值 {_signed(single['mean'], 4)}），但它把「前三条全开」的 "
        f"{len(lifted)} 个 seed 补到上限（{', '.join(str(s) for s in lifted) or '无'}），"
        f"变差的 seed {len(harmed)} 个。原因不是护栏弱，而是 `pass^k` 要求 k 次全过："
        "其他类别的故障还在时，恢复一个 `bad_json` 不产生任何分数变化。",
        f"- **`fpm=0` 不是天花板**：关掉注入故障且不开启护栏时 `pass^k` 平均只有 "
        f"{no_fault:.4f}，反而低于四条护栏全开 {ceiling:.4f}。因为 `idempotency` "
        "还修了任务脚本里**固有的重复写**（与注入故障无关）。拿 `fpm=0` 当分母算"
        "「达成率」会得到 >100% 的荒谬数字。",
        f"- **剩余 gap 不该由护栏修**：{task_count} 个任务里有 {len(intrinsic)} 个"
        "在任何护栏组合、任何 seed 下都 k 次全不过（见下表），全是脚本固有缺陷。"
        "让护栏去修它们，等于让评测替模型把任务做对 —— 是作弊，不是可靠性提升。",
        "",
        "## 方法与九格输入",
        "",
        "| 格子 | 护栏 | 故障注入率 | 用途 |",
        "| --- | --- | --- | --- |",
    ]
    for cell, guards, fpm, purpose in CELLS:
        guard_label = "`" + "`, `".join(guards) + "`" if guards else "全部关闭"
        lines.append(f"| `{cell}` | {guard_label} | {fpm}/1000 | {purpose} |")
    lines += [
        "",
        f"测量输入固定：任务集 `{robustness.DEFAULT_TASKS}`、k={robustness.DEFAULT_K}、"
        f"`SeededBackend`、{len(seeds)} 个 seed{seed_note}。seed 列表：`{seed_list}`。",
        "",
        "> **`fpm=0` 的两格是诊断参照，不是对照组**：它们改了故障注入率这个测量输入，"
        "因此**不与 OFF 组做因果对比**，只用来标注「哪些失败来自注入故障、哪些来自"
        "任务脚本」。因果结论只取前七格之间「只差 `[guards].enabled`」的对照。"
        "RNG 在这两格里根本没被使用，所以它们与 seed 无关 —— 这个性质本身可复现。",
        "",
        "> **逐字节可复现**：seed 集、任务集、k、故障率、九格定义全部硬编码并版本化；"
        "RNG 由 `(seed, task_id, trial, cursor)` 经 SHA256 派生。模块不读时钟、不读"
        "环境变量、不用全局随机源；跑批只写共用临时目录，仓库里只留下证据 JSONL 与"
        "本报告。`--from-jsonl` 可从证据只读重渲染，不重跑评测。",
        "",
        "## 30 seed 汇总（描述性统计）",
        "",
        "| 格子 | 平均 `pass^k` | min | max | 平均 `pass¹` | `invalid_calls` 合计 | `total_steps` 合计 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cell in CELL_NAMES:
        item = summaries[cell]
        lines.append(
            f"| `{cell}` | {item['passk_mean']:.4f} | {item['passk_min']:.2f} | "
            f"{item['passk_max']:.2f} | {item['pass1_mean']:.4f} | "
            f"{item['invalid_calls']} | {item['total_steps']} |"
        )
    lines += [
        "",
        "## 核心对照",
        "",
        "| 对照 | 结果 | 含义 |",
        "| --- | --- | --- |",
        f"| `all4` vs `no_fault_all4` | {all4_vs_upper}/{all4_total} 个 seed 相等 | "
        "四类注入故障已被完全恢复；护栏组合达到当前任务集的可达上限 |",
        f"| `all3` vs `no_fault_all4` | {all3_vs_upper}/{all3_total} 个 seed 相等 | "
        "前三条护栏还留着 `bad_json` 这个缺口 |",
        f"| `off` → `all4` | {plain_guard:.4f} → {ceiling:.4f} | "
        "W3 四条护栏在本任务集上拿到的全部收益 |",
        f"| `no_fault` → `no_fault_all4` | {no_fault:.4f} → "
        f"{summaries['no_fault_all4']['passk_mean']:.4f} | "
        "关掉注入故障后护栏仍在涨：涨的是**固有重复写**被幂等去重修好 |",
        f"| `all3` → `all4` | {len(lifted)} 个 seed 提升、{len(harmed)} 个变差 | "
        "`json_recovery` 是补齐残余 gap 的最后一块拼图 |",
        "",
        "四类注入故障与运行时机制的归属（`bad_args` 被两条护栏覆盖，其余一对一）：",
        "",
        "| 注入故障 | 恢复机制 | 对应护栏 |",
        "| --- | --- | --- |",
        "| `bad_args` | 执行前参数校验 / 工具报错后重发 | `schema_validation` / `tool_retry` |",
        "| `wrong_tool` | 工具报错后重发 | `tool_retry` |",
        "| `duplicate_call` | 幂等键拦截重复写 | `idempotency` |",
        "| `bad_json` | JSON 重试改走 `[GUARD-REJECT]` 协议、重发被跳过的动作 | `json_recovery` |",
        "",
        "## 不可恢复的固有缺陷（从证据自动推导，未硬编码任务 ID）",
        "",
        f"下表是 `no_fault_all4` 里**所有 seed 都 k 次全不过**的 {len(intrinsic)} 个任务；"
        "改任务集后本表会自动更新。",
        "",
        "| 任务 | `fpm=0` 无护栏归因 | 四条全开后归因 |",
        "| --- | --- | --- |",
    ]
    for item in intrinsic:
        lines.append(
            f"| `{item['task_id']}` | `{item['no_fault_failure']}` | "
            f"`{item['all4_failure']}` |"
        )
    lines += [
        "",
        "这些失败分别来自脚本自带的坏 JSON、缺参数、错工具、没有 final、超退 / 取消后"
        "退款、以及 final 漏说必要告知。它们与 seed 无关、与故障注入率无关（每 seed "
        "计数恒定），是 W1 时代刻意留下的「难看的基线」。护栏按设计不碰它们："
        "`SeededBackend` 只在注入故障时存 `_pending_correct`，固有缺陷没有可重发的"
        "干净动作，拒绝消息只会自然收敛到 `TERMINATION_JSON_FAILURE` / `MAX_STEPS`。",
        "",
        "## 两个口径陷阱（读表前先看）",
        "",
        "1. **不要拿 `fpm=0` 当 oracle / 天花板**。它只关掉注入故障，脚本固有缺陷仍在；"
        "而 `idempotency` 的去重机制天然覆盖脚本里固有的重复写，所以护栏全开可以"
        "严格超过它。任何「达成率 = 实测提升 / (fpm=0 − OFF)」的算法都会算出 >100%，"
        "那是分母选错了，不是护栏超常发挥。W3 的对照组始终是 `fpm=60` 的 OFF 组。",
        "2. **不要把剩余 gap 读成「护栏还不够强」**。上表的固有缺陷是任务集设计的一部分，"
        "不是运行时能修的可靠性问题。要继续提高 `pass^k`，该改的是任务集（把固有缺陷"
        "分层标注、或补充可被运行时恢复的故障），而不是让护栏越界替模型做对 —— "
        "那样测到的是评测作弊，不是 harness 的价值。",
        "",
        "## 为什么剩余两条路线表护栏当前不可测",
        "",
        "- **上下文压缩**：`ScriptedBackend` / `SeededBackend` 没有真实上下文窗口，"
        "`messages` 长度不受限、`ModelReply.prompt_tokens` 恒为 0。压缩没有可观测的"
        "自变量落点；强行裁 messages 只会改变脚本后端的行为契约，等于换测量仪器。",
        "- **token / 步数预算**：`tokens` 恒 0，步数预算是 W1 起固定的 `MAX_STEPS=12`，"
        "改它同样是改测量仪器而不是「加护栏」。且 W3 四条护栏共享的 "
        "`MAX_GUARD_REJECTIONS` 已经是运行时预算，与本条不是同一个变量。",
        "- 要测它们需要：接 `OpenAIBackend` 拿真实 token / 延迟、构造有长上下文与成本"
        "压力的任务集、并新增对应的观测口径。这属于 W4，不是把开关硬塞进当前 harness。",
        "",
        "## 口径与限制",
        "",
        "- **不是显著性检验**：没有置信区间、没有 p 值、不做多重比较校正；只是把同一套"
        "确定性跑批在多个 seed 上重做一遍，报告逐格取值与符号分布。",
        "- **seed 是人为选定的一组整数，不是随机抽样**：「N 个里 X 个相等」不能当概率"
        "估计，更不能外推到真实模型、其他任务集或其他故障注入率。",
        f"- **分辨率极粗**：{task_count} 条任务 × k={robustness.DEFAULT_K}，"
        "`pass^k` 只能取 0.10 的整数倍；`+0.10` 只代表「一个任务从不是 k 次全过变成"
        "k 次全过」，不是连续的效应量。",
        "- **全部数字来自确定性模拟器**：`SeededBackend` 没有语言理解能力，注入的是人为"
        "噪声；`tokens` / `latency_ms` 恒为 0，**不构成任何真实模型能力、成本或延迟结论**。",
        "- **护栏拒绝协议只在模拟器里验证过**：`[GUARD-REJECT]` 靠 `_pending_correct` "
        "触发重发干净动作；真实模型能否读懂拒绝消息并改对，必须接 `OpenAIBackend` 后"
        "另开实验，本表不能外推。",
        "- 效应只做 `|v| < 1e-9 → 0` 的浮点噪声归零，不做任何四舍五入或结果修饰。",
        "",
        "## 下一步",
        "",
        "- W3 护栏阶段到此收口：四条护栏覆盖四类注入故障，组合已到可达上限，不再"
        "追加「又能涨 0.10」的护栏（那只会是零效应或越界）。",
        "- W4：接真实模型，验证 `[GUARD-REJECT]` 协议能否被读懂并改对，并让上下文 / "
        "预算类护栏具备可观测的测量输入。",
        "- 提高 `pass^k` 分辨率：扩大任务集或提高 k，并把固有缺陷分层标注，否则 0.10 "
        "的步进会继续淹没真实差异。",
        "- 前端 demo 仍后置。",
        "",
        "## 数据来源与复现",
        "",
        f"每行一格的完整指标（seed / cell / guards / fault_per_mille / pass¹ / pass^k / "
        f"`invalid_calls` / `total_steps` / `failure_counts` / 逐任务序列）在 "
        f"`{EVIDENCE_NAME}`。单变量 A/B 的正式留档在 `experiments/runs/<run_id>/`。",
        "",
        "```",
        "python -m eval.ceiling",
        "python -m eval.ceiling --from-jsonl",
        "python -m unittest discover -s tests -v",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.ceiling",
        description="W3 可达上限分析：九格 × 30 seed，只读诊断，不新增护栏",
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
            k=robustness.DEFAULT_K,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(
            robustness.dumps_records(records).encode("utf-8")
        )

    text = render_report(records)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(text.encode("utf-8"))

    summaries = {cell: cell_summary(records, cell) for cell in CELL_NAMES}
    equal, total, _ = equal_seed_count(records, "all4", "no_fault_all4")
    print(f"seed 数：{len(set(int(r['seed']) for r in records))}，证据行：{len(records)}")
    print(f"  OFF 平均 pass^k      : {summaries['off']['passk_mean']:.4f}")
    print(f"  四条全开平均 pass^k  : {summaries['all4']['passk_mean']:.4f}")
    print(f"  无故障参照平均 pass^k: {summaries['no_fault']['passk_mean']:.4f}")
    print(f"  全开 = 无故障+全开   : {equal}/{total} 个 seed")
    print(f"  不可恢复的固有缺陷任务: {len(unrecoverable_tasks(records))} 个")
    print(f"证据：{evidence_path}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
