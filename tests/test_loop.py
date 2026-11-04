"""运行时单测：护栏、span 完整性、后端可切换。"""
from __future__ import annotations

import io
import json
import unittest

from env import db
from runtime import guard, loop
from runtime.model import (
    BackendConfig,
    OpenAIBackend,
    ScriptedBackend,
    build_backend,
    config_from_env,
)

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

SPAN_FIELDS = {
    "trial_id",
    "step",
    "role",
    "tool",
    "args",
    "observation",
    "tokens",
    "latency_ms",
}


class TestParseAction(unittest.TestCase):
    def test_valid_actions(self):
        action, reason = loop.parse_action(
            '{"tool":"get_order","args":{"order_id":"O-100"}}'
        )
        self.assertEqual(action["tool"], "get_order")
        self.assertEqual(reason, "")
        action, reason = loop.parse_action('{"final":"done"}')
        self.assertEqual(action["final"], "done")

    def test_invalid_actions(self):
        for raw in ("", "not-json", '{"tool":"x"', '{"args":{}}', "[]"):
            with self.subTest(raw=raw):
                action, _ = loop.parse_action(raw)
                self.assertIsNone(action)


class TestLoop(unittest.TestCase):
    def _run(self, script, max_steps=None):
        template = db.build_template(INITIAL)
        conn = db.reset(template)
        budget = guard.StepBudget(
            max_steps=max_steps if max_steps is not None else guard.MAX_STEPS
        )
        result = loop.run_turn(
            [],
            ScriptedBackend(script),
            conn,
            trial_id="T-t1",
            counter=loop.StepCounter(),
            budget=budget,
        )
        conn.close()
        template.close()
        return result, budget

    def test_json_retry_then_success(self):
        result, budget = self._run(
            [
                "bad",
                {"tool": "get_order", "args": {"order_id": "O-100"}},
                {"final": "done"},
            ]
        )
        self.assertEqual(result.termination, loop.TERMINATION_FINAL)
        self.assertEqual(result.final_text, "done")
        self.assertEqual(budget.used, 3)

    def test_two_retries_exhausted(self):
        result, budget = self._run(
            ["bad1", "bad2", "bad3", {"final": "never"}]
        )
        self.assertEqual(result.termination, loop.TERMINATION_JSON_FAILURE)
        self.assertEqual(budget.used, 3)

    def test_max_steps_triggered(self):
        result, budget = self._run(
            [{"tool": "get_order", "args": {"order_id": "O-100"}}], max_steps=5
        )
        self.assertEqual(result.termination, loop.TERMINATION_MAX_STEPS)
        self.assertEqual(budget.used, 5)

    def test_spans_have_all_fields_and_increasing_steps(self):
        result, _ = self._run(
            [
                {"tool": "get_order", "args": {"order_id": "O-100"}},
                {"final": "ok"},
            ]
        )
        self.assertTrue(all(set(span) == SPAN_FIELDS for span in result.spans))
        steps = [span["step"] for span in result.spans]
        self.assertEqual(steps, list(range(1, len(steps) + 1)))
        self.assertTrue(all(span["trial_id"] == "T-t1" for span in result.spans))
        self.assertTrue(
            all(span["tokens"] == 0 and span["latency_ms"] == 0 for span in result.spans)
        )

    def test_tool_errors_are_collected_not_fatal(self):
        result, _ = self._run(
            [
                {"tool": "refund_money", "args": {}},
                {"final": "抱歉"},
            ]
        )
        self.assertEqual(result.termination, loop.TERMINATION_FINAL)
        self.assertIn("wrong_tool", result.errors)


class TestBackends(unittest.TestCase):
    def test_scripted_is_deterministic_and_repeats_last(self):
        backend = ScriptedBackend(
            [{"tool": "get_order", "args": {"order_id": "O-100"}}]
        )
        first = backend.generate([])
        self.assertEqual(backend.generate([]), first)
        self.assertEqual(backend.generate([]), first)

    def test_build_backend_switches(self):
        self.assertIsInstance(build_backend(BackendConfig(), []), ScriptedBackend)
        config = BackendConfig(
            name="openai", base_url="http://x/v1", api_key="k", model="m"
        )
        self.assertIsInstance(build_backend(config, []), OpenAIBackend)
        with self.assertRaises(ValueError):
            build_backend(BackendConfig(name="openai"), [])

    def test_config_from_env(self):
        config = config_from_env(
            {"LLM_BASE_URL": "http://x", "LLM_API_KEY": "k", "LLM_MODEL": "m"}
        )
        self.assertEqual((config.name, config.model), ("openai", "m"))

    def test_openai_backend_without_network(self):
        captured = []

        def opener(request):
            captured.append(request)
            payload = {"choices": [{"message": {"content": '{"final":"ok"}'}}]}
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        backend = OpenAIBackend("http://x/v1", "k", "m", opener=opener)
        self.assertEqual(
            backend.generate([{"role": "user", "content": "hi"}]),
            '{"final":"ok"}',
        )
        self.assertEqual(captured[0].full_url, "http://x/v1/chat/completions")
