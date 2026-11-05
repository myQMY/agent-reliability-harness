"""W2 条件话术：规则解析、命中语义、主链路确实传入上下文。

特别守住一条边界：默认任务集在 ScriptedBackend 下不命中任何规则，
第二句话术与 W1 完全一致，冻结基线因此保持可比。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from eval import runner, trace
from eval.user_sim import (
    ConditionalUtterance,
    RuleUserSimulator,
    TurnContext,
    parse_rules,
)

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"
UTTERANCES = ["帮我退款", "那退 3000 分吧。"]


class TestPlainSimulator(unittest.TestCase):
    def test_turns_replays_preset_utterances(self):
        sim = RuleUserSimulator(UTTERANCES)
        self.assertEqual(sim.turn_count, 2)
        self.assertEqual(sim.rule_count, 0)
        self.assertEqual(list(sim.turns()), UTTERANCES)
        self.assertEqual(sim.utterance_for(1), UTTERANCES[0])
        self.assertEqual(sim.utterance_for(2), UTTERANCES[1])

    def test_empty_utterances_rejected(self):
        with self.assertRaises(ValueError):
            RuleUserSimulator([])

    def test_turn_out_of_range_rejected(self):
        sim = RuleUserSimulator(UTTERANCES)
        for turn in (0, 3, -1):
            with self.subTest(turn=turn):
                with self.assertRaises(ValueError):
                    sim.utterance_for(turn)

    def test_turns_never_hits_rule_without_context(self):
        sim = RuleUserSimulator(
            UTTERANCES,
            [{"turn": 2, "text": "替换了", "when": {"final_contains_any": ["请问"]}}],
        )
        self.assertEqual(list(sim.turns()), UTTERANCES)


class TestConditionalRules(unittest.TestCase):
    def setUp(self):
        self.sim = RuleUserSimulator(
            UTTERANCES,
            [{"turn": 2, "text": "订单 O-100，退 3000 分。", "when": {"final_contains_any": ["请问", "哪笔"]}}],
        )

    def test_hits_when_previous_final_asks(self):
        self.assertEqual(
            self.sim.utterance_for(2, TurnContext(last_final="请问要退哪笔订单？")),
            "订单 O-100，退 3000 分。",
        )

    def test_does_not_hit_without_keyword(self):
        self.assertEqual(
            self.sim.utterance_for(2, TurnContext(last_final="订单状态是 shipped。")),
            UTTERANCES[1],
        )
        self.assertEqual(self.sim.utterance_for(2), UTTERANCES[1])

    def test_first_turn_never_uses_rules(self):
        self.assertEqual(
            self.sim.utterance_for(1, TurnContext(last_final="请问要退哪笔订单？")),
            UTTERANCES[0],
        )

    def test_tool_called_condition(self):
        sim = RuleUserSimulator(
            UTTERANCES,
            [{"turn": 2, "text": "查过了就继续", "when": {"tool_called": "get_order"}}],
        )
        self.assertEqual(
            sim.utterance_for(2, TurnContext(last_tools=("get_order",))),
            "查过了就继续",
        )
        self.assertEqual(
            sim.utterance_for(2, TurnContext(last_tools=("get_customer",))),
            UTTERANCES[1],
        )

    def test_termination_condition_and_and_semantics(self):
        sim = RuleUserSimulator(
            UTTERANCES,
            [{
                "turn": 2,
                "text": "重试后继续",
                "when": {"tool_called": "refund_order", "termination": "final"},
            }],
        )
        self.assertEqual(
            sim.utterance_for(
                2,
                TurnContext(last_tools=("refund_order",), last_termination="final"),
            ),
            "重试后继续",
        )
        self.assertEqual(
            sim.utterance_for(
                2,
                TurnContext(last_tools=("refund_order",), last_termination="max_steps"),
            ),
            UTTERANCES[1],
        )

    def test_first_matching_rule_wins(self):
        sim = RuleUserSimulator(
            UTTERANCES,
            [
                {"turn": 2, "text": "第一条", "when": {"final_contains_any": ["请问"]}},
                {"turn": 2, "text": "第二条", "when": {"final_contains_any": ["请问"]}},
            ],
        )
        self.assertEqual(sim.utterance_for(2, TurnContext(last_final="请问")), "第一条")

    def test_empty_condition_never_matches(self):
        rule = ConditionalUtterance(turn=2, text="x")
        self.assertFalse(rule.matches(TurnContext(last_final="anything")))


class TestRuleValidation(unittest.TestCase):
    def test_unknown_fields_rejected(self):
        for raw in (
            [{"turn": 2, "text": "x", "typo": 1, "when": {"tool_called": "get_order"}}],
            [{"turn": 2, "text": "x", "when": {"mystery": 1}}],
            [{"turn": 2, "text": "x", "when": []}],
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_rules(raw)

    def test_invalid_values_rejected(self):
        for raw in (
            [{"turn": 1, "text": "x", "when": {"tool_called": "get_order"}}],
            [{"turn": 2, "text": "", "when": {"tool_called": "get_order"}}],
            [{"turn": "2", "text": "x", "when": {"tool_called": "get_order"}}],
            [{"turn": 2, "text": "x", "when": {}}],
            [{"turn": 2, "text": "x", "when": {"final_contains_any": [1]}}],
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_rules(raw)

    def test_valid_rule_shape(self):
        rules = parse_rules(
            [{"turn": 2, "text": "x", "when": {"final_contains_any": ["请问"]}}]
        )
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].final_contains_any, ("请问",))


class TestTaskSetAndRunner(unittest.TestCase):
    def test_t10_rule_keeps_frozen_baseline_utterance(self):
        task = next(t for t in runner.load_tasks(TASKS) if t["id"] == "T10")
        sim = RuleUserSimulator(task["user_utterances"], task["utterance_rules"])
        self.assertEqual(sim.rule_count, 1)
        self.assertEqual(
            sim.utterance_for(2, TurnContext(last_final="订单 O-102 当前状态是 shipped。")),
            task["user_utterances"][1],
        )
        self.assertNotEqual(
            sim.utterance_for(2, TurnContext(last_final="请问要退哪笔订单？")),
            task["user_utterances"][1],
        )

    def test_default_run_trace_keeps_original_second_utterance(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            runner.run(
                runner.RunConfig(
                    tasks_path=TASKS,
                    k=1,
                    results_dir=base / "results",
                    traces_dir=base / "traces",
                )
            )
            spans = trace.read_jsonl(base / "traces" / "T10-t1.jsonl")
        user_turns = [s["observation"] for s in spans if s["role"] == "user"]
        self.assertEqual(len(user_turns), 2)
        self.assertEqual(user_turns[1], "那帮我退 3000 分吧。")

    def test_runner_passes_previous_final_into_simulator(self):
        """端到端证明上下文真的传进主链路，而不只是模拟器单测里能命中。"""
        task = {
            "id": "X1",
            "user_utterances": list(UTTERANCES),
            "utterance_rules": [
                {
                    "turn": 2,
                    "text": "订单 O-100，退 3000 分。",
                    "when": {"final_contains_any": ["请问"]},
                }
            ],
            "initial_db": {"customers": [], "orders": [], "audit_log": []},
            "target_db": {},
            "policy_checks": [],
            "required_disclosures": [],
            "script": [
                {"final": "请问要退哪笔订单？"},
                {"final": "办好了。"},
            ],
        }
        with TemporaryDirectory() as d:
            base = Path(d)
            tasks_path = base / "tasks.jsonl"
            tasks_path.write_text(json.dumps(task, ensure_ascii=False) + "\n", encoding="utf-8")
            runner.run(
                runner.RunConfig(
                    tasks_path=tasks_path,
                    k=1,
                    results_dir=base / "results",
                    traces_dir=base / "traces",
                )
            )
            spans = trace.read_jsonl(base / "traces" / "X1-t1.jsonl")
        user_turns = [s["observation"] for s in spans if s["role"] == "user"]
        self.assertEqual(user_turns, [UTTERANCES[0], "订单 O-100，退 3000 分。"])


if __name__ == "__main__":
    unittest.main()
