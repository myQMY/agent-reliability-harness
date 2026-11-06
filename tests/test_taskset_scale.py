"""W3 测量标尺升级（``tasks/mvp_30.jsonl`` + ``eval/taskset_scale.py``）的验收。

钉死七件事：

1. **既有任务集逐字节不变**：``mvp_10.jsonl`` 的 sha256 固定，且逐字节等于
   ``mvp_30.jsonl`` 的前 10 行；生成器构造规则与产物完全一致；
2. **新增任务的脚本确实正确**：全部是 dict 动作、工具名合法、金额不超退，
   且在 ``ScriptedBackend`` 下 k 次全通过（证明 ``target_db`` / 策略检查 /
   必要告知与脚本一致，没有靠放宽判分换数字）；
3. **分辨率数学**：步进恒为 1/N、与 k 无关；纯函数拒绝缺格静默补零；
   信息位点由证据自动推导、源码不硬编码任务 ID；
4. **正式证据结论**与既有 ``ceiling`` / ``resolution`` / ``robustness`` 三份
   归档报告逐项交叉验证，特别是 ``off→schema`` 的**单变量直接差**与
   ``w3_robustness.md`` 的**2×2 主效应**不是同一个量；
5. **口径边界**：报告写明任务集是第三自变量、分数不可跨分母相减、
   恒通过位点不算功劳、不外推真实模型；
6. **逐字节可复现**：CLI 连跑两次、``--from-jsonl`` 只读重渲染；
7. **零侵入**：既有 8 个 ``run_id`` / 归档报告 / 冻结基线 / 版本化常量不变。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from env import tools
from eval import analyze, attribution, ceiling, guard_matrix, resolution
from eval import config as exp_config
from eval import robustness, runner, taskset_scale as scale
from runtime import guard
from runtime.model import BackendConfig

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks"
EXPERIMENTS = ROOT / "experiments"
MVP10 = TASKS / "mvp_10.jsonl"
MVP30 = TASKS / "mvp_30.jsonl"
BUILDER = TASKS / "build_mvp_30.py"
MODULE_SOURCE = ROOT / "eval" / "taskset_scale.py"
EVIDENCE = EXPERIMENTS / scale.EVIDENCE_NAME
REPORT = EXPERIMENTS / scale.REPORT_NAME

# 既有 10 条任务集的固定指纹：任何改动都会让 W1/W2/W3 全部留档失去可比性。
MVP10_SHA256 = "0a3e2af34af872f1c26e976585b961f86458b1ec48368134bc5229bde26b62da"

EXPECTED_RECORD_COUNT = 182          # 2×3×30 + 2 行 ScriptedBackend 参考基线
EXPECTED_SEED_COUNT = 30
EXPECTED_V1_TASK_COUNT = 10
EXPECTED_V2_TASK_COUNT = 30
EXPECTED_V1_LEVELS = 3
EXPECTED_V2_LEVELS = 10
EXPECTED_V1_INFORMATIVE = 3
EXPECTED_V2_INFORMATIVE = 19
EXPECTED_V2_CONSTANT_PASS = 4
EXPECTED_V1_WEAK = {"positive": 4, "zero": 26, "negative": 0}
EXPECTED_V2_WEAK = {"positive": 27, "zero": 3, "negative": 0}
EXPECTED_ALL4_V1 = 0.3000
EXPECTED_ALL4_V2 = 23 / 30

EXISTING_RUNS = (
    ("experiments/config.toml", "baseline_scripted-a346e8acef15"),
    ("experiments/calibration.toml", "calibration_seeded-ea22719ba7c2"),
    ("experiments/guard_ab_off.toml", "w3_ab_off-346ab5e98382"),
    ("experiments/guard_ab_on.toml", "w3_ab_on-fa17143eb761"),
    ("experiments/guard_ab_idem.toml", "w3_idem_on-a4e1925e1619"),
    ("experiments/guard_ab_both.toml", "w3_both_on-3c15f3bd94fd"),
    ("experiments/guard_ab_retry.toml", "w3_retry_on-5c178cab56a0"),
    ("experiments/guard_ab_json_recovery.toml",
     "w3_json_recovery_on-c507312a3e9a"),
)

HISTORICAL_REPORTS = (
    "w3_robustness.md",
    "w3_tool_retry_sweep.md",
    "w3_json_recovery_sweep.md",
    "w3_ceiling.md",
)
OLD_WORDING = "扩大任务集或提高 k"


def load_evidence() -> list[dict]:
    return robustness.load_records(EVIDENCE.read_text(encoding="utf-8"))


def load_tasks(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestTaskSetFixture(unittest.TestCase):
    def test_existing_task_set_is_byte_identical(self):
        self.assertEqual(
            hashlib.sha256(MVP10.read_bytes()).hexdigest(), MVP10_SHA256
        )
        self.assertEqual(len(load_tasks(MVP10)), EXPECTED_V1_TASK_COUNT)

    def test_v2_prefix_is_literally_the_v1_bytes(self):
        self.assertTrue(MVP30.read_bytes().startswith(MVP10.read_bytes()))
        self.assertEqual(
            MVP30.read_text(encoding="utf-8").splitlines()[:10],
            MVP10.read_text(encoding="utf-8").splitlines(),
        )

    def test_v2_has_thirty_unique_ordered_ids(self):
        tasks = load_tasks(MVP30)
        self.assertEqual(len(tasks), EXPECTED_V2_TASK_COUNT)
        self.assertEqual(
            [task["id"] for task in tasks],
            [f"T{index:02d}" for index in range(1, 31)],
        )
        for task in tasks:
            self.assertIn("initial_db", task)
            self.assertIn("target_db", task)
            self.assertIn("policy_checks", task)
            self.assertIn("required_disclosures", task)
            self.assertIn("script", task)
            self.assertIn("user_utterances", task)

    def test_builder_matches_the_versioned_fixture(self):
        spec = importlib.util.spec_from_file_location("build_mvp_30", BUILDER)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        self.assertEqual(module.build_bytes(), MVP30.read_bytes())

    def test_new_tasks_have_correct_scripts_and_legal_arguments(self):
        tasks = load_tasks(MVP30)[EXPECTED_V1_TASK_COUNT:]
        self.assertEqual(len(tasks), 20)
        for task in tasks:
            with self.subTest(task=task["id"]):
                self.assertTrue(task["script"])
                for item in task["script"]:
                    self.assertIsInstance(item, dict)
                    if "tool" in item:
                        self.assertIn(item["tool"], tools.TOOL_SCHEMAS)
                        self.assertIsInstance(item.get("args"), dict)
                self.assertTrue(
                    isinstance(task["script"][-1], dict)
                    and "final" in task["script"][-1],
                    "脚本必须以 final 正常结束，不能是死循环",
                )
                totals = {
                    row["id"]: row["total_cents"]
                    for row in task["initial_db"]["orders"]
                }
                for row in task["target_db"].get("orders", ()):
                    self.assertLessEqual(
                        row.get("refunded_cents", 0), totals[row["id"]]
                    )

    def test_new_tasks_all_pass_under_the_scripted_backend(self):
        """最强的守门：脚本正确 + target_db 一致 + 判分口径没被放宽。"""
        old_ids = {task["id"] for task in load_tasks(MVP10)}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            rows = runner.run(
                runner.RunConfig(
                    tasks_path=MVP30,
                    k=3,
                    backend=BackendConfig(),
                    results_dir=out / "results",
                    traces_dir=out / "traces",
                )
            )
        new_rows = [row for row in rows if row["task_id"] not in old_ids]
        self.assertEqual(len(new_rows), 20 * 3)
        self.assertEqual(
            [(row["task_id"], row["trial"], row["failure_type"])
             for row in new_rows if row["passed"] != 1],
            [],
        )


class TestPureFunctions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_evidence()

    def test_nominal_step_depends_only_on_task_count(self):
        self.assertAlmostEqual(scale.nominal_step(10), 0.10, places=9)
        self.assertAlmostEqual(scale.nominal_step(30), 1 / 30, places=9)
        with self.assertRaises(ValueError):
            scale.nominal_step(0)
        # 与 k 无关：换 k 不改 passk 的定义
        rows = [
            {"task_id": f"T{i}", "trial": 1, "passed": 1 if i < 3 else 0,
             "failure_type": "unknown", "steps": 1, "invalid_calls": 0,
             "tokens": 0, "latency_ms": 0}
            for i in range(10)
        ]
        self.assertEqual(analyze.summarize(rows, 1)["passk"], 0.3)
        self.assertEqual(analyze.summarize(rows, 8)["passk"], 0.3)

    def test_collect_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            scale.collect((11,), k=0)
        with self.assertRaises(ValueError):
            scale.collect((11,), fault_per_mille=1001)
        with self.assertRaises(ValueError):
            scale.collect(())
        with self.assertRaises(ValueError):
            scale.collect((11,), tasksets=(("a", "tasks/mvp_10.jsonl"),
                                           ("a", "tasks/mvp_30.jsonl")))

    def test_summaries_refuse_missing_cells(self):
        truncated = [
            row for row in self.records
            if not (row["taskset"] == scale.V2 and row["cell"] == "all4"
                    and row["seed"] == 11)
        ]
        with self.assertRaises(ValueError):
            scale.cell_summary(truncated, scale.V2, "all4")
        # per_seed_delta 只配 off/schema 两格，删 all4 不影响它；
        # 必须单独构造一个 off 缺格的证据，才算真的测到拒绝静默补零。
        truncated_off = [
            row for row in self.records
            if not (row["taskset"] == scale.V2 and row["cell"] == "off"
                    and row["seed"] == 11)
        ]
        with self.assertRaises(ValueError):
            scale.per_seed_delta(truncated_off, scale.V2, "off", "schema")
        with self.assertRaises(ValueError):
            scale.attainable_levels([], scale.V2)
        with self.assertRaises(ValueError):
            scale.per_seed_delta(self.records, scale.V1, "off", "not_a_cell")

    def test_information_sites_are_derived_not_hardcoded(self):
        sites = scale.information_sites(self.records, scale.V2)
        self.assertEqual(sites["total"], EXPECTED_V2_TASK_COUNT)
        self.assertEqual(
            sites["total"],
            len(sites["informative"]) + len(sites["constant_fail"])
            + len(sites["constant_pass"]),
        )
        helpers_only = MODULE_SOURCE.read_text(encoding="utf-8").split(
            "def render_report"
        )[0]
        for task_id in ("T02", "T06", "T23", "T26", "T30"):
            self.assertNotIn(f'"{task_id}"', helpers_only)

    def test_scripted_reference_exposes_integer_hits(self):
        reference = scale.scripted_reference(self.records)
        self.assertEqual(reference[scale.V1]["hits"], 2)
        self.assertEqual(reference[scale.V2]["hits"], 22)
        self.assertEqual(
            reference[scale.V2]["hits"] - reference[scale.V1]["hits"], 20
        )
        self.assertNotEqual(
            reference[scale.V1]["task_count"], reference[scale.V2]["task_count"]
        )


class TestEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_evidence()

    def test_complete_two_by_three_by_thirty_grid_plus_reference(self):
        self.assertEqual(len(self.records), EXPECTED_RECORD_COUNT)
        self.assertEqual(len(scale.seed_values(self.records)), EXPECTED_SEED_COUNT)
        self.assertEqual(
            tuple(scale.seed_values(self.records)),
            tuple(sorted(robustness.DEFAULT_SEEDS)),
        )
        self.assertEqual(scale.task_names(self.records), scale.TASKSET_NAMES)
        grid = {
            (row["taskset"], row["cell"], int(row["seed"]))
            for row in self.records
            if row["cell"] in scale.CELL_NAMES
        }
        self.assertEqual(len(grid), 2 * 3 * EXPECTED_SEED_COUNT)

    def test_guards_match_cells_and_measurement_inputs_are_shared(self):
        expected = {
            "off": (),
            "schema": (guard.GUARD_SCHEMA_VALIDATION,),
            "all4": tuple(scale.ALL4),
        }
        for row in self.records:
            if row["cell"] in expected:
                self.assertEqual(tuple(row["guards"]), expected[row["cell"]])
            self.assertEqual(row["trial_count"], row["task_count"] * 3)

    def test_every_passk_sits_on_its_own_one_over_n_grid(self):
        for row in self.records:
            step = 1 / row["task_count"]
            quotient = row["passk"] / step
            with self.subTest(taskset=row["taskset"], cell=row["cell"],
                              seed=row["seed"]):
                self.assertAlmostEqual(quotient, round(quotient), places=8)
            hits = sum(bool(item["all_pass"]) for item in row["per_task"])
            self.assertEqual(hits, row["passk_hits"])
            self.assertEqual(len(row["per_task"]), row["task_count"])


class TestFindings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_evidence()
        cls.summary = scale.scale_summary(cls.records)
        cls.weak = scale.weak_effect_summary(cls.records)

    def test_nominal_step_and_attainable_levels(self):
        old, new = self.summary[scale.V1], self.summary[scale.V2]
        self.assertAlmostEqual(old["step"], 0.10, places=9)
        self.assertAlmostEqual(new["step"], 1 / 30, places=9)
        self.assertEqual(old["level_count"], EXPECTED_V1_LEVELS)
        self.assertEqual(new["level_count"], EXPECTED_V2_LEVELS)
        self.assertGreater(new["level_count"], old["level_count"])
        self.assertEqual(old["informative"], EXPECTED_V1_INFORMATIVE)
        self.assertEqual(new["informative"], EXPECTED_V2_INFORMATIVE)
        self.assertEqual(
            new["constant_pass"], EXPECTED_V2_CONSTANT_PASS
        )
        self.assertEqual(new["constant_fail"], old["constant_fail"])

    def test_all4_matches_both_existing_ceilings(self):
        self.assertAlmostEqual(
            self.summary[scale.V1]["cells"]["all4"]["passk_mean"],
            EXPECTED_ALL4_V1, places=9,
        )
        self.assertAlmostEqual(
            self.summary[scale.V2]["cells"]["all4"]["passk_mean"],
            EXPECTED_ALL4_V2, places=9,
        )

    def test_weak_effect_sign_distributions(self):
        for name, expected in (
            (scale.V1, EXPECTED_V1_WEAK), (scale.V2, EXPECTED_V2_WEAK)
        ):
            signs = self.weak[name]["signs"]
            with self.subTest(taskset=name):
                for key, value in expected.items():
                    self.assertEqual(signs[key], value)
        self.assertGreater(
            self.weak[scale.V2]["signs"]["positive"],
            self.weak[scale.V1]["signs"]["positive"],
        )
        self.assertGreater(
            len(self.weak[scale.V2]["levels"]),
            len(self.weak[scale.V1]["levels"]),
        )

    def test_v1_weak_effect_cross_checks_with_resolution_evidence(self):
        """与 w3_resolution.md 的 k=3 off→schema 单变量直接差逐项一致。"""
        resolution_records = robustness.load_records(
            (EXPERIMENTS / resolution.EVIDENCE_NAME).read_text(
                encoding="utf-8"
            )
        )
        funnel = resolution.funnel_summary(
            resolution_records, 3, resolution.SINGLE
        )
        self.assertEqual(
            len(funnel["zero_effect_seeds"]),
            self.weak[scale.V1]["signs"]["zero"],
        )
        self.assertAlmostEqual(
            funnel["delta_mean"],
            self.weak[scale.V1]["signs"]["mean"], places=9,
        )
        self.assertIn(
            "26 个零效应 seed",
            (EXPERIMENTS / "w3_resolution.md").read_text(encoding="utf-8"),
        )

    def test_robustness_main_effect_is_a_different_quantity(self):
        """w3_robustness.md 的正 5 / 零 25 是 2×2 主效应，不可直接比对。"""
        robustness_records = robustness.load_records(
            (EXPERIMENTS / robustness.EVIDENCE_NAME).read_text(
                encoding="utf-8"
            )
        )
        dist = robustness.distribution(
            robustness.per_seed_effects(robustness_records)
        )
        self.assertEqual(dist["schema"]["positive"], 5)
        self.assertEqual(dist["schema"]["zero"], 25)
        self.assertNotEqual(
            dist["schema"]["positive"],
            self.weak[scale.V1]["signs"]["positive"],
        )

    def test_intrinsic_failure_set_agrees_with_ceiling_analysis(self):
        ceiling_records = robustness.load_records(
            (EXPERIMENTS / ceiling.EVIDENCE_NAME).read_text(
                encoding="utf-8"
            )
        )
        from_ceiling = {
            item["task_id"] for item in ceiling.unrecoverable_tasks(
                ceiling_records
            )
        }
        self.assertEqual(
            from_ceiling,
            set(self.summary[scale.V1]["sites"]["constant_fail"]),
        )
        self.assertEqual(len(from_ceiling), 7)

    def test_attribution_shared_cells_still_agree(self):
        attribution_records = robustness.load_records(
            (EXPERIMENTS / attribution.EVIDENCE_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertAlmostEqual(
            attribution.cell_mean(
                attribution_records, "off", robustness.DEFAULT_FAULT_PER_MILLE
            ),
            self.summary[scale.V1]["cells"]["off"]["passk_mean"], places=9,
        )
        self.assertAlmostEqual(
            attribution.cell_mean(
                attribution_records, "all4", robustness.DEFAULT_FAULT_PER_MILLE
            ),
            self.summary[scale.V1]["cells"]["all4"]["passk_mean"], places=9,
        )


class TestReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = REPORT.read_text(encoding="utf-8")

    def test_archived_report_matches_current_render(self):
        self.assertEqual(
            scale.render_report(load_evidence()).encode("utf-8"),
            REPORT.read_bytes(),
        )

    def test_report_states_the_core_scale_claim(self):
        for phrase in (
            "v2 扩展任务集", "名义步进", "实测可达档位数",
            "携带信息的位点", "尺子灵敏度确实提高", "恒通过位点",
            "不携带变化信息", "新增的 20 条里真正贡献可翻转档位的是 16 条",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_report_states_every_caveat(self):
        for phrase in (
            "任务集是第三个自变量", "分数不可直接相减", "不可混用",
            "新任务脚本正确 ≠ 任务更容易", "仍是二值化指标",
            "不是显著性检验", "不外推真实模型", "不替代 W4",
            "版本化 fixture", "2×2 四格主效应", "两处符号分布不可直接比对",
            "没有把这些恒通过位点算作功劳",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_report_refuses_empty_evidence(self):
        with self.assertRaises(ValueError):
            scale.render_report([])

    def test_small_self_check_does_not_claim_thirty_seeds(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with redirect_stdout(io.StringIO()):
                scale.main(["--seeds", "11,1,2", "--out-dir", str(out)])
            text = (out / scale.REPORT_NAME).read_text(encoding="utf-8")
        self.assertIn("3 个版本化 seed", text)
        self.assertIn("= 18 行评测证据", text)
        self.assertNotIn("30 个版本化 seed", text)


class TestReproducibility(unittest.TestCase):
    def test_cli_produces_identical_artifacts_twice(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            artifacts = []
            for index in (1, 2):
                out = root / str(index)
                with redirect_stdout(io.StringIO()):
                    scale.main(["--seeds", "11,1,2", "--out-dir", str(out)])
                artifacts.append((
                    (out / scale.EVIDENCE_NAME).read_bytes(),
                    (out / scale.REPORT_NAME).read_bytes(),
                ))
            self.assertEqual(artifacts[0], artifacts[1])

            readonly = root / "readonly"
            readonly.mkdir()
            (readonly / scale.EVIDENCE_NAME).write_bytes(artifacts[0][0])
            with redirect_stdout(io.StringIO()):
                scale.main(["--from-jsonl", "--out-dir", str(readonly)])
            self.assertEqual(
                (readonly / scale.REPORT_NAME).read_bytes(),
                artifacts[0][1],
            )

    def test_from_jsonl_refuses_missing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    scale.main(["--from-jsonl", "--out-dir", str(Path(d))])

    def test_repository_evidence_renders_the_archived_report(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            (target / scale.EVIDENCE_NAME).write_bytes(EVIDENCE.read_bytes())
            with redirect_stdout(io.StringIO()):
                scale.main(["--from-jsonl", "--out-dir", str(target)])
            self.assertEqual(
                (target / scale.REPORT_NAME).read_bytes(), REPORT.read_bytes()
            )


class TestZeroInvasiveness(unittest.TestCase):
    def test_existing_run_ids_are_untouched(self):
        for filename, expected in EXISTING_RUNS:
            with self.subTest(config=filename):
                self.assertEqual(
                    exp_config.run_id(
                        exp_config.load_config(ROOT / filename)
                    ),
                    expected,
                )

    def test_existing_archived_reports_still_render_identically(self):
        self.assertEqual(
            (EXPERIMENTS / "w3_guard_matrix.md").read_bytes(),
            guard_matrix.render_matrix(
                guard_matrix.build_matrix()
            ).encode("utf-8"),
        )
        self.assertEqual(
            (EXPERIMENTS / robustness.REPORT_NAME).read_bytes(),
            robustness.render_sweep(
                robustness.load_records(
                    (EXPERIMENTS / robustness.EVIDENCE_NAME).read_text(
                        encoding="utf-8"
                    )
                )
            ).encode("utf-8"),
        )
        for module in (ceiling, resolution, attribution):
            report = EXPERIMENTS / module.REPORT_NAME
            with self.subTest(report=report.name):
                self.assertEqual(
                    report.read_bytes(),
                    module.render_report(
                        robustness.load_records(
                            (EXPERIMENTS / module.EVIDENCE_NAME).read_text(
                                encoding="utf-8"
                            )
                        )
                    ).encode("utf-8"),
                )

    def test_existing_modules_still_point_at_the_v1_task_set(self):
        # robustness.py 自己定义 DEFAULT_TASKS，不会写成 robustness.DEFAULT_TASKS。
        self.assertIn(
            'DEFAULT_TASKS = "tasks/mvp_10.jsonl"',
            (ROOT / "eval" / "robustness.py").read_text(encoding="utf-8"),
        )
        for name in ("ceiling", "resolution", "attribution"):
            source = (ROOT / "eval" / f"{name}.py").read_text(
                encoding="utf-8"
            )
            with self.subTest(module=name):
                self.assertIn("robustness.DEFAULT_TASKS", source)
        self.assertEqual(robustness.DEFAULT_TASKS, "tasks/mvp_10.jsonl")

    def test_historical_reports_keep_the_old_wording_verbatim(self):
        for name in HISTORICAL_REPORTS:
            text = (EXPERIMENTS / name).read_text(encoding="utf-8")
            with self.subTest(report=name):
                self.assertIn(OLD_WORDING, text)

    def test_frozen_baseline_files_are_still_present(self):
        for relative in (
            "experiments/mvp_baseline.md",
            "experiments/baseline/result_rows.jsonl",
        ):
            path = ROOT / relative
            with self.subTest(path=relative):
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)


class TestModuleContract(unittest.TestCase):
    def test_source_is_clock_free_and_dependency_free(self):
        source = MODULE_SOURCE.read_text(encoding="utf-8")
        for banned in (
            "datetime", "time.time", "import time", "import random",
            "uuid", "os.environ", "requests", "numpy", "pandas",
        ):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)
        self.assertIn("TemporaryDirectory", source)
        self.assertIn("--from-jsonl", source)

    def test_renderer_has_no_hardcoded_conclusion_numbers(self):
        renderer = MODULE_SOURCE.read_text(encoding="utf-8").split(
            "def render_report"
        )[1]
        for literal in (
            "0.7667", "0.7333", "0.5600", "0.6200", "0.4333",
            "0.0333", "0.3000",
        ):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, renderer)

    def test_measurement_inputs_are_reused_not_duplicated(self):
        source = MODULE_SOURCE.read_text(encoding="utf-8")
        for phrase in (
            "robustness.DEFAULT_K", "robustness.DEFAULT_SEEDS",
            "robustness.DEFAULT_FAULT_PER_MILLE", "robustness.DEFAULT_TASKS",
            "robustness.parse_seeds", "ceiling.ALL4",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)

    def test_docs_describe_the_new_scale_and_its_boundaries(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "w3_taskset_scale", "eval.taskset_scale", "mvp_30",
            "版本化任务集", "第三个自变量",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)
        tech = (ROOT / "docs" / "tech-stack.md").read_text(encoding="utf-8")
        self.assertIn("eval/taskset_scale.py", tech)
        self.assertIn("w3_taskset_scale", tech)
        tasks_readme = (TASKS / "README.md").read_text(encoding="utf-8")
        self.assertIn("mvp_30.jsonl", tasks_readme)
        self.assertIn("build_mvp_30.py", tasks_readme)
        self.assertIn("不得再生成或修改", tasks_readme)


if __name__ == "__main__":
    unittest.main()
