"""指标汇总与 Markdown 结果表生成。

报告里不写生成时间、不写绝对路径，所有数字来自结果行与实验配置；
字符串拼接生成 Markdown，保持结果可 git diff，不引入任何绘图 / 表格依赖。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from eval.config import ExperimentConfig
from eval.grader import FAILURE_TYPES, NO_FAILURE
from eval.runner import Row, Task
from runtime.guard import (
    GUARD_IDEMPOTENCY,
    GUARD_JSON_RECOVERY,
    GUARD_TOOL_RETRY,
)


def summarize(rows: list[Row], k: int) -> dict[str, Any]:
    by_task: dict[str, list[Row]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)
    for task_rows in by_task.values():
        task_rows.sort(key=lambda row: row["trial"])

    pass1_hits = sum(1 for rs in by_task.values() if rs[0]["passed"] == 1)
    passk_hits = sum(1 for rs in by_task.values() if all(r["passed"] == 1 for r in rs))
    task_count = len(by_task)

    failures = Counter(
        row["failure_type"] for row in rows if row["passed"] == 0
    )
    total_steps = sum(row["steps"] for row in rows)
    invalid_calls = sum(row["invalid_calls"] for row in rows)
    latencies = sorted(row["latency_ms"] for row in rows)
    p95_index = max(0, math.ceil(0.95 * len(latencies)) - 1)

    per_task = []
    for task_id in sorted(by_task):
        task_rows = by_task[task_id]
        sequence = "".join("1" if r["passed"] else "0" for r in task_rows)
        failed = [r["failure_type"] for r in task_rows if r["passed"] == 0]
        per_task.append(
            {
                "task_id": task_id,
                "sequence": sequence,
                "all_pass": all(r["passed"] == 1 for r in task_rows),
                "failure_type": failed[0] if failed else NO_FAILURE,
                "avg_steps": sum(r["steps"] for r in task_rows) / len(task_rows),
            }
        )

    return {
        "k": k,
        "task_count": task_count,
        "trial_count": len(rows),
        "pass1_hits": pass1_hits,
        "passk_hits": passk_hits,
        "pass1": pass1_hits / task_count if task_count else 0.0,
        "passk": passk_hits / task_count if task_count else 0.0,
        "avg_steps": total_steps / len(rows) if rows else 0.0,
        "total_steps": total_steps,
        "invalid_calls": invalid_calls,
        "invalid_call_rate": invalid_calls / total_steps if total_steps else 0.0,
        "total_tokens": sum(row["tokens"] for row in rows),
        "p95_latency_ms": latencies[p95_index] if latencies else 0,
        "failure_counts": dict(failures),
        "per_task": per_task,
    }


def _caveats(config: ExperimentConfig) -> list[str]:
    """口径说明随后端分支，避免切真实模型后报告自相矛盾。"""
    common_head = [
        "- **pass¹**：每个任务首次执行的平均通过率。",
        "- **pass^k**：同一任务 k 次独立执行全部通过的任务占比。",
    ]
    common_tail = [
        "- 无效工具调用率 = 工具层报错次数 / 总步数，来自 trace 的事件计数。",
        "- 失败归因取 trace 中**首个**可判定偏离点作主归因；并列原因与偏离点证据写入"
        "结果行的 `reasons` / `first_divergence`，判不出时如实记为 `unknown`。",
    ]
    if config.backend == "scripted":
        return common_head + [
            "- ScriptedBackend 是确定性脚本，同一任务 k 次结果完全一致，"
            "因此本次 **pass^k 与 pass¹ 必然相等**；pass^k 暴露「偶尔成功」的能力"
            "要等接入真实模型后才显现。这一相等不是缺陷，是测量链路确定性的证据。",
            "- `tokens` / `latency_ms` 恒为 0：脚本后端不产生真实模型调用，"
            "这是保持产物确定性的零值，**不是成本数据**，不能据此下成本或延迟结论。",
        ] + common_tail
    if config.backend == "seeded":
        if config.guards:
            capability = (
                "- **本次已开启运行时护栏**（名称见「运行配置」）：本表是 W3 单变量对照的"
                "实验组结果，不再是纯校准。模拟器仍**没有语言理解能力**，注入的仍是人为噪声，"
                "因此这些数字**不构成真实模型能力、成本或延迟结论**；"
                "护栏是否有效请以「与对照组对照（A/B）」表与对照组 `config.json` 为准。"
            )
            if GUARD_IDEMPOTENCY in config.guards:
                # 幂等护栏拦下的是工具层不报错的重复写：invalid_calls 可以
                # 一点不变而 pass^k 提升，旧版「下降不等于提升」的措辞覆盖不了。
                mechanism = (
                    "- 护栏在工具执行前拦下调用，产生 `role=guard` 的 span 与一条拒绝消息："
                    "不写库、不计入 `invalid_calls`，grader 的判分口径不因开关而改变。"
                    "**`invalid_calls` 与成功率没有必然联系**：`schema_validation` 拦下坏参数"
                    "会让它下降，但任务仍可能失败（归因从 `bad_args` 变成 `unknown`）；"
                    "`idempotency` 拦下的是工具层**不报错**的重复写，`invalid_calls` 可以"
                    "完全不变而 `pass^k` 提升。论效果只看 `pass^k`。"
                )
            elif GUARD_TOOL_RETRY in config.guards:
                # 重试发生在工具报错之后，errors 如实保留，旧版「不写库、
                # 不计入 invalid_calls」的措辞对这条护栏是错的。
                mechanism = (
                    "- `tool_retry` 在**工具层报错之后**工作：如实保留报错的 tool span 与 "
                    "`errors`，只额外产生一个 `role=guard` 的 span 与一条拒绝消息，"
                    "让后端重发干净动作。因此 **`invalid_calls` 不会因这条护栏而下降**"
                    "（只会持平或上升）——错误确实发生过一次，重发又会让后端多消耗一次输出，"
                    "若后续动作本身也有缺陷就会再报一次错；论效果仍只能看 `pass^k`。重试受共享的 "
                    "`MAX_GUARD_REJECTIONS` 上限约束，不做真实 sleep、不引入指数延迟，"
                    "以保证逐字节可复现；grader 的判分口径不因开关而改变。"
                )
            elif GUARD_JSON_RECOVERY in config.guards:
                # 格式错误发生在 tools.call 之前，invalid_calls 与这条护栏无关；
                # 保留 RETRY_HINT 证据使 grader 的 output_format 归因链路不变。
                mechanism = (
                    "- `json_recovery` 修的是**输出格式失败**：W1 的 JSON 重试只给出 "
                    "`RETRY_HINT` 提示，被替换掉的那个动作会被直接跳过。这条护栏在"
                    "保留原提示证据的基础上，额外追加一个 `role=guard` 的 span 与一条 "
                    "`[GUARD-REJECT]` 消息，让后端重发那一个动作；脚本里固有的连续坏 "
                    "JSON 没有干净动作可重发，依然失败——护栏没有越界。**格式错误不进入 "
                    "`tools.call`，`invalid_calls` 与这条护栏无关**，论效果只看 `pass^k`；"
                    "grader 的归因与判分口径不因开关而改变。恢复受共享的 "
                    "`MAX_GUARD_REJECTIONS` 上限约束，保证逐字节可复现。"
                )
            else:
                mechanism = (
                    "- 护栏在工具执行前拦下调用，产生 `role=guard` 的 span 与一条拒绝消息："
                    "不写库、不计入 `invalid_calls`，grader 的判分口径不因开关而改变。"
                    "**`invalid_calls` 下降不等于成功率提升**——任务脚本固有的缺陷被拦下后"
                    "依然失败，只是归因可能从 `bad_args` 变成 `unknown`；论效果应看 `pass^k`。"
                )
        else:
            capability = (
                "- **它没有语言理解能力**：故障是按固定规则人为注入的噪声（坏 JSON / 错参数 / "
                "错工具 / 重复写），任务脚本里固有的缺陷原样保留。本表数字只校准测量链路，"
                "**不构成任何模型能力、成本或延迟结论**，也不是 W3 护栏的对照结果。"
            )
            mechanism = ""
        head = common_head + [
            "- SeededBackend 是**确定性伪随机模拟器**：RNG 由 `(seed, task_id, trial, cursor)` "
            "经 SHA256 派生，不使用时间、UUID 或全局随机源，因此同配置连跑两次仍逐字节一致。",
            "- 不同 trial 的 RNG 流互相独立，所以同一任务 k 次结果可能不同；"
            "**pass^k 在本后端下首次具备区分度**，可用于校准「偶尔成功」的测量链路。",
            capability,
        ]
        if mechanism:
            head.append(mechanism)
        return head + [
            "- `tokens` / `latency_ms` 恒为 0：模拟器不产生真实模型调用，"
            "这是保持产物确定性的零值，**不是成本数据**。",
        ] + common_tail
    return common_head + [
        "- OpenAIBackend 调用真实模型，同一任务 k 次结果可能不同；"
        "pass^k 此时才真正用于暴露「偶尔成功」。",
        "- 结果受模型服务影响，不保证逐字节可复现；"
        "「同配置连跑两次逐字节一致」这一验收项仅在 ScriptedBackend 下成立。",
        "- `tokens` 来自响应的 `usage`（prompt + completion），`latency_ms` 是单次调用的"
        "墙钟耗时取整；两者是真实观测，但**不参与判分**，且随服务波动。",
    ] + common_tail


FROZEN_COMPARE = "experiments/baseline/result_rows.jsonl"


def _comparison_lines(
    summary: dict[str, Any],
    baseline_rows: list[Row],
    k: int,
    cross_backend: bool = False,
    compare_path: str = FROZEN_COMPARE,
) -> list[str]:
    base_k = max((row["trial"] for row in baseline_rows), default=k)
    base = summarize(baseline_rows, base_k)
    is_frozen = compare_path == FROZEN_COMPARE
    if is_frozen:
        lines = [
            "## 与冻结基线对照",
            "",
            f"> 基线来自 `experiments/baseline/result_rows.jsonl`（W1 冻结，k={base_k}）；"
            "本报告不修改该文件。",
        ]
        if cross_backend:
            lines += [
                "",
                "> ⚠️ **本次后端与冻结基线不同，本表只是数值罗列，不是消融实验。** "
                "差异来自后端替换（含人为注入的故障），不能作为 harness 或护栏有效性的因果证据；"
                "单变量对照必须在同一后端下、一次只改一个变量进行。",
            ]
        column = "冻结基线"
    else:
        # W3 A/B：对照组是另一次跑批（通常是同后端、只差护栏开关）。
        # 不再套用「冻结基线」的措辞，也不加跨后端警告，但必须提醒读者自己核对单变量。
        lines = [
            "## 与对照组对照（A/B）",
            "",
            f"> 对照组结果行来自 `{compare_path}`（k={base_k}）；本报告不修改该文件。",
            "> 是否构成**单变量对照**请以两份 `config.json` 为准：只有目标变量不同，"
            "本表的差异才能作因果解读。",
        ]
        column = "对照组"
    lines += [
        "",
        f"| 指标 | {column} | 本次 | 变化 |",
        "| --- | --- | --- | --- |",
    ]
    metrics = (
        ("pass¹", "pass1", 2),
        (f"pass^{base_k}", "passk", 2),
        ("平均步数", "avg_steps", 2),
        ("无效工具调用率", "invalid_call_rate", 2),
    )
    for label, key, precision in metrics:
        before, after = base[key], summary[key]
        lines.append(
            f"| {label} | {before:.{precision}f} | {after:.{precision}f} | "
            f"{after - before:+.{precision}f} |"
        )
    for name in FAILURE_TYPES:
        before = base["failure_counts"].get(name, 0)
        after = summary["failure_counts"].get(name, 0)
        lines.append(f"| {name} | {before} | {after} | {after - before:+d} |")
    return lines


def _attribution_lines(rows: list[Row]) -> list[str]:
    """按任务列出首个失败 trial 的归因证据；完整证据在结果行里。"""
    lines = [
        "## 归因证据（首个偏离点）",
        "",
        "> 主归因取 trace 中按 step 顺序的第一个可判定偏离点；这里每个任务只列首个失败 trial，"
        "全部 trial 的证据见结果行的 `reasons` / `first_divergence`。",
        "",
        "| task_id | trial_id | 主归因 | 偏离点 | 并列原因 |",
        "| --- | --- | --- | --- | --- |",
    ]
    seen: set[str] = set()
    for row in sorted(rows, key=lambda item: (item["task_id"], item["trial"])):
        if row["passed"] or row["task_id"] in seen:
            continue
        seen.add(row["task_id"])
        divergence = row.get("first_divergence")
        if isinstance(divergence, dict):
            anchor = (
                f"step {divergence['step']} / {divergence['kind']} / "
                f"{divergence.get('detail') or '-'}"
            )
        else:
            anchor = "—"
        reasons = ", ".join(row.get("reasons", ())) or "—"
        lines.append(
            f"| {row['task_id']} | `{row['trial_id']}` | {row['failure_type']} | "
            f"{anchor} | {reasons.replace('|', '｜')} |"
        )
    if not seen:
        lines.append("| — | — | — | — | 本次没有失败 trial |")
    elif not any("first_divergence" in row for row in rows):
        lines.append("| — | — | — | — | 结果行为旧版 schema，未携带归因证据 |")
    return lines


def render_report(
    rows: list[Row],
    tasks: list[Task],
    config: ExperimentConfig,
    rid: str,
    baseline_rows: list[Row] | None = None,
    config_path: str | None = None,
    max_calls: int | None = None,
) -> str:
    """生成报告；只依赖结果行、配置、基线与预算参数，绝不依赖输出路径或时钟。"""
    summary = summarize(rows, config.k)
    # openai 路径的复现命令必须写全 --max-calls：eval.run 会在构造后端、
    # 发起任何模型调用之前对缺失预算直接 SystemExit。优先记录本次真实闸门；
    # 离线重渲染拿不到 CLI 参数时，用与 eval.budget.estimate_plan(cells=1)
    # 同源的单格最坏上界兜底，保证复制出来的命令仍可执行。
    budget_calls = (
        max_calls
        if max_calls is not None
        else max(1, summary["task_count"] * config.k * config.max_steps)
    )
    briefs: dict[str, str] = {}
    for task in tasks:
        text = task["user_utterances"][0]
        briefs[task["id"]] = text[:24] + ("…" if len(text) > 24 else "")
    seeded_label = (
        "SeededBackend（确定性伪随机模拟器，W3 单变量对照实验）"
        if config.guards
        else "SeededBackend（确定性伪随机模拟器，仅用于校准测量链路）"
    )
    backend_name = {
        "scripted": "ScriptedBackend（确定性脚本）",
        "seeded": seeded_label,
        "openai": "OpenAIBackend",
    }[config.backend]
    seeded_rows = (
        [
            f"| seed | {config.seed} |",
            f"| 故障注入率 | {config.fault_per_mille}/1000 |",
        ]
        if config.backend == "seeded"
        else []
    )
    # 只在开启护栏时才加行：OFF 组与 W1 / W2.5 的既有报告逐字节不变
    guard_rows = (
        [f"| 已开启护栏 | {', '.join(f'`{name}`' for name in config.guards)} |"]
        if config.guards
        else []
    )
    # 预算闸门只作用于 openai：如实记录本次上界，其它后端一行都不加，
    # 既有 scripted / seeded 归档报告逐字节不变。
    budget_rows = (
        [f"| 预算上界（--max-calls） | {budget_calls} |"]
        if config.backend == "openai"
        else []
    )
    lines = [
        f"# 实验结果：{config.name} / `{rid}`",
        "",
        "> 本表由 `python -m eval.run` 依据结果行如实生成，数字未修饰。",
        "> 生效配置来自实验配置文件与显式 CLI 覆盖；留档模式下"
        "同目录另有 `config.json` 存证。`run_id` 由配置摘要确定性生成，"
        "同配置必得同一 `run_id`。",
        "",
        "## 运行配置",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| run_id | `{rid}` |",
        f"| 后端 | {backend_name} |",
        *seeded_rows,
        *guard_rows,
        *budget_rows,
        f"| 任务集 | `{config.tasks}` |",
        f"| 任务数 | {summary['task_count']} |",
        f"| k | {summary['k']} |",
        f"| trial 总数 | {summary['trial_count']} |",
        f"| MAX_STEPS（配置） | {config.max_steps} |",
        f"| JSON 失败重试（配置） | {config.max_json_retries} 次 |",
        "| 领域库 | 内存 SQLite，每 trial 从同一模板 backup 重置 |",
        "| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1 |",
        "",
        "## 口径与限制（读表前先看）",
        "",
        *_caveats(config),
        "",
        "## 总体指标",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| pass¹ | {summary['pass1_hits']}/{summary['task_count']} = {summary['pass1']:.2f} |",
        f"| pass^{summary['k']} | {summary['passk_hits']}/{summary['task_count']} = {summary['passk']:.2f} |",
        f"| 平均步数 | {summary['avg_steps']:.2f} |",
        f"| 无效工具调用率 | {summary['invalid_calls']}/{summary['total_steps']} = {summary['invalid_call_rate']:.2f} |",
        f"| token 合计 | {summary['total_tokens']} |",
        f"| p95 延迟 | {summary['p95_latency_ms']} ms |",
        "",
        "## 失败类型分布",
        "",
        "| 失败类型 | 失败 trial 数 |",
        "| --- | --- |",
    ]
    for name in FAILURE_TYPES:
        lines.append(f"| {name} | {summary['failure_counts'].get(name, 0)} |")
    lines += [
        "",
        "## 逐任务结果",
        "",
        "| task_id | 首句话术 | k 次结果 | 全部通过 | 主要失败类型 | 平均步数 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in summary["per_task"]:
        lines.append(
            f"| {item['task_id']} | {briefs.get(item['task_id'], '')} | `{item['sequence']}` | "
            f"{'是' if item['all_pass'] else '否'} | {item['failure_type']} | "
            f"{item['avg_steps']:.2f} |"
        )
    lines += [""] + _attribution_lines(rows)
    if baseline_rows is not None:
        lines += [""] + _comparison_lines(
            summary,
            baseline_rows,
            config.k,
            cross_backend=config.backend != "scripted",
            compare_path=config.compare,
        )
    reproduce = "python -m eval.run"
    if config_path:
        # 非默认配置文件必须显式带上，否则复现命令会算出另一个 run_id
        reproduce += f" --config {config_path}"
    reproduce += f" --tasks {config.tasks} --k {config.k}"
    if config.backend == "seeded":
        reproduce += (
            f" --backend seeded --seed {config.seed}"
            f" --fault-per-mille {config.fault_per_mille}"
        )
    elif config.backend == "openai":
        # 缺 --max-calls 的复现命令会被预算闸门直接拒绝启动，
        # 等于给读者一条复制即失败的命令；这里必须与运行配置表一致。
        reproduce += f" --backend openai --max-calls {budget_calls}"
    for name in config.guards:
        reproduce += f" --guard {name}"
    next_step = (
        "做 W3 单变量对照时，只改一个配置字段（如 `[guards].enabled`），把 `compare` "
        "指向对照组的 `result_rows.jsonl`；重跑会生成新的 `run_id` 与独立报告，"
        "用「与对照组对照（A/B）」表判断改动是否真的有效。"
        if config.guards
        else "做单变量对照实验时，只改 `experiments/config.toml` 中的一个字段，"
        "重跑后会生成新的 `run_id` 与独立报告；用「与冻结基线对照」表判断这次改动是否真的有效。"
    )
    lines += [
        "",
        "## 复现",
        "",
        "```",
        reproduce,
        "python -m unittest discover -s tests -v",
        "```",
        "",
        "逐 trial 证据在 `traces/<trial_id>.jsonl`，结果行在 "
        "`results/result_rows.jsonl`；两者与本报告用 `trial_id` 串联。",
        "",
        "## 下一步",
        "",
        next_step + "不要为了好看而修饰基线。",
        "",
    ]
    return "\n".join(lines)
