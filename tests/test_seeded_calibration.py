"""W2.5 校准验收：pass^k 首次具备区分度，且测量链路仍然可信。

这里断言的是「尺子能分辨偶发失败」，不是「性能提升」：
seeded 是人为注入噪声的模拟器，数字不构成任何模型能力结论。
"""
from __future__ import annotations

import io
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from eval import analyze
from eval import config as exp_config
from eval import run as cli
from eval import runner, trace
from runtime.model import BackendConfig

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"
CALIBRATION = ROOT / "experiments" / "calibration.toml"
BASELINE_ROWS = ROOT / "experiments" / "baseline" / "result_rows.jsonl"


def calibration_backend() -> BackendConfig:
    cfg = exp_config.load_config(CALIBRATION)
    return BackendConfig(
        name="seeded", seed=cfg.seed, fault_per_mille=cfg.fault_per_mille
    )


def run_seeded(base: Path, k: int = 3) -> list[dict]:
    return runner.run(
        runner.RunConfig(
            tasks_path=TASKS,
            k=k,
            backend=calibration_backend(),
            results_dir=base / "results",
            traces_dir=base / "traces",
        )
    )


class TestCalibrationConfig(unittest.TestCase):
    def test_calibration_config_exists_and_is_seeded(self):
        cfg = exp_config.load_config(CALIBRATION)
        self.assertEqual(cfg.backend, "seeded")
        self.assertEqual(cfg.name, "calibration_seeded")
        self.assertGreater(cfg.fault_per_mille, 0)
        self.assertLess(cfg.fault_per_mille, 1000)
        self.assertEqual(cfg.k, 3)

    def test_calibration_run_id_differs_from_frozen_baseline(self):
        self.assertNotEqual(
            exp_config.run_id(exp_config.load_config(CALIBRATION)),
            exp_config.run_id(exp_config.load_config()),
        )


