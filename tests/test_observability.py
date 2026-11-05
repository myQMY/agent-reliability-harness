"""W2 观测链路：ModelReply、usage 传播、span 注入、观测不进判分。

覆盖「模型层产生观测 → loop 挂到 assistant span → runner 汇总进结果行」，
并守住两条边界：ScriptedBackend 恒为零（逐字节一致）、真实观测不参与判分。
"""
from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from env import db
from eval import grader, runner, trace
from runtime import guard, loop
from runtime.model import (
    BackendConfig,
    ModelReply,
    OpenAIBackend,
    ScriptedBackend,
)

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"
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
SCRIPT = [
    {"tool": "get_order", "args": {"order_id": "O-100"}},
    {"final": "done"},
]


class ObservedScriptedBackend:
    """给确定性脚本套上非零观测，用来验证注入而不改变行为。"""

    def __init__(self, script, *, prompt=13, completion=5, latency_ms=7):
        self._inner = ScriptedBackend(script)
        self.prompt = prompt
        self.completion = completion
        self.latency_ms = latency_ms

    def generate(self, messages) -> ModelReply:
        base = self._inner.generate(messages)
        return ModelReply(
            text=base.text,
            prompt_tokens=self.prompt,
            completion_tokens=self.completion,
            latency_ms=self.latency_ms,
        )


def run_turn(backend):
    template = db.build_template(INITIAL)
    conn = db.reset(template)
    result = loop.run_turn(
        [],
        backend,
        conn,
        trial_id="T-t1",
        counter=loop.StepCounter(),
        budget=guard.StepBudget(),
    )
    state = db.dump_state(conn)
    conn.close()
    template.close()
    return result, state


class TestModelReply(unittest.TestCase):
    def test_tokens_is_prompt_plus_completion(self):
        reply = ModelReply(text="x", prompt_tokens=11, completion_tokens=7)
        self.assertEqual(reply.tokens, 18)

    def test_defaults_are_zero_and_immutable(self):
        reply = ModelReply(text="x")
        self.assertEqual((reply.tokens, reply.latency_ms), (0, 0))
        with self.assertRaises(Exception):
            reply.text = "y"


class TestScriptedObservability(unittest.TestCase):
    def test_scripted_reply_is_zero_and_deterministic(self):
        # 单项脚本用尽后会重复最后一项，用来验证「确定性」而非脚本内容
        backend = ScriptedBackend([SCRIPT[0]])
        first = backend.generate([])
        self.assertIsInstance(first, ModelReply)
        self.assertEqual(backend.generate([]).text, first.text)
        self.assertEqual((first.tokens, first.latency_ms), (0, 0))

    def test_all_spans_are_zero_under_scripted(self):
        result, _ = run_turn(ScriptedBackend(SCRIPT))
        self.assertTrue(result.spans)
        self.assertTrue(
            all(s["tokens"] == 0 and s["latency_ms"] == 0 for s in result.spans)
        )


class TestOpenAIObservability(unittest.TestCase):
    def make_backend(self, payload):
        def opener(request):
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        return OpenAIBackend("http://x/v1", "k", "m", opener=opener)

    def test_usage_is_read_from_response(self):
        payload = {
            "choices": [{"message": {"content": '{"final":"ok"}'}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 34, "total_tokens": 154},
        }
        reply = self.make_backend(payload).generate([])
        self.assertEqual(reply.text, '{"final":"ok"}')
        self.assertEqual((reply.prompt_tokens, reply.completion_tokens), (120, 34))
        self.assertEqual(reply.tokens, 154)
        self.assertIsInstance(reply.latency_ms, int)
        self.assertGreaterEqual(reply.latency_ms, 0)

    def test_missing_or_null_usage_degrades_to_zero(self):
        for usage in ({}, None, {"prompt_tokens": None, "completion_tokens": None}):
            payload = {"choices": [{"message": {"content": "x"}}]}
            if usage is not None:
                payload["usage"] = usage
            with self.subTest(usage=usage):
                reply = self.make_backend(payload).generate([])
                self.assertEqual(reply.tokens, 0)


class TestSpanInjection(unittest.TestCase):
    def test_only_assistant_spans_carry_observations(self):
        result, _ = run_turn(ObservedScriptedBackend(SCRIPT))
        assistants = [s for s in result.spans if s["role"] == "assistant"]
        others = [s for s in result.spans if s["role"] != "assistant"]
        self.assertEqual(len(assistants), 2)
        self.assertTrue(all(s["tokens"] == 18 for s in assistants))
        self.assertTrue(all(s["latency_ms"] == 7 for s in assistants))
        self.assertTrue(
            all(s["tokens"] == 0 and s["latency_ms"] == 0 for s in others)
        )
        self.assertEqual(sum(s["tokens"] for s in result.spans), 36)
        self.assertEqual(
            {key for s in result.spans for key in s},
            {
                "trial_id", "step", "role", "tool", "args",
                "observation", "tokens", "latency_ms",
            },
        )

    def test_observations_do_not_change_behavior(self):
        plain, state_a = run_turn(ScriptedBackend(SCRIPT))
        observed, state_b = run_turn(ObservedScriptedBackend(SCRIPT))
        self.assertEqual(state_a, state_b)
        self.assertEqual(plain.termination, observed.termination)
        self.assertEqual(plain.final_text, observed.final_text)
        self.assertEqual(plain.errors, observed.errors)
        self.assertEqual(
            [s["observation"] for s in plain.spans],
            [s["observation"] for s in observed.spans],
        )

    def test_observations_do_not_change_verdict(self):
        _, state = run_turn(ScriptedBackend(SCRIPT))
        result, _ = run_turn(ScriptedBackend(SCRIPT))
        spans = result.spans
        noisy = [dict(s, tokens=999999, latency_ms=888888) for s in spans]
        task = runner.load_tasks(TASKS)[0]
        info = {"termination": result.termination, "errors": [], "final_text": "done"}
        self.assertEqual(
            grader.grade(state, task, dict(info, spans=spans)),
            grader.grade(state, task, dict(info, spans=noisy)),
        )


class TestUsageReachesResultRows(unittest.TestCase):
    def test_runner_sums_model_usage_into_rows(self):
        def opener(request):
            payload = {
                "choices": [{"message": {"content": '{"final":"ok"}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        backend = BackendConfig(
            name="openai", base_url="http://x/v1", api_key="k", model="m"
        )
        with patch("runtime.model.urllib.request.urlopen", opener):
            with TemporaryDirectory() as d:
                base = Path(d)
                rows = runner.run(
                    runner.RunConfig(
                        tasks_path=TASKS,
                        k=1,
                        backend=backend,
                        results_dir=base / "results",
                        traces_dir=base / "traces",
                    )
                )
                spans = trace.read_jsonl(base / "traces" / "T01-t1.jsonl")
        self.assertEqual(len(rows), 10)
        for row in rows:
            with self.subTest(task=row["task_id"]):
                expected = 18 * (2 if row["task_id"] == "T10" else 1)
                self.assertEqual(row["tokens"], expected)
        assistants = [s for s in spans if s["role"] == "assistant"]
        self.assertEqual(len(assistants), 1)
        self.assertTrue(all(s["tokens"] == 18 for s in assistants))
        self.assertEqual(sum(s["tokens"] for s in spans), 18)


if __name__ == "__main__":
    unittest.main()
