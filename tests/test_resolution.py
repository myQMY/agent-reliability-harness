"""W3 测量仪器审计（``eval/resolution.py``）的验收。

本文件钉死六件事：
1. ``pass^k`` 的分辨率 = 1 / 任务数，**与 k 无关**：用真实 ``analyze.summarize``
   在多档 k 上构造合成结果行验证，而不只是检查一个常量；
2. 三档 k × 30 seed × 三格的证据结构完整，逐任务序列与 k 对齐，
   k 只增加 trial、不改变已有 trial 的随机流（``pass¹`` 逐档相同）；
3. **两类零效应必须分开计量**：多数零效应是护栏没修好任何 trial，少数才是
   「修好了 trial 却被 k 次全过门槛吞掉」；trial 级 1→0 变差不等于任务级
   负效应。两种方向的过度解读都被钉死；
4. 报告与证据逐字节可复现，``--from-jsonl`` 只读重渲染、CLI 连跑两次一致；
5. **零侵入**：既有 run_id / 归档报告 / 冻结基线不变；四份历史归档报告里的
   旧口径原句**保留原文**（不回溯改字），活文档 README / tech-stack 已纠正；
6. 审计结论与 [可达上限分析] 交叉一致：7 个固有缺陷任务、all4 平均 0.3000。
"""
from __future__ import annotations

import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from eval import analyze, ceiling, guard_matrix, resolution, robustness
from eval import config as exp_config
from runtime import guard

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT / "experiments"
RUNS = EXPERIMENTS / "runs"
MODULE_SOURCE = ROOT / "eval" / "resolution.py"
EVIDENCE = EXPERIMENTS / resolution.EVIDENCE_NAME
REPORT = EXPERIMENTS / resolution.REPORT_NAME

# 同一批 30-seed 证据上的实测结论；改 seed 集 / k 档位 / 护栏都会让这里失败。
EXPECTED_K_VALUES = (3, 5, 8)
EXPECTED_RECORD_COUNT = 270
EXPECTED_FUNNEL = {
    (3, "schema"): dict(repaired=7, regressed=3, flipped=4, masked=2, zero=26),
    (3, "all4"): dict(repaired=100, regressed=0, flipped=47, masked=0, zero=0),
    (5, "schema"): dict(repaired=11, regressed=5, flipped=7, masked=3, zero=24),
    (5, "all4"): dict(repaired=167, regressed=0, flipped=58, masked=0, zero=0),
    (8, "schema"): dict(repaired=20, regressed=9, flipped=11, masked=7, zero=20),
    (8, "all4"): dict(repaired=268, regressed=0, flipped=68, masked=0, zero=0),
}

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


def synthetic_rows(task_count: int, k: int, hits: int) -> list[dict]:
    """构造 passk_hits == hits 的合成结果行，喂给真实 analyze.summarize。"""
    rows = []
    for index in range(1, task_count + 1):
        task_id = f"T{index:02d}"
        all_pass = index <= hits
        for trial in range(1, k + 1):
            passed = 1 if all_pass or trial == 1 else 0
            rows.append({
                "trial_id": f"{task_id}-t{trial}",
                "task_id": task_id,
                "trial": trial,
                "passed": passed,
                "failure_type": "none" if passed else "unknown",
                "steps": 1,
                "tokens": 0,
                "latency_ms": 0,
                "invalid_calls": 0,
            })
    return rows


