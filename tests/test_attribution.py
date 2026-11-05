"""W2 失败归因：trace 首个偏离点、unknown 兜底、证据落结果行。

同时守住 W3 对照实验的命根子：口径升级后，默认任务集的 pass 数字与
6 类失败分布必须与 W1 冻结基线逐项一致，不能靠改归因把基线「修好看」。
"""
from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from env import db, tools
from eval import analyze, grader, runner, trace
from eval import config as exp_config
from runtime.loop import RETRY_HINT

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"
FROZEN = ROOT / "experiments" / "baseline" / "result_rows.jsonl"

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


def task(target_db=None, policy_checks=(), disclosures=()):
    return {
        "id": "A",
        "user_utterances": ["退款"],
        "initial_db": INITIAL,
        "target_db": target_db or {},
        "policy_checks": list(policy_checks),
        "required_disclosures": list(disclosures),
    }


def span(step, role, observation=None, tool=None):
    return {
        "trial_id": "A-t1",
        "step": step,
        "role": role,
        "tool": tool,
        "args": None,
        "observation": observation,
        "tokens": 0,
        "latency_ms": 0,
    }


def info(spans=(), errors=(), termination="final", final_text="办好了"):
    return {
        "termination": termination,
        "errors": list(errors),
        "final_text": final_text,
        "spans": list(spans),
    }


def state_after(actions):
    template = db.build_template(INITIAL)
    conn = db.reset(template)
    for name, args in actions:
        tools.call(name, args, conn)
    state = db.dump_state(conn)
    conn.close()
    template.close()
    return state


class TestTraceEvents(unittest.TestCase):
    def test_events_are_extracted_and_sorted_by_step(self):
        spans = [
            span(7, "assistant", "x"),
            span(3, "tool", {"ok": False, "error": "bad_args"}, "refund_order"),
            span(5, "tool", {"ok": True}, "refund_order"),
            span(2, "user", RETRY_HINT + " (invalid_json)"),
            span(1, "user", "我要退款"),
        ]
        events = grader.trace_events(spans)
        self.assertEqual([e["step"] for e in events], [2, 3, 5, 7])
        self.assertEqual(
            [e["kind"] for e in events],
            ["output_format", "bad_args", "state_write", "assistant"],
        )

    def test_read_tools_and_normal_utterances_are_not_events(self):
        spans = [
            span(1, "user", "帮我查一下"),
            span(2, "tool", {"ok": True}, "get_order"),
        ]
        self.assertEqual(grader.trace_events(spans), [])


