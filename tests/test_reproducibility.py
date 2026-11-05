"""确定性验收：同配置连跑两次，产物逐字节一致（设计文档 §8）。"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from eval import run, runner, trace
from runtime.model import BackendConfig

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"
BASELINE_ROWS = ROOT / "experiments" / "baseline" / "result_rows.jsonl"


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

    def test_seeded_runner_outputs_are_byte_identical(self):
        """伪随机后端同样必须逐字节可复现，否则 W3 对照实验无法归因。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            for name in ("first", "second"):
                runner.run(
                    runner.RunConfig(
                        tasks_path=TASKS,
                        k=3,
                        backend=BackendConfig(
                            name="seeded", seed=11, fault_per_mille=60
                        ),
                        results_dir=root / name / "results",
                        traces_dir=root / name / "traces",
                    )
                )
            first, second = snapshot(root / "first"), snapshot(root / "second")
            self.assertEqual(set(first), set(second))
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 31)

    def test_default_scripted_metrics_still_match_frozen_baseline(self):
        """加第三后端不得改变默认跑批的任何测量结果。

        注意不能断言「结果行文件逐字节一致」：冻结基线是 W1 的 9 字段
        schema，W2 又给结果行加了 reasons / first_divergence 两个归因字段。
        可守门的是所有测量指标逐项一致，以及公共字段的值完全一致。
        """
        with TemporaryDirectory() as d:
            base = Path(d)
            rows = runner.run(
                runner.RunConfig(
                    tasks_path=TASKS,
                    k=3,
                    results_dir=base / "results",
                    traces_dir=base / "traces",
                )
            )
        frozen = trace.read_jsonl(BASELINE_ROWS)
        self.assertEqual(len(rows), len(frozen))
        metrics = (
            "trial_id", "task_id", "trial", "passed", "failure_type",
            "steps", "tokens", "latency_ms", "invalid_calls",
        )
        for before, after in zip(frozen, rows):
            with self.subTest(trial=before["trial_id"]):
                self.assertEqual(
                    [after[key] for key in metrics],
                    [before[key] for key in metrics],
                )

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
