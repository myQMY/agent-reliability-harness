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
        "- `tokens` / `latency_ms` 当前在所有后端下都是 0：ScriptedBackend 使用占位常量，"
        "OpenAIBackend 尚未接入 usage 观测（W2 待办），因此成本与延迟结论暂不成立。",
    ]
    if config.backend == "scripted":
        return common_head + [
            "- ScriptedBackend 是确定性脚本，同一任务 k 次结果完全一致，"
            "因此本次 **pass^k 与 pass¹ 必然相等**；pass^k 暴露「偶尔成功」的能力"
            "要等 W2 接入真实模型后才显现。这一相等不是缺陷，是测量链路确定性的证据。",
        ] + common_tail
    return common_head + [
        "- OpenAIBackend 调用真实模型，同一任务 k 次结果可能不同；"
        "pass^k 此时才真正用于暴露「偶尔成功」。",
        "- 结果受模型服务影响，不保证逐字节可复现；"
        "「同配置连跑两次逐字节一致」这一验收项仅在 ScriptedBackend 下成立。",
    ] + common_tail


def _comparison_lines(
    summary: dict[str, Any], baseline_rows: list[Row], k: int
) -> list[str]:
    base_k = max((row["trial"] for row in baseline_rows), default=k)
    base = summarize(baseline_rows, base_k)
    lines = [
        "## 与冻结基线对照",
        "",
        f"> 基线来自 `experiments/baseline/result_rows.jsonl`（W1 冻结，k={base_k}）；"
        "本报告不修改该文件。",
        "",
        "| 指标 | 冻结基线 | 本次 | 变化 |",
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


def render_report(
    rows: list[Row],
    tasks: list[Task],
    config: ExperimentConfig,
    rid: str,
    baseline_rows: list[Row] | None = None,
) -> str:
    """生成报告；内容只依赖结果行、配置与基线行，绝不依赖输出路径或时钟。"""
    summary = summarize(rows, config.k)
    briefs: dict[str, str] = {}
    for task in tasks:
        text = task["user_utterances"][0]
        briefs[task["id"]] = text[:24] + ("…" if len(text) > 24 else "")
    backend_name = (
        "ScriptedBackend（确定性脚本）"
        if config.backend == "scripted"
        else "OpenAIBackend"
    )
    lines = [
        f"# 实验结果：{config.name} / `{rid}`",
        "",
        "> 本表由 `python -m eval.run` 依据结果行如实生成，数字未修饰。",
        "> 生效配置来自 `experiments/config.toml` 与显式 CLI 覆盖；留档模式下"
        "同目录另有 `config.json` 存证。`run_id` 由配置摘要确定性生成，"
        "同配置必得同一 `run_id`。",
        "",
        "## 运行配置",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| run_id | `{rid}` |",
        f"| 后端 | {backend_name} |",
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
    if baseline_rows is not None:
        lines += [""] + _comparison_lines(summary, baseline_rows, config.k)
    reproduce = f"python -m eval.run --tasks {config.tasks} --k {config.k}"
    if config.backend != "scripted":
        reproduce += " --backend openai"
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
        "做单变量对照实验时，只改 `experiments/config.toml` 中的一个字段，"
        "重跑后会生成新的 `run_id` 与独立报告；用「与冻结基线对照」表判断这次改动是否真的有效。"
        "不要为了好看而修饰基线。",
        "",
    ]
    return "\n".join(lines)
