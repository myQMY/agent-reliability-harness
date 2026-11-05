"""确定性验收：同配置连跑两次，产物逐字节一致（设计文档 §8）。"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from eval import run, runner, trace

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"


def snapshot(base: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(base)): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


class TestByteIdentical(unittest.TestCase):
    def test_runner_outputs_are_byte_identical(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            for name in ("first", "second"):
                runner.run(
                    runner.RunConfig(
                        tasks_path=TASKS,
                        k=3,
                        results_dir=root / name / "results",
                        traces_dir=root / name / "traces",
                    )
                )
            first, second = snapshot(root / "first"), snapshot(root / "second")
            self.assertEqual(set(first), set(second))
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 31)

    def test_cli_reports_are_byte_identical(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            reports = []
            for name in ("first", "second"):
                report = root / name / "report.md"
                run.main(
                    [
                        "--tasks",
                        str(TASKS),
                        "--k",
                        "3",
                        "--results-dir",
                        str(root / name / "results"),
                        "--traces-dir",
                        str(root / name / "traces"),
                        "--report",
                        str(report),
                    ]
                )
                reports.append(report.read_bytes())
            self.assertEqual(reports[0], reports[1])

    def test_serialization_is_order_independent(self):
        first = trace.dumps({"b": 1, "a": {"d": 2, "c": 3}})
        second = trace.dumps({"a": {"c": 3, "d": 2}, "b": 1})
        self.assertEqual(first, second)
        self.assertEqual(first, '{"a":{"c":3,"d":2},"b":1}')

    def test_rows_contain_only_deterministic_fields(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            rows = runner.run(
                runner.RunConfig(
                    tasks_path=TASKS,
                    k=1,
                    results_dir=base / "results",
                    traces_dir=base / "traces",
                )
            )
            expected = {
                "trial_id",
                "task_id",
                "trial",
                "passed",
                "failure_type",
                "reasons",
                "first_divergence",
                "steps",
                "tokens",
                "latency_ms",
                "invalid_calls",
            }
            self.assertTrue(all(set(row) == expected for row in rows))
            self.assertTrue(
                all(row["tokens"] == 0 and row["latency_ms"] == 0 for row in rows)
            )
            self.assertFalse(
                any("time" in key or "uuid" in key for row in rows for key in row)
            )
