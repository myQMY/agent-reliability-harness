"""W4 前置守门测试：真实模型路径可测、预算可强制、探针数得对、任务集不变。

全部离线：fake opener 捕获 HTTP payload，不发起任何网络请求、不花一分钱。
同时钉死两条硬约束——(1) 两个版本化任务集逐字节不变；(2) 确定性后端的
既有产物不因 W4 的上下文注入 / 预算装饰器而改变。
"""
from __future__ import annotations

import hashlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from env import tools
from eval import budget, config as exp_config, guard_probe, runner, trace
from eval import run as cli
from runtime import guard
from runtime.model import (
    BackendConfig,
    ModelReply,
    OpenAIBackend,
    ScriptedBackend,
    SYSTEM_PROMPT,
    TOOL_RESULT_PREFIX,
    build_tool_context,
)

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"

# 两个已提交 fixture 的 sha256；W4 及以后一律不得改动
TASKSET_HASHES = {
    "mvp_10.jsonl": "0a3e2af34af872f1c26e976585b961f86458b1ec48368134bc5229bde26b62da",
    "mvp_30.jsonl": "33d25c59410268c5d7417237871b9c750a98499f70883e4c1458bde59108a255",
}

STATE = {
    "customers": [{"id": "C-1", "name": "A", "email": "a@x", "tier": "gold"}],
    "orders": [
        {
            "id": "O-100", "customer_id": "C-1", "status": "shipped",
            "total_cents": 4200, "refunded_cents": 0,
        }
    ],
    "audit_log": [],
}


def make_span(step, role, observation, **extra):
    span = {
        "trial_id": "T-t1", "step": step, "role": role,
        "tool": None, "args": None, "observation": observation,
        "tokens": 0, "latency_ms": 0,
    }
    span.update(extra)
    return span


def capture_opener(captured, *, text='{"final":"ok"}', usage=None):
    payload = {
        "choices": [{"message": {"content": text}}],
        "usage": usage or {"prompt_tokens": 11, "completion_tokens": 7},
    }

    def opener(request):
        captured.append(json.loads(request.data.decode("utf-8")))
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    return opener


class FixedBackend:
    """不联网的假后端，用来单测预算闸门。"""

    def __init__(self, *, prompt=10, completion=5):
        self.prompt = prompt
        self.completion = completion

    def generate(self, messages) -> ModelReply:
        return ModelReply(
            text='{"final":"ok"}',
            prompt_tokens=self.prompt,
            completion_tokens=self.completion,
        )


class TestVersionedTasksetUntouched(unittest.TestCase):
    """用户明确要求：W4 不得改已提交的两个任务集。"""

    def test_both_fixtures_keep_their_sha256(self):
        for name, expected in TASKSET_HASHES.items():
            with self.subTest(name=name):
                digest = hashlib.sha256(
                    (ROOT / "tasks" / name).read_bytes()
                ).hexdigest()
                self.assertEqual(digest, expected)

    def test_v2_prefix_still_equals_v1_bytes(self):
        v1 = (ROOT / "tasks" / "mvp_10.jsonl").read_bytes()
        v2 = (ROOT / "tasks" / "mvp_30.jsonl").read_bytes()
        self.assertTrue(v2.startswith(v1))


class TestToolContext(unittest.TestCase):
    def test_context_lists_every_tool_and_initial_state(self):
        text = build_tool_context(STATE)
        for name in (
            "get_customer", "get_order", "list_customer_orders",
            "refund_order", "cancel_order", "notify_customer",
        ):
            with self.subTest(tool=name):
                self.assertIn(name, text)
        self.assertIn("O-100", text)
        self.assertIn("C-1", text)
        self.assertIn("amount_cents", text)

    def test_context_is_deterministic_and_clock_free(self):
        self.assertEqual(build_tool_context(STATE), build_tool_context(STATE))
        # 键顺序固定，同一状态换个插入顺序结果仍一致
        shuffled = {
            "audit_log": STATE["audit_log"],
            "orders": STATE["orders"],
            "customers": STATE["customers"],
        }
        self.assertEqual(build_tool_context(STATE), build_tool_context(shuffled))


