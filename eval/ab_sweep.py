"""W3 单变量 A/B 的跨 seed 证据：把「每组护栏必须附多 seed 分布」工程化。

`eval/robustness.py` 已经证明既有 2×2 报告的 ``seed=11`` 是偏乐观样本，
并在结论里立下纪律：后续每组护栏都要在同一 30-seed 集上给出效应符号分布，
只报一个 seed 的 A/B 不算可复现结论。本模块把这条纪律做成可复用能力——
给定一条护栏名，就对 ``OFF`` / ``ON`` 两组在同一批 seed 上重跑，产出
逐 seed 的 Δpass^k 证据与符号分布报告。下一组护栏只需换 ``--treatment``，
不需要再写一个一次性脚本。

纪律：
- **测量输入固定**：任务集、k、后端、故障注入率、seed 集全部复用
  ``robustness.py`` 的常量，CLI 不允许覆盖，避免一次检查混入第二个自变量。
- 不读时钟、不读环境变量、不用全局随机源、零第三方依赖；跑批只写一个
  共用临时目录，仓库里只留下证据 JSONL 与 Markdown 报告。
- ``--from-jsonl`` 从证据只读重渲染，不重跑评测；同命令两次产物逐字节一致。
- 报告是**描述性统计，不是显著性检验**：不给置信区间、不给 p 值。

用法：
    python -m eval.ab_sweep                       # 全量：tool_retry 的 30-seed A/B
    python -m eval.ab_sweep --from-jsonl          # 只读证据重渲染
    python -m eval.ab_sweep --seeds 11,1,2 --out-dir <dir>   # 小规模自检
    python -m eval.ab_sweep --treatment idempotency           # 换一组护栏复用同一能力
"""
from __future__ import annotations

import argparse
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from eval import analyze, runner, robustness
from eval.config import resolve_input
from runtime.guard import (
    GUARD_JSON_RECOVERY,
    GUARD_NAMES,
    GUARD_TOOL_RETRY,
)
from runtime.model import BackendConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "experiments"
DEFAULT_TREATMENT = GUARD_TOOL_RETRY

CONTROL = "control"
TREATMENT = "treatment"
CELLS = (CONTROL, TREATMENT)
EFFECT_EPSILON = 1e-9

Record = dict[str, Any]
Pair = dict[str, Any]


def evidence_name(treatment: str) -> str:
    return f"w3_{treatment}_sweep.jsonl"


def report_name(treatment: str) -> str:
    return f"w3_{treatment}_sweep.md"


def collect(
    seeds: Sequence[int],
    *,
    treatment: str,
    tasks_path: Path,
    k: int = robustness.DEFAULT_K,
    fault_per_mille: int = robustness.DEFAULT_FAULT_PER_MILLE,
) -> list[Record]:
    """在临时目录里重跑 OFF / ON 两组 × 每个 seed，返回紧凑的结构化证据。

    只取 ``runner.run`` 返回的 rows 做汇总，不读跑批落盘文件；各格反复
    覆盖同名文件也不影响结果。证据里带上 ``guards`` 与 ``treatment``，
    使 ``--from-jsonl`` 能在完全离线的情况下重渲染出同一份报告。
    """
    if treatment not in GUARD_NAMES:
        raise ValueError(f"未知护栏：{treatment}；已实现：{list(GUARD_NAMES)}")
    records: list[Record] = []
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        for seed in seeds:
            for cell, guards in ((CONTROL, ()), (TREATMENT, (treatment,))):
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
                        "treatment": treatment,
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


