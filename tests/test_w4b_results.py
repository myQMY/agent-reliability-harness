"""W4-b 实测归档守门测试。

只读已入库的证据，不联网、不重跑模型、不重算判分。目的不是「证明实验成功」，
而是把两件事钉死：① W4-b 的**主读数是探针分母**（17 次拒绝），不是 `pass^k`
（两格仍 0.00）；② 报告必须连三层边界一起讲，防止把 17/17 responsive
误读成「护栏有效 / W3 机制已被验证」。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from eval import config as exp_config
from eval import w4_preflight as preflight
from runtime import guard

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS = REPO_ROOT / "experiments" / "runs"
OFF_RID = "w4b_off_flash-f6e556cd7201"
ON_RID = "w4b_all4_flash-6d7222dc87af"
FIRST_ROUND = ("w4_off_flash-61491decf48c", "w4_all4_flash-f6a4ea1674dc")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rows(rid: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in _text(RUNS / rid / "result_rows.jsonl").splitlines()
        if line.strip()
    ]


def _config(rid: str) -> dict[str, Any]:
    return json.loads(_text(RUNS / rid / "config.json"))


class TestW4bArchives(unittest.TestCase):
    def test_both_archives_have_three_artifacts(self):
        for rid in (OFF_RID, ON_RID):
            directory = RUNS / rid
            self.assertTrue(directory.is_dir(), rid)
            for name in ("config.json", "result_rows.jsonl", "report.md"):
                self.assertTrue((directory / name).is_file(), f"{rid}/{name}")

    def test_run_ids_are_new_and_config_matches(self):
        for rid in (OFF_RID, ON_RID):
            self.assertNotIn(rid, FIRST_ROUND)
            self.assertEqual(_config(rid)["run_id"], rid)
        off = exp_config.load_config(
            REPO_ROOT / "experiments" / "w4b_off_flash.toml"
        )
        on = exp_config.load_config(
            REPO_ROOT / "experiments" / "w4b_all4_flash.toml"
        )
        self.assertEqual(exp_config.run_id(off), OFF_RID)
        self.assertEqual(exp_config.run_id(on), ON_RID)
        self.assertEqual(
            json.loads(_text(RUNS / OFF_RID / "config.json")),
            exp_config.config_snapshot(off, OFF_RID),
        )

    def test_each_cell_has_30_trials_all_failed(self):
        for rid in (OFF_RID, ON_RID):
            rows = _rows(rid)
            self.assertEqual(len(rows), 30, rid)
            self.assertEqual({row["passed"] for row in rows}, {0}, rid)
            self.assertEqual({row["trial"] for row in rows}, {1, 2, 3}, rid)

    def test_injection_reached_the_off_cell(self):
        # 首轮 OFF 组 invalid_calls 合计为 0；W4-b 必须 > 0，否则说明注入
        # 没接上，ALL4 那 17 次拒绝就无从谈起。
        rows = _rows(OFF_RID)
        self.assertGreater(sum(row["invalid_calls"] for row in rows), 0)
        self.assertIn(
            "bad_args",
            {row["failure_type"] for row in rows},
        )

    def test_single_variable_includes_injection_params(self):
        off = _config(OFF_RID)["config"]
        on = _config(ON_RID)["config"]
        for field in (*preflight._MEASURED_FIELDS,):
            self.assertEqual(off[field], on[field], field)
        self.assertEqual(off["seed"], 11)
        self.assertEqual(off["fault_per_mille"], 300)
        self.assertEqual(off.get("guards", []), [])
        self.assertEqual(tuple(on["guards"]), tuple(guard.GUARD_NAMES))

    def test_compare_points_to_w4b_off_archive(self):
        self.assertEqual(_config(OFF_RID)["config"]["compare"], "")
        self.assertEqual(
            _config(ON_RID)["config"]["compare"],
            f"experiments/runs/{OFF_RID}/result_rows.jsonl",
        )

    def test_ab_table_shows_no_passk_resolution(self):
        text = _text(RUNS / ON_RID / "report.md")
        self.assertIn("与对照组对照（A/B）", text)
        self.assertIn(f"experiments/runs/{OFF_RID}/result_rows.jsonl", text)
        self.assertIn("| pass^3 | 0.00 | 0.00 | +0.00 |", text)


class TestW4bProbe(unittest.TestCase):
    def test_denominator_is_nonzero_and_four_guards_covered(self):
        text = _text(REPO_ROOT / "experiments" / "w4b_probe.md")
        self.assertIn("| w4b_off | 0 | 0 | 0 | 0 | 0 | 0 |", text)
        self.assertIn("| w4b_all4 | 17 | 17 | 17 | 17 | 17 | 15 |", text)
        for name in guard.GUARD_NAMES:
            self.assertIn(f"`{name}`", text, name)

    def test_device_calibration_proves_denominator_comes_from_injection(self):
        text = _text(REPO_ROOT / "experiments" / "w4b_probe_dryrun.md")
        self.assertIn("| OFF（护栏全关） | 14 | 0 | 0 | 0 |", text)
        self.assertIn("| ALL4（四条全开） | 14 | 14 | 14 | 14 |", text)
        self.assertIn("fpm=0", text)
        self.assertIn("不是 W4-b 的实验结论", text)


class TestW4bReportDiscipline(unittest.TestCase):
    def test_report_leads_with_the_injected_output_warning(self):
        text = _text(REPO_ROOT / "experiments" / "w4b_report.md")
        self.assertTrue(text.startswith("# W4-b 实测报告"))
        # 置顶警告与 §6 的边界措辞不同，两处都要在：前者管第一眼，后者管结论。
        self.assertIn(
            "本报告的全部输出都被注入了输出级故障，不是纯净模型行为", text[:600]
        )
        self.assertIn("输出被注入扰动，不是纯净模型行为", text)

    def test_report_states_h1_supported_only_literally(self):
        text = _text(REPO_ROOT / "experiments" / "w4b_report.md")
        for phrase in (
            "H1 在字面定义上得到支持",
            "装置局限",
            "被替换掉的干净动作没有留档",
            "含这部分**机械成分**",
            "继续不得外推",
            "假设性重算",
            "输出层",
        ):
            self.assertIn(phrase, text, phrase)

    def test_report_does_not_claim_guards_work(self):
        text = _text(REPO_ROOT / "experiments" / "w4b_report.md")
        self.assertIn("护栏在真实模型上有效 / 复现了 W3 的提升", text)
        self.assertIn("响应机制成立 ≠ 任务成功 ≠ 护栏有效", text)
        self.assertIn("这不等于护栏有效", text)
        for banned in (
            "护栏在真实模型上有效（已验证）",
            "W3 的 0.1433 在真实模型上复现",
            "100% 理解率，已证实",
            "护栏让分数提升",
        ):
            self.assertNotIn(banned, text, banned)

    def test_report_records_the_off_cell_safety_gate(self):
        text = _text(REPO_ROOT / "experiments" / "w4b_report.md")
        self.assertIn("保险闸门", text)
        self.assertIn("invalid_calls = 11", text)
        self.assertIn("21 处注入特征", text)

    def test_design_doc_has_judgement_rules_and_stop_conditions(self):
        text = _text(REPO_ROOT / "docs" / "w4b-design.md")
        for phrase in (
            "拒绝后绝不代答",
            "成功判据不是 `pass^k` 提升",
            "H1 判定规则",
            "口径红线与停止条件",
            "experiments/w4b_report.md",
        ):
            self.assertIn(phrase, text, phrase)


class TestW4bPreflightSnapshot(unittest.TestCase):
    def test_snapshot_is_ready(self):
        text = _text(REPO_ROOT / "experiments" / "w4b_preflight.md")
        self.assertIn("整体状态：**READY**", text)
        self.assertIn("0 FAIL", text)
        self.assertTrue((RUNS / OFF_RID / "result_rows.jsonl").is_file())


class TestFirstRoundUntouched(unittest.TestCase):
    def test_first_round_archives_still_exist_with_same_run_ids(self):
        for rid in FIRST_ROUND:
            self.assertEqual(_config(rid)["run_id"], rid)
            self.assertEqual(len(_rows(rid)), 30)
            self.assertNotIn("fault_per_mille", _config(rid)["config"])

    def test_two_versioned_tasksets_sha256_unchanged(self):
        import hashlib

        for name, expected in preflight.TASKSET_SHA256.items():
            actual = hashlib.sha256(
                (REPO_ROOT / "tasks" / name).read_bytes()
            ).hexdigest()
            self.assertEqual(actual, expected, name)


if __name__ == "__main__":
    unittest.main()
