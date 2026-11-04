"""Harness 单测：批量执行、trial_id 串联、trace 完整性、CLI 端到端。"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from eval import run, runner, trace

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"
SPAN_FIELDS = {
    "trial_id",
    "step",
    "role",
    "tool",
    "args",
    "observation",
    "tokens",
    "latency_ms",
}


def config_for(base: Path, k: int = 3) -> runner.RunConfig:
    return runner.RunConfig(
        tasks_path=TASKS,
        k=k,
        results_dir=base / "results",
        traces_dir=base / "traces",
    )


class TestRunner(unittest.TestCase):
    def test_repeat_count_and_deterministic_ids(self):
        with TemporaryDirectory() as d:
            rows = runner.run(config_for(Path(d), k=3))
            self.assertEqual(len(rows), 30)
            self.assertEqual(len({row["trial_id"] for row in rows}), 30)
            self.assertEqual(rows[0]["trial_id"], "T01-t1")
            self.assertEqual(rows[-1]["trial_id"], "T10-t3")
            passed = sum(row["passed"] for row in rows)
            self.assertGreater(passed, 0)
            self.assertLess(passed, len(rows))
            self.assertEqual(
                {row["failure_type"] for row in rows if not row["passed"]},
                {
                    "policy_violation",
                    "output_format",
                    "wrong_tool",
                    "bad_args",
                    "loop_or_timeout",
                    "intent_misread",
                },
            )

    def test_k_parameter_controls_repeats(self):
        with TemporaryDirectory() as d:
            rows = runner.run(config_for(Path(d), k=2))
            self.assertEqual(len(rows), 20)
            self.assertEqual(sorted({row["trial"] for row in rows}), [1, 2])

    def test_results_rows_are_sorted(self):
        with TemporaryDirectory() as d:
            rows = runner.run(config_for(Path(d), k=3))
            keys = [(row["task_id"], row["trial"]) for row in rows]
            self.assertEqual(keys, sorted(keys))

    def test_trace_files_and_span_integrity(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            rows = runner.run(config_for(base, k=3))
            files = sorted((base / "traces").glob("*.jsonl"))
            self.assertEqual(len(files), 30)
            self.assertEqual(
                {path.stem for path in files},
                {row["trial_id"] for row in rows},
            )
            for path in files:
                spans = trace.read_jsonl(path)
                self.assertTrue(spans)
                self.assertTrue(all(set(span) == SPAN_FIELDS for span in spans))
                self.assertTrue(
                    all(span["trial_id"] == path.stem for span in spans)
                )
                self.assertEqual(
                    [span["step"] for span in spans],
                    list(range(1, len(spans) + 1)),
                )

    def test_multiturn_task_keeps_state_across_turns(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            runner.run(config_for(base, k=1))
            spans = trace.read_jsonl(base / "traces" / "T10-t1.jsonl")
            user_turns = [s for s in spans if s["role"] == "user"]
            self.assertEqual(len(user_turns), 2)
            self.assertEqual(
                [s["step"] for s in user_turns], [1, 5]
            )

    def test_cli_end_to_end(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            report = base / "report.md"
            rc = run.main(
                [
                    "--tasks",
                    str(TASKS),
                    "--k",
                    "3",
                    "--results-dir",
                    str(base / "results"),
                    "--traces-dir",
                    str(base / "traces"),
                    "--report",
                    str(report),
                ]
            )
            self.assertEqual(rc, 0)
            rows = trace.read_jsonl(base / "results" / "result_rows.jsonl")
            self.assertEqual(len(rows), 30)
            text = report.read_text(encoding="utf-8")
            for token in ("pass¹", "pass^3", "| T01 |", "loop_or_timeout"):
                with self.subTest(token=token):
                    self.assertIn(token, text)
