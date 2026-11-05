"""W3 多种子稳健性检验的验收。

本文件钉死四件事：
1. 效应公式与 `guard_matrix.effects_of` 同源，符号分布是纯计算，
   缺任一格直接报错而不是静默补零；
2. 仓库留档的证据 / 报告与当前代码逐字节一致，seed 集版本化，
   不许有人偷偷换 seed 把结论挑好看；
3. 核心结论：既有 `seed=11` 报告是偏乐观样本，
   `schema_validation` 在多数 seed 上零效应、但没有任何 seed 出现负效应；
4. 本轮对既有留档零侵入：run_id、矩阵报告与冻结基线不变，
   模块不读时钟 / 不用全局随机 / 不引第三方依赖，CLI 产物逐字节可复现。
"""
from __future__ import annotations

import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from eval import guard_matrix, robustness
from eval import config as exp_config

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "experiments" / robustness.EVIDENCE_NAME
REPORT = ROOT / "experiments" / robustness.REPORT_NAME
MATRIX_REPORT = ROOT / "experiments" / "w3_guard_matrix.md"
MODULE_SOURCE = ROOT / "eval" / "robustness.py"

# 30 个 seed 的实测符号分布；改 seed 集或改护栏都会让这里失败，
# 这正是守门的目的：稳健性结论必须能被重新算出来。
EXPECTED_DIST = {
    "schema": (5, 25, 0),
    "idempotency": (27, 3, 0),
    "interaction": (1, 29, 0),
}


def compact(text: str) -> str:
    return "".join(text.split())


def synthetic(seed: int, off: float, schema: float, idem: float,
              both: float) -> list[dict]:
    """只构造 per_seed_effects 需要的最少字段。"""
    return [
        {"seed": seed, "cell": cell, "passk": value}
        for cell, value in (
            ("off", off), ("schema", schema), ("idem", idem), ("both", both)
        )
    ]


