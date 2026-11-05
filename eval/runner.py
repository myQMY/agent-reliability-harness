"""批量执行器：串行跑 N 条任务 × k 次，每个 trial 独立环境。

模板在任务级建一次，k 个 trial 用 Connection.backup() 从同一快照重置；
trial_id = task_id + trial index，同时作为结果行字段与 trace 文件名，
保证「任务 / span / 结果行」三者可串联（设计文档 §6）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from env import db
from eval import grader, trace
from eval.user_sim import RuleUserSimulator, TurnContext
from runtime import guard, loop
from runtime.model import BackendConfig, build_backend

Task = dict[str, Any]
Row = dict[str, Any]


@dataclass(frozen=True)
class RunConfig:
    tasks_path: Path
    k: int = 3
    backend: BackendConfig = field(default_factory=BackendConfig)
    results_dir: Path = Path("results")
    traces_dir: Path = Path("traces")
    # 护栏值来自实验配置（eval/config.py）；默认值与冻结基线一致
    max_steps: int = guard.MAX_STEPS
    max_json_retries: int = guard.MAX_JSON_RETRIES
    # W3 起的可开关护栏；空元组表示全部关闭（OFF 组），逐字等价 W1 行为
    guards: tuple[str, ...] = ()


def load_tasks(path: Path) -> list[Task]:
    tasks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            tasks.append(json.loads(line))
    return tasks


def _run_trial(
    task: Task,
    trial: int,
    config: RunConfig,
    template: Any,
) -> Row:
    trial_id = f"{task['id']}-t{trial}"
    conn = db.reset(template)
    backend = build_backend(
        config.backend,
        task.get("script", ()),
        task_id=task["id"],
        trial=trial,
    )
    counter = loop.StepCounter()
    budget = guard.StepBudget(max_steps=config.max_steps)
    simulator = RuleUserSimulator(
        task["user_utterances"], task.get("utterance_rules", ())
    )

    messages: list[dict[str, str]] = []
    spans: list[dict[str, Any]] = []
    final_texts: list[str] = []
    errors: list[str] = []
    termination = ""
    context = TurnContext()

    for turn in range(1, simulator.turn_count + 1):
        utterance = simulator.utterance_for(turn, context)
        messages.append({"role": "user", "content": utterance})
        spans.append(
            loop.make_span(
                trial_id, counter.next(), "user", observation=utterance
            )
        )
        result = loop.run_turn(
            messages,
            backend,
            conn,
            trial_id=trial_id,
            counter=counter,
            budget=budget,
            max_json_retries=config.max_json_retries,
            guards=config.guards,
        )
        spans.extend(result.spans)
        errors.extend(result.errors)
        final_texts.append(result.final_text)
        termination = result.termination
        context = TurnContext(
            last_final=result.final_text,
            last_termination=result.termination,
            last_tools=tuple(
                span["tool"]
                for span in result.spans
                if span["role"] == "tool" and span["tool"]
            ),
        )
        if result.termination != loop.TERMINATION_FINAL:
            break

    state = db.dump_state(conn)
    verdict = grader.grade(
        state,
        task,
        {
            "termination": termination,
            "errors": errors,
            "final_text": "\n".join(text for text in final_texts if text),
            "spans": spans,
        },
    )
    row: Row = {
        "trial_id": trial_id,
        "task_id": task["id"],
        "trial": trial,
        "passed": verdict["passed"],
        "failure_type": verdict["failure_type"],
        "reasons": verdict["reasons"],
        "first_divergence": verdict["first_divergence"],
        "steps": budget.used,
        "tokens": sum(span["tokens"] for span in spans),
        "latency_ms": sum(span["latency_ms"] for span in spans),
        "invalid_calls": len(errors),
    }
    trace.write_jsonl(config.traces_dir / f"{trial_id}.jsonl", spans)
    conn.close()
    return row


def run(config: RunConfig) -> list[Row]:
    tasks = load_tasks(config.tasks_path)
    rows: list[Row] = []
    for task in tasks:
        template = db.build_template(task["initial_db"])
        for trial in range(1, config.k + 1):
            rows.append(_run_trial(task, trial, config, template))
        template.close()
    rows.sort(key=lambda row: (row["task_id"], row["trial"]))
    trace.write_jsonl(config.results_dir / "result_rows.jsonl", rows)
    return rows
