"""P0 实验记录体系：配置解析、run_id、冻结基线、报告口径、护栏生效。

覆盖「配置 → run_id → 留档报告」这条链路，确保所有字段真实生效、
冻结基线不被覆盖、报告口径随后端切换且不含非确定值。
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from env import db
from eval import analyze
from eval import config as exp_config
from eval import run as cli
from eval import runner, trace
from runtime import guard, loop
from runtime.model import ScriptedBackend

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"
BASELINE_MD = ROOT / "experiments" / "mvp_baseline.md"
BASELINE_ROWS = ROOT / "experiments" / "baseline" / "result_rows.jsonl"

INITIAL = {
    "customers": [],
    "orders": [
        {
            "id": "O-100",
            "customer_id": "C-1",
            "status": "shipped",
            "total_cents": 4200,
            "refunded_cents": 0,
        }
    ],
    "audit_log": [],
}


def cli_args(base: Path) -> list[str]:
    """把所有产物导向临时目录，绝不污染仓库。"""
    return [
        "--results-dir",
        str(base / "results"),
        "--traces-dir",
        str(base / "traces"),
        "--runs-dir",
        str(base / "runs"),
    ]


def quiet_main(argv: list[str]) -> int:
    with redirect_stdout(io.StringIO()):
        return cli.main(argv)


class TestExperimentConfig(unittest.TestCase):
    def test_default_config_matches_frozen_baseline(self):
        cfg = exp_config.load_config()
        self.assertEqual(cfg.tasks, "tasks/mvp_10.jsonl")
        self.assertEqual(cfg.k, 3)
        self.assertEqual(cfg.backend, "scripted")
        self.assertEqual(cfg.max_steps, guard.MAX_STEPS)
        self.assertEqual(cfg.max_json_retries, guard.MAX_JSON_RETRIES)

    def test_every_toml_field_is_applied(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "cfg.toml"
            path.write_text(
                '[experiment]\nname = "ablation_a"\n'
                'tasks = "tasks/mvp_10.jsonl"\nk = 2\nbackend = "scripted"\n'
                'compare = ""\n[guards]\nmax_steps = 7\nmax_json_retries = 1\n',
                encoding="utf-8",
            )
            cfg = exp_config.load_config(path)
        self.assertEqual(
            (cfg.name, cfg.k, cfg.max_steps, cfg.max_json_retries, cfg.compare),
            ("ablation_a", 2, 7, 1, ""),
        )

    def test_unknown_fields_are_rejected(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "bad.toml"
            for body in (
                '[experiment]\ntypo_field = 1\n',
                '[mystery]\nx = 1\n',
                '[seeded]\ntypo_field = 1\n',
            ):
                path.write_text(body, encoding="utf-8")
                with self.subTest(body=body.strip()):
                    with self.assertRaises(ValueError):
                        exp_config.load_config(path)

    def test_seeded_section_is_applied(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "cfg.toml"
            path.write_text(
                '[experiment]\nbackend = "seeded"\n'
                '[seeded]\nseed = 99\nfault_per_mille = 250\n',
                encoding="utf-8",
            )
            cfg = exp_config.load_config(path)
        self.assertEqual((cfg.backend, cfg.seed, cfg.fault_per_mille), ("seeded", 99, 250))

    def test_invalid_values_are_rejected(self):
        base = exp_config.load_config()
        for changes in (
            {"k": 0},
            {"backend": "gemini"},
            {"name": "../escape"},
            {"max_steps": 0},
            {"max_json_retries": -1},
            {"tasks": ""},
            {"seed": -1},
            {"fault_per_mille": -1},
            {"fault_per_mille": 1001},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    exp_config.with_overrides(base, **changes)


class TestRunId(unittest.TestCase):
    FROZEN_RID = "baseline_scripted-a346e8acef15"
    FROZEN_SNAPSHOT = (
        '{"config":{"backend":"scripted","compare":'
        '"experiments/baseline/result_rows.jsonl","k":3,'
        '"max_json_retries":2,"max_steps":12,"name":'
        '"baseline_scripted","tasks":"tasks/mvp_10.jsonl"},'
        '"run_id":"baseline_scripted-a346e8acef15"}'
    )

    def test_default_run_id_is_frozen(self):
        """W2.5 加 seed 字段不得改变 W1 冻结留档的 run_id。"""
        cfg = exp_config.load_config()
        self.assertEqual(exp_config.run_id(cfg), self.FROZEN_RID)

    def test_default_config_snapshot_is_frozen(self):
        """config.json 快照逐字节不变，否则默认归档目录会被悄悄改写。"""
        cfg = exp_config.load_config()
        self.assertEqual(
            trace.dumps(exp_config.config_snapshot(cfg, self.FROZEN_RID)),
            self.FROZEN_SNAPSHOT,
        )

    def test_seeded_parameters_change_run_id(self):
        base = exp_config.with_overrides(
            exp_config.load_config(), backend="seeded", seed=1, fault_per_mille=100
        )
        ids = {exp_config.run_id(base)}
        ids.add(exp_config.run_id(exp_config.with_overrides(base, seed=2)))
        ids.add(exp_config.run_id(exp_config.with_overrides(base, fault_per_mille=200)))
        self.assertEqual(len(ids), 3)
        self.assertIn("seed", exp_config._core(base))
        self.assertNotIn("seed", exp_config._core(exp_config.load_config()))

    def test_stable_and_path_safe(self):
        cfg = exp_config.load_config()
        rid = exp_config.run_id(cfg)
        self.assertEqual(rid, exp_config.run_id(cfg))
        self.assertTrue(rid.startswith(cfg.name + "-"))
        self.assertNotIn("/", rid)
        self.assertNotIn("\\", rid)
        self.assertNotIn(":", rid)

    def test_any_field_change_changes_run_id(self):
        base = exp_config.load_config()
        ids = {exp_config.run_id(base)}
        for changes in (
            {"k": 4},
            {"max_steps": 8},
            {"max_json_retries": 0},
            {"backend": "openai"},
            {"name": "ablation_b"},
        ):
            ids.add(exp_config.run_id(exp_config.with_overrides(base, **changes)))
        self.assertEqual(len(ids), 6)

    def test_cli_overlay_keeps_same_run_id_as_config(self):
        """验收命令显式传同义参数，不应与纯配置模式算出两个 run_id。"""
        base = exp_config.load_config()
        overlaid = exp_config.with_overrides(
            base, tasks="tasks/mvp_10.jsonl", k=3
        )
        self.assertEqual(exp_config.run_id(base), exp_config.run_id(overlaid))


class TestFrozenBaseline(unittest.TestCase):
    def test_baseline_rows_are_frozen(self):
        self.assertTrue(BASELINE_ROWS.is_file())
        rows = trace.read_jsonl(BASELINE_ROWS)
        self.assertEqual(len(rows), 30)
        self.assertEqual(sum(row["passed"] for row in rows), 6)

    def test_default_run_never_writes_baseline_report(self):
        before = BASELINE_MD.read_bytes()
        with TemporaryDirectory() as d:
            self.assertEqual(quiet_main(cli_args(Path(d))), 0)
        self.assertEqual(BASELINE_MD.read_bytes(), before)

    def test_explicit_report_to_baseline_is_refused(self):
        before = BASELINE_MD.read_bytes()
        with TemporaryDirectory() as d:
            base = Path(d)
            argv = cli_args(base)[:4] + ["--report", str(BASELINE_MD)]
            with self.assertRaises(SystemExit):
                quiet_main(argv)
            self.assertFalse((base / "results").exists(), "拒绝应在跑批之前发生")
        self.assertEqual(BASELINE_MD.read_bytes(), before)


class TestReport(unittest.TestCase):
    def _render(self, backend: str, baseline: list[dict] | None) -> str:
        cfg = exp_config.with_overrides(exp_config.load_config(), backend=backend)
        tasks = runner.load_tasks(exp_config.resolve_input(cfg.tasks))
        rows = trace.read_jsonl(BASELINE_ROWS)
        return analyze.render_report(
            rows, tasks, cfg, exp_config.run_id(cfg), baseline
        )

    def test_scripted_caveats(self):
        text = self._render("scripted", None)
        self.assertIn("必然相等", text)
        self.assertIn("不是成本数据", text)
        self.assertNotIn("不保证逐字节可复现", text)

    def test_openai_caveats_are_honest(self):
        text = self._render("openai", None)
        self.assertIn("不保证逐字节可复现", text)
        self.assertIn("来自响应的 `usage`", text)
        self.assertIn("不参与判分", text)
        self.assertNotIn("尚未接入 usage 观测", text)
        self.assertIn("--backend openai", text)
        self.assertNotIn("必然相等", text)

    def test_seeded_caveats_are_honest(self):
        text = self._render("seeded", None)
        for phrase in (
            "确定性伪随机模拟器",
            "首次具备区分度",
            "没有语言理解能力",
            "不构成任何模型能力",
            "不是成本数据",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertNotIn("--backend openai", text)
        self.assertIn("--backend seeded", text)

    def test_cross_backend_diff_has_warning(self):
        rows = trace.read_jsonl(BASELINE_ROWS)
        text = self._render("seeded", rows)
        self.assertIn("## 与冻结基线对照", text)
        self.assertIn("本次后端与冻结基线不同", text)
        self.assertIn("不能作为 harness 或护栏有效性的因果证据", text)

    def test_scripted_diff_has_no_cross_backend_warning(self):
        rows = trace.read_jsonl(BASELINE_ROWS)
        self.assertNotIn(
            "本次后端与冻结基线不同", self._render("scripted", rows)
        )

    def test_diff_table_on_identical_rows(self):
        rows = trace.read_jsonl(BASELINE_ROWS)
        text = self._render("scripted", rows)
        self.assertIn("## 与冻结基线对照", text)
        self.assertIn("| 冻结基线 | 本次 | 变化 |", text)
        self.assertIn("| pass¹ | 0.20 | 0.20 | +0.00 |", text)
        self.assertIn("| policy_violation | 9 | 9 | +0 |", text)

    def test_no_diff_table_without_baseline(self):
        self.assertNotIn("## 与冻结基线对照", self._render("scripted", None))


class TestConfiguredGuards(unittest.TestCase):
    def test_max_steps_from_config_is_applied(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            rows = runner.run(
                runner.RunConfig(
                    tasks_path=TASKS,
                    k=1,
                    results_dir=base / "results",
                    traces_dir=base / "traces",
                    max_steps=2,
                )
            )
        self.assertTrue(all(row["steps"] <= 2 for row in rows))
        self.assertIn("loop_or_timeout", {row["failure_type"] for row in rows})

    def test_max_json_retries_is_applied_to_loop(self):
        template = db.build_template(INITIAL)
        conn = db.reset(template)

        def run(retries: int) -> loop.TurnResult:
            return loop.run_turn(
                [],
                ScriptedBackend(["bad"]),
                conn,
                trial_id="T-t1",
                counter=loop.StepCounter(),
                budget=guard.StepBudget(max_steps=10),
                max_json_retries=retries,
            )

        zero = run(0)
        default = run(guard.MAX_JSON_RETRIES)
        conn.close()
        template.close()
        self.assertEqual(zero.termination, loop.TERMINATION_JSON_FAILURE)
        self.assertEqual(len(zero.spans), 2)
        self.assertEqual(len(default.spans), 6)


class TestRunArchive(unittest.TestCase):
    def test_archive_mode_writes_config_and_report(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            self.assertEqual(quiet_main(cli_args(base)), 0)
            rid = exp_config.run_id(exp_config.load_config())
            run_dir = base / "runs" / rid
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            snapshot = json.loads(
                (run_dir / "config.json").read_text(encoding="utf-8")
            )
        self.assertEqual(snapshot["run_id"], rid)
        self.assertEqual(snapshot["config"]["k"], 3)
        self.assertIn(rid, report.splitlines()[0])
        self.assertNotIn(str(base), report)

    def test_archive_reports_are_byte_identical(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            reports = []
            for name in ("first", "second"):
                base = root / name
                self.assertEqual(quiet_main(cli_args(base)), 0)
                rid = exp_config.run_id(exp_config.load_config())
                reports.append((base / "runs" / rid / "report.md").read_bytes())
        self.assertEqual(reports[0], reports[1])


if __name__ == "__main__":
    unittest.main()