def per_seed_pairs(records: Sequence[Record]) -> list[Pair]:
    """按证据里的出现顺序分组；缺任一格直接报错，不静默补零。"""
    by_seed: dict[int, dict[str, Record]] = {}
    order: list[int] = []
    for record in records:
        seed = int(record["seed"])
        if seed not in by_seed:
            by_seed[seed] = {}
            order.append(seed)
        by_seed[seed][str(record["cell"])] = record
    result: list[Pair] = []
    for seed in order:
        cells = by_seed[seed]
        missing = [name for name in CELLS if name not in cells]
        if missing:
            raise ValueError(f"seed={seed} 缺少格子 {missing}，拒绝静默补零")
        result.append(
            {
                "seed": seed,
                "control": cells[CONTROL],
                "treatment": cells[TREATMENT],
                "delta": float(cells[TREATMENT]["passk"])
                - float(cells[CONTROL]["passk"]),
            }
        )
    return result


def distribution(values: Sequence[float]) -> dict[str, Any]:
    """跨 seed 的 Δ 符号分布与描述性量；不是显著性检验。"""
    signs = Counter(
        "+" if value > EFFECT_EPSILON
        else ("-" if value < -EFFECT_EPSILON else "0")
        for value in values
    )
    return {
        "positive": signs["+"],
        "zero": signs["0"],
        "negative": signs["-"],
        "mean": sum(values) / len(values) if values else 0.0,
        "min": min(values) if values else 0.0,
        "max": max(values) if values else 0.0,
    }


def _signed(value: float, precision: int = 2) -> str:
    return f"{0.0 if abs(value) < EFFECT_EPSILON else value:+.{precision}f}"


def _totals(records: Sequence[Record], cell: str, key: str) -> int:
    return sum(int(record[key]) for record in records if record["cell"] == cell)


