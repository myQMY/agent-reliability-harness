"""W3 归因对照实验（``eval/attribution.py``）的验收。

注意：``tests/test_attribution.py`` 已被 W2「失败归因」测试占用，本文件
只测 W3 的模型能力轴 × 护栏轴对照，命名加 ``w3_`` 前缀以免混淆。

本文件钉死六件事：
1. **模型轴是严格嵌套的单变量轴**：故障阈值由后端同源的 SHA256 派生流给出、
   不含 fpm，降 fpm 只删故障而不改其余故障类型；低档故障位点集合必须是
   高档的子集；
2. 2×8 全因子 × 30 seed = 480 行证据的结构完整、guards 与 cell 对齐、
   逐任务序列与 k 对齐、所有 ``pass^k`` 都落在 1/N 的格点上；
3. **核心结论由证据钉死**：``all4`` 全程平坦 0.3000、``off`` 单调且有
   0.2000 上界、等效模型提升不可达、收益拆分恒等式成立、逐 seed 无负效应；
   并与 ``ceiling`` / ``resolution`` 两份既有归档报告逐项交叉验证；
4. 报告与证据逐字节可复现，``--from-jsonl`` 只读重渲染、CLI 连跑两次一致；
5. **零侵入**：既有 8 个 ``run_id`` / 归档报告 / 冻结基线不变；历史归档报告
   里的旧口径原句保留原文（不回溯改字）；
6. 源码无时钟 / 环境变量 / 全局随机 / 第三方依赖，测量输入复用版本化常量，
   且报告把「不外推真实模型、不替代 W4」等边界逐条写明。
"""
from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from eval import analyze, attribution, ceiling, guard_matrix, resolution
from eval import config as exp_config
from eval import robustness
from runtime import guard
from runtime.model import BackendConfig, SeededBackend

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT / "experiments"
MODULE_SOURCE = ROOT / "eval" / "attribution.py"
EVIDENCE = EXPERIMENTS / attribution.EVIDENCE_NAME
REPORT = EXPERIMENTS / attribution.REPORT_NAME

# 30-seed 全量证据上的实测结论；改 seed 集 / fpm 档位 / 护栏都会让这里失败。
EXPECTED_RECORD_COUNT = 480
EXPECTED_SEED_COUNT = 30
EXPECTED_FPM_AXIS = (0, 10, 20, 30, 40, 60, 80, 100)
EXPECTED_OFF_MEANS = (
    0.2000, 0.1867, 0.1733, 0.1567, 0.1533, 0.1433, 0.1267, 0.1067,
)
EXPECTED_ALL4 = 0.3000
EXPECTED_REFERENCE_OFF = 0.1433
EXPECTED_MODEL_PATH = 0.0567
EXPECTED_HARNESS_PATH = 0.1567
EXPECTED_INTRINSIC_EXTRA = 0.1000
EXPECTED_INTRINSIC_TASKS = 7

# 新增实验组后必须仍保持不变的既有 run_id。
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


