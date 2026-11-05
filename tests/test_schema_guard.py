"""W3 第一条护栏 schema_validation 的单元、链路与 A/B 对照验收。

本文件最重要的不是「护栏能拦下坏参数」，而是钉死三件事：
1. OFF 组与 W1 / W2.5 逐字节等价，护栏绝不在默认路径生效；
2. 两份 A/B 配置的测量输入逐字段相同，唯一自变量是 guards——否则
   「提升来自 harness」这句话不成立；
3. 提升要看 pass^k，不能拿 invalid_calls 下降充数（脚本固有缺陷被拦下
   后仍失败，只是归因从 bad_args 变成 unknown）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from env import db, tools
from eval import analyze
from eval import config as exp_config
from eval import grader, runner, trace
from eval import run as cli
from runtime import guard, loop
from runtime.model import (
    FAULT_BAD_ARGS,
    BackendConfig,
    ModelReply,
    ScriptedBackend,
    SeededBackend,
)

ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "tasks" / "mvp_10.jsonl"
FROZEN_ROWS = ROOT / "experiments" / "baseline" / "result_rows.jsonl"
OFF_CONFIG = ROOT / "experiments" / "guard_ab_off.toml"
ON_CONFIG = ROOT / "experiments" / "guard_ab_on.toml"
SCHEMA_GUARD = (guard.GUARD_SCHEMA_VALIDATION,)

# 进入测量链路的字段；name / compare 只影响归档位置与报告展示
MEASURE_KEYS = (
    "tasks",
    "k",
    "backend",
    "max_steps",
    "max_json_retries",
    "seed",
    "fault_per_mille",
)


def run_rows(guards: tuple[str, ...], target: Path) -> list[runner.Row]:
    return runner.run(
        runner.RunConfig(
            tasks_path=TASKS_PATH,
            k=3,
            backend=BackendConfig(name="seeded", seed=11, fault_per_mille=60),
            results_dir=target / "results",
            traces_dir=target / "traces",
            guards=guards,
        )
    )


class SequencedBackend:
    """按给定文本序列返回的后端，并记录每次 generate 看到的 messages。"""

    def __init__(self, texts: list[str]):
        self._texts = list(texts)
        self._last = texts[-1] if texts else ""
        self.seen: list[list[dict[str, str]]] = []

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        self.seen.append(list(messages))
        text = self._texts.pop(0) if self._texts else self._last
        self._last = text
        return ModelReply(text=text)


def first_tool_action() -> dict:
    task = next(t for t in runner.load_tasks(TASKS_PATH) if t["id"] == "T01")
    return next(
        item
        for item in task["script"]
        if isinstance(item, dict) and "tool" in item
    )


class TestSchemaRules(unittest.TestCase):
    def test_valid_calls_pass(self):
        cases = {
            "get_customer": {"customer_id": "C-1"},
            "get_order": {"order_id": "O-100"},
            "list_customer_orders": {"customer_id": "C-1"},
            "refund_order": {"order_id": "O-100", "amount_cents": 2500},
            "cancel_order": {"order_id": "O-100"},
            "notify_customer": {"customer_id": "C-1", "message": "ok"},
        }
        for name, args in cases.items():
            with self.subTest(tool=name):
                self.assertIsNone(tools.validate_args(name, args))
                self.assertIsNone(guard.rejection_message(name, args))

    def test_missing_null_and_wrong_types_are_rejected(self):
        bad = (
            ("get_order", {}),
            ("get_order", {"order_id": None}),
            ("refund_order", {"order_id": "O-100"}),
            ("refund_order", {"order_id": "O-100", "amount_cents": True}),
            ("refund_order", {"order_id": "O-100", "amount_cents": "2500"}),
            ("notify_customer", {"customer_id": "C-1", "message": 3}),
        )
        for name, args in bad:
            with self.subTest(tool=name, args=args):
                self.assertEqual(tools.validate_args(name, args), "bad_args")
                message = guard.rejection_message(name, args)
                self.assertIsNotNone(message)
                self.assertTrue(message.startswith(guard.GUARD_REJECT_PREFIX))

    def test_unknown_tool_is_not_guarded(self):
        """工具名合法性归工具层，护栏只管参数：这是单变量实验的边界。"""
        self.assertIsNone(tools.validate_args("__missing_tool__", {}))
        self.assertIsNone(guard.rejection_message("__missing_tool__", {}))
        observation, error = tools.call("__missing_tool__", {}, None)
        self.assertEqual(error, "wrong_tool")
        self.assertEqual(observation["error"], "wrong_tool")

    def test_tools_layer_behaviour_is_unchanged(self):
        """护栏前移不能偷偷改变工具层原有的报错契约。"""
        task = next(t for t in runner.load_tasks(TASKS_PATH) if t["id"] == "T01")
        template = db.build_template(task["initial_db"])
        conn = db.reset(template)
        try:
            observation, error = tools.call("get_order", {}, conn)
            self.assertEqual(error, "bad_args")
            self.assertFalse(observation["ok"])
        finally:
            conn.close()
            template.close()

    def test_guard_span_is_not_consumed_by_grader(self):
        """判分口径不因护栏开关改变：guard span 不产生可归因事件。"""
        span = loop.make_span("T01-t1", 3, "guard", tool="get_order", args={},
                              observation="[GUARD-REJECT] bad_args")
        self.assertEqual(grader.trace_events([span]), [])


class TestLoopInterception(unittest.TestCase):
    def _conn(self):
        task = next(t for t in runner.load_tasks(TASKS_PATH) if t["id"] == "T01")
        template = db.build_template(task["initial_db"])
        return template, db.reset(template)

    def test_rejection_happens_before_tool_call(self):
        action = first_tool_action()
        bad = json.dumps({"tool": action["tool"], "args": {}},
                         ensure_ascii=False, sort_keys=True)
        good = json.dumps(action, ensure_ascii=False, sort_keys=True)
        final = json.dumps({"final": "ok"}, ensure_ascii=False)
        backend = SequencedBackend([bad, good, final])
        template, conn = self._conn()
        try:
            messages: list[dict[str, str]] = []
            result = loop.run_turn(
                messages, backend, conn, trial_id="T01-t1",
                counter=loop.StepCounter(), budget=guard.StepBudget(),
                guards=SCHEMA_GUARD,
            )
        finally:
            conn.close()
            template.close()
        guard_spans = [s for s in result.spans if s["role"] == "guard"]
        self.assertEqual(len(guard_spans), 1)
        self.assertIn("args", guard_spans[0])
        # 第二次 generate 必须看到以前缀开头的末条消息，后端才有机会重发
        self.assertTrue(
            backend.seen[1][-1]["content"].startswith(guard.GUARD_REJECT_PREFIX)
        )
        self.assertEqual(result.termination, loop.TERMINATION_FINAL)
        self.assertNotIn("bad_args", result.errors, "拦下的调用不得计入 invalid_calls")

    def test_guard_off_emits_nothing(self):
        action = first_tool_action()
        bad = json.dumps({"tool": action["tool"], "args": {}},
                         ensure_ascii=False, sort_keys=True)
        backend = SequencedBackend([bad, json.dumps({"final": "ok"})])
        template, conn = self._conn()
        try:
            result = loop.run_turn(
                [], backend, conn, trial_id="T01-t1",
                counter=loop.StepCounter(), budget=guard.StepBudget(),
            )
        finally:
            conn.close()
            template.close()
        self.assertFalse([s for s in result.spans if s["role"] == "guard"])
        self.assertIn("bad_args", result.errors, "OFF 组必须保留工具层报错")

    def test_rejection_budget_prevents_infinite_loop(self):
        """脚本后端不响应拒绝消息；护栏必须自行收敛，不能吃光步数前一直空转。"""
        action = first_tool_action()
        bad = json.dumps({"tool": action["tool"], "args": {}},
                         ensure_ascii=False, sort_keys=True)
        backend = ScriptedBackend([bad])
        template, conn = self._conn()
        try:
            result = loop.run_turn(
                [], backend, conn, trial_id="T01-t1",
                counter=loop.StepCounter(), budget=guard.StepBudget(),
                guards=SCHEMA_GUARD,
            )
        finally:
            conn.close()
            template.close()
        guard_spans = [s for s in result.spans if s["role"] == "guard"]
        self.assertEqual(len(guard_spans), guard.MAX_GUARD_REJECTIONS)
        self.assertEqual(result.termination, loop.TERMINATION_MAX_STEPS)

    def test_seeded_pending_correct_expires_after_one_call(self):
        """wrong_tool 故障残留的干净动作，不得被后续无关的护栏拦截误触发。"""
        backend = SeededBackend(
            BackendConfig(name="seeded", seed=11),
            [first_tool_action(), {"final": "ok"}],
            task_id="T01", trial=1, force_fault=FAULT_BAD_ARGS,
        )
        bad = backend.generate([]).text
        self.assertEqual(json.loads(bad)["args"], {})
        # 紧邻的下一次不带前缀 -> 纠正机会作废，正常推进 cursor
        backend.generate([{"role": "tool", "content": '{"ok":false}'}])
        later = backend.generate(
            [{"role": "user", "content": f"{guard.GUARD_REJECT_PREFIX} 重试"}]
        ).text
        self.assertNotIn(first_tool_action()["tool"], later)


class TestConfigAndRunId(unittest.TestCase):
    def test_existing_run_ids_are_frozen(self):
        default = exp_config.load_config()
        calibration = exp_config.load_config(
            ROOT / "experiments" / "calibration.toml"
        )
        self.assertEqual(exp_config.run_id(default), "baseline_scripted-a346e8acef15")
        self.assertEqual(exp_config.run_id(calibration), "calibration_seeded-ea22719ba7c2")

    def test_guard_switch_changes_run_id_but_not_when_off(self):
        calibration = exp_config.load_config(
            ROOT / "experiments" / "calibration.toml"
        )
        self.assertNotIn("guards", exp_config._core(calibration))
        on = exp_config.with_overrides(calibration, guards=SCHEMA_GUARD)
        self.assertEqual(exp_config._core(on)["guards"], ["schema_validation"])
        self.assertNotEqual(exp_config.run_id(on), exp_config.run_id(calibration))

    def test_ab_configs_differ_only_in_the_guard_switch(self):
        """单变量纪律的机器可读证据：测量输入逐字段相同。"""
        off = exp_config.load_config(OFF_CONFIG)
        on = exp_config.load_config(ON_CONFIG)
        for key in MEASURE_KEYS:
            with self.subTest(field=key):
                self.assertEqual(getattr(off, key), getattr(on, key))
        self.assertEqual(off.guards, ())
        self.assertEqual(on.guards, SCHEMA_GUARD)
        self.assertEqual(off.backend, "seeded")
        self.assertEqual(on.compare, "experiments/runs/w3_ab_off-346ab5e98382/result_rows.jsonl")

    def test_unknown_or_duplicate_guards_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.toml"
            for body in (
                '[guards]\nenabled = ["teleport"]\n',
                '[guards]\nenabled = ["schema_validation", "schema_validation"]\n',
                '[guards]\nenabled = "schema_validation"\n',
            ):
                path.write_text(body, encoding="utf-8")
                with self.subTest(body=body.strip()), self.assertRaises(ValueError):
                    exp_config.load_config(path)

    def test_cli_guard_merges_without_duplicates(self):
        args = cli.build_parser().parse_args(
            ["--guard", "schema_validation", "--guard", "schema_validation"]
        )
        self.assertEqual(args.guard, ["schema_validation", "schema_validation"])
        base = exp_config.load_config(ON_CONFIG)
        merged = tuple(dict.fromkeys([*base.guards, *(args.guard or ())]))
        self.assertEqual(merged, SCHEMA_GUARD)


class TestABExperiment(unittest.TestCase):
    def test_off_group_is_byte_reproducible(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            run_rows((), p / "off")
            run_rows((), p / "off2")
            self.assertEqual(
                (p / "off" / "results" / "result_rows.jsonl").read_bytes(),
                (p / "off2" / "results" / "result_rows.jsonl").read_bytes(),
            )

    def test_on_group_is_byte_reproducible(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            run_rows(SCHEMA_GUARD, p / "a")
            run_rows(SCHEMA_GUARD, p / "b")
            self.assertEqual(
                (p / "a" / "results" / "result_rows.jsonl").read_bytes(),
                (p / "b" / "results" / "result_rows.jsonl").read_bytes(),
            )

    def test_guard_raises_passk_not_pass1(self):
        """核心验收：同后端、同 seed，只开一条护栏，pass^k 严格提升。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            off = run_rows((), p / "off")
            on = run_rows(SCHEMA_GUARD, p / "on")
        so, sn = analyze.summarize(off, 3), analyze.summarize(on, 3)
        self.assertEqual(so["pass1"], sn["pass1"])
        self.assertGreater(sn["passk"], so["passk"])
        self.assertLess(sn["invalid_calls"], so["invalid_calls"])
        flipped = [
            (a["trial_id"], a["passed"], b["passed"])
            for a, b in zip(off, on)
            if a["passed"] != b["passed"]
        ]
        self.assertTrue(flipped, "必须至少有一个 trial 因护栏从失败变通过")
        self.assertTrue(all(before == 0 and after == 1 for _, before, after in flipped))
        self.assertNotIn(
            "bad_args", {r["failure_type"] for r in on if not r["passed"]}
        )

    def test_intrinsic_defects_are_not_silently_fixed(self):
        """护栏不能修好脚本固有缺陷；否则等于「对着答案改答案」。"""
        with tempfile.TemporaryDirectory() as d:
            on = run_rows(SCHEMA_GUARD, Path(d) / "on")
        t08 = [r for r in on if r["task_id"] == "T08"]
        self.assertEqual(len(t08), 3)
        self.assertTrue(all(r["passed"] == 0 for r in t08))

    def test_default_scripted_metrics_still_match_frozen_baseline(self):
        """护栏是可选开关，默认路径的测量指标一个都不许变。"""
        frozen = trace.read_jsonl(FROZEN_ROWS)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            rows = runner.run(
                runner.RunConfig(
                    tasks_path=TASKS_PATH, k=3,
                    results_dir=p / "results", traces_dir=p / "traces",
                )
            )
        keys = ("passed", "failure_type", "steps", "tokens", "latency_ms", "invalid_calls")
        self.assertEqual(
            [tuple(r[k] for k in keys) for r in frozen],
            [tuple(r[k] for k in keys) for r in rows],
        )