class TestPureStatistics(unittest.TestCase):
    def test_per_seed_effects_reuse_the_matrix_formula(self):
        rows = robustness.per_seed_effects(synthetic(1, 0.1, 0.2, 0.2, 0.3))
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0]["effects"],
            guard_matrix.effects_of(
                {"off": 0.1, "schema": 0.2, "idem": 0.2, "both": 0.3}
            ),
        )

    def test_seed_order_follows_evidence_not_numeric_sort(self):
        records = synthetic(42, 0.0, 0.1, 0.1, 0.2) + synthetic(
            7, 0.0, 0.0, 0.1, 0.1
        )
        self.assertEqual(
            [row["seed"] for row in robustness.per_seed_effects(records)],
            [42, 7],
        )

    def test_missing_cell_fails_loudly(self):
        records = synthetic(1, 0.0, 0.1, 0.1, 0.2)[:-1]
        with self.assertRaisesRegex(ValueError, "缺少格子"):
            robustness.per_seed_effects(records)

    def test_distribution_counts_positive_zero_and_negative(self):
        records = (
            synthetic(1, 0.0, 0.2, 0.1, 0.3)   # schema +0.2, idem +0.1
            + synthetic(2, 0.1, 0.1, 0.1, 0.1)  # 全零
            + synthetic(3, 0.3, 0.2, 0.3, 0.2)  # schema -0.1
        )
        dist = robustness.distribution(robustness.per_seed_effects(records))
        self.assertEqual(
            (dist["schema"]["positive"], dist["schema"]["zero"],
             dist["schema"]["negative"]),
            (1, 1, 1),
        )
        self.assertEqual(
            (dist["idempotency"]["positive"], dist["idempotency"]["zero"],
             dist["idempotency"]["negative"]),
            (1, 2, 0),
        )
        self.assertEqual(dist["interaction"]["zero"], 3)
        self.assertAlmostEqual(dist["schema"]["mean"], 0.1 / 3)

    def test_float_noise_is_zero_not_negative(self):
        dist = robustness.distribution(
            robustness.per_seed_effects(synthetic(1, 0.1, 0.2, 0.2, 0.3))
        )
        self.assertEqual(dist["interaction"]["zero"], 1)
        self.assertEqual(dist["interaction"]["negative"], 0)

    def test_parse_seeds_defaults_and_rejects_bad_input(self):
        self.assertEqual(robustness.parse_seeds(None), robustness.DEFAULT_SEEDS)
        self.assertEqual(robustness.parse_seeds("11, 1,2"), (11, 1, 2))
        for bad in ("", "a,b", "1,-2", "1,1"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                robustness.parse_seeds(bad)

    def test_evidence_roundtrip_is_byte_stable(self):
        records = synthetic(11, 0.1, 0.2, 0.2, 0.3)
        text = robustness.dumps_records(records)
        self.assertEqual(text, robustness.dumps_records(records))
        self.assertEqual(robustness.load_records(text), records)
        self.assertTrue(text.endswith("\n"))

    def test_render_handles_a_sweep_without_the_headline_seed(self):
        text = robustness.render_sweep(synthetic(2, 0.1, 0.1, 0.2, 0.2))
        self.assertIn("不与该报告逐项对照", text)
        self.assertNotIn(str(ROOT), text, "报告里不得出现绝对路径")


class TestArchivedEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = robustness.load_records(EVIDENCE.read_text(encoding="utf-8"))

    def test_evidence_shape_and_versioned_seed_set(self):
        self.assertEqual(len(self.records), 120)
        seeds = []
        for record in self.records:
            if record["seed"] not in seeds:
                seeds.append(record["seed"])
        self.assertEqual(tuple(seeds), robustness.DEFAULT_SEEDS)
        self.assertEqual(len(seeds), 30)
        self.assertEqual(
            [record["cell"] for record in self.records[:4]],
            list(robustness.CELL_NAMES),
        )
        self.assertEqual(
            set(self.records[0]),
            {"seed", "cell", "guards", "pass1", "passk", "passk_hits",
             "task_count", "invalid_calls", "total_steps", "failure_counts"},
        )

    def test_archived_report_matches_code_byte_for_byte(self):
        self.assertEqual(
            REPORT.read_bytes(),
            robustness.render_sweep(self.records).encode("utf-8"),
        )
        self.assertEqual(
            EVIDENCE.read_bytes(),
            robustness.dumps_records(self.records).encode("utf-8"),
        )

    def test_distribution_matches_the_frozen_conclusion(self):
        dist = robustness.distribution(robustness.per_seed_effects(self.records))
        for key, (positive, zero, negative) in EXPECTED_DIST.items():
            with self.subTest(effect=key):
                self.assertEqual(
                    (dist[key]["positive"], dist[key]["zero"],
                     dist[key]["negative"]),
                    (positive, zero, negative),
                )
        self.assertAlmostEqual(dist["schema"]["mean"], 0.015)
        self.assertAlmostEqual(dist["idempotency"]["mean"], 0.095)

    def test_seed_11_is_the_optimistic_archive_sample(self):
        rows = robustness.per_seed_effects(self.records)
        headline = next(row for row in rows if row["seed"] == 11)
        self.assertEqual(
            headline["cells"],
            {"off": 0.1, "schema": 0.2, "idem": 0.2, "both": 0.3},
        )
        self.assertAlmostEqual(headline["effects"]["schema"], 0.10)
        self.assertAlmostEqual(headline["effects"]["idempotency"], 0.10)
        # 既有矩阵报告的单 seed 结论仍原样留档，本轮一个字节都没改
        self.assertEqual(
            MATRIX_REPORT.read_bytes(),
            guard_matrix.render_matrix(guard_matrix.build_matrix()).encode("utf-8"),
        )
        self.assertIn(
            "| pass^k | +0.10 | +0.10 | +0.00 |",
            MATRIX_REPORT.read_text(encoding="utf-8"),
        )

    def test_no_negative_effect_and_one_nonzero_interaction(self):
        rows = robustness.per_seed_effects(self.records)
        self.assertTrue(
            all(row["effects"][key] >= -robustness.EFFECT_EPSILON
                for row in rows for key in robustness.EFFECT_KEYS),
            "护栏不得让任何 seed 的 pass^k 变差",
        )
        nonzero = [
            row["seed"] for row in rows
            if abs(row["effects"]["interaction"]) > robustness.EFFECT_EPSILON
        ]
        self.assertEqual(nonzero, [9001])

    def test_report_states_the_honest_caveats(self):
        text = REPORT.read_text(encoding="utf-8")
        for phrase in (
            "偏乐观样本", "不能外推", "不是显著性检验", "不是随机抽样",
            "分辨率极粗", "没有任何一个出现负效应", "w3_guard_matrix.md",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        self.assertNotIn(str(ROOT), text)

    def test_existing_run_ids_are_untouched(self):
        self.assertEqual(
            exp_config.run_id(exp_config.load_config()),
            "baseline_scripted-a346e8acef15",
        )
        for filename, expected in (
            ("guard_ab_off.toml", "w3_ab_off-346ab5e98382"),
            ("guard_ab_on.toml", "w3_ab_on-fa17143eb761"),
            ("guard_ab_idem.toml", "w3_idem_on-a4e1925e1619"),
            ("guard_ab_both.toml", "w3_both_on-3c15f3bd94fd"),
        ):
            with self.subTest(config=filename):
                self.assertEqual(
                    exp_config.run_id(
                        exp_config.load_config(ROOT / "experiments" / filename)
                    ),
                    expected,
                )


class TestReproducibility(unittest.TestCase):
    def test_cli_produces_identical_artifacts_twice(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            artifacts = []
            for index in (1, 2):
                out = root / str(index)
                with redirect_stdout(io.StringIO()):
                    robustness.main(
                        ["--seeds", "11,1,2", "--out-dir", str(out)]
                    )
                artifacts.append((
                    (out / robustness.EVIDENCE_NAME).read_bytes(),
                    (out / robustness.REPORT_NAME).read_bytes(),
                ))
            self.assertEqual(artifacts[0], artifacts[1])

            readonly = root / "readonly"
            readonly.mkdir()
            (readonly / robustness.EVIDENCE_NAME).write_bytes(artifacts[0][0])
            with redirect_stdout(io.StringIO()):
                robustness.main(["--from-jsonl", "--out-dir", str(readonly)])
            self.assertEqual(
                (readonly / robustness.REPORT_NAME).read_bytes(),
                artifacts[0][1],
            )

    def test_from_jsonl_refuses_missing_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    robustness.main(["--from-jsonl", "--out-dir", d])


class TestModuleContract(unittest.TestCase):
    def test_source_is_clock_free_and_dependency_free(self):
        source = MODULE_SOURCE.read_text(encoding="utf-8")
        for banned in (
            "datetime", "time.time", "import time", "import random",
            "random.random", "uuid", "os.environ", "requests", "pydantic",
        ):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)
        tops = set(
            re.findall(r"^(?:from|import)\s+([A-Za-z_]\w*)", source, re.M)
        )
        self.assertTrue(
            tops <= {
                "__future__", "argparse", "json", "tempfile", "collections",
                "pathlib", "typing", "eval", "runtime",
            },
            f"出现非标准库 / 非本地导入：{sorted(tops)}",
        )

    def test_effect_formula_is_imported_not_duplicated(self):
        source = MODULE_SOURCE.read_text(encoding="utf-8")
        self.assertIn("from eval.guard_matrix import effects_of", source)
        self.assertIn("effects_of(cells)", source)
        self.assertIn("TemporaryDirectory", source)
        self.assertIn("--from-jsonl", source)

    def test_seed_set_is_hardcoded_and_unique(self):
        seeds = robustness.DEFAULT_SEEDS
        self.assertEqual(len(seeds), 30)
        self.assertEqual(len(set(seeds)), 30)
        self.assertEqual(seeds[0], 11, "既有报告所用 seed 必须排第一")
        self.assertTrue(all(isinstance(value, int) and value >= 0 for value in seeds))

    def test_docs_describe_the_robustness_limit(self):
        readme = compact((ROOT / "README.md").read_text(encoding="utf-8"))
        stack = compact((ROOT / "docs" / "tech-stack.md").read_text(encoding="utf-8"))
        for phrase in ("多种子", "偏乐观", "w3_robustness.md", "不是显著性检验"):
            with self.subTest(doc="README.md", phrase=phrase):
                self.assertIn(compact(phrase), readme)
        for phrase in ("eval/robustness.py", "多种子", "偏乐观", "不是显著性检验"):
            with self.subTest(doc="tech-stack.md", phrase=phrase):
                self.assertIn(compact(phrase), stack)


if __name__ == "__main__":
    unittest.main()
