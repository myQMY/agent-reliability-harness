"""指标汇总与 Markdown 结果表生成。

报告里不写生成时间、不写真实耗时，所有数字来自结果行；
字符串拼接生成 Markdown，保持结果可 git diff，不引入任何绘图 / 表格依赖。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from eval.grader import FAILURE_TYPES, NO_FAILURE
from eval.runner import RunConfig, Row, Task
from runtime import guard


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


def render_report(
    rows: list[Row], tasks: list[Task], config: RunConfig
) -> str:
    summary = summarize(rows, config.k)
    briefs: dict[str, str] = {}
    for task in tasks:
        text = task["user_utterances"][0]
        briefs[task["id"]] = text[:24] + ("…" if len(text) > 24 else "")
    backend_name = (
        "ScriptedBackend（确定性脚本）"
        if config.backend.name == "scripted"
        else "OpenAIBackend"
    )
    lines = [
        "# MVP 基线结果",
        "",
        "> 本表由 `python -m eval.run` 依据 `results/result_rows.jsonl` 如实生成，数字未修饰。",
        "> 基线难看是资产：它的用途是校准测量链路，并为 W3 的单变量对照实验提供参照。",
        "",
        "## 运行配置",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 后端 | {backend_name} |",
        f"| 任务集 | `{config.tasks_path.as_posix()}` |",
        f"| 任务数 | {summary['task_count']} |",
        f"| k | {summary['k']} |",
        f"| trial 总数 | {summary['trial_count']} |",
        f"| MAX_STEPS | {guard.MAX_STEPS} |",
        f"| JSON 失败重试 | {guard.MAX_JSON_RETRIES} 次 |",
        "| 领域库 | 内存 SQLite，每 trial 从同一模板 backup 重置 |",
        "| 判分 | 终态逐字段 diff + 策略检查 + 必要告知，全部满足才计 1 |",
        "",
        "## 口径与限制（读表前先看）",
        "",
        "- **pass¹**：每个任务首次执行的平均通过率。",
        "- **pass^k**：同一任务 k 次独立执行全部通过的任务占比。",
        "- ScriptedBackend 是确定性脚本，同一任务 k 次结果完全一致，"
        "因此本次 **pass^k 与 pass¹ 必然相等**；pass^k 暴露「偶尔成功」的能力"
        "要等 W2 接入真实模型后才显现。这一相等不是缺陷，是测量链路确定性的证据。",
        "- `tokens` / `latency_ms` 在该后端下统一为占位常量 0，"
        "不代表真实模型开销；成本与延迟观测同样留到 W2。",
        "- 无效工具调用率 = 工具层报错次数 / 总步数，来自 trace 的事件计数。",
        "",
        "## 总体指标",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| pass¹ | {summary['pass1_hits']}/{summary['task_count']} = {summary['pass1']:.2f} |",
        f"| pass^{summary['k']} | {summary['passk_hits']}/{summary['task_count']} = {summary['passk']:.2f} |",
        f"| 平均步数 | {summary['avg_steps']:.2f} |",
        f"| 无效工具调用率 | {summary['invalid_calls']}/{summary['total_steps']} = {summary['invalid_call_rate']:.2f} |",
        f"| token 合计（占位） | {summary['total_tokens']} |",
        f"| p95 延迟（占位） | {summary['p95_latency_ms']} ms |",
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
    lines += [
        "",
        "## 复现",
        "",
        "```",
        f"python -m eval.run --tasks {config.tasks_path.as_posix()} --k {config.k}",
        "python -m unittest discover -s tests -v",
        "```",
        "",
        "逐 trial 证据在 `traces/<trial_id>.jsonl`，结果行在 "
        "`results/result_rows.jsonl`；两者与本报告用 `trial_id` 串联。",
        "",
        "## 下一步",
        "",
        "W3 按上表失败归因逐项加护栏（参数校验、幂等键、上下文压缩、预算等），"
        "一次只改一个变量，重跑同一条命令后与本表做对照；不要为了好看而修饰基线。",
        "",
    ]
    return "\n".join(lines)