class TestModelAxis(unittest.TestCase):
    def test_fpm_axis_is_versioned_and_contains_the_reference_and_zero(self):
        self.assertEqual(attribution.FPM_AXIS, EXPECTED_FPM_AXIS)
        self.assertEqual(
            attribution.REFERENCE_FPM, robustness.DEFAULT_FAULT_PER_MILLE
        )
        self.assertEqual(tuple(attribution.DEFAULT_SEEDS), robustness.DEFAULT_SEEDS)
        self.assertIn(0, attribution.FPM_AXIS)
        self.assertEqual(attribution.ZERO_FPM, 0)

    def test_all4_matches_guard_names_and_ceiling_definition(self):
        self.assertEqual(set(attribution.ALL4), set(guard.GUARD_NAMES))
        self.assertEqual(len(attribution.ALL4), 4)
        self.assertEqual(attribution.ALL4, ceiling.ALL4)
        self.assertEqual(
            [name for name, _ in attribution.CELLS], ["off", "all4"]
        )
        self.assertEqual(attribution.CELLS[0][1], ())
        self.assertEqual(attribution.CELLS[1][1], attribution.ALL4)

    def test_threshold_rng_is_derived_from_the_same_backend_stream(self):
        """阈值必须真的来自后端同源 RNG，不能是报告里编造的示例数。"""
        thresholds = attribution.fault_thresholds(
            task_ids=("T01",), trials=(1,), cursors=(0, 1, 6, 7)
        )
        backend = SeededBackend(
            BackendConfig(name="seeded", seed=attribution.THRESHOLD_SEED),
            (),
            task_id="T01",
            trial=1,
        )
        for cursor in (0, 1, 6, 7):
            with self.subTest(cursor=cursor):
                self.assertEqual(
                    thresholds[f"T01:1:{cursor}"],
                    backend._rng(cursor).randrange(1000),
                )

    def test_threshold_matches_actual_fault_injection_decision(self):
        """阈值 < fpm 必须精确对应 ``_pick_fault`` 是否注入故障。"""
        cursor = 6
        threshold = attribution.fault_thresholds(
            task_ids=("T01",), trials=(1,), cursors=(cursor,)
        )[f"T01:1:{cursor}"]
        for fpm in attribution.FPM_AXIS:
            backend = SeededBackend(
                BackendConfig(
                    name="seeded",
                    seed=attribution.THRESHOLD_SEED,
                    fault_per_mille=fpm,
                ),
                ({"tool": "get_order", "args": {}},),
                task_id="T01",
                trial=1,
            )
            injected = backend._pick_fault(cursor) is not None
            with self.subTest(fpm=fpm):
                self.assertEqual(injected, threshold < fpm)

    def test_fault_sets_are_strictly_nested_along_the_axis(self):
        thresholds = attribution.fault_thresholds()
        self.assertEqual(
            len(thresholds),
            len(attribution.THRESHOLD_TASK_IDS)
            * len(attribution.THRESHOLD_TRIALS)
            * len(attribution.THRESHOLD_CURSORS),
        )
        nested = attribution.nested_fault_counts(thresholds)
        self.assertTrue(attribution.is_nested(nested))
        self.assertEqual(nested[0]["count"], 0)
        self.assertEqual(
            [row["fpm"] for row in nested], list(attribution.FPM_AXIS)
        )
        for previous, current in zip(nested, nested[1:]):
            with self.subTest(fpm=current["fpm"]):
                self.assertLessEqual(previous["count"], current["count"])
                self.assertTrue(set(previous["keys"]) <= set(current["keys"]))
        self.assertGreater(nested[-1]["count"], nested[0]["count"])
        # 键里不能有 Markdown 表格分隔符
        self.assertFalse(any("|" in key for key in thresholds))

    def test_is_nested_rejects_non_monotone_evidence(self):
        broken = [
            {"fpm": 0, "count": 0, "keys": ()},
            {"fpm": 10, "count": 2, "keys": ("a", "b")},
            {"fpm": 20, "count": 1, "keys": ("a",)},
        ]
        self.assertFalse(attribution.is_nested(broken))
        self.assertFalse(attribution.is_nested([
            {"fpm": 0, "count": 1, "keys": ("a",)},
            {"fpm": 10, "count": 2, "keys": ("b", "c")},
        ]))

    def test_collect_rejects_invalid_axis_and_k(self):
        tasks = exp_config.resolve_input(robustness.DEFAULT_TASKS)
        with self.assertRaises(ValueError):
            attribution.collect((11,), tasks_path=tasks, k=0)
        with self.assertRaises(ValueError):
            attribution.collect((11,), tasks_path=tasks, fpm_axis=())
        with self.assertRaises(ValueError):
            attribution.collect((11,), tasks_path=tasks, fpm_axis=(20, 20))
        with self.assertRaises(ValueError):
            attribution.collect((11,), tasks_path=tasks, fpm_axis=(0, 1001))


class TestEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_evidence()

    def test_evidence_is_a_complete_two_by_thirty_by_eight_grid(self):
        self.assertEqual(len(self.records), EXPECTED_RECORD_COUNT)
        self.assertEqual(attribution.fpm_values(self.records), EXPECTED_FPM_AXIS)
        self.assertEqual(
            len(attribution.seed_values(self.records)), EXPECTED_SEED_COUNT
        )
        self.assertEqual(
            tuple(attribution.seed_values(self.records)),
            tuple(sorted(robustness.DEFAULT_SEEDS)),
        )
        grid = {
            (int(row["fpm"]), int(row["seed"]), row["cell"])
            for row in self.records
        }
        self.assertEqual(len(grid), EXPECTED_RECORD_COUNT)

    def test_guards_match_cell_names_and_single_variable_discipline(self):
        for row in self.records:
            expected = (
                () if row["cell"] == "off" else tuple(attribution.ALL4)
            )
            self.assertEqual(tuple(row["guards"]), expected)
            self.assertEqual(row["task_count"], 10)
            self.assertEqual(row["trial_count"], 30)

    def test_per_task_sequences_match_k_and_passk_hits(self):
        for row in self.records:
            sequence_list = row["per_task"]
            self.assertEqual(len(sequence_list), row["task_count"])
            for item in sequence_list:
                self.assertEqual(len(item["sequence"]), 3)
                self.assertTrue(set(item["sequence"]) <= {"0", "1"})
            hits = sum(bool(item["all_pass"]) for item in sequence_list)
            self.assertEqual(hits, row["passk_hits"])
            self.assertAlmostEqual(
                row["passk"], row["passk_hits"] / row["task_count"]
            )

    def test_every_passk_sits_on_the_one_over_n_grid(self):
        step = 1 / 10
        for row in self.records:
            quotient = row["passk"] / step
            with self.subTest(fpm=row["fpm"], seed=row["seed"]):
                self.assertAlmostEqual(quotient, round(quotient), places=9)

    def test_pass1_is_within_bounds_for_every_cell(self):
        for row in self.records:
            self.assertGreaterEqual(row["pass1"], 0.0)
            self.assertLessEqual(row["pass1"], 1.0)

    def test_axis_rows_reject_missing_cells(self):
        truncated = [
            row for row in self.records
            if not (row["fpm"] == 0 and row["cell"] == "all4")
        ]
        with self.assertRaises(ValueError):
            attribution.axis_rows(truncated)
        with self.assertRaises(ValueError):
            attribution.axis_row(truncated, 9999)

    def test_cell_mean_and_per_seed_delta_reject_incomplete_evidence(self):
        truncated = [
            row for row in self.records
            if not (row["fpm"] == 60 and row["cell"] == "all4"
                    and row["seed"] == 11)
        ]
        with self.assertRaises(ValueError):
            attribution.per_seed_delta(truncated, 60)
        with self.assertRaises(ValueError):
            attribution.equivalent_fpm(truncated, 0.3, "not_a_cell")


