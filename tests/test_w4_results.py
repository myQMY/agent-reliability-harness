"""W4 首轮真实模型实验的归档守门测试。

只读取已入库的证据文件：不发起网络请求、不重跑模型、不重算判分。
这些断言的目的不是「证明实验成功」，而是把首轮**负面结果**的关键事实钉死，
防止后续把两格 0.00 / 探针 0 次拒绝误读成正向结论、或静默改动归档。
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from eval import w4_preflight as preflight
from runtime import guard

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS = REPO_ROOT / "experiments" / "runs"
OFF_RID = "w4_off_flash-61491decf48c"
ON_RID = "w4_all4_flash-f6a4ea1674dc"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rows(rid: str) -> list[dict[str, Any]]:
    lines = _text(RUNS / rid / "result_rows.jsonl").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _config(rid: str) -> dict[str, Any]:
    return json.loads(_text(RUNS / rid / "config.json"))


class TestW4Archives(unittest.TestCase):
    def test_both_archives_have_three_artifacts(self):
        for rid in (OFF_RID, ON_RID):
            directory = RUNS / rid
            self.assertTrue(directory.is_dir(), rid)
            for name in ("config.json", "result_rows.jsonl", "report.md"):
                self.assertTrue((directory / name).is_file(), f"{rid}/{name}")

    def test_each_cell_has_30_trials_all_failed(self):
        for rid in (OFF_RID, ON_RID):
            rows = _rows(rid)
            self.assertEqual(len(rows), 30, rid)
            self.assertEqual({row["passed"] for row in rows}, {0}, rid)
            self.assertEqual({row["trial"] for row in rows}, {1, 2, 3}, rid)
            self.assertEqual(sum(row["invalid_calls"] for row in rows), 0, rid)

    def test_single_variable_is_guards_enabled(self):
        off = _config(OFF_RID)["config"]
        on = _config(ON_RID)["config"]
        for field in ("tasks", "k", "backend", "max_steps", "max_json_retries"):
            self.assertEqual(off[field], on[field], field)
        self.assertEqual(off.get("guards", []), [])
        self.assertEqual(tuple(on["guards"]), tuple(guard.GUARD_NAMES))

    def test_compare_points_to_the_same_backend_off_archive(self):
        self.assertEqual(_config(OFF_RID)["config"]["compare"], "")
        self.assertEqual(
            _config(ON_RID)["config"]["compare"],
            f"experiments/runs/{OFF_RID}/result_rows.jsonl",
        )

    def test_ab_table_exists_and_shows_zero_difference(self):
        text = _text(RUNS / ON_RID / "report.md")
        self.assertIn("与对照组对照（A/B）", text)
        self.assertIn(f"experiments/runs/{OFF_RID}/result_rows.jsonl", text)
        self.assertIn("| pass^3 | 0.00 | 0.00 | +0.00 |", text)


class TestW4Probe(unittest.TestCase):
    def test_probe_counts_zero_rejections_in_both_cells(self):
        text = _text(REPO_ROOT / "experiments" / "w4_probe.md")
        self.assertIn("| w4_off | 0 | 0 | 0 | 0 | 0 | 0 |", text)
        self.assertIn("| w4_all4 | 0 | 0 | 0 | 0 | 0 | 0 |", text)

    def test_zero_rejections_did_not_erase_offline_calibration(self):
        # 首轮 0 拒绝是「装置没制造出触发条件」，不能反过来把尺子的
        # 离线校准证据（全开组能数到拒绝）抹掉，否则会误判探针失效。
        text = _text(REPO_ROOT / "experiments" / "w4_probe_dryrun.md")
        self.assertIn("responsive", text)
        self.assertIn("seeded_all4", text)


class TestW4ReportDiscipline(unittest.TestCase):
    def test_report_states_h1_untested_not_falsified(self):
        text = _text(REPO_ROOT / "experiments" / "w4_report.md")
        for phrase in (
            "H1 未被检验",
            "分母为 0",
            "不得外推",
            "不可拼成一条曲线",
            "假设性重算",
            "护栏从未介入",
            "fault_per_mille",
        ):
            self.assertIn(phrase, text, phrase)

    def test_report_does_not_claim_guards_work(self):
        text = _text(REPO_ROOT / "experiments" / "w4_report.md")
        self.assertIn("不能据此判断护栏在真实模型上有效或无效", text)
        self.assertIn("**不说**「护栏在真实模型上无效 / 有害」", text)
        for banned in (
            "W4 证明了护栏有效",
            "复现了 W3 的提升",
            "护栏让 `pass^k` 提升",
        ):
            self.assertNotIn(banned, text, banned)

    def test_design_doc_records_the_device_defect(self):
        text = _text(REPO_ROOT / "docs" / "w4-design.md")
        self.assertIn("装置缺陷", text)
        self.assertIn("W4-b", text)
        self.assertIn("H1 未被检验", text)


class TestW4PreflightSnapshot(unittest.TestCase):
    def test_snapshot_is_ready_only_after_off_archive_exists(self):
        text = _text(REPO_ROOT / "experiments" / "w4_preflight.md")
        self.assertIn("READY", text)
        self.assertIn("0 FAIL", text)
        self.assertTrue((RUNS / OFF_RID / "result_rows.jsonl").is_file())


class TestTasksetsUntouched(unittest.TestCase):
    def test_two_versioned_tasksets_sha256_unchanged(self):
        for name, expected in preflight.TASKSET_SHA256.items():
            actual = hashlib.sha256(
                (REPO_ROOT / "tasks" / name).read_bytes()
            ).hexdigest()
            self.assertEqual(actual, expected, name)


if __name__ == "__main__":
    unittest.main()
