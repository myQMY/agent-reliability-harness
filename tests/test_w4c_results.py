"""W4-c 实测归档守门测试。

只读已入库的证据，不联网、不重跑模型、不重算判分。三件事钉死：

1. 本组主读数是**判定分布**（15 个可比事件 / 14 identical / 1 different /
   `missing = 0`），不是 `pass^k`（两格仍 0.00）；
2. 报告必须连「同策略复算、动作空间窄、不等于 W3 机制被验证、不等于护栏
   有效」这几层边界一起讲，防止把 14/15 读成理解率；
3. W3 / W4 首轮 / W4-b 的归档与两个任务集零改动，side-car 不进归档三件。
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from eval import config as exp_config
from eval import w4_preflight as preflight
from runtime import guard

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS = REPO_ROOT / "experiments" / "runs"
OFF_RID = "w4c_off_flash-b891da858ccb"
ON_RID = "w4c_all4_flash-b38a0c5106d6"
PRIOR_ROUNDS = (
    "w4_off_flash-61491decf48c",
    "w4_all4_flash-f6a4ea1674dc",
    "w4b_off_flash-f6e556cd7201",
    "w4b_all4_flash-6d7222dc87af",
)


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


class TestW4cArchives(unittest.TestCase):
    def test_both_archives_have_exactly_three_artifacts(self):
        for rid in (OFF_RID, ON_RID):
            directory = RUNS / rid
            self.assertTrue(directory.is_dir(), rid)
            self.assertEqual(
                {p.name for p in directory.iterdir() if p.is_file()},
                {"config.json", "result_rows.jsonl", "report.md"},
                rid,
            )
            # side-car 是独立目录，绝不混入归档三件，也不进 config 快照。
            self.assertNotIn("clean_actions_dir", _text(directory / "config.json"))

    def test_side_car_is_gitignored_like_other_run_artifacts(self):
        """side-car 是观测产物：实际 ignore 规则与报告/设计文档的口径必须一致。"""
        ignore = _text(REPO_ROOT / ".gitignore")
        for pattern in ("/traces/", "/results/", "/clean_actions/"):
            self.assertIn(pattern, ignore, pattern)
        report = _text(REPO_ROOT / "experiments" / "w4c_report.md")
        self.assertIn("被 `.gitignore` 排除，不入库", report)
        design = _text(REPO_ROOT / "docs" / "w4c-design.md")
        self.assertIn("它是观测产物", design)

    def test_run_ids_are_new_and_config_matches(self):
        for rid in (OFF_RID, ON_RID):
            self.assertNotIn(rid, PRIOR_ROUNDS)
            self.assertEqual(_config(rid)["run_id"], rid)
        off = exp_config.load_config(
            REPO_ROOT / "experiments" / "w4c_off_flash.toml"
        )
        on = exp_config.load_config(
            REPO_ROOT / "experiments" / "w4c_all4_flash.toml"
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
        # 首轮 W4 的 OFF 组 invalid_calls 合计为 0；W4-c 必须 > 0。
        rows = _rows(OFF_RID)
        self.assertGreater(sum(row["invalid_calls"] for row in rows), 0)
        self.assertIn("bad_args", {row["failure_type"] for row in rows})

    def test_judgement_ceiling_is_unchanged(self):
        for rid in (OFF_RID, ON_RID):
            rows = _rows(rid)
            self.assertEqual(
                sum(1 for r in rows if "terminal_state_mismatch" in r["reasons"]),
                30,
                rid,
            )

    def test_single_variable_includes_injection_params(self):
        off = _config(OFF_RID)["config"]
        on = _config(ON_RID)["config"]
        for field in preflight._MEASURED_FIELDS:
            self.assertEqual(off[field], on[field], field)
        self.assertEqual(off["seed"], 11)
        self.assertEqual(off["fault_per_mille"], 300)
        self.assertEqual(off.get("guards", []), [])
        self.assertEqual(tuple(on["guards"]), tuple(guard.GUARD_NAMES))

    def test_compare_points_to_w4c_off_archive(self):
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


class TestW4cProbeEvidence(unittest.TestCase):
    def test_summary_rows_are_exact(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_probe.md")
        self.assertIn("| w4c_off | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |", text)
        self.assertIn("| w4c_all4 | 15 | 15 | 14 | 1 | 0 | 0 | 0 | 0 | 0 |", text)

    def test_three_guard_kinds_and_no_idempotency_row(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_probe.md")
        self.assertIn("| w4c_all4 | `schema_validation` | 4 | 4 | 4 | 0 |", text)
        self.assertIn("| w4c_all4 | `tool_retry` | 5 | 5 | 5 | 0 |", text)
        self.assertIn("| w4c_all4 | `json_recovery` | 6 | 6 | 5 | 1 |", text)
        # 本轮确实没有 idempotency 拒绝，不能把 W4-b 那 1 次混进来。
        self.assertNotIn("| w4c_all4 | `idempotency` |", text)

    def test_only_different_event_is_a_free_text_rewrite(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_probe.md")
        self.assertIn("`T08-t1`", text)
        self.assertIn("`different`", text)
        self.assertIn("输出被注入扰动，不是纯净模型行为", text)
        self.assertIn("comparable = identical + different + final_instead", text)


class TestW4cCalibration(unittest.TestCase):
    def test_three_way_device_calibration(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_probe_dryrun.md")
        self.assertIn("| `fpm=0` 基准 | 给不同动作 | 0 | 0 | 0 | 0 | 0 | 0 |", text)
        self.assertIn("| A：给不同动作 | 固定的另一个合法读工具调用 | 16 | 16 | 13 | 0 | 13 | 0 |", text)
        self.assertIn("| B：重发同一动作 | 返回上一次被替换的干净动作 | 16 | 16 | 13 | 13 | 0 | 0 |", text)
        self.assertIn("不是 W4-c 的实验结论", text)
        self.assertIn("防代答红线", text)
        self.assertIn("零成本", text)


class TestW4cReportDiscipline(unittest.TestCase):
    def test_report_leads_with_the_injected_output_warning(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_report.md")
        self.assertTrue(text.startswith("# W4-c 实测报告"))
        self.assertIn(
            "本报告的全部输出都被注入了输出级故障，不是纯净模型行为", text[:400]
        )

    def test_report_states_the_main_readout_and_the_open_item(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_report.md")
        for phrase in (
            "**14 次 `identical`**",
            "**1 次 `different`**",
            "`missing = 0`",
            "15 个可比事件",
            "输出层得到支持",
            "本组主读数是判定分布，不是通过率",
        ):
            self.assertIn(phrase, text, phrase)

    def test_report_carries_every_caveat(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_report.md")
        for phrase in (
            "同策略复算，不是回忆 / 理解",
            "动作空间窄",
            "14/15 不是理解率",
            "**15/15 的工具名与结构化参数完全一致**",
            "`identical` 是规范化判定",
            "4 个",
            "`idempotency_skipped` 分层本轮 0 样本",
            "不是纯净模型行为",
            "不等于 W3 机制被完整验证",
            "不等于护栏有效",
            "30/30 都带 `terminal_state_mismatch`",
            "描述性统计，不是显著性检验",
            "继续不得外推",
            "建议收口",
        ):
            self.assertIn(phrase, text, phrase)

    def test_report_does_not_claim_guards_work(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_report.md")
        self.assertIn("护栏在真实模型上有效 / 复现了 W3 的提升", text)
        self.assertIn("论效果只认 `pass^k`", text)
        for banned in (
            "护栏在真实模型上有效（已验证）",
            "W3 的 0.1433 在真实模型上复现",
            "14/15 的理解率已证实",
            "护栏让分数提升",
        ):
            self.assertNotIn(banned, text, banned)

    def test_report_records_the_three_zero_cost_gates(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_report.md")
        self.assertIn("保险闸", text)
        self.assertIn("`invalid_calls = 15`", text)
        self.assertIn("12 PASS / READY", text)
        self.assertIn("A 路", text)
        self.assertIn("B 路", text)

    def test_report_warns_about_auto_reproduction_command(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_report.md")
        self.assertIn("不含", text)
        self.assertIn("--clean-actions-dir", text)
        self.assertIn("照抄那段命令不会生成 side-car", text)

    def test_design_doc_has_prewritten_rules(self):
        text = _text(REPO_ROOT / "docs" / "w4c-design.md")
        for phrase in (
            "成功判据不是 `pass^k` 提升",
            "防代答红线",
            "判定规则（跑批前写死）",
            "停止条件",
        ):
            self.assertIn(phrase, text, phrase)


class TestW4cPreflightSnapshot(unittest.TestCase):
    def test_snapshot_is_ready(self):
        text = _text(REPO_ROOT / "experiments" / "w4c_preflight.md")
        self.assertIn("整体状态：**READY**", text)
        self.assertIn("0 FAIL", text)
        self.assertTrue((RUNS / OFF_RID / "result_rows.jsonl").is_file())


class TestPriorRoundsUntouched(unittest.TestCase):
    def test_prior_archives_still_exist_with_same_run_ids(self):
        for rid in PRIOR_ROUNDS:
            self.assertEqual(_config(rid)["run_id"], rid)
            self.assertEqual(len(_rows(rid)), 30)

    def test_first_round_config_has_no_injection_params(self):
        for rid in PRIOR_ROUNDS[:2]:
            self.assertNotIn("fault_per_mille", _config(rid)["config"])

    def test_two_versioned_tasksets_sha256_unchanged(self):
        for name, expected in preflight.TASKSET_SHA256.items():
            actual = hashlib.sha256(
                (REPO_ROOT / "tasks" / name).read_bytes()
            ).hexdigest()
            self.assertEqual(actual, expected, name)


if __name__ == "__main__":
    unittest.main()