class TestOpenAIWireFormat(unittest.TestCase):
    def test_default_prompt_is_unchanged_when_no_context(self):
        captured = []
        backend = OpenAIBackend(
            "http://x/v1", "k", "m",
            opener=capture_opener(captured),
        )
        backend.generate([{"role": "user", "content": "hi"}])
        self.assertEqual(captured[0]["messages"][0]["content"], SYSTEM_PROMPT)
        self.assertEqual(len(captured[0]["messages"]), 2)

    def test_context_is_appended_after_base_prompt(self):
        captured = []
        context = build_tool_context(STATE)
        backend = OpenAIBackend(
            "http://x/v1", "k", "m", system_prompt=context,
            opener=capture_opener(captured),
        )
        system = backend.system_prompt
        self.assertTrue(system.startswith(SYSTEM_PROMPT))
        self.assertIn("refund_order", system)
        self.assertEqual(captured, [])
        backend.generate([])
        self.assertEqual(captured[0]["messages"][0]["content"], system)

    def test_tool_role_is_rewritten_to_user_text(self):
        captured = []
        backend = OpenAIBackend("http://x/v1", "k", "m", opener=capture_opener(captured))
        backend.generate([
            {"role": "user", "content": "我要退 O-100"},
            {"role": "assistant", "content": '{"tool":"get_order"}'},
            {"role": "tool", "content": '{"ok":true,"order":{"id":"O-100"}}'},
            {"role": "user", "content": f"{guard.GUARD_REJECT_PREFIX} 请重发"},
        ])
        messages = captured[0]["messages"]
        roles = [m["role"] for m in messages]
        self.assertNotIn("tool", roles)
        self.assertEqual(roles[-1], "user")
        self.assertTrue(messages[3]["content"].startswith(TOOL_RESULT_PREFIX))
        self.assertIn('"ok":true', messages[3]["content"])
        # guard 拒绝消息必须原样送达，这正是 W4 要验证的东西
        self.assertTrue(messages[4]["content"].startswith(guard.GUARD_REJECT_PREFIX))

    def test_runner_injects_context_only_for_openai(self):
        captured = []
        with patch("runtime.model.urllib.request.urlopen", capture_opener(captured)):
            with TemporaryDirectory() as d:
                base = Path(d)
                rows = runner.run(
                    runner.RunConfig(
                        tasks_path=TASKS, k=1,
                        backend=BackendConfig(
                            name="openai", base_url="http://x/v1",
                            api_key="k", model="m",
                        ),
                        results_dir=base / "results",
                        traces_dir=base / "traces",
                    )
                )
        self.assertEqual(len(rows), 10)
        self.assertTrue(captured)
        system = captured[0]["messages"][0]["content"]
        self.assertIn("get_order", system)
        self.assertIn("O-100", system)
        # 每个 trial 的 system 都是本 trial 初始状态，确定性且含工具清单
        self.assertTrue(
            all("notify_customer" in c["messages"][0]["content"] for c in captured)
        )

    def test_scripted_path_never_builds_context(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            plain = runner.run(
                runner.RunConfig(
                    tasks_path=TASKS, k=1,
                    backend=BackendConfig(),
                    results_dir=base / "a", traces_dir=base / "ta",
                )
            )
            with_context = runner.run(
                runner.RunConfig(
                    tasks_path=TASKS, k=1,
                    backend=BackendConfig(system_prompt="不会被消费"),
                    results_dir=base / "b", traces_dir=base / "tb",
                )
            )
            self.assertEqual(plain, with_context)
            self.assertTrue(
                all(row["tokens"] == 0 and row["latency_ms"] == 0 for row in plain)
            )
        # 脚本后端不读取 messages，注入与否都不应改变输出
        self.assertEqual(
            ScriptedBackend([{"final": "ok"}]).generate([]).text,
            ScriptedBackend([{"final": "ok"}]).generate(
                [{"role": "system", "content": "x"}]
            ).text,
        )


class TestBudgetMath(unittest.TestCase):
    def test_price_card_cost(self):
        price = budget.PriceCard(0.15, 0.6, "USD")
        self.assertAlmostEqual(price.cost(1_000_000, 1_000_000), 0.75)

    def test_estimate_plan_is_an_upper_bound(self):
        plan = budget.estimate_plan(
            task_count=10, k=3, max_steps=12,
            avg_prompt_tokens=1500, avg_completion_tokens=200,
            cells=2, price=budget.PriceCard(0.15, 0.6, "USD"),
        )
        self.assertEqual(plan["calls_upper_bound"], 720)
        self.assertEqual(plan["prompt_tokens_upper_bound"], 1_080_000)
        self.assertEqual(plan["completion_tokens_upper_bound"], 144_000)
        self.assertEqual(plan["total_tokens_upper_bound"], 1_224_000)
        self.assertAlmostEqual(plan["total_cost"], 0.2484)

    def test_invalid_inputs_are_rejected(self):
        with self.assertRaises(ValueError):
            budget.estimate_plan(
                task_count=0, k=3, max_steps=12,
                avg_prompt_tokens=1, avg_completion_tokens=1,
            )
        for limit in (
            budget.BudgetLimit(),
            budget.BudgetLimit(max_calls=0),
            budget.BudgetLimit(max_cost=1.0),
        ):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    limit.validate()
        with self.assertRaises(ValueError):
            budget.PriceCard(-1, 1).validate()

    def test_count_tasks_reads_jsonl(self):
        self.assertEqual(budget.count_tasks(TASKS), 10)
        self.assertEqual(
            budget.count_tasks(ROOT / "tasks" / "mvp_30.jsonl"), 30
        )


class TestBudgetGate(unittest.TestCase):
    def test_ledger_is_shared_across_trials(self):
        ledger = budget.BudgetLedger()
        limit = budget.BudgetLimit(max_calls=5)
        first = budget.BudgetedBackend(FixedBackend(), ledger, limit)
        second = budget.BudgetedBackend(FixedBackend(), ledger, limit)
        first.generate([])
        second.generate([])
        self.assertEqual(ledger.calls, 2)
        self.assertEqual(ledger.tokens, 30)

    def test_max_calls_stops_before_the_next_request(self):
        ledger = budget.BudgetLedger()
        backend = budget.BudgetedBackend(
            FixedBackend(), ledger, budget.BudgetLimit(max_calls=2)
        )
        backend.generate([])
        backend.generate([])
        with self.assertRaises(budget.BudgetExceeded):
            backend.generate([])
        self.assertEqual(ledger.calls, 2, "超限的那次不应发出")

    def test_token_and_cost_gates(self):
        ledger = budget.BudgetLedger()
        backend = budget.BudgetedBackend(
            FixedBackend(prompt=10, completion=5),
            ledger,
            budget.BudgetLimit(max_total_tokens=15),
        )
        backend.generate([])
        with self.assertRaises(budget.BudgetExceeded):
            backend.generate([])

        ledger2 = budget.BudgetLedger()
        backend2 = budget.BudgetedBackend(
            FixedBackend(prompt=1_000_000, completion=1_000_000),
            ledger2,
            budget.BudgetLimit(
                max_cost=0.5, price=budget.PriceCard(0.15, 0.6, "USD")
            ),
        )
        backend2.generate([])
        with self.assertRaises(budget.BudgetExceeded):
            backend2.generate([])

    def test_calls_gate_still_works_when_usage_is_missing(self):
        ledger = budget.BudgetLedger()
        backend = budget.BudgetedBackend(
            FixedBackend(prompt=0, completion=0),
            ledger,
            budget.BudgetLimit(max_calls=1, max_total_tokens=1_000_000),
        )
        backend.generate([])
        self.assertEqual(ledger.tokens, 0)
        with self.assertRaises(budget.BudgetExceeded):
            backend.generate([])

    def test_runner_enforces_one_gate_for_the_whole_run(self):
        captured = []
        with patch("runtime.model.urllib.request.urlopen", capture_opener(captured)):
            with TemporaryDirectory() as d:
                base = Path(d)
                with self.assertRaises(budget.BudgetExceeded):
                    runner.run(
                        runner.RunConfig(
                            tasks_path=TASKS, k=1,
                            backend=BackendConfig(
                                name="openai", base_url="http://x/v1",
                                api_key="k", model="m",
                            ),
                            results_dir=base / "results",
                            traces_dir=base / "traces",
                            budget_limit=budget.BudgetLimit(max_calls=3),
                        )
                    )
        self.assertEqual(len(captured), 3, "第 4 次调用必须被闸门挡住")


class TestCliRequiresExplicitBudget(unittest.TestCase):
    def test_openai_without_max_calls_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--backend", "openai"])
        self.assertIn("--max-calls", str(ctx.exception))

    def test_budget_flags_on_deterministic_backend_are_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--max-calls", "10"])
        self.assertIn("openai", str(ctx.exception))

    def test_max_cost_requires_price(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--backend", "openai", "--max-calls", "10", "--max-cost", "1"])
        self.assertIn("--price-in", str(ctx.exception))


class TestGuardProbe(unittest.TestCase):
    def test_classify_covers_four_guards_and_unknown(self):
        samples = (
            (guard.rejection_message("refund_order", {}), guard.GUARD_SCHEMA_VALIDATION),
            (guard.idempotency_rejection("refund_order", {"order_id": "O-1"}, guard.GuardState(executed_write_keys={guard.idempotency_key("refund_order", {"order_id": "O-1"})})), guard.GUARD_IDEMPOTENCY),
            (guard.tool_retry_message("x", tools.BAD_ARGS), guard.GUARD_TOOL_RETRY),
            (guard.json_recovery_hint("invalid_json"), guard.GUARD_JSON_RECOVERY),
            ("[GUARD-REJECT] 随便一句话", "unknown"),
        )
        for text, expected in samples:
            with self.subTest(expected=expected):
                self.assertTrue(text.startswith(guard.GUARD_REJECT_PREFIX))
                self.assertEqual(guard_probe.classify(text), expected)

    def test_responsive_requires_legal_and_changed_reply(self):
        bad = '{"tool":"refund_order","args":{}}'
        good = '{"tool":"refund_order","args":{"order_id":"O-100","amount_cents":2500}}'
        reject = guard.rejection_message("refund_order", {})
        base = [make_span(1, "assistant", bad), make_span(2, "guard", reject)]

        changed = guard_probe.probe_spans(base + [
            make_span(3, "assistant", good),
            make_span(4, "tool", {"ok": True}, tool="refund_order"),
        ])
        self.assertEqual(len(changed), 1)
        self.assertTrue(changed[0].responsive)
        self.assertTrue(changed[0].followup_tool_ok)
        self.assertEqual(changed[0].kind, guard.GUARD_SCHEMA_VALIDATION)

        repeated = guard_probe.probe_spans(base + [make_span(3, "assistant", bad)])
        self.assertFalse(repeated[0].responsive)
        self.assertTrue(repeated[0].parse_ok)
        self.assertFalse(repeated[0].changed)

        unparseable = guard_probe.probe_spans(base + [make_span(3, "assistant", "{not-json")])
        self.assertFalse(unparseable[0].parse_ok)

        no_reply = guard_probe.probe_spans(base)
        self.assertFalse(no_reply[0].has_reply)
        self.assertFalse(no_reply[0].responsive)

    def test_non_guard_spans_are_ignored(self):
        spans = [
            make_span(1, "assistant", '{"final":"ok"}'),
            make_span(2, "user", guard.GUARD_REJECT_PREFIX + " 但 role 不是 guard"),
        ]
        self.assertEqual(guard_probe.probe_spans(spans), [])

    def test_dry_run_calibration_is_offline_and_sane(self):
        off = guard_probe.dry_run_events(TASKS, ())
        all4 = guard_probe.dry_run_events(TASKS, tuple(guard.GUARD_NAMES))
        self.assertEqual(off, [], "护栏全关时探针不能凭空数出拒绝")
        self.assertTrue(all4)
        kinds = set(guard_probe.summarize(all4)["kinds_seen"])
        self.assertEqual(
            kinds,
            {
                guard.GUARD_SCHEMA_VALIDATION, guard.GUARD_IDEMPOTENCY,
                guard.GUARD_TOOL_RETRY, guard.GUARD_JSON_RECOVERY,
            },
        )
        summary = guard_probe.summarize(all4)
        self.assertLessEqual(summary["responsive"], summary["total"])
        self.assertGreater(summary["responsive"], 0)
        self.assertEqual(off, guard_probe.dry_run_events(TASKS, ()))

    def test_read_only_mode_reads_archived_traces(self):
        with TemporaryDirectory() as d:
            target = Path(d) / "T01-t1.jsonl"
            spans = [
                make_span(1, "assistant", "{not-json"),
                make_span(2, "guard", guard.json_recovery_hint("invalid_json")),
                make_span(3, "assistant", '{"final":"ok"}'),
            ]
            trace.write_jsonl(target, spans)
            events = guard_probe.events_from_dir(Path(d))
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].responsive)