class TestABReport(unittest.TestCase):
    def _render(self, cfg, rows, off_rows):
        rid = exp_config.run_id(cfg)
        return analyze.render_report(
            rows,
            runner.load_tasks(TASKS_PATH),
            cfg,
            rid,
            off_rows,
            config_path="experiments/guard_ab_on.toml",
        )

    def test_ab_report_uses_comparison_title_and_warns_about_invalid_calls(self):
        cfg = exp_config.load_config(ON_CONFIG)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            off = run_rows((), p / "off")
            on = run_rows(SCHEMA_GUARD, p / "on")
        text = self._render(cfg, on, off)
        self.assertIn("## 与对照组对照（A/B）", text)
        self.assertNotIn("## 与冻结基线对照", text)
        self.assertIn("| 指标 | 对照组 | 本次 | 变化 |", text)
        self.assertIn("| 已开启护栏 | `schema_validation` |", text)
        self.assertIn("`invalid_calls` 下降不等于成功率提升", text)
        self.assertIn("--guard schema_validation", text)
        self.assertIn("--config experiments/guard_ab_on.toml", text)
        self.assertNotIn("⚠️", text, "同后端 A/B 不该出现跨后端警告")

    def test_off_report_still_uses_frozen_baseline_title(self):
        cfg = exp_config.load_config(OFF_CONFIG)
        frozen = trace.read_jsonl(FROZEN_ROWS)
        with tempfile.TemporaryDirectory() as d:
            off = run_rows((), Path(d) / "off")
        text = analyze.render_report(
            off, runner.load_tasks(TASKS_PATH), cfg,
            exp_config.run_id(cfg), frozen,
            config_path="experiments/guard_ab_off.toml",
        )
        self.assertIn("## 与冻结基线对照", text)
        self.assertNotIn("## 与对照组对照", text)
        self.assertNotIn("| 已开启护栏", text)
        self.assertIn("⚠️", text, "seeded 对冻结 scripted 基线必须保留跨后端警告")


if __name__ == "__main__":
    unittest.main()