class TestFirstDivergence(unittest.TestCase):
    def test_earliest_tool_error_wins(self):
        spans = [
            span(3, "tool", {"ok": False, "error": "bad_args"}, "refund_order"),
            span(5, "tool", {"ok": False, "error": "wrong_tool"}, "refund_money"),
        ]
        verdict = grader.grade(
            db.normalize_target({}, INITIAL),
            task({"orders": [{"id": "O-100", "refunded_cents": 2500}]}),
            info(spans),
        )
        self.assertEqual(verdict["failure_type"], "bad_args")
        self.assertEqual(verdict["first_divergence"]["step"], 3)

    def test_policy_violation_anchors_first_write_tool(self):
        state = state_after(
            [("refund_order", {"order_id": "O-100", "amount_cents": 9999})]
        )
        spans = [
            span(2, "tool", {"ok": True}, "get_order"),
            span(4, "tool", {"ok": True}, "refund_order"),
            span(6, "tool", {"ok": True}, "notify_customer"),
        ]
        verdict = grader.grade(
            state, task(policy_checks=("no_over_refund",)), info(spans)
        )
        self.assertEqual(verdict["failure_type"], "policy_violation")
        self.assertEqual(verdict["first_divergence"]["step"], 4)
        self.assertEqual(verdict["first_divergence"]["kind"], "state_write")

    def test_json_failure_anchors_last_retry(self):
        spans = [
            span(2, "user", RETRY_HINT + " (invalid_json)"),
            span(4, "user", RETRY_HINT + " (missing_fields)"),
        ]
        verdict = grader.grade(
            db.normalize_target({}, INITIAL),
            task(),
            info(spans, termination="json_failure", final_text=""),
        )
        self.assertEqual(verdict["failure_type"], "output_format")
        self.assertEqual(verdict["first_divergence"]["step"], 4)

    def test_max_steps_anchors_last_event(self):
        spans = [span(2, "assistant", "x"), span(4, "assistant", "y")]
        verdict = grader.grade(
            db.normalize_target({}, INITIAL),
            task(),
            info(spans, termination="max_steps", final_text=""),
        )
        self.assertEqual(verdict["failure_type"], "loop_or_timeout")
        self.assertEqual(verdict["first_divergence"]["step"], 4)

    def test_unknown_when_terminal_state_is_off_without_evidence(self):
        verdict = grader.grade(
            db.normalize_target({}, INITIAL),
            task({"orders": [{"id": "O-100", "refunded_cents": 2500}]}),
            info([span(1, "user", "退款")]),
        )
        self.assertEqual(verdict["failure_type"], "unknown")
        self.assertIsNone(verdict["first_divergence"])
        self.assertIn("evidence:none", verdict["reasons"])
        self.assertIn("attribution:unknown", verdict["reasons"])

    def test_intent_misread_when_only_disclosure_is_missing(self):
        verdict = grader.grade(
            db.normalize_target({}, INITIAL),
            task(disclosures=("O-100",)),
            info([span(1, "user", "退款")], final_text="办好了"),
        )
        self.assertEqual(verdict["failure_type"], "intent_misread")

    def test_errors_fallback_is_marked_as_such(self):
        verdict = grader.grade(
            db.normalize_target({}, INITIAL),
            task({"orders": [{"id": "O-100", "refunded_cents": 2500}]}),
            {"termination": "final", "errors": ["wrong_tool"], "final_text": "x"},
        )
        self.assertEqual(verdict["failure_type"], "wrong_tool")
        self.assertIsNone(verdict["first_divergence"])
        self.assertIn("evidence:errors_fallback", verdict["reasons"])

    def test_reasons_are_sorted_and_passed_case_is_clean(self):
        verdict = grader.grade(
            db.normalize_target({}, INITIAL), task(), info()
        )
        self.assertEqual(verdict["passed"], 1)
        self.assertEqual(verdict["reasons"], [])
        self.assertIsNone(verdict["first_divergence"])
        failed = grader.grade(
            db.normalize_target({}, INITIAL),
            task(disclosures=("O-100", "2500")),
            info(),
        )
        self.assertEqual(failed["reasons"], sorted(failed["reasons"]))


class TestAttributionInResults(unittest.TestCase):
    def test_rows_carry_reasons_and_divergence(self):
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
        self.assertTrue(all("reasons" in row for row in rows))
        self.assertTrue(all("first_divergence" in row for row in rows))
        failed = [row for row in rows if not row["passed"]]
        self.assertTrue(
            all(any(r.startswith("attribution:") for r in row["reasons"]) for row in failed)
        )
        self.assertTrue(
            all(row["first_divergence"] is not None for row in failed[:3])
        )

    def test_default_distribution_matches_frozen_baseline(self):
        """口径升级不得改变测量结果：这是 W3 单变量对照的前提。"""
        frozen = trace.read_jsonl(FROZEN)
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
        self.assertEqual(
            [(r["passed"], r["failure_type"], r["steps"]) for r in frozen],
            [(r["passed"], r["failure_type"], r["steps"]) for r in rows],
        )
        self.assertEqual(
            Counter(r["failure_type"] for r in frozen if not r["passed"]),
            Counter(r["failure_type"] for r in rows if not r["passed"]),
        )

    def test_report_has_attribution_section_with_step_anchor(self):
        cfg = exp_config.load_config()
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
        text = analyze.render_report(
            rows, runner.load_tasks(TASKS), cfg, exp_config.run_id(cfg),
            trace.read_jsonl(FROZEN),
        )
        self.assertIn("## 归因证据（首个偏离点）", text)
        self.assertIn("step 3 / wrong_tool / refund_money", text)
        self.assertIn("| T02 | `T02-t1` | policy_violation |", text)


if __name__ == "__main__":
    unittest.main()
