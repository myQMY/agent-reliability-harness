"""W3 第二条护栏 idempotency 与 2×2 全因子实验的验收。

本文件钉死的四件事：
1. 幂等键由 (tool, args) 经 SHA256 确定性派生，只拦**已成功执行**的写调用；
   读工具重复、失败调用、未知工具都不归它管，状态跨 turn 共享；
2. 四份配置构成 2×2 全因子，测量输入逐字段相同，只有 guards 不同；
   既有的 4 个 run_id 与 3 份留档报告逐字节不变；
3. 核心反直觉结论：`idempotency` 让 pass^k 提升，但 `invalid_calls` 可以
   **一点不变**——它拦的是工具层不报错的重复写。因此中间指标不能当代理；
4. 矩阵报告的主效应 / 交互效应是纯计算，且仓库留档与代码渲染逐字节一致。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from env import db, tools
from eval import analyze
from eval import config as exp_config
from eval import grader, guard_matrix, runner, trace
from eval import run as cli
from runtime import guard, loop
from runtime.model import BackendConfig, ModelReply, ScriptedBackend

ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "tasks" / "mvp_10.jsonl"
FROZEN_ROWS = ROOT / "experiments" / "baseline" / "result_rows.jsonl"
MATRIX_REPORT = ROOT / "experiments" / "w3_guard_matrix.md"

SCHEMA = guard.GUARD_SCHEMA_VALIDATION
IDEM = guard.GUARD_IDEMPOTENCY

CONFIG_PATHS = {
    "off": ROOT / "experiments" / "guard_ab_off.toml",
    "schema": ROOT / "experiments" / "guard_ab_on.toml",
    "idem": ROOT / "experiments" / "guard_ab_idem.toml",
    "both": ROOT / "experiments" / "guard_ab_both.toml",
}
RUN_IDS = {
    "off": "w3_ab_off-346ab5e98382",
    "schema": "w3_ab_on-fa17143eb761",
    "idem": "w3_idem_on-a4e1925e1619",
    "both": "w3_both_on-3c15f3bd94fd",
}
CELL_GUARDS = {
    "off": (),
    "schema": (SCHEMA,),
    "idem": (IDEM,),
    "both": (SCHEMA, IDEM),
}
OFF_COMPARE = "experiments/runs/w3_ab_off-346ab5e98382/result_rows.jsonl"

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


def write_action() -> dict:
    task = next(t for t in runner.load_tasks(TASKS_PATH) if t["id"] == "T01")
    return next(
        item
        for item in task["script"]
        if isinstance(item, dict) and item.get("tool") == "refund_order"
    )


def action_text(action: dict) -> str:
    return json.dumps(action, ensure_ascii=False, sort_keys=True)


class TestIdempotencyRules(unittest.TestCase):
    def test_key_is_deterministic_and_order_independent(self):
        args = {"order_id": "O-100", "amount_cents": 2500}
        a = guard.idempotency_key("refund_order", args)
        b = guard.idempotency_key(
            "refund_order", {"amount_cents": 2500, "order_id": "O-100"}
        )
        self.assertEqual(a, b)
        self.assertEqual(len(a), guard.IDEMPOTENCY_KEY_LENGTH)
        self.assertNotEqual(
            a, guard.idempotency_key("refund_order", {**args, "amount_cents": 2501})
        )
        self.assertNotEqual(a, guard.idempotency_key("cancel_order", args))

    def test_write_tool_list_has_one_source(self):
        """护栏 / 判分器 / 故障注入必须共用同一份清单，新增工具只改一处。"""
        from runtime import model

        self.assertIs(tools.WRITE_TOOLS, grader.WRITE_TOOLS)
        self.assertIs(tools.WRITE_TOOLS, model._SEEDED_WRITE_TOOLS)

    def test_read_and_unknown_tools_are_never_guarded(self):
        state = guard.GuardState()
        guard.record_write("get_order", {"order_id": "O-100"}, state)
        guard.record_write("__missing_tool__", {}, state)
        self.assertEqual(state.executed_write_keys, set())
        self.assertIsNone(
            guard.idempotency_rejection("get_order", {"order_id": "O-100"}, state)
        )
        self.assertIsNone(guard.idempotency_rejection("__missing_tool__", {}, state))

    def test_rejection_message_carries_the_key_for_audit(self):
        state = guard.GuardState()
        action = write_action()
        key = guard.idempotency_key(action["tool"], action["args"])
        self.assertIsNone(
            guard.idempotency_rejection(action["tool"], action["args"], state)
        )
        guard.record_write(action["tool"], action["args"], state)
        message = guard.idempotency_rejection(action["tool"], action["args"], state)
        self.assertIsNotNone(message)
        self.assertTrue(message.startswith(guard.GUARD_REJECT_PREFIX))
        self.assertIn(key, message)

    def test_none_state_is_a_noop(self):
        action = write_action()
        self.assertIsNone(
            guard.idempotency_rejection(action["tool"], action["args"], None)
        )
        guard.record_write(action["tool"], action["args"], None)


class TestLoopInterception(unittest.TestCase):
    def _conn(self):
        task = next(t for t in runner.load_tasks(TASKS_PATH) if t["id"] == "T01")
        template = db.build_template(task["initial_db"])
        return template, db.reset(template)

    def _turn(self, backend, guards, state, conn, messages=None):
        return loop.run_turn(
            [] if messages is None else messages,
            backend,
            conn,
            trial_id="T01-t1",
            counter=loop.StepCounter(),
            budget=guard.StepBudget(),
            guards=guards,
            guard_state=state,
        )

    def test_duplicate_write_is_intercepted_before_second_call(self):
        action = write_action()
        text = action_text(action)
        backend = SequencedBackend([text, text, action_text({"final": "ok"})])
        state = guard.GuardState()
        template, conn = self._conn()
        try:
            result = self._turn(backend, (IDEM,), state, conn)
            final_state = db.dump_state(conn)
        finally:
            conn.close()
            template.close()
        guard_spans = [s for s in result.spans if s["role"] == "guard"]
        tool_spans = [s for s in result.spans if s["role"] == "tool"]
        self.assertEqual(len(guard_spans), 1)
        self.assertEqual(len(tool_spans), 1)
        self.assertTrue(
            backend.seen[2][-1]["content"].startswith(guard.GUARD_REJECT_PREFIX)
        )
        self.assertNotIn("bad_args", result.errors)
        refunds = [r for r in final_state["audit_log"] if r["action"] == "refund"]
        self.assertEqual(len(refunds), 1, "重复写不得真的落到领域库")

    def test_guard_off_executes_both_writes(self):
        action = write_action()
        text = action_text(action)
        backend = SequencedBackend([text, text, action_text({"final": "ok"})])
        template, conn = self._conn()
        try:
            result = self._turn(backend, (), None, conn)
            final_state = db.dump_state(conn)
        finally:
            conn.close()
            template.close()
        self.assertFalse([s for s in result.spans if s["role"] == "guard"])
        refunds = [r for r in final_state["audit_log"] if r["action"] == "refund"]
        self.assertEqual(len(refunds), 2)

    def test_read_tool_repetition_is_never_intercepted(self):
        action = {"tool": "get_order", "args": {"order_id": "O-100"}}
        text = action_text(action)
        backend = SequencedBackend([text, text, action_text({"final": "ok"})])
        template, conn = self._conn()
        try:
            result = self._turn(backend, (IDEM,), guard.GuardState(), conn)
        finally:
            conn.close()
            template.close()
        self.assertFalse([s for s in result.spans if s["role"] == "guard"])
        self.assertEqual(len([s for s in result.spans if s["role"] == "tool"]), 2)

    def test_failed_write_is_not_recorded(self):
        """工具层报错的调用没改变状态，重复它不算幂等违规。"""
        bad = {
            "tool": "refund_order",
            "args": {"order_id": "O-MISSING", "amount_cents": 100},
        }
        text = action_text(bad)
        backend = SequencedBackend([text, text, action_text({"final": "ok"})])
        template, conn = self._conn()
        try:
            result = self._turn(backend, (IDEM,), guard.GuardState(), conn)
        finally:
            conn.close()
            template.close()
        self.assertFalse([s for s in result.spans if s["role"] == "guard"])
        self.assertEqual(len([s for s in result.spans if s["role"] == "tool"]), 2)

    def test_state_is_shared_across_turns(self):
        action = write_action()
        text = action_text(action)
        state = guard.GuardState()
        template, conn = self._conn()
        try:
            first = self._turn(
                SequencedBackend([text, action_text({"final": "a"})]),
                (IDEM,), state, conn,
            )
            second = self._turn(
                SequencedBackend([text, action_text({"final": "b"})]),
                (IDEM,), state, conn,
            )
        finally:
            conn.close()
            template.close()
        self.assertFalse([s for s in first.spans if s["role"] == "guard"])
        self.assertEqual(len([s for s in second.spans if s["role"] == "guard"]), 1)

    def test_rejection_budget_converges(self):
        action = write_action()
        backend = ScriptedBackend([action_text(action)])
        template, conn = self._conn()
        try:
            result = self._turn(backend, (IDEM,), guard.GuardState(), conn)
        finally:
            conn.close()
            template.close()
        self.assertEqual(
            len([s for s in result.spans if s["role"] == "guard"]),
            guard.MAX_GUARD_REJECTIONS,
        )
        self.assertEqual(result.termination, loop.TERMINATION_MAX_STEPS)

    def test_guard_span_is_not_consumed_by_grader(self):
        span = loop.make_span(
            "T01-t1", 3, "guard", tool="refund_order", args={},
            observation="[GUARD-REJECT] duplicate",
        )
        self.assertEqual(grader.trace_events([span]), [])


class TestConfigAndRunIds(unittest.TestCase):
    def test_existing_and_new_run_ids_are_frozen(self):
        self.assertEqual(
            exp_config.run_id(exp_config.load_config()),
            "baseline_scripted-a346e8acef15",
        )
        self.assertEqual(
            exp_config.run_id(
                exp_config.load_config(ROOT / "experiments" / "calibration.toml")
            ),
            "calibration_seeded-ea22719ba7c2",
        )
        for name, path in CONFIG_PATHS.items():
            with self.subTest(cell=name):
                self.assertEqual(exp_config.run_id(exp_config.load_config(path)), RUN_IDS[name])

    def test_four_cells_form_a_full_factorial(self):
        configs = {name: exp_config.load_config(path) for name, path in CONFIG_PATHS.items()}
        base = configs["off"]
        for name, cfg in configs.items():
            for key in MEASURE_KEYS:
                with self.subTest(cell=name, field=key):
                    self.assertEqual(getattr(cfg, key), getattr(base, key))
            with self.subTest(cell=name, field="guards"):
                self.assertEqual(cfg.guards, CELL_GUARDS[name])
            with self.subTest(cell=name, field="backend"):
                self.assertEqual(cfg.backend, "seeded")
        for name in ("schema", "idem", "both"):
            self.assertEqual(configs[name].compare, OFF_COMPARE)

    def test_guard_switch_only_enters_summary_when_enabled(self):
        off = exp_config.load_config(CONFIG_PATHS["off"])
        self.assertNotIn("guards", exp_config._core(off))
        for name in ("schema", "idem", "both"):
            cfg = exp_config.load_config(CONFIG_PATHS[name])
            with self.subTest(cell=name):
                self.assertEqual(list(exp_config._core(cfg)["guards"]), list(cfg.guards))
                self.assertNotEqual(exp_config.run_id(cfg), exp_config.run_id(off))

    def test_cli_accepts_both_guards(self):
        args = cli.build_parser().parse_args(
            ["--guard", "schema_validation", "--guard", "idempotency"]
        )
        self.assertEqual(args.guard, ["schema_validation", "idempotency"])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.toml"
            path.write_text('[guards]\nenabled = ["idempotencyy"]\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                exp_config.load_config(path)


class TestFactorialExperiment(unittest.TestCase):
    def test_four_cells_match_archives_byte_for_byte(self):
        """留档是另一次独立跑批，因此同时验证了确定性与归档未被篡改。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            for name, guards in CELL_GUARDS.items():
                target = p / name
                run_rows(guards, target)
                archived = (
                    ROOT / "experiments" / "runs" / RUN_IDS[name] / "result_rows.jsonl"
                ).read_bytes()
                with self.subTest(cell=name):
                    self.assertEqual(
                        (target / "results" / "result_rows.jsonl").read_bytes(),
                        archived,
                    )

    def test_idempotency_raises_passk_without_touching_invalid_calls(self):
        """核心验收：中间指标对「重复写」这类修复完全没有分辨力。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            off = run_rows((), p / "off")
            on = run_rows((IDEM,), p / "on")
        so, sn = analyze.summarize(off, 3), analyze.summarize(on, 3)
        self.assertGreater(sn["passk"], so["passk"])
        self.assertGreater(sn["pass1"], so["pass1"])
        self.assertEqual(sn["invalid_calls"], so["invalid_calls"])
        flipped = [
            (a["trial_id"], a["passed"], b["passed"])
            for a, b in zip(off, on)
            if a["passed"] != b["passed"]
        ]
        self.assertTrue(flipped, "必须至少有一个 trial 因幂等护栏从失败变通过")
        self.assertTrue(all(before == 0 and after == 1 for _, before, after in flipped))

    def test_duplicate_write_defect_is_fixed_but_bad_args_is_not(self):
        """T03 脚本故意写了两次相同退款；T08 缺参数是另一类固有缺陷。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            off = run_rows((), p / "off")
            on = run_rows((IDEM,), p / "on")
        t03_off = [r["passed"] for r in off if r["task_id"] == "T03"]
        t03_on = [r["passed"] for r in on if r["task_id"] == "T03"]
        self.assertEqual(t03_off, [0, 0, 0])
        self.assertEqual(t03_on, [1, 1, 1])
        t08 = [r["passed"] for r in on if r["task_id"] == "T08"]
        self.assertEqual(t08, [0, 0, 0], "缺参数不归幂等护栏管，不得被静默修好")

    def test_both_cell_is_at_least_each_single_cell(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            summaries = {
                name: analyze.summarize(run_rows(guards, p / name), 3)
                for name, guards in CELL_GUARDS.items()
            }
        both = summaries["both"]["passk"]
        self.assertGreaterEqual(both, summaries["schema"]["passk"])
        self.assertGreaterEqual(both, summaries["idem"]["passk"])
        self.assertGreater(both, summaries["off"]["passk"])

    def test_idem_group_guard_spans_only_target_writes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            run_rows((IDEM,), p / "on")
            spans = [
                span
                for path in sorted((p / "on" / "traces").glob("*.jsonl"))
                for span in trace.read_jsonl(path)
                if span["role"] == "guard"
            ]
        self.assertTrue(spans, "护栏必须在真实跑批里被触发，不能只活在单测里")
        self.assertTrue(all(s["tool"] in tools.WRITE_TOOLS for s in spans))
        self.assertTrue(all("幂等键" in s["observation"] for s in spans))

    def test_default_scripted_metrics_still_match_frozen_baseline(self):
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


class TestReportAndCaveats(unittest.TestCase):
    def _render_archived(self, name: str, config_path: str | None):
        cfg = exp_config.load_config(CONFIG_PATHS[name])
        rid = RUN_IDS[name]
        run_dir = ROOT / "experiments" / "runs" / rid
        rows = trace.read_jsonl(run_dir / "result_rows.jsonl")
        compare = exp_config.resolve_input(cfg.compare) if cfg.compare else None
        baseline = trace.read_jsonl(compare) if compare and compare.is_file() else None
        return analyze.render_report(
            rows, runner.load_tasks(TASKS_PATH), cfg, rid, baseline,
            config_path=config_path,
        )

    def test_three_existing_archived_reports_are_unchanged(self):
        """改口径措辞必须按护栏分支，不能悄悄改写已经归档的 A/B 结论。"""
        archived = (
            ("off", "experiments/guard_ab_off.toml"),
            ("schema", "experiments/guard_ab_on.toml"),
        )
        for name, config_path in archived:
            with self.subTest(cell=name):
                path = (
                    ROOT / "experiments" / "runs" / RUN_IDS[name] / "report.md"
                )
                self.assertEqual(
                    path.read_bytes(),
                    self._render_archived(name, config_path).encode("utf-8"),
                )
        default_dir = ROOT / "experiments" / "runs" / "baseline_scripted-a346e8acef15"
        default = default_dir / "report.md"
        cfg = exp_config.load_config()
        # 留档报告的行是 W2 的 11 字段 schema，对照表才来自 W1 冻结基线；
        # 两者不能混用，否则「归因证据」表会对不上。
        rows = trace.read_jsonl(default_dir / "result_rows.jsonl")
        self.assertEqual(
            default.read_bytes(),
            analyze.render_report(
                rows, runner.load_tasks(TASKS_PATH), cfg,
                exp_config.run_id(cfg), trace.read_jsonl(FROZEN_ROWS),
            ).encode("utf-8"),
        )

    def test_new_archived_reports_and_snapshots_match_code(self):
        """留档不是一次性产物：报告与 config.json 都必须能由当前代码重算。"""
        for name, config_path in (
            ("idem", "experiments/guard_ab_idem.toml"),
            ("both", "experiments/guard_ab_both.toml"),
        ):
            with self.subTest(cell=name):
                run_dir = ROOT / "experiments" / "runs" / RUN_IDS[name]
                self.assertEqual(
                    (run_dir / "report.md").read_bytes(),
                    self._render_archived(name, config_path).encode("utf-8"),
                )
                cfg = exp_config.load_config(CONFIG_PATHS[name])
                expected = (
                    trace.dumps(exp_config.config_snapshot(cfg, RUN_IDS[name])) + "\n"
                ).encode("utf-8")
                self.assertEqual((run_dir / "config.json").read_bytes(), expected)

    def test_idem_report_explains_flat_invalid_calls(self):
        cfg = exp_config.load_config(CONFIG_PATHS["idem"])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            off = run_rows((), p / "off")
            on = run_rows((IDEM,), p / "on")
        text = analyze.render_report(
            on, runner.load_tasks(TASKS_PATH), cfg,
            exp_config.run_id(cfg), off,
            config_path="experiments/guard_ab_idem.toml",
        )
        self.assertIn("## 与对照组对照（A/B）", text)
        self.assertIn("| 已开启护栏 | `idempotency` |", text)
        self.assertIn("`invalid_calls` 与成功率没有必然联系", text)
        self.assertIn("完全不变而 `pass^k` 提升", text)
        self.assertIn("--guard idempotency", text)
        self.assertIn("--config experiments/guard_ab_idem.toml", text)
        self.assertNotIn("⚠️", text, "同后端 A/B 不该出现跨后端警告")

    def test_schema_report_keeps_its_original_caveat(self):
        text = (
            ROOT / "experiments" / "runs" / RUN_IDS["schema"] / "report.md"
        ).read_text(encoding="utf-8")
        self.assertIn("`invalid_calls` 下降不等于成功率提升", text)
        self.assertNotIn("与成功率没有必然联系", text)


class TestGuardMatrix(unittest.TestCase):
    def test_effects_of_is_pure_and_correct(self):
        additive = {"off": 0.0, "schema": 0.2, "idem": 0.2, "both": 0.4}
        effects = guard_matrix.effects_of(additive)
        self.assertAlmostEqual(effects["schema"], 0.2)
        self.assertAlmostEqual(effects["idempotency"], 0.2)
        self.assertAlmostEqual(effects["interaction"], 0.0)
        self.assertEqual(additive["off"], 0.0, "不得修改输入")
        interacting = {"off": 0.0, "schema": 0.2, "idem": 0.1, "both": 0.5}
        self.assertAlmostEqual(guard_matrix.effects_of(interacting)["interaction"], 0.2)

    def test_float_noise_is_normalized_to_zero(self):
        values = {"off": 0.1, "schema": 0.2, "idem": 0.2, "both": 0.3}
        self.assertEqual(guard_matrix.effects_of(values)["interaction"], 0.0)

    def test_archived_matrix_matches_code_and_is_reproducible(self):
        first = guard_matrix.build_matrix()
        second = guard_matrix.build_matrix()
        text = guard_matrix.render_matrix(first)
        self.assertEqual(text, guard_matrix.render_matrix(second))
        self.assertEqual(MATRIX_REPORT.read_bytes(), text.encode("utf-8"))
        self.assertIn("| pass^k | +0.10 | +0.10 | +0.00 |", text)
        self.assertIn("`invalid_calls` 7 → 7", text)
        self.assertIn("不构成任何真实模型能力", text)
        self.assertIn("不承诺跨进程幂等", text)
        self.assertNotIn(str(ROOT), text, "报告里不得出现绝对路径")

    def test_missing_cell_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                guard_matrix.build_matrix(Path(d))

    def test_effects_from_archives_show_additivity(self):
        cells = guard_matrix.build_matrix()["cells"]
        passk = {
            name: cell["summary"]["passk"] for name, cell in cells.items()
        }
        effects = guard_matrix.effects_of(passk)
        self.assertAlmostEqual(effects["schema"], 0.10)
        self.assertAlmostEqual(effects["idempotency"], 0.10)
        self.assertEqual(effects["interaction"], 0.0)


if __name__ == "__main__":
    unittest.main()