class TestW4ConfigsAndDocs(unittest.TestCase):
    def test_two_cells_differ_only_in_guards(self):
        off = exp_config.load_config(ROOT / "experiments" / "w4_off_flash.toml")
        on = exp_config.load_config(ROOT / "experiments" / "w4_all4_flash.toml")
        self.assertEqual(off.guards, ())
        self.assertEqual(on.guards, guard.GUARD_NAMES)
        for field in ("tasks", "k", "backend", "max_steps", "max_json_retries"):
            with self.subTest(field=field):
                self.assertEqual(getattr(off, field), getattr(on, field))
        self.assertEqual(off.backend, "openai")

    def test_compare_points_at_off_group_archive(self):
        off = exp_config.load_config(ROOT / "experiments" / "w4_off_flash.toml")
        on = exp_config.load_config(ROOT / "experiments" / "w4_all4_flash.toml")
        rid = exp_config.run_id(off)
        self.assertEqual(rid, "w4_off_flash-61491decf48c")
        self.assertEqual(
            on.compare, f"experiments/runs/{rid}/result_rows.jsonl"
        )
        self.assertEqual(on.compare, "experiments/runs/w4_off_flash-61491decf48c/result_rows.jsonl")

    def test_docs_and_evidence_exist_with_honest_caveats(self):
        for rel in (
            "docs/w4-design.md",
            "experiments/w4_budget_estimate.md",
            "experiments/w4_probe_dryrun.md",
            "eval/budget.py",
            "eval/guard_probe.py",
        ):
            with self.subTest(path=rel):
                self.assertTrue((ROOT / rel).is_file(), f"缺少 {rel}")
        design = (ROOT / "docs" / "w4-design.md").read_text(encoding="utf-8")
        for phrase in (
            "未花一分钱", "可证伪", "不得外推", "上界", "不修改任何已提交任务集",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, design)
        dryrun = (ROOT / "experiments" / "w4_probe_dryrun.md").read_text(encoding="utf-8")
        self.assertIn("不构成 W4 的实验结论", dryrun)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in ("docs/w4-design.md", "eval.budget", "eval.guard_probe"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