class TestPassKDiscriminates(unittest.TestCase):
    def test_passk_is_strictly_below_pass1(self):
        """核心验收：k 次重复开始暴露「偶尔成功」。"""
        with TemporaryDirectory() as d:
            rows = run_seeded(Path(d))
            summary = analyze.summarize(rows, 3)
        self.assertEqual(summary["trial_count"], 30)
        self.assertLess(
            summary["passk"],
            summary["pass1"],
            "seeded 后端下 pass^k 必须小于 pass¹，否则校准没有生效",
        )
        self.assertGreater(summary["passk"], 0, "不应退化成全部失败这种无信息量结果")

    def test_some_task_has_mixed_outcomes(self):
        """同一任务的 k 次序列里必须同时出现通过与失败。"""
        with TemporaryDirectory() as d:
            rows = run_seeded(Path(d))
            summary = analyze.summarize(rows, 3)
        mixed = [
            item["task_id"]
            for item in summary["per_task"]
            if 0 < item["sequence"].count("1") < len(item["sequence"])
        ]
        self.assertTrue(mixed, f"没有任何任务出现结果分化：{summary['per_task']}")

    def test_results_differ_from_frozen_baseline(self):
        with TemporaryDirectory() as d:
            rows = run_seeded(Path(d))
        frozen = trace.read_jsonl(BASELINE_ROWS)
        self.assertNotEqual(
            [(r["trial_id"], r["passed"], r["failure_type"]) for r in rows],
            [(r["trial_id"], r["passed"], r["failure_type"]) for r in frozen],
        )

    def test_failure_attribution_fields_are_complete(self):
        """W2 的归因链路在注入故障下依然能给出证据。

        intent_misread / unknown 按 W2 口径允许没有可定位的偏离点，
        但必须在 reasons 里如实标注 evidence:none，不能假装找到了。
        """
        with TemporaryDirectory() as d:
            rows = run_seeded(Path(d))
        failures = [r for r in rows if not r["passed"]]
        self.assertTrue(failures)
        self.assertTrue(
            all(r["reasons"] for r in failures), "失败 trial 必须带 reasons"
        )
        unlocated = [r for r in failures if r["first_divergence"] is None]
        for row in unlocated:
            with self.subTest(trial=row["trial_id"]):
                self.assertIn("evidence:none", row["reasons"])
                self.assertIn(
                    row["failure_type"], ("intent_misread", "unknown")
                )
        located = len(failures) - len(unlocated)
        self.assertGreater(
            located / len(failures), 0.8,
            "注入了工具层故障后，绝大多数失败应能定位到首个偏离点",
        )
        kinds = {r["failure_type"] for r in failures}
        self.assertGreaterEqual(
            len(kinds), 4, f"注入故障应让失败分布更丰富，实际只有 {kinds}"
        )

    def test_seeded_outputs_are_byte_identical_across_runs(self):
        """伪随机不等于不可复现：同配置连跑两次产物逐字节一致。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            blobs = []
            for name in ("first", "second"):
                base = root / name
                run_seeded(base)
                blobs.append(
                    {
                        str(p.relative_to(base)): p.read_bytes()
                        for p in sorted(base.rglob("*"))
                        if p.is_file()
                    }
                )
        self.assertEqual(set(blobs[0]), set(blobs[1]))
        self.assertEqual(blobs[0], blobs[1])

    def test_seeded_rows_keep_deterministic_schema(self):
        with TemporaryDirectory() as d:
            rows = run_seeded(Path(d))
        expected = {
            "trial_id", "task_id", "trial", "passed", "failure_type",
            "reasons", "first_divergence", "steps", "tokens", "latency_ms",
            "invalid_calls",
        }
        self.assertTrue(all(set(row) == expected for row in rows))
        self.assertTrue(
            all(row["tokens"] == 0 and row["latency_ms"] == 0 for row in rows)
        )
        self.assertFalse(
            any("time" in key or "uuid" in key for row in rows for key in row)
        )


class TestSeededReportIsHonest(unittest.TestCase):
    def _render_seeded(self) -> str:
        cfg = exp_config.load_config(CALIBRATION)
        with TemporaryDirectory() as d:
            rows = run_seeded(Path(d))
        tasks = runner.load_tasks(exp_config.resolve_input(cfg.tasks))
        baseline = trace.read_jsonl(BASELINE_ROWS) if cfg.compare else None
        return analyze.render_report(
            rows, tasks, cfg, exp_config.run_id(cfg), baseline
        )

    def test_caveats_state_it_is_not_a_capability_result(self):
        text = self._render_seeded()
        for phrase in (
            "确定性伪随机模拟器",
            "首次具备区分度",
            "没有语言理解能力",
            "不构成任何模型能力",
            "不是成本数据",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_report_shows_seed_and_backend(self):
        cfg = exp_config.load_config(CALIBRATION)
        text = self._render_seeded()
        self.assertIn("SeededBackend", text)
        self.assertIn(f"| seed | {cfg.seed} |", text)
        self.assertIn(f"| 故障注入率 | {cfg.fault_per_mille}/1000 |", text)

    def test_reproduce_command_uses_seeded_not_openai(self):
        text = self._render_seeded()
        self.assertIn("--backend seeded", text)
        self.assertIn("--seed 11", text)
        self.assertIn("--fault-per-mille", text)
        self.assertNotIn("--backend openai", text)

    def test_cross_backend_diff_table_has_warning(self):
        text = self._render_seeded()
        self.assertIn("## 与冻结基线对照", text)
        self.assertIn("本次后端与冻结基线不同", text)
        self.assertIn("不能作为 harness 或护栏有效性的因果证据", text)


class TestSeededCLI(unittest.TestCase):
    def test_cli_calibration_run_succeeds(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            with redirect_stdout(io.StringIO()):
                rc = cli.main(
                    [
                        "--config", str(CALIBRATION),
                        "--results-dir", str(base / "results"),
                        "--traces-dir", str(base / "traces"),
                        "--runs-dir", str(base / "runs"),
                    ]
                )
            self.assertEqual(rc, 0)
            cfg = exp_config.load_config(CALIBRATION)
            run_dir = base / "runs" / exp_config.run_id(cfg)
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn("SeededBackend", report)
            self.assertNotIn(str(base), report)

    def test_cli_accepts_seed_overrides(self):
        parser = cli.build_parser()
        args = parser.parse_args(
            ["--backend", "seeded", "--seed", "99", "--fault-per-mille", "500"]
        )
        self.assertEqual((args.backend, args.seed, args.fault_per_mille), ("seeded", 99, 500))

    def test_nondefault_config_is_in_reproduce_command(self):
        """复现命令不带 --config 会算出另一个 run_id，留档必须写全。"""
        with TemporaryDirectory() as d:
            base = Path(d)
            with redirect_stdout(io.StringIO()):
                cli.main(
                    [
                        "--config", str(CALIBRATION),
                        "--results-dir", str(base / "results"),
                        "--traces-dir", str(base / "traces"),
                        "--runs-dir", str(base / "runs"),
                    ]
                )
            rid = exp_config.run_id(exp_config.load_config(CALIBRATION))
            report = (base / "runs" / rid / "report.md").read_text(encoding="utf-8")
        self.assertIn("--config experiments/calibration.toml", report)
        self.assertNotIn(str(CALIBRATION), report)

    def test_default_config_report_has_no_config_flag(self):
        """默认配置的报告保持原样，不因新增参数改变字节。"""
        with TemporaryDirectory() as d:
            base = Path(d)
            with redirect_stdout(io.StringIO()):
                cli.main(
                    [
                        "--results-dir", str(base / "results"),
                        "--traces-dir", str(base / "traces"),
                        "--runs-dir", str(base / "runs"),
                    ]
                )
            rid = exp_config.run_id(exp_config.load_config())
            report = (base / "runs" / rid / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("--config", report)


if __name__ == "__main__":
    unittest.main()
