"""W4-b 守门测试：输出级故障注入的接线、口径与「不代答」红线。

W4-b 与 W3 的 SeededBackend 共用同一份 fault_rng / pick_fault / fault_text，
但有一处本质差异必须钉死：**收到 [GUARD-REJECT] 后绝不硬编码重发干净动作**，
而是照常调用底层真实模型、只跳过注入。若哪天有人把 _pending_correct 抄过来，
H1 就又被 harness 代答了，W4-b 会退化成一次昂贵的 SeededBackend 复跑。

同时守门：
- 首轮 W4 的 run_id / config.json 逐字节不变（fpm=0 不进摘要、不包装）；
- openai + fpm>0 时注入参数进 run_id，避免「同 run_id 不同注入率」覆盖报告；
- 离线装置校准能证明分母只来自注入（fpm=0 基准必须为 0 guard span）。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from env import db, tools
from eval import config as exp_config
from eval import guard_probe, trace, w4_preflight, w4b_probe
from eval import run as cli
from runtime import guard, loop
from runtime.model import (
    FAULT_BAD_ARGS,
    FAULT_BAD_JSON,
    FAULT_DUPLICATE_CALL,
    FAULT_WRONG_TOOL,
    SEEDED_FAULTS,
    Backend,
    BackendConfig,
    ModelReply,
    OpenAIBackend,
    OutputFaultBackend,
    ScriptedBackend,
    SeededBackend,
    build_backend,
    build_tool_context,
    fault_rng,
    fault_text,
    pick_fault,
)

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "tasks" / "mvp_10.jsonl"
W4B_OFF = ROOT / "experiments" / "w4b_off_flash.toml"
W4B_ALL4 = ROOT / "experiments" / "w4b_all4_flash.toml"

FIRST_ROUND_RIDS = (
    "w4_off_flash-61491decf48c",
    "w4_all4_flash-f6a4ea1674dc",
)

CLEAN_ACTION = json.dumps(
    {"tool": "refund_order", "args": {"order_id": "O-100", "amount_cents": 2500}},
    ensure_ascii=False,
    sort_keys=True,
)


class _FixedBackend:
    """每次都返回同一段干净工具动作，并记录调用次数；不联网。"""

    def __init__(self, text: str = CLEAN_ACTION):
        self.text = text
        self.calls = 0

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        self.calls += 1
        return ModelReply(
            text=self.text, prompt_tokens=100, completion_tokens=20, latency_ms=7
        )


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self._body


class _FakeOpener:
    """只返回一段固定干净动作；记录 payload 以便断言消息接线。"""

    def __init__(self, text: str = CLEAN_ACTION):
        self.text = text
        self.requests: list[bytes] = []

    def __call__(self, request) -> _FakeResponse:
        self.requests.append(request.data)
        body = json.dumps(
            {
                "choices": [{"message": {"content": self.text}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ).encode("utf-8")
        return _FakeResponse(body)


def _faulted(inner: Backend | None = None, **kwargs) -> OutputFaultBackend:
    options = dict(seed=11, fault_per_mille=1000, task_id="T01", trial=1)
    options.update(kwargs)
    return OutputFaultBackend(inner or _FixedBackend(), **options)


class TestSharedFaultPrimitives(unittest.TestCase):
    """「与 W3 同口径」不是文档承诺，是同一份代码。"""

    def test_four_fault_kinds_are_versioned(self):
        self.assertEqual(
            set(SEEDED_FAULTS),
            {
                FAULT_BAD_JSON,
                FAULT_BAD_ARGS,
                FAULT_WRONG_TOOL,
                FAULT_DUPLICATE_CALL,
            },
        )

    def test_fault_rng_is_deterministic_and_shared_with_seeded_backend(self):
        config = BackendConfig(name="seeded", seed=11, fault_per_mille=60)
        backend = SeededBackend(config, [], task_id="T01", trial=2)
        for cursor in range(12):
            a = fault_rng(11, "T01", 2, cursor)
            b = backend._rng(cursor)
            self.assertEqual(
                [a.randrange(1000) for _ in range(4)],
                [b.randrange(1000) for _ in range(4)],
            )

    def test_pick_fault_matches_seeded_backend(self):
        config = BackendConfig(name="seeded", seed=11, fault_per_mille=300)
        backend = SeededBackend(config, [], task_id="T01", trial=1)
        for cursor in range(30):
            self.assertEqual(
                backend._pick_fault(cursor),
                pick_fault(11, "T01", 1, cursor, 300, set()),
                f"cursor={cursor}",
            )

    def test_fault_text_matches_seeded_backend_for_every_kind(self):
        config = BackendConfig(name="seeded", seed=11, fault_per_mille=300)
        backend = SeededBackend(config, [], task_id="T01", trial=1)
        action = json.loads(CLEAN_ACTION)
        for kind in SEEDED_FAULTS:
            self.assertEqual(
                backend._fault_text(kind, action),
                fault_text(kind, action, ()),
                kind,
            )
        self.assertEqual(fault_text(FAULT_BAD_JSON, action, ()), "{not-json")
        self.assertIn("__missing_tool__", fault_text(FAULT_WRONG_TOOL, action, ()))
        self.assertEqual(
            json.loads(fault_text(FAULT_BAD_ARGS, action, ()))["args"], {}
        )
        self.assertEqual(
            fault_text(FAULT_DUPLICATE_CALL, action, (CLEAN_ACTION,)), CLEAN_ACTION
        )


class TestOutputFaultBackend(unittest.TestCase):
    def test_fpm_zero_is_byte_identical_passthrough(self):
        inner = _FixedBackend()
        backend = _faulted(inner, fault_per_mille=0)
        for _ in range(5):
            reply = backend.generate([{"role": "user", "content": "hi"}])
            self.assertEqual(reply.text, CLEAN_ACTION)
        self.assertEqual(backend.injected, [])
        self.assertEqual(inner.calls, 5)

    def test_injection_keeps_real_observations(self):
        backend = _faulted()
        reply = backend.generate([{"role": "user", "content": "hi"}])
        self.assertNotEqual(reply.text, CLEAN_ACTION)
        self.assertEqual(reply.prompt_tokens, 100)
        self.assertEqual(reply.completion_tokens, 20)
        self.assertEqual(reply.latency_ms, 7)
        self.assertEqual(reply.tokens, 120)
        self.assertEqual(len(backend.injected), 1)

    def test_rejection_response_is_a_real_call_and_is_never_faulted(self):
        """W4-b 与 W3 的本质差异：拒绝后不重发干净动作，也不扰动响应。"""
        inner = _FixedBackend()
        backend = _faulted(inner)
        backend.generate([{"role": "user", "content": "hi"}])
        rejection = guard.tool_retry_message("refund_order", tools.BAD_ARGS)
        self.assertTrue(rejection.startswith(guard.GUARD_REJECT_PREFIX))
        reply = backend.generate([{"role": "user", "content": rejection}])
        self.assertEqual(reply.text, CLEAN_ACTION)
        self.assertEqual(inner.calls, 2, "必须真的再调一次底层后端")
        self.assertEqual(len(backend.injected), 1, "拒绝后的响应不得再被扰动")

    def test_skip_next_blocks_consecutive_faults(self):
        inner = _FixedBackend()
        backend = _faulted(inner)
        first = backend.generate([{"role": "user", "content": "hi"}])
        second = backend.generate([{"role": "user", "content": "继续"}])
        self.assertNotEqual(first.text, CLEAN_ACTION)
        self.assertEqual(second.text, CLEAN_ACTION)
        self.assertEqual(len(backend.injected), 1)

    def test_final_and_unparseable_outputs_are_not_faulted(self):
        for text in ('{"final": "完成"}', "{not-json", '{"bad": 1}'):
            backend = _faulted(_FixedBackend(text))
            self.assertEqual(
                backend.generate([{"role": "user", "content": "hi"}]).text, text
            )
            self.assertEqual(backend.injected, [])

    def test_duplicate_call_replays_last_successful_write(self):
        """重放的必须是已成功执行过的写动作，否则 idempotency 拦不住。"""
        task_id = next(
            f"T{i:03d}"
            for i in range(500)
            if pick_fault(11, f"T{i:03d}", 1, 0, 1000, set())
            == FAULT_DUPLICATE_CALL
        )
        tool_observation = json.dumps(
            {"ok": True, "order_id": "O-100", "refunded_cents": 2500},
            ensure_ascii=False,
            sort_keys=True,
        )
        messages = [
            {"role": "assistant", "content": CLEAN_ACTION},
            {"role": "tool", "content": tool_observation},
            {"role": "user", "content": "继续"},
        ]
        backend = OutputFaultBackend(
            _FixedBackend(CLEAN_ACTION),
            seed=11,
            fault_per_mille=1000,
            task_id=task_id,
            trial=1,
        )
        reply = backend.generate(messages)
        self.assertEqual(reply.text, CLEAN_ACTION)
        self.assertEqual(backend.injected, [(0, FAULT_DUPLICATE_CALL)])
        self.assertEqual(backend._last_successful_write(messages), CLEAN_ACTION)
        # 没有成功写动作时按 W3 口径退化成 bad_args
        self.assertEqual(
            json.loads(
                fault_text(FAULT_DUPLICATE_CALL, json.loads(CLEAN_ACTION), ())
            )["args"],
            {},
        )


class TestBuildBackendWiring(unittest.TestCase):
    def setUp(self):
        self.base = dict(
            name="openai",
            base_url="https://api.deepseek.com",
            api_key="FAKE-NOT-REAL",
            model="deepseek-flash",
        )

    def test_fpm_zero_returns_bare_openai_backend(self):
        backend = build_backend(
            BackendConfig(**self.base), [], task_id="T01", trial=1
        )
        self.assertIsInstance(backend, OpenAIBackend)
        self.assertNotIsInstance(backend, OutputFaultBackend)

    def test_fpm_positive_wraps_and_forwards_identity(self):
        backend = build_backend(
            BackendConfig(**self.base, seed=11, fault_per_mille=300),
            [],
            task_id="T01",
            trial=1,
        )
        self.assertIsInstance(backend, OutputFaultBackend)
        self.assertIsInstance(backend.inner, OpenAIBackend)
        self.assertEqual(backend._task_id, "T01")
        self.assertEqual(backend._trial, 1)
        self.assertEqual(backend._fault_per_mille, 300)

    def test_task_id_and_trial_change_the_fault_sequence(self):
        def kinds(task_id, trial):
            return [
                pick_fault(11, task_id, trial, c, 1000, set()) for c in range(8)
            ]

        self.assertNotEqual(kinds("T01", 1), kinds("T02", 1))
        self.assertNotEqual(kinds("T01", 1), kinds("T01", 2))


class TestConfigRunId(unittest.TestCase):
    def test_first_round_run_ids_and_snapshots_are_unchanged(self):
        for name, rid in zip(
            ("w4_off_flash.toml", "w4_all4_flash.toml"), FIRST_ROUND_RIDS
        ):
            cfg = exp_config.load_config(ROOT / "experiments" / name)
            self.assertEqual(exp_config.run_id(cfg), rid)
            archived = ROOT / "experiments" / "runs" / rid / "config.json"
            self.assertTrue(archived.is_file())
            self.assertEqual(
                trace.dumps(exp_config.config_snapshot(cfg, rid)) + "\n",
                archived.read_text(encoding="utf-8"),
            )

    def test_openai_fpm_zero_core_has_no_injection_fields(self):
        cfg = exp_config.with_overrides(
            exp_config.load_config(W4B_OFF), fault_per_mille=0
        )
        core = exp_config._core(cfg)
        self.assertNotIn("seed", core)
        self.assertNotIn("fault_per_mille", core)

    def test_openai_fpm_positive_core_includes_injection_fields(self):
        core = exp_config._core(exp_config.load_config(W4B_OFF))
        self.assertEqual(core["seed"], 11)
        self.assertEqual(core["fault_per_mille"], 300)

    def test_changing_seed_or_fpm_changes_run_id(self):
        base = exp_config.load_config(W4B_OFF)
        ids = {
            exp_config.run_id(base),
            exp_config.run_id(exp_config.with_overrides(base, seed=12)),
            exp_config.run_id(
                exp_config.with_overrides(base, fault_per_mille=200)
            ),
        }
        self.assertEqual(len(ids), 3)

    def test_w4b_configs_are_single_variable_and_compare_linked(self):
        off = exp_config.load_config(W4B_OFF)
        on = exp_config.load_config(W4B_ALL4)
        for field in w4_preflight._MEASURED_FIELDS:
            self.assertEqual(getattr(off, field), getattr(on, field), field)
        self.assertEqual(off.guards, ())
        self.assertEqual(on.guards, guard.GUARD_NAMES)
        expected = (
            f"experiments/runs/{exp_config.run_id(off)}/result_rows.jsonl"
        )
        self.assertEqual(on.compare, expected)
        self.assertNotEqual(exp_config.run_id(off), exp_config.run_id(on))
        for rid in FIRST_ROUND_RIDS:
            self.assertNotIn(rid, (exp_config.run_id(off), exp_config.run_id(on)))

    def test_preflight_measured_fields_include_injection_params(self):
        self.assertIn("seed", w4_preflight._MEASURED_FIELDS)
        self.assertIn("fault_per_mille", w4_preflight._MEASURED_FIELDS)

    def test_cli_forwards_injection_params_to_backend_config(self):
        """eval.run 的 openai 分支必须把 seed / fpm 传进 BackendConfig。"""
        captured: dict[str, BackendConfig] = {}

        def fake_run(cfg):
            captured["backend"] = cfg.backend
            raise RuntimeError("STOP before rendering")

        env = {
            "LLM_BASE_URL": "https://api.deepseek.com",
            "LLM_API_KEY": "FAKE-NOT-REAL",
            "LLM_MODEL": "deepseek-flash",
        }
        with patch.object(cli, "load_dotenv", return_value=env), patch.object(
            cli.runner, "run", side_effect=fake_run
        ):
            with self.assertRaisesRegex(RuntimeError, "STOP"):
                cli.main(
                    ["--config", str(W4B_OFF), "--max-calls", "720"]
                )
        backend = captured["backend"]
        self.assertEqual(backend.name, "openai")
        self.assertEqual(backend.seed, 11)
        self.assertEqual(backend.fault_per_mille, 300)


class TestDeviceCalibration(unittest.TestCase):
    def test_clean_script_drops_inherent_defects_and_duplicate_writes(self):
        task = {
            "script": [
                "{not-json",
                {"tool": "__missing_tool__", "args": {}},
                {"tool": "refund_order", "args": {"order_id": "O-100"}},
                CLEAN_ACTION,
                CLEAN_ACTION,
                {"final": "done"},
            ]
        }
        actions = w4b_probe.clean_script(task)
        parsed = [json.loads(text) for text in actions]
        self.assertTrue(all("tool" in p or "final" in p for p in parsed))
        writes = [p for p in parsed if p.get("tool") == "refund_order"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(parsed[-1], {"final": w4b_probe.PLACEHOLDER_FINAL})

    def test_off_and_fpm_zero_baseline_have_no_guard_spans(self):
        common = dict(tasks_path=TASKS, seed=11, k=3)
        off = w4b_probe.dry_run(guards=(), fault_per_mille=300, **common)
        baseline = w4b_probe.dry_run(
            guards=guard.GUARD_NAMES, fault_per_mille=0, **common
        )
        self.assertEqual(off["guard_spans"], 0)
        self.assertEqual(off["events"], [])
        self.assertEqual(baseline["guard_spans"], 0)
        self.assertEqual(baseline["events"], [])
        self.assertGreater(len(off["injected"]), 0)

    def test_all4_denominator_comes_from_injection_and_covers_four_kinds(self):
        on = w4b_probe.dry_run(
            tasks_path=TASKS,
            guards=guard.GUARD_NAMES,
            seed=11,
            fault_per_mille=300,
            k=3,
        )
        self.assertGreater(on["guard_spans"], 0)
        self.assertEqual(len(on["events"]), on["guard_spans"])
        self.assertEqual(
            {kind for _, kind in on["injected"]}, set(SEEDED_FAULTS)
        )
        summary = guard_probe.summarize(on["events"])
        self.assertEqual(
            set(summary["kinds_seen"]),
            set(guard.GUARD_NAMES),
        )

    def test_dryrun_report_is_labelled_as_device_calibration(self):
        text = (
            ROOT / "experiments" / "w4b_probe_dryrun.md"
        ).read_text(encoding="utf-8")
        for phrase in (
            "不是 W4-b 的实验结论",
            "不预示真实模型的 responsive 率",
            "零成本、零网络",
        ):
            self.assertIn(phrase, text)
        self.assertIn("fault_per_mille=300", text)


class TestOpenAIPathEndToEnd(unittest.TestCase):
    """注入器必须能包在真实 openai 代码路径上，且 guard span 会落进 trace。"""

    def test_fake_opener_loop_produces_guard_spans_with_real_usage(self):
        opener = _FakeOpener()
        task = json.loads(TASKS.read_text(encoding="utf-8").splitlines()[0])
        template = db.build_template(task["initial_db"])
        conn = db.reset(template)
        try:
            real = OpenAIBackend(
                "https://api.deepseek.com", "FAKE-NOT-REAL", "deepseek-flash",
                opener=opener,
                system_prompt=build_tool_context(db.dump_state(conn)),
            )
            backend = OutputFaultBackend(
                real, seed=11, fault_per_mille=1000, task_id="T01", trial=1
            )
            result = loop.run_turn(
                [{"role": "user", "content": task["user_utterances"][0]}],
                backend,
                conn,
                trial_id="T01-t1",
                counter=loop.StepCounter(),
                budget=guard.StepBudget(max_steps=6),
                guards=guard.GUARD_NAMES,
                guard_state=guard.GuardState(),
            )
        finally:
            conn.close()
            template.close()
        guard_spans = [s for s in result.spans if s["role"] == "guard"]
        self.assertGreater(len(guard_spans), 0)
        events = guard_probe.probe_spans(result.spans)
        self.assertGreater(len(events), 0)
        assistant = [s for s in result.spans if s["role"] == "assistant"]
        self.assertTrue(all(s["tokens"] == 120 for s in assistant))
        self.assertTrue(opener.requests, "必须真的经过 OpenAIBackend 发请求")
        payload = json.loads(opener.requests[0].decode("utf-8"))
        self.assertEqual(payload["temperature"], 0)
        self.assertIn("可用工具", payload["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