def render_sweep(records: Sequence[Record], treatment: str) -> str:
    """渲染 Markdown；只依赖证据与模块常量，不含路径、时钟或机器信息。"""
    names = {str(record["treatment"]) for record in records}
    if names and names != {treatment}:
        raise ValueError(f"证据里的护栏名 {sorted(names)} 与参数 {treatment} 不一致")
    pairs = per_seed_pairs(records)
    dist = distribution([float(pair["delta"]) for pair in pairs])
    seed_count = len(pairs)
    seed_list = ", ".join(str(pair["seed"]) for pair in pairs)
    positive_seeds = [
        pair["seed"] for pair in pairs if pair["delta"] > EFFECT_EPSILON
    ]
    headline = next(
        (pair for pair in pairs if pair["seed"] == robustness.DEFAULT_SEEDS[0]),
        None,
    )
    inv_off = _totals(records, CONTROL, "invalid_calls")
    inv_on = _totals(records, TREATMENT, "invalid_calls")
    steps_off = _totals(records, CONTROL, "total_steps")
    steps_on = _totals(records, TREATMENT, "total_steps")
    full_sweep = tuple(pair["seed"] for pair in pairs) == robustness.DEFAULT_SEEDS
    seed_note = (
        "（与 `w3_robustness.md` **完全相同**的一组整数）"
        if full_sweep
        else "（本次为自定义子集，完整扫描请不带 `--seeds` 重跑）"
    )

    lines = [
        f"# W3 `{treatment}` 护栏：单变量 A/B 的跨 seed 证据",
        "",
        f"> 由 `python -m eval.ab_sweep` 依据 `{evidence_name(treatment)}` 如实生成；"
        "不含生成时间，同命令两次产物逐字节一致。",
        "> 本模块不新增护栏、不改判分口径、不修改任何既有留档："
        "四份 2×2 配置的 `run_id` / `config.json` / `result_rows.jsonl` / `report.md`、"
        "`w3_guard_matrix.md`、`w3_robustness.*` 与冻结基线全部一字节未动。",
        "",
        "## 为什么要做这份检查",
        "",
        "既有 [多种子稳健性检验](w3_robustness.md) 已经证明：2×2 全因子报告只在 "
        "`seed=11` 上跑过一次，10 条任务、k=3，`pass^k` 分辨率 0.10，"
        "而换 30 个 seed 后 `schema_validation` 在多数 seed 上完全测不到提升。"
        "**`seed=11` 是偏乐观样本**。因此本轮新增护栏不再只报一次 A/B："
        "对照纪律是「同一 seed 集、只差 `[guards].enabled`、附符号分布」。",
        "",
        "## 方法与输入",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        "| 后端 | `SeededBackend`（确定性伪随机模拟器） |",
        f"| 任务集 | `{robustness.DEFAULT_TASKS}`（10 条） |",
        f"| k | {robustness.DEFAULT_K} |",
        f"| 故障注入率 | {robustness.DEFAULT_FAULT_PER_MILLE}/1000 |",
        f"| seed 数 | {seed_count}{seed_note} |",
        f"| seed 列表 | `{seed_list}` |",
        f"| 对照组 | `enabled = []` |",
        f"| 实验组 | `enabled = [\"{treatment}\"]` |",
        "| 唯一自变量 | `[guards].enabled` |",
        "| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1 |",
        "| 效应定义 | 逐 seed 的 `Δpass^k = ON − OFF`；符号分布取正 / 零 / 负计数 |",
        "",
        "> **逐字节可复现**：seed 集、任务集、k、故障率全部硬编码在 "
        "`eval/robustness.py` / `eval/ab_sweep.py` 并版本化；RNG 由 "
        "`(seed, task_id, trial, cursor)` 经 SHA256 派生。模块不读时钟、不读环境变量、"
        "不用全局随机源；跑批只写共用临时目录，仓库里只留下证据 JSONL 与本报告。"
        "`--from-jsonl` 可从证据只读重渲染，不重跑评测。",
        "",
        "## 逐 seed 结果",
        "",
        "| seed | OFF `pass^k` | ON `pass^k` | Δ | OFF `invalid_calls` | ON `invalid_calls` |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for pair in pairs:
        off, on = pair["control"], pair["treatment"]
        lines.append(
            f"| {pair['seed']} | {float(off['passk']):.2f} | "
            f"{float(on['passk']):.2f} | {_signed(float(pair['delta']))} | "
            f"{int(off['invalid_calls'])} | {int(on['invalid_calls'])} |"
        )

    lines += [
        "",
        "## 跨 seed 汇总（描述性统计，不是显著性检验）",
        "",
        "| 项 | 正 | 零 | 负 | 均值 | 最小 | 最大 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        f"| Δ`pass^k` | {dist['positive']} | {dist['zero']} | "
        f"{dist['negative']} | {_signed(dist['mean'], 4)} | "
        f"{_signed(dist['min'])} | {_signed(dist['max'])} |",
        "",
        f"- 出现正效应的 seed：`{', '.join(str(s) for s in positive_seeds) or '无'}`。",
        f"- 两组合计 `invalid_calls`：{inv_off} → {inv_on}（差 {inv_on - inv_off:+d}）；"
        f"`total_steps`：{steps_off} → {steps_on}（差 {steps_on - steps_off:+d}）。",
        "",
    ]
    if headline is not None:
        off, on = headline["control"], headline["treatment"]
        lines += [
            f"### 与留档 A/B 的对照（`seed={headline['seed']}`）",
            "",
            f"既有 OFF 组留档与本轮实验组在这一 seed 上的 `pass^k` 为 "
            f"{float(off['passk']):.2f} → {float(on['passk']):.2f}"
            f"（Δ {_signed(float(headline['delta']))}），`pass¹` "
            f"{float(off['pass1']):.2f} → {float(on['pass1']):.2f}，"
            f"`invalid_calls` {int(off['invalid_calls'])} → {int(on['invalid_calls'])}。"
            "单个 seed 的数字不代表全样本（合计见上表）；正式的单变量 A/B 表在 "
            "`experiments/runs/<run_id>/report.md`；本节只是把留档那一格放回 "
            "30 个 seed 的语境里，避免把单次结果当结论。",
            "",
        ]
    else:
        lines += [
            "### 与留档 A/B 的对照",
            "",
            f"- 本次 seed 集不含 `{robustness.DEFAULT_SEEDS[0]}`（既有留档所用的 seed），"
            "因此这里不与留档逐项对照；完整扫描请不带 `--seeds` 重跑。",
            "",
        ]

    lines += ["## 机制：这条护栏到底修了什么", ""]
    if treatment == GUARD_TOOL_RETRY:
        lines += [
            f"`{treatment}` 是路线表里「退避」在本同步确定性 harness 里的落地形态："
            "工具层报错后追加一条 `[GUARD-REJECT]` 消息要求重发，重试受共享的 "
            "`MAX_GUARD_REJECTIONS` 上限约束。**不做真实 sleep、不引入指数延迟**——"
            "那会破坏逐字节复现，也不是本实验的自变量。",
            "",
            "四类人为缺陷与运行时机制的归属（互不重叠，便于单变量归因）：",
            "",
            "| 缺陷 | 既有机制 | `tool_retry` |",
            "| --- | --- | --- |",
            "| `bad_json` | W1 的 JSON 重试（只提示，不恢复被跳过的动作） | 不触发：未进入 `tools.call` |",
            "| `bad_args` | `schema_validation` 在执行前拦下 | 工具报错后重发 |",
            "| `wrong_tool` | 两条执行前护栏都明确放行 | **唯一能覆盖它的护栏** |",
            "| `duplicate_call` | `idempotency` 拦重复写 | 工具层不报错，不触发 |",
            "",
            "- **只有可恢复的注入故障能被修好**。`SeededBackend` 注入故障时会把干净动作"
            "存进 `_pending_correct`，护栏的拒绝消息才能触发它重发；任务脚本里**固有的**"
            "缺陷（如 `T07` 的 `refund_money` 错工具、`T08` 的 `refund_order` 缺参数）"
            "没有这份干净动作，重发只是让后端推进到下一个脚本项。这些 trial 在 trace 里"
            "照样产生 `role=guard` 的 span，但任务仍然失败——护栏没有越界去修固有缺陷。",
            "- **错误证据如实保留**：报错的 `tool` span 与 `errors` 都在原处，护栏只额外"
            "追加一个 `guard` span 与一条拒绝消息，因此 `invalid_calls` **不会因这条护栏"
            "而下降，只会持平或上升**：错误确实发生过一次，重发又会让后端多消耗一次输出，"
            "若后续动作本身也有缺陷就会再报一次错。本轮两组合计的变化见上表；"
            "中间指标下降、持平、上升都不能替代 `pass^k`。"
            "`grader` 不消费 `guard` span，判分口径不因开关改变。",
            "- **正效应 seed 集合与既有两条护栏不是包含关系**：作用位置不同（执行前 vs "
            "失败后），在不同随机流下各有独自生效的 seed。不能因为「多覆盖一类错误」就"
            "推断它必然更优；逐 seed 的符号分布才是可复现证据。",
            "",
        ]
    elif treatment == GUARD_JSON_RECOVERY:
        lines += [
            f"`{treatment}` 覆盖四类注入故障里最后一条没有护栏覆盖的类别：`bad_json`。"
            "W1 的 JSON 重试只给出 `RETRY_HINT` 提示，被替换掉的那个动作会被直接跳过；"
            "本护栏**保留原提示证据**（grader 据此仍归因 `output_format`），只额外追加一个 "
            "`role=guard` 的 span 与一条 `[GUARD-REJECT]` 消息，让 `SeededBackend` 重发"
            "那一个干净动作。脚本里固有的连续坏 JSON 没有干净动作可重发，会自然收敛到 "
            "`TERMINATION_JSON_FAILURE`，护栏不越界。",
            "",
            "| 缺陷 | 既有机制 | `json_recovery` |",
            "| --- | --- | --- |",
            "| `bad_json` | W1 的 JSON 重试只提示、跳过动作 | **唯一能恢复被跳过动作的护栏** |",
            "| `bad_args` | `schema_validation` / `tool_retry` | 不触发：JSON 合法 |",
            "| `wrong_tool` | `tool_retry` | 不触发：JSON 合法 |",
            "| `duplicate_call` | `idempotency` | 不触发：JSON 合法 |",
            "",
            "- **格式错误不进入 `tools.call`**，所以 `invalid_calls` 与这条护栏无关；"
            "论效果只看 `pass^k`。恢复受共享的 `MAX_GUARD_REJECTIONS` 上限约束，"
            "不做真实 sleep，保证逐字节可复现；`grader` 的归因与判分口径不因开关改变。",
            "- **单变量 A/B 会低估这类护栏**：`pass^k` 要求 k 次全过，其他三类注入故障"
            "还在时，恢复一个 `bad_json` 往往不产生分数变化。它的价值要在「前三条护栏"
            "全开」的组合里才显出来 —— 见 [可达上限分析](w3_ceiling.md)：四条全开把"
            "残余 gap 归零，且逐 seed 等于「关掉注入故障 + 四条全开」。",
            "",
        ]
    else:
        lines += [
            f"`{treatment}` 由 `ab_sweep` 沿用同一套 30-seed 单变量 A/B 口径扫描；"
            "具体机制见 `runtime/guard.py` 与对应留档报告。"
            "**论效果只看 `pass^k`**，中间指标的升降或持平都不能当代理。",
            "",
        ]
    lines += [
        "## 口径与限制（读表前先看）",
        "",
        "- **不是显著性检验**：没有置信区间、没有 p 值、不做多重比较校正。这里只是把"
        "同一套确定性跑批在多个 seed 上重做一遍，报告效应符号分布。",
        "- **seed 是人为选定的一组整数，不是随机抽样**：「N 个里 X 个为正」不能当"
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
        "- 剩余护栏（上下文压缩、token / 步数预算）继续「一组一条、单变量 A/B」，"
        "并用 `--treatment <护栏名>` 复用本模块产出同一 seed 集的符号分布。",
        "- 若要把 `pass^k` 当成主要指标，先扩大任务集或提高 k，否则 0.10 的分辨率"
        "会继续淹没真实差异。",
        "- 真实模型下的拒绝协议验证仍是开放项。",
        "",
        "## 数据来源与复现",
        "",
        f"每行一格的完整指标（seed / cell / guards / pass¹ / pass^k / invalid_calls / "
        f"total_steps / failure_counts）在 `{evidence_name(treatment)}`；既有 2×2 的"
        "逐格证据在 `experiments/runs/<run_id>/`。",
        "",
        "```",
        "python -m eval.ab_sweep",
        "python -m eval.ab_sweep --from-jsonl",
        "python -m unittest discover -s tests -v",
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.ab_sweep",
        description="对一条护栏做 OFF / ON 的跨 seed 单变量 A/B，报告效应符号分布",
    )
    parser.add_argument(
        "--seeds", default=None,
        help="逗号分隔的整数；默认使用 eval/robustness.py 里版本化的 seed 集",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--treatment", choices=GUARD_NAMES, default=DEFAULT_TREATMENT,
        help="实验组开启的护栏；对照组恒为全部关闭",
    )
    parser.add_argument(
        "--from-jsonl", action="store_true",
        help="只读已有证据重渲染报告，不重跑评测",
    )
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    evidence_path = out_dir / evidence_name(args.treatment)
    report_path = out_dir / report_name(args.treatment)

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
            treatment=args.treatment,
            tasks_path=resolve_input(robustness.DEFAULT_TASKS),
            k=robustness.DEFAULT_K,
            fault_per_mille=robustness.DEFAULT_FAULT_PER_MILLE,
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(
            robustness.dumps_records(records).encode("utf-8")
        )

    text = render_sweep(records, args.treatment)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(text.encode("utf-8"))

    dist = distribution(
        [float(pair["delta"]) for pair in per_seed_pairs(records)]
    )
    print(f"seed 数：{len(per_seed_pairs(records))}，证据行：{len(records)}")
    print(
        f"  Δpass^k  正 {dist['positive']:2d} / 零 {dist['zero']:2d} / "
        f"负 {dist['negative']:2d}，均值 {dist['mean']:+.4f}"
    )
    print(f"证据：{evidence_path}")
    print(f"报告：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