class TestPureFunctions(unittest.TestCase):
    def test_resolution_is_inverse_of_task_count(self):
        self.assertAlmostEqual(resolution.resolution(10), 0.1)
        self.assertAlmostEqual(resolution.resolution(20), 0.05)
        self.assertAlmostEqual(resolution.resolution(1), 1.0)
        self.assertAlmostEqual(resolution.resolution(100), 0.01)

    def test_resolution_rejects_non_positive_task_count(self):
        for value in (0, -1, -10):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    resolution.resolution(value)

    def test_passk_grid_has_n_plus_one_points_from_zero_to_one(self):
        grid = resolution.passk_grid(10)
        self.assertEqual(len(grid), 11)
        self.assertAlmostEqual(grid[0], 0.0)
        self.assertAlmostEqual(grid[-1], 1.0)
        for index, value in enumerate(grid):
            self.assertAlmostEqual(value, index * 0.1)

    def test_is_on_grid_rejects_values_between_grid_points(self):
        for value in (0.1, 0.2, 0.3, 0.0, 1.0):
            self.assertTrue(resolution.is_on_grid(value, 10))
        for value in (0.15, 0.25, 0.33, -0.1, 1.1):
            self.assertFalse(resolution.is_on_grid(value, 10))

    def test_real_summarize_passk_is_independent_of_k(self):
        """关键守门：k 不进入 pass^k 公式，只影响「分子能拿到多少」。"""
        for k in (3, 5, 8, 20):
            for hits in range(11):
                with self.subTest(k=k, hits=hits):
                    summary = analyze.summarize(synthetic_rows(10, k, hits), k)
                    self.assertEqual(summary["k"], k)
                    self.assertEqual(summary["task_count"], 10)
                    self.assertEqual(summary["passk_hits"], hits)
                    self.assertAlmostEqual(
                        summary["passk"], hits * resolution.resolution(10)
                    )
                    self.assertTrue(
                        resolution.is_on_grid(summary["passk"], 10)
                    )

    def test_count_passes_counts_and_validates_sequences(self):
        self.assertEqual(resolution.count_passes(("101", "111", "000")), 5)
        self.assertEqual(resolution.count_passes(()), 0)
        with self.assertRaises(ValueError):
            resolution.count_passes(("101", "11"))
        with self.assertRaises(ValueError):
            resolution.count_passes(("102",))

    def test_k_and_seed_values_are_derived_from_evidence_not_hardcoded(self):
        records = load_evidence()
        self.assertEqual(resolution.k_values(records), EXPECTED_K_VALUES)
        self.assertEqual(
            resolution.seed_values(records),
            tuple(sorted(robustness.DEFAULT_SEEDS)),
        )
        shuffled = list(reversed(records))
        self.assertEqual(resolution.k_values(shuffled), EXPECTED_K_VALUES)
        self.assertEqual(
            resolution.seed_values(shuffled), resolution.seed_values(records)
        )


class TestEvidenceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_evidence()

    def test_evidence_is_a_complete_three_by_thirty_by_three_grid(self):
        self.assertEqual(len(self.records), EXPECTED_RECORD_COUNT)
        combos = {
            (int(r["k"]), int(r["seed"]), str(r["cell"]))
            for r in self.records
        }
        self.assertEqual(len(combos), EXPECTED_RECORD_COUNT)
        for k in EXPECTED_K_VALUES:
            for seed in robustness.DEFAULT_SEEDS:
                for cell in resolution.CELL_NAMES:
                    self.assertIn((k, seed, cell), combos)

    def test_guards_match_cell_names_and_single_variable_discipline(self):
        expected = {
            "off": (),
            "schema": ("schema_validation",),
            "all4": (
                "schema_validation", "idempotency", "tool_retry",
                "json_recovery",
            ),
        }
        for record in self.records:
            with self.subTest(record=(record["k"], record["seed"], record["cell"])):
                self.assertEqual(
                    tuple(record["guards"]), expected[record["cell"]]
                )
        for k in EXPECTED_K_VALUES:
            for seed in robustness.DEFAULT_SEEDS:
                cells = [
                    r for r in self.records
                    if r["k"] == k and r["seed"] == seed
                ]
                self.assertEqual(
                    len({r["task_count"] for r in cells}), 1,
                    "同一 k / seed 下三格的任务数必须相同",
                )

    def test_per_task_sequences_match_k_and_are_binary(self):
        for record in self.records:
            k = int(record["k"])
            self.assertEqual(len(record["per_task"]), record["task_count"])
            self.assertEqual(record["trial_count"], k * record["task_count"])
            for item in record["per_task"]:
                self.assertEqual(len(item["sequence"]), k)
                self.assertTrue(set(item["sequence"]) <= {"0", "1"})
                self.assertEqual(
                    item["all_pass"], item["sequence"].count("1") == k
                )

    def test_larger_k_only_appends_trials_without_changing_earlier_ones(self):
        """k 只增加重复次数；前 k=3 个 trial 的随机流不随 k 档位变化。"""
        by_key = {
            (int(r["k"]), int(r["seed"]), r["cell"]): {
                item["task_id"]: item["sequence"] for item in r["per_task"]
            }
            for r in self.records
        }
        for seed in robustness.DEFAULT_SEEDS:
            for cell in resolution.CELL_NAMES:
                base = by_key[(3, seed, cell)]
                for k in (5, 8):
                    longer = by_key[(k, seed, cell)]
                    for task_id, seq in base.items():
                        with self.subTest(seed=seed, cell=cell, k=k, task=task_id):
                            self.assertEqual(longer[task_id][:3], seq)

    def test_pass1_is_identical_across_all_k_values(self):
        by_key = {
            (int(r["k"]), int(r["seed"]), r["cell"]): r["pass1"]
            for r in self.records
        }
        for seed in robustness.DEFAULT_SEEDS:
            for cell in resolution.CELL_NAMES:
                values = {by_key[(k, seed, cell)] for k in EXPECTED_K_VALUES}
                self.assertEqual(len(values), 1)

    def test_task_flags_are_exclusive_and_cover_every_task(self):
        for k in EXPECTED_K_VALUES:
            flags = resolution.task_flags(self.records, k)
            self.assertEqual(len(flags), 10)
            for task_id, item in flags.items():
                with self.subTest(k=k, task=task_id):
                    self.assertEqual(
                        sum(bool(item[key]) for key in
                            ("always_pass", "always_fail", "variable")),
                        1,
                    )

    def test_effective_range_has_only_four_usable_levels(self):
        for k in EXPECTED_K_VALUES:
            item = resolution.effective_range(self.records, k)
            with self.subTest(k=k):
                self.assertEqual(item["task_count"], 10)
                self.assertAlmostEqual(item["step"], 0.1)
                self.assertEqual(item["grid_size"], 11)
                self.assertEqual(item["fixed_pass"], 0)
                self.assertEqual(item["fixed_fail"], 7)
                self.assertEqual(item["variable"], 3)
                self.assertEqual(item["levels"], 4)
                self.assertAlmostEqual(item["floor"], 0.0)
                self.assertAlmostEqual(item["ceiling"], 0.3)

    def test_intrinsic_tasks_agree_with_ceiling_analysis(self):
        """两个独立跑批的模块必须推导出同一批固有缺陷任务。"""
        ceiling_records = robustness.load_records(
            (EXPERIMENTS / ceiling.EVIDENCE_NAME).read_text(encoding="utf-8")
        )
        from_ceiling = {
            item["task_id"] for item in ceiling.unrecoverable_tasks(ceiling_records)
        }
        flags = resolution.task_flags(self.records, 3)
        from_resolution = {
            task_id for task_id, item in flags.items() if item["always_fail"]
        }
        self.assertEqual(from_ceiling, from_resolution)
        self.assertEqual(len(from_ceiling), 7)

    def test_k3_all4_mean_matches_ceiling_analysis(self):
        ceiling_records = robustness.load_records(
            (EXPERIMENTS / ceiling.EVIDENCE_NAME).read_text(encoding="utf-8")
        )
        self.assertAlmostEqual(
            resolution.k_summary(self.records, 3)["passk_mean_all4"],
            ceiling.cell_summary(ceiling_records, "all4")["passk_mean"],
        )

    def test_task_flags_and_paired_reject_incomplete_evidence(self):
        # 抽掉 k=3 / seed=11 的 schema 格：三格不齐时拒绝静默补零。
        truncated = [
            record for record in self.records
            if not (
                record["k"] == 3 and record["seed"] == 11
                and record["cell"] == "schema"
            )
        ]
        self.assertEqual(len(truncated), len(self.records) - 1)
        with self.assertRaises(ValueError):
            resolution.task_flags(truncated, 3)
        with self.assertRaises(ValueError):
            resolution.funnel(truncated, 3, "schema")
        with self.assertRaises(ValueError):
            resolution.funnel(truncated, 3, "all4")
        with self.assertRaises(ValueError):
            resolution.task_flags([], 3)
        with self.assertRaises(ValueError):
            resolution.funnel(self.records, 3, "not_a_cell")

    def test_collect_rejects_non_positive_k(self):
        with self.assertRaises(ValueError):
            resolution.collect(
                (11,), tasks_path=ROOT / "tasks" / "mvp_10.jsonl",
                k_values=(0,),
            )


class TestFunnelContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_evidence()

    def test_funnel_delta_equals_flipped_tasks_over_n(self):
        for k in EXPECTED_K_VALUES:
            for treatment in resolution.TREATMENTS:
                for row in resolution.funnel(self.records, k, treatment):
                    with self.subTest(k=k, treatment=treatment, seed=row["seed"]):
                        self.assertAlmostEqual(
                            row["delta"], row["flipped_tasks"] / 10
                        )

    def test_funnel_summary_matches_frozen_measurements(self):
        for (k, treatment), expected in EXPECTED_FUNNEL.items():
            item = resolution.funnel_summary(self.records, k, treatment)
            with self.subTest(k=k, treatment=treatment):
                self.assertEqual(item["seed_count"], 30)
                self.assertEqual(item["repaired_trials"], expected["repaired"])
                self.assertEqual(item["regressed_trials"], expected["regressed"])
                self.assertEqual(item["flipped_tasks"], expected["flipped"])
                self.assertEqual(len(item["masked_seeds"]), expected["masked"])
                self.assertEqual(len(item["zero_effect_seeds"]), expected["zero"])
                self.assertEqual(item["k"], k)
                self.assertEqual(item["treatment"], treatment)

    def test_masked_seeds_are_a_strict_subset_of_zero_effect_seeds(self):
        for k in EXPECTED_K_VALUES:
            item = resolution.funnel_summary(self.records, k, "schema")
            self.assertTrue(
                set(item["masked_seeds"]) <= set(item["zero_effect_seeds"])
            )
            self.assertLess(len(item["masked_seeds"]),
                            len(item["zero_effect_seeds"]))

    def test_two_kinds_of_zero_effect_are_separated_not_conflated(self):
        """26 个零效应 seed 里 24 个 repaired=0，只有 2 个是被门槛吞掉。"""
        rows = {
            row["seed"]: row
            for row in resolution.funnel(self.records, 3, "schema")
        }
        zero = [
            seed for seed, row in rows.items()
            if abs(row["delta"]) < resolution.EFFECT_EPSILON
        ]
        no_repair = [seed for seed in zero if rows[seed]["repaired_trials"] == 0]
        masked = [seed for seed in zero if rows[seed]["masked"]]
        self.assertEqual(len(zero), 26)
        self.assertEqual(len(no_repair), 24)
        self.assertEqual(len(masked), 2)
        self.assertEqual(set(masked), {5, 3001})
        self.assertFalse(set(no_repair) & set(masked))

    def test_masking_grows_with_k_but_zero_effects_shrink(self):
        """数据推翻了「提高 k 更难测到效应」：难度、可翻转空间、掩蔽率同向变化。"""
        counts = {
            k: resolution.funnel_summary(self.records, k, "schema")
            for k in EXPECTED_K_VALUES
        }
        self.assertEqual(
            [len(counts[k]["masked_seeds"]) for k in EXPECTED_K_VALUES],
            [2, 3, 7],
        )
        self.assertEqual(
            [len(counts[k]["zero_effect_seeds"]) for k in EXPECTED_K_VALUES],
            [26, 24, 20],
        )
        self.assertEqual(
            [counts[k]["flipped_tasks"] for k in EXPECTED_K_VALUES],
            [4, 7, 11],
        )

    def test_all4_has_no_regression_no_masking_and_no_zero_effect(self):
        for k in EXPECTED_K_VALUES:
            item = resolution.funnel_summary(self.records, k, "all4")
            with self.subTest(k=k):
                self.assertEqual(item["regressed_trials"], 0)
                self.assertEqual(item["masked_seeds"], [])
                self.assertEqual(item["zero_effect_seeds"], [])

    def test_no_task_level_negative_effect_anywhere(self):
        for k in EXPECTED_K_VALUES:
            for treatment in resolution.TREATMENTS:
                for row in resolution.funnel(self.records, k, treatment):
                    self.assertGreaterEqual(
                        row["delta"], -resolution.EFFECT_EPSILON
                    )

    def test_trial_regression_is_not_a_task_level_regression(self):
        """17 次 trial 级 1→0 变差，但逐 seed Δpass^k 没有一个负值。"""
        total = sum(
            resolution.funnel_summary(self.records, k, "schema")["regressed_trials"]
            for k in EXPECTED_K_VALUES
        )
        self.assertEqual(total, 17)
        negatives = [
            (k, treatment, row["seed"])
            for k in EXPECTED_K_VALUES
            for treatment in resolution.TREATMENTS
            for row in resolution.funnel(self.records, k, treatment)
            if row["delta"] < -resolution.EFFECT_EPSILON
        ]
        self.assertEqual(negatives, [])

    def test_k_summary_values_all_sit_on_the_nominal_grid(self):
        for k in EXPECTED_K_VALUES:
            item = resolution.k_summary(self.records, k)
            with self.subTest(k=k):
                self.assertAlmostEqual(item["step"], 0.1)
                self.assertTrue(item["off_on_grid"])
                self.assertEqual(item["seed_count"], 30)
                self.assertEqual(item["trial_count"], 10 * k)
                self.assertTrue(set(item["off_values"]) <= {0.0, 0.1, 0.2})


class TestReportContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = load_evidence()
        cls.text = REPORT.read_text(encoding="utf-8")

    def test_archived_report_matches_current_render(self):
        self.assertEqual(
            REPORT.read_bytes(),
            resolution.render_report(self.records).encode("utf-8"),
        )

    def test_report_states_the_central_correction(self):
        for phrase in (
            "分辨率只由任务数决定", "与 k 无关", "难度旋钮",
            "提高 k**」错误", "最小步进恒为 0.10", "11 个格点",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_report_does_not_overclaim_masking(self):
        for phrase in (
            "效应掩蔽真实存在，但不是零效应的主因",
            "24 个是护栏", "只有 2 个是修好了",
            "不能把 W3 的零效应一律解释成「尺子太钝」",
            "既**不能把 W3 的零效应一律读成「护栏没用」**",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)
        self.assertNotIn("零效应主要不是「护栏没干活」", self.text)
        self.assertNotIn("回归为零", self.text)

    def test_report_states_honest_caveats_and_keeps_history(self):
        for phrase in (
            "不是显著性检验", "不是随机抽样", "不构成任何真实模型",
            "历史留档按实验记录纪律保留原文", "不回溯改字",
            "主指标仍是 k=3", "w3_ceiling.md", "w3_robustness.md",
            "两类零效应分开计量", "trial 级 1→0 变差不是任务级负效应",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)
        self.assertNotIn(str(ROOT), self.text)
        self.assertNotIn("C:\\", self.text)

    def test_report_lists_every_seed_in_the_funnel_table(self):
        for seed in robustness.DEFAULT_SEEDS:
            with self.subTest(seed=seed):
                self.assertIn(f"| {seed} | ", self.text)

    def test_report_quotes_the_old_wording_it_corrects(self):
        self.assertIn(OLD_WORDING, self.text)
        self.assertIn("「扩大任务集」正确", self.text)

    def test_report_separates_formula_projection_from_measurements(self):
        self.assertIn("**下表是公式推算，不是实测结果**", self.text)
        self.assertIn("| 100 | 0.0100 |", self.text)


class TestReproducibility(unittest.TestCase):
    def test_cli_produces_identical_artifacts_twice(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            artifacts = []
            for index in (1, 2):
                out = root / str(index)
                with redirect_stdout(io.StringIO()):
                    resolution.main(
                        ["--seeds", "11,1,2", "--out-dir", str(out)]
                    )
                artifacts.append((
                    (out / resolution.EVIDENCE_NAME).read_bytes(),
                    (out / resolution.REPORT_NAME).read_bytes(),
                ))
            self.assertEqual(artifacts[0], artifacts[1])

            readonly = root / "readonly"
            readonly.mkdir()
            (readonly / resolution.EVIDENCE_NAME).write_bytes(artifacts[0][0])
            with redirect_stdout(io.StringIO()):
                resolution.main(["--from-jsonl", "--out-dir", str(readonly)])
            self.assertEqual(
                (readonly / resolution.REPORT_NAME).read_bytes(),
                artifacts[0][1],
            )

    def test_from_jsonl_refuses_missing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    resolution.main(["--from-jsonl", "--out-dir", str(Path(d))])

    def test_repository_evidence_renders_the_archived_report(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d)
            (target / resolution.EVIDENCE_NAME).write_bytes(EVIDENCE.read_bytes())
            with redirect_stdout(io.StringIO()):
                resolution.main(["--from-jsonl", "--out-dir", str(target)])
            self.assertEqual(
                (target / resolution.REPORT_NAME).read_bytes(),
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
            (EXPERIMENTS / robustness.REPORT_NAME).read_bytes(),
            robustness.render_sweep(
                robustness.load_records(
                    (EXPERIMENTS / robustness.EVIDENCE_NAME).read_text(
                        encoding="utf-8"
                    )
                )
            ).encode("utf-8"),
        )
        self.assertEqual(
            (EXPERIMENTS / "w3_guard_matrix.md").read_bytes(),
            guard_matrix.render_matrix(guard_matrix.build_matrix()).encode("utf-8"),
        )
        self.assertEqual(
            (EXPERIMENTS / ceiling.REPORT_NAME).read_bytes(),
            ceiling.render_report(
                robustness.load_records(
                    (EXPERIMENTS / ceiling.EVIDENCE_NAME).read_text(
                        encoding="utf-8"
                    )
                )
            ).encode("utf-8"),
        )

    def test_historical_reports_keep_the_old_wording_verbatim(self):
        """归档报告是当时的证据：口径纠正不回溯改字。"""
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
            "random.random", "uuid", "os.environ", "requests", "pydantic",
        ):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)
        tops = set(re.findall(r"^(?:from|import)\s+([A-Za-z_]\w*)", source, re.M))
        self.assertTrue(
            tops <= {
                "__future__", "argparse", "tempfile", "pathlib", "typing",
                "eval", "runtime",
            },
            f"出现非标准库 / 非本地导入：{sorted(tops)}",
        )

    def test_source_has_no_hardcoded_task_ids(self):
        source = MODULE_SOURCE.read_text(encoding="utf-8")
        for index in range(1, 11):
            with self.subTest(index=index):
                self.assertNotIn(f"T{index:02d}", source)

    def test_measurement_inputs_are_reused_not_duplicated(self):
        source = MODULE_SOURCE.read_text(encoding="utf-8")
        for phrase in (
            "robustness.DEFAULT_TASKS", "robustness.parse_seeds",
            "robustness.dumps_records", "robustness.load_records",
            "robustness.DEFAULT_FAULT_PER_MILLE", "analyze.summarize",
            "TemporaryDirectory", "--from-jsonl",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)

    def test_k_values_and_cells_are_versioned(self):
        self.assertEqual(resolution.DEFAULT_K_VALUES, (3, 5, 8))
        self.assertEqual(resolution.CELL_NAMES, ("off", "schema", "all4"))
        self.assertEqual(resolution.TREATMENTS, ("schema", "all4"))
        self.assertEqual(
            len(resolution.CELLS[-1][1]), 4,
            "all4 格必须开启全部四条护栏",
        )
        self.assertEqual(
            tuple(sorted(resolution.CELLS[-1][1])),
            tuple(sorted(guard.GUARD_NAMES)),
        )


class TestDocumentation(unittest.TestCase):
    def test_readme_states_resolution_and_the_correction(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "w3_resolution.md", "测量仪器审计", "分辨率恒为 1/N",
            "提高 k 不改变分辨率", "难度旋钮", "新增独立任务",
            "零效应要拆成两类", "主指标仍是 k=3",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertNotIn("扩任务集 / 提高 k / 把固有缺陷分层标注", text)

    def test_tech_stack_describes_the_new_module_and_the_discipline(self):
        text = (ROOT / "docs" / "tech-stack.md").read_text(encoding="utf-8")
        for phrase in (
            "eval/resolution.py", "最小步进恒为 1/N", "与 k 无关",
            "不回溯改字", "零效应拆成两类", "w3_resolution.md",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_live_docs_no_longer_recommend_raising_k(self):
        """活文档里「提高 k」只能出现在否定 / 纠正语境。"""
        for relative in ("README.md", "docs/tech-stack.md"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(doc=relative):
                self.assertNotIn("先扩大任务集或提高 k，否则", text)
                self.assertIn("提高 k", text)


if __name__ == "__main__":
    unittest.main()