class TestFindings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_evidence()

    def test_off_curve_is_strictly_monotone_and_matches_frozen_values(self):
        rows = attribution.axis_rows(self.records)
        self.assertEqual(len(rows), len(EXPECTED_FPM_AXIS))
        for row, expected in zip(rows, EXPECTED_OFF_MEANS):
            with self.subTest(fpm=row["fpm"]):
                self.assertAlmostEqual(row["off_passk"], expected, places=4)
        self.assertTrue(
            attribution.is_monotone_nonincreasing(self.records, "off")
        )
        values = [row["off_passk"] for row in rows]
        self.assertLess(values[-1], values[0])

    def test_all4_curve_is_flat_at_the_attainable_ceiling(self):
        for row in attribution.axis_rows(self.records):
            with self.subTest(fpm=row["fpm"]):
                self.assertAlmostEqual(
                    row["all4_passk"], EXPECTED_ALL4, places=9
                )
                self.assertAlmostEqual(
                    row["all4_min"], EXPECTED_ALL4, places=9
                )
                self.assertAlmostEqual(
                    row["all4_max"], EXPECTED_ALL4, places=9
                )
        self.assertTrue(
            all(
                row["delta"] > 0
                for row in attribution.axis_rows(self.records)
            )
        )

    def test_reference_point_and_lift_decomposition(self):
        decomp = attribution.lift_decomposition(self.records)
        self.assertAlmostEqual(
            decomp["baseline"], EXPECTED_REFERENCE_OFF, places=4
        )
        self.assertAlmostEqual(
            decomp["all4_at_reference"], EXPECTED_ALL4, places=9
        )
        self.assertAlmostEqual(
            decomp["off_at_zero"], 0.2000, places=9
        )
        self.assertAlmostEqual(
            decomp["model_path"], EXPECTED_MODEL_PATH, places=4
        )
        self.assertAlmostEqual(
            decomp["harness_path"], EXPECTED_HARNESS_PATH, places=4
        )
        self.assertAlmostEqual(
            decomp["intrinsic_extra"], EXPECTED_INTRINSIC_EXTRA, places=9
        )
        self.assertAlmostEqual(
            decomp["unreachable_by_model"], 0.1000, places=9
        )
        self.assertTrue(decomp["flat_identity_holds"])
        self.assertAlmostEqual(decomp["model_share"], 0.3618, places=3)

    def test_equivalent_model_upgrade_is_unreachable(self):
        result = attribution.equivalent_fpm(
            self.records, EXPECTED_ALL4, "off"
        )
        self.assertFalse(result["reachable"])
        self.assertAlmostEqual(result["best_value"], 0.2000, places=9)
        self.assertEqual(result["best_fpm"], 0)
        self.assertAlmostEqual(result["gap"], 0.1000, places=9)
        reachable = attribution.equivalent_fpm(self.records, 0.10, "off")
        self.assertTrue(reachable["reachable"])
        self.assertGreaterEqual(reachable["value"], 0.10)

    def test_harness_lift_exceeds_any_point_on_the_model_axis(self):
        rows = attribution.axis_rows(self.records)
        ceiling_value = max(row["off_passk"] for row in rows)
        self.assertLess(
            ceiling_value,
            attribution.axis_row(
                self.records, attribution.REFERENCE_FPM
            )["all4_passk"],
        )

    def test_no_seed_has_a_negative_harness_effect_at_the_reference_point(self):
        deltas = attribution.per_seed_delta(
            self.records, attribution.REFERENCE_FPM
        )
        self.assertEqual(len(deltas), EXPECTED_SEED_COUNT)
        signs = ceiling.sign_counts(deltas)
        self.assertEqual(signs["positive"], EXPECTED_SEED_COUNT)
        self.assertEqual(signs["zero"], 0)
        self.assertEqual(signs["negative"], 0)
        self.assertAlmostEqual(
            signs["mean"], EXPECTED_HARNESS_PATH, places=4
        )

    def test_intrinsic_task_count_is_derived_not_hardcoded(self):
        self.assertEqual(
            attribution.intrinsic_task_count(self.records),
            EXPECTED_INTRINSIC_TASKS,
        )
        source = MODULE_SOURCE.read_text(encoding="utf-8")
        helpers_only = source.split("def render_report")[0]
        for task_id in ("T02", "T06", "T07", "T09"):
            self.assertNotIn(f'"{task_id}"', helpers_only)

    def test_intrinsic_tasks_agree_with_ceiling_analysis(self):
        ceiling_records = robustness.load_records(
            (EXPERIMENTS / ceiling.EVIDENCE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            len(ceiling.unrecoverable_tasks(ceiling_records)),
            attribution.intrinsic_task_count(self.records),
        )

    def test_cross_check_with_ceiling_report_on_shared_cells(self):
        ceiling_records = robustness.load_records(
            (EXPERIMENTS / ceiling.EVIDENCE_NAME).read_text(encoding="utf-8")
        )
        shared = {
            "off": ("off", 60),
            "all4": ("all4", 60),
            "no_fault": ("off", 0),
            "no_fault_all4": ("all4", 0),
        }
        for cell, (our_cell, fpm) in shared.items():
            with self.subTest(ceiling_cell=cell):
                self.assertAlmostEqual(
                    ceiling.cell_summary(ceiling_records, cell)["passk_mean"],
                    attribution.cell_mean(self.records, our_cell, fpm),
                    places=9,
                )

    def test_cross_check_with_resolution_report_at_k3(self):
        resolution_records = robustness.load_records(
            (EXPERIMENTS / resolution.EVIDENCE_NAME).read_text(encoding="utf-8")
        )
        summary = resolution.k_summary(resolution_records, 3)
        self.assertAlmostEqual(
            summary["passk_mean_off"], EXPECTED_REFERENCE_OFF, places=4
        )
        self.assertAlmostEqual(
            summary["passk_mean_all4"], EXPECTED_ALL4, places=9
        )
        self.assertAlmostEqual(summary["step"], 0.10, places=9)

    def test_passk_definition_is_still_hits_over_task_count(self):
        """分辨率仍是 1/N，本实验没有偷偷换判分口径。"""
        rows = [
            {"task_id": f"T{i}", "trial": 1, "passed": 1 if i < 3 else 0,
             "failure_type": "unknown", "steps": 1, "invalid_calls": 0,
             "tokens": 0, "latency_ms": 0}
            for i in range(10)
        ]
        self.assertEqual(analyze.summarize(rows, 1)["passk"], 0.3)


class TestReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = REPORT.read_text(encoding="utf-8")

    def test_archived_report_matches_current_render(self):
        self.assertEqual(
            attribution.render_report(load_evidence()).encode("utf-8"),
            REPORT.read_bytes(),
        )

    def test_report_states_the_core_attribution_claim(self):
        self.assertIn("提升来自 harness，还是换一个更强的模型", self.text)
        self.assertIn("只靠降低模型故障率无法达到", self.text)
        self.assertIn("换模型路径最多兑现", self.text)
        self.assertIn("**`all4` 曲线全程平坦**", self.text)

    def test_report_states_every_caveat(self):
        for phrase in (
            "fpm 不是「模型能力」的全部",
            "`fpm=0` 不是「完美模型」",
            "不替代 W4",
            "分辨率仍是 0.10",
            "因子实验不是 A/B",
            "不是显著性检验",
            "不构成任何成本或延迟结论",
            "不能外推到任何真实模型",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_report_points_out_the_idempotency_caveat(self):
        self.assertIn("idempotency", self.text)
        self.assertIn("T03", self.text)
        self.assertIn("不是「harness 普遍强于更强模型」的证明", self.text)

    def test_report_proves_the_axis_is_nested(self):
        self.assertIn("位点示例", self.text)
        self.assertIn("较上一档新增", self.text)
        self.assertIn("| 100 | 44 |", self.text)

    def test_report_lists_every_fpm_and_every_seed(self):
        for fpm in EXPECTED_FPM_AXIS:
            self.assertIn(f"| {fpm} | ", self.text)
        for seed in robustness.DEFAULT_SEEDS:
            self.assertIn(f"| {seed} | ", self.text)

    def test_report_has_no_hardcoded_thirty_seed_claim_for_small_runs(self):
        """自检用的小样本不得在报告里谎称 30 个 seed。"""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with redirect_stdout(io.StringIO()):
                attribution.main(["--seeds", "11,1,2", "--out-dir", str(out)])
            text = (out / attribution.REPORT_NAME).read_text(encoding="utf-8")
        self.assertIn("3 个版本化 seed", text)
        self.assertIn("共 48 行证据", text)
        self.assertNotIn("30 个版本化 seed", text)

    def test_report_refuses_empty_evidence(self):
        with self.assertRaises(ValueError):
            attribution.render_report([])


class TestReproducibility(unittest.TestCase):
    def test_cli_produces_identical_artifacts_twice(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            artifacts = []
            for index in (1, 2):
                out = root / str(index)
                with redirect_stdout(io.StringIO()):
                    attribution.main(
                        ["--seeds", "11,1,2", "--out-dir", str(out)]
                    )
                artifacts.append((
                    (out / attribution.EVIDENCE_NAME).read_bytes(),
                    (out / attribution.REPORT_NAME).read_bytes(),
                ))
            self.assertEqual(artifacts[0], artifacts[1])

            readonly = root / "readonly"
            readonly.mkdir()
            (readonly / attribution.EVIDENCE_NAME).write_bytes(artifacts[0][0])
            with redirect_stdout(io.StringIO()):
                attribution.main(
                    ["--from-jsonl", "--out-dir", str(readonly)]
                )
            self.assertEqual(
                (readonly / attribution.REPORT_NAME).read_bytes(),
                artifacts[0][1],
            )

    def test_from_jsonl_refuses_missing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    attribution.main(
                        ["--from-jsonl", "--out-dir", str(Path(d))]
                    )

    def test_repository_evidence_renders_the_archived_report(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            (target / attribution.EVIDENCE_NAME).write_bytes(EVIDENCE.read_bytes())
            with redirect_stdout(io.StringIO()):
                attribution.main(
                    ["--from-jsonl", "--out-dir", str(target)]
                )
            self.assertEqual(
                (target / attribution.REPORT_NAME).read_bytes(),
                REPORT.read_bytes(),
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
        for module in (ceiling, resolution):
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

    def test_source_has_no_hardcoded_conclusion_numbers(self):
        """报告措辞必须由证据推导，不能把实测数字写死在结论里。"""
        renderer = MODULE_SOURCE.read_text(encoding="utf-8").split(
            "def render_report"
        )[1]
        for literal in ("0.3000", "0.2000", "0.1433", "36.2"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, renderer)

    def test_measurement_inputs_are_reused_not_duplicated(self):
        source = MODULE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("robustness.DEFAULT_K", source)
        self.assertIn("robustness.DEFAULT_TASKS", source)
        self.assertIn("robustness.DEFAULT_SEEDS", source)
        self.assertIn("robustness.DEFAULT_FAULT_PER_MILLE", source)
        self.assertIn("robustness.parse_seeds", source)

    def test_fpm_axis_and_cells_are_versioned(self):
        self.assertEqual(attribution.CELL_NAMES, ("off", "all4"))
        self.assertEqual(attribution.CONTROL, "off")
        self.assertEqual(attribution.TREATMENT, "all4")
        self.assertEqual(attribution.EVIDENCE_NAME, "w3_attribution.jsonl")
        self.assertEqual(attribution.REPORT_NAME, "w3_attribution.md")

    def test_readme_describes_the_attribution_experiment(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "w3_attribution.md", "模型能力轴", "归因对照", "eval.attribution",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_tech_stack_describes_the_new_module(self):
        text = (ROOT / "docs" / "tech-stack.md").read_text(encoding="utf-8")
        self.assertIn("eval/attribution.py", text)
        self.assertIn("w3_attribution", text)


if __name__ == "__main__":
    unittest.main()
