"""SeededBackend 单元契约：确定性、故障构造、收敛与 W3 预留接口。

这些用例只测后端本身，不跑完整 loop / grader，因此每个断言都直接可归因。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from runtime.model import (
    FAULT_BAD_ARGS,
    FAULT_BAD_JSON,
    FAULT_DUPLICATE_CALL,
    FAULT_WRONG_TOOL,
    GUARD_REJECT_PREFIX,
    SEEDED_FAULTS,
    BackendConfig,
    ScriptedBackend,
    SeededBackend,
)

SCRIPT = [
    {"tool": "get_order", "args": {"order_id": "O-100"}},
    {"tool": "refund_order", "args": {"order_id": "O-100", "amount_cents": 2500}},
    {
        "tool": "notify_customer",
        "args": {"customer_id": "C-1", "message": "已退款 2500 分"},
    },
    {"final": "订单 O-100 已为您退款 2500 分。"},
]


def seeded(seed: int = 11, fpm: int = 60, **kwargs) -> SeededBackend:
    return SeededBackend(
        BackendConfig(name="seeded", seed=seed, fault_per_mille=fpm),
        SCRIPT,
        **kwargs,
    )


def sequence(backend: SeededBackend, count: int = 6) -> list[str]:
    return [backend.generate([]).text for _ in range(count)]


class TestDeterminism(unittest.TestCase):
    def test_same_trial_is_byte_identical(self):
        self.assertEqual(
            sequence(seeded(trial=1, task_id="T01")),
            sequence(seeded(trial=1, task_id="T01")),
        )

    def test_different_trials_diverge(self):
        """RNG 流按 trial 隔离，这是 pass^k 具备区分度的前提。"""
        self.assertNotEqual(
            sequence(seeded(fpm=1000, trial=1, task_id="T01")),
            sequence(seeded(fpm=1000, trial=2, task_id="T01")),
        )

    def test_different_tasks_diverge(self):
        self.assertNotEqual(
            sequence(seeded(fpm=1000, trial=1, task_id="T01")),
            sequence(seeded(fpm=1000, trial=1, task_id="T02")),
        )

    def test_zero_fault_rate_equals_scripted(self):
        """关掉噪声必须逐字回到冻结基线行为。"""
        clean = seeded(fpm=0, trial=1, task_id="T01")
        scripted = ScriptedBackend(SCRIPT)
        self.assertEqual(
            [clean.generate([]).text for _ in range(8)],
            [scripted.generate([]).text for _ in range(8)],
        )

    def test_rng_sources_are_explicit_and_stable(self):
        """RNG 由 SHA256 派生，不依赖进程内状态；跨实例同参必得同一串。"""
        first = sequence(seeded(fpm=500, trial=1, task_id="T01"))
        second = sequence(seeded(fpm=500, trial=1, task_id="T01"))
        self.assertEqual(first, second)
        self.assertIn("hashlib.sha256", SEEDED_SOURCE)


class TestFaultInjection(unittest.TestCase):
    def test_all_four_faults_are_constructible(self):
        for kind in SEEDED_FAULTS:
            with self.subTest(kind=kind):
                backend = seeded(force_fault=kind, task_id="T01", trial=1)
                self.assertEqual(backend.generate([]).text, EXPECTED_FAULTS[kind])

    def test_bad_json_is_not_parseable(self):
        text = seeded(force_fault=FAULT_BAD_JSON).generate([]).text
        with self.assertRaises(ValueError):
            json.loads(text)

    def test_bad_args_and_wrong_tool_keep_shape(self):
        for kind, tool in ((FAULT_BAD_ARGS, "get_order"), (FAULT_WRONG_TOOL, "__missing_tool__")):
            action = json.loads(seeded(force_fault=kind).generate([]).text)
            self.assertEqual(action["tool"], tool)

    def test_duplicate_call_degrades_gracefully_at_start(self):
        """开局没有写动作可重放时退化为 bad_args，而不是崩掉或空输出。"""
        text = seeded(force_fault=FAULT_DUPLICATE_CALL).generate([]).text
        self.assertEqual(json.loads(text)["tool"], "get_order")

    def test_duplicate_call_replays_previous_write(self):
        """干净跑一遍拿到写动作，再用高故障率构造「后续动作重放它」的场景。"""
        clean = seeded(fpm=0)
        clean_texts = sequence(clean, 3)
        first_write = next(
            text
            for text in clean_texts
            if json.loads(text).get("tool") == "refund_order"
        )
        # 先用干净序列把写动作喂进去，再强制在下一个动作上注入 duplicate_call
        backend = SeededBackend(
            BackendConfig(name="seeded", seed=11, fault_per_mille=0),
            SCRIPT,
            task_id="T01",
            trial=1,
        )
        backend.generate([])  # get_order
        backend.generate([])  # refund_order -> 记录为已执行的写动作
        backend._force_fault = FAULT_DUPLICATE_CALL
        self.assertEqual(backend.generate([]).text, first_write)

    def test_script_intrinsic_bad_json_is_preserved(self):
        """任务脚本里故意写坏的 JSON 原样输出，不被注入覆盖。"""
        backend = SeededBackend(
            BackendConfig(name="seeded", seed=11, fault_per_mille=1000),
            ['{"tool": "get_order", "args": {"order_id": "O-100"', {"final": "ok"}],
            task_id="T-bad",
            trial=1,
        )
        self.assertTrue(backend.generate([]).text.startswith('{"tool": "get_order"'))
        self.assertEqual(backend.generate([]).text, '{"final": "ok"}')

    def test_final_action_is_never_perturbed(self):
        """final 不参与注入，避免后端永远无法结束本轮。"""
        backend = seeded(fpm=1000, force_fault=FAULT_BAD_JSON)
        texts = sequence(backend, len(SCRIPT))
        self.assertEqual(json.loads(texts[-1])["final"], "订单 O-100 已为您退款 2500 分。")

    def test_each_cursor_is_injected_at_most_once(self):
        """cursor 恒前进，故障不会被重试反复注入、也不会吃光 MAX_STEPS。"""
        backend = seeded(fpm=1000, force_fault=FAULT_BAD_JSON)
        texts = sequence(backend, 8)
        # 前三个工具动作各注入一次，第四个 final 干净，之后重复末项
        self.assertEqual(texts[:3], ["{not-json"] * 3)
        self.assertEqual(texts[3], texts[4])
        self.assertEqual(texts[4], texts[7])

    def test_exhausted_script_repeats_last_item_without_faults(self):
        backend = seeded(fpm=1000, force_fault=FAULT_BAD_JSON)
        texts = sequence(backend, 10)
        self.assertEqual(texts[-1], texts[3])
        self.assertNotIn("{not-json", texts[4:])


class TestRepairProtocol(unittest.TestCase):
    def test_guard_reject_replays_clean_action(self):
        backend = seeded(force_fault=FAULT_BAD_JSON)
        faulted = backend.generate([]).text
        corrected = backend.generate(
            [{"role": "user", "content": f"{GUARD_REJECT_PREFIX} 参数不合法"}]
        ).text
        self.assertNotEqual(faulted, corrected)
        self.assertIn("get_order", corrected)
        self.assertNotIn("not-json", corrected)

    def test_guard_reject_only_triggers_once_per_fault(self):
        """纠正消费掉 pending 后，残留前缀不得反复重发同一动作。"""
        backend = seeded(force_fault=FAULT_BAD_JSON)
        backend.generate([])
        hint = [{"role": "user", "content": f"{GUARD_REJECT_PREFIX} rejected"}]
        self.assertIn("get_order", backend.generate(hint).text)
        # 第二次带同样前缀时 pending 已清空，继续走脚本下一个动作（再次注入故障）
        self.assertEqual(backend.generate(hint).text, "{not-json")

    def test_runtime_retry_hint_does_not_silently_repair(self):
        """运行时的 RETRY_HINT / 工具报错不触发主动纠正，以保留故障后果。"""
        from runtime.loop import RETRY_HINT

        backend = seeded(force_fault=FAULT_BAD_JSON)
        backend.generate([])
        for hint in (RETRY_HINT, '{"ok": false, "error": "bad_args"}'):
            with self.subTest(hint=hint):
                fresh = seeded(force_fault=FAULT_BAD_JSON)
                fresh.generate([])
                self.assertEqual(
                    fresh.generate([{"role": "user", "content": hint}]).text,
                    "{not-json",
                )

    def test_unknown_force_fault_is_rejected(self):
        with self.assertRaises(ValueError):
            seeded(force_fault="teleport")


class TestObservationValues(unittest.TestCase):
    def test_model_reply_is_always_zero(self):
        backend = seeded(fpm=1000)
        for _ in range(5):
            reply = backend.generate([])
            self.assertEqual(
                (reply.tokens, reply.prompt_tokens, reply.completion_tokens, reply.latency_ms),
                (0, 0, 0, 0),
            )

    def test_empty_script_returns_empty_reply(self):
        backend = SeededBackend(BackendConfig(name="seeded"), [])
        self.assertEqual(backend.generate([]).text, "")


EXPECTED_FAULTS = {
    FAULT_BAD_JSON: "{not-json",
    FAULT_BAD_ARGS: '{"args": {}, "tool": "get_order"}',
    FAULT_WRONG_TOOL: '{"args": {"order_id": "O-100"}, "tool": "__missing_tool__"}',
    # 开局无写动作时退化为 bad_args
    FAULT_DUPLICATE_CALL: '{"args": {}, "tool": "get_order"}',
}

# 供源码级守门使用
SEEDED_SOURCE = (Path(__file__).resolve().parents[1] / "runtime" / "model.py").read_text(
    encoding="utf-8"
)


if __name__ == "__main__":
    unittest.main()
