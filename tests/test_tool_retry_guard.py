"""W3 第三条护栏 tool_retry 与通用 A/B 多种子扫描的验收。

本文件钉死五件事：
1. ``tool_retry`` 工作在**工具报错之后**：如实保留 tool span / errors，
   只追加 guard span 与拒绝消息；成功的调用、非重试类错误都不触发，
   重试受共享预算约束能收敛；
2. 四类人为缺陷与运行时机制一一对应，只有带 ``_pending_correct`` 的
   可恢复注入故障能被修好；T07 / T08 的脚本固有缺陷产生 guard span 但
   任务仍失败，护栏没有越界；
3. 新 A/B 与既有 OFF 组的测量输入逐字段相同，唯一自变量是
   ``[guards].enabled``；既有 6 个 run_id / 5 份归档报告 / 矩阵报告 /
   稳健性证据与冻结基线逐字节不变；
4. 30-seed 符号分布（正 9 / 零 21 / 负 0）与留档证据逐字节一致，
   ``--from-jsonl`` 与 CLI 连跑两次都可复现；
5. 正效应 seed 集合与 ``schema_validation`` 不是包含关系——
   作用位置不同，不能推断「多覆盖一类错误就必然更优」。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from env import db, tools
from eval import ab_sweep, analyze, guard_matrix, robustness, runner, trace
from eval import config as exp_config
from eval import run as cli
from runtime import guard, loop
from runtime.model import BackendConfig, ModelReply

ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "tasks" / "mvp_10.jsonl"
RUNS = ROOT / "experiments" / "runs"
RETRY = guard.GUARD_TOOL_RETRY

RETRY_CONFIG = "experiments/guard_ab_retry.toml"
OFF_CONFIG = "experiments/guard_ab_off.toml"
OFF_RID = "w3_ab_off-346ab5e98382"
RETRY_RID = "w3_retry_on-5c178cab56a0"

# (配置文件, 冻结 run_id)；新增护栏不得改变其中任何一个
EXISTING_RUNS = (
    ("experiments/config.toml", "baseline_scripted-a346e8acef15"),
    ("experiments/calibration.toml", "calibration_seeded-ea22719ba7c2"),
    (OFF_CONFIG, OFF_RID),
    ("experiments/guard_ab_on.toml", "w3_ab_on-fa17143eb761"),
    ("experiments/guard_ab_idem.toml", "w3_idem_on-a4e1925e1619"),
    ("experiments/guard_ab_both.toml", "w3_both_on-3c15f3bd94fd"),
)

EVIDENCE = ROOT / "experiments" / ab_sweep.evidence_name(RETRY)
REPORT = ROOT / "experiments" / ab_sweep.report_name(RETRY)
SWEEP_MODULE = ROOT / "eval" / "ab_sweep.py"

# 进入测量链路的字段；name / compare 只影响归档位置与报告展示
MEASURE_KEYS = (
    "tasks", "k", "backend", "max_steps", "max_json_retries",
    "seed", "fault_per_mille",
)

# 同一 30-seed 集上的实测结果；改 seed 集或改护栏都会让这里失败
EXPECTED_DIST = (9, 21, 0)
EXPECTED_MEAN = 0.03
SCHEMA_POSITIVE_SEEDS = {3, 11, 101, 2026, 9001}
RETRY_POSITIVE_SEEDS = {3, 5, 11, 13, 101, 2026, 2048, 4096, 12345}


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


def action_text(action: dict) -> str:
    return json.dumps(action, ensure_ascii=False, sort_keys=True)


BAD_TOOL = {"tool": "__missing_tool__", "args": {"order_id": "O-100"}}
BAD_ARGS = {"tool": "refund_order", "args": {}}
GOOD_READ = {"tool": "get_order", "args": {"order_id": "O-100"}}
FINAL = {"final": "done"}


class TestRetryRules(unittest.TestCase):
    def test_guard_names_are_ordered_and_whitelisted(self):
        # 前三条的顺序是既有留档的判定顺序，必须钉死；新护栏只允许追加在末尾。
        self.assertEqual(
            guard.GUARD_NAMES[:3],
            (
                guard.GUARD_SCHEMA_VALIDATION,
                guard.GUARD_IDEMPOTENCY,
                RETRY,
            ),
        )
        self.assertEqual(RETRY, "tool_retry")
        self.assertIn(guard.GUARD_JSON_RECOVERY, guard.GUARD_NAMES)

    def test_retryable_errors_are_exactly_the_validation_failures(self):
        self.assertEqual(
            set(guard.RETRYABLE_TOOL_ERRORS),
            {tools.WRONG_TOOL, tools.BAD_ARGS},
        )

    def test_retry_message_is_deterministic_and_explains_no_state_written(self):
        msg = guard.tool_retry_message("refund_order", tools.BAD_ARGS)
        self.assertTrue(msg.startswith(guard.GUARD_REJECT_PREFIX))
        self.assertIn("refund_order", msg)
        self.assertIn(tools.BAD_ARGS, msg)
        self.assertIn("没有写入任何状态", msg)
        self.assertEqual(msg, guard.tool_retry_message("refund_order", tools.BAD_ARGS))
        self.assertNotEqual(msg, guard.tool_retry_message("cancel_order", tools.BAD_ARGS))

    def test_config_accepts_retry_and_still_rejects_unknown_or_duplicate(self):
        cfg = exp_config.load_config(ROOT / RETRY_CONFIG)
        self.assertEqual(cfg.guards, (RETRY,))
        for name in ("teleport", "tool_retryy"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                path = Path(d) / "bad.toml"
                path.write_text(
                    f'[guards]\nenabled = ["{name}"]\n', encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    exp_config.load_config(path)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "dup.toml"
            path.write_text(
                f'[guards]\nenabled = ["{RETRY}", "{RETRY}"]\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                exp_config.load_config(path)

    def test_all_existing_run_ids_stay_frozen(self):
        for rel, rid in EXISTING_RUNS:
            with self.subTest(config=rel):
                self.assertEqual(
                    exp_config.run_id(exp_config.load_config(ROOT / rel)), rid
                )
        self.assertEqual(
            exp_config.run_id(exp_config.load_config(ROOT / RETRY_CONFIG)),
            RETRY_RID,
        )
        self.assertNotIn(RETRY_RID, {rid for _, rid in EXISTING_RUNS})


class TestLoopRecovery(unittest.TestCase):
    def _conn(self):
        task = next(t for t in runner.load_tasks(TASKS_PATH) if t["id"] == "T01")
        template = db.build_template(task["initial_db"])
        return template, db.reset(template)

    def _turn(self, backend, guards):
        template, conn = self._conn()
        try:
            return loop.run_turn(
                [],
                backend,
                conn,
                trial_id="T01-t1",
                counter=loop.StepCounter(),
                budget=guard.StepBudget(),
                guards=guards,
            )
        finally:
            conn.close()
            template.close()

    def test_failed_call_keeps_error_evidence_and_appends_guard_span(self):
        backend = SequencedBackend(
            [action_text(BAD_TOOL), action_text(GOOD_READ), action_text(FINAL)]
        )
        result = self._turn(backend, (RETRY,))
        guard_spans = [s for s in result.spans if s["role"] == "guard"]
        tool_spans = [s for s in result.spans if s["role"] == "tool"]
        self.assertEqual(len(guard_spans), 1)
        self.assertEqual(len(tool_spans), 2, "报错与重发的 tool span 都要保留")
        self.assertEqual(result.errors, [tools.WRONG_TOOL], "错误证据不得被擦掉")
        self.assertTrue(
            guard_spans[0]["observation"].startswith(guard.GUARD_REJECT_PREFIX)
        )
        # 第二次 generate 看到的是「报错的 tool 观察 + 拒绝消息」完整证据链
        seen = backend.seen[1]
        self.assertEqual(seen[-1]["role"], "user")
        self.assertTrue(
            seen[-1]["content"].startswith(guard.GUARD_REJECT_PREFIX)
        )
        self.assertIn("tool", [m["role"] for m in seen])

    def test_guard_off_has_no_guard_span_but_same_error_evidence(self):
        backend = SequencedBackend(
            [action_text(BAD_TOOL), action_text(GOOD_READ), action_text(FINAL)]
        )
        result = self._turn(backend, ())
        self.assertFalse([s for s in result.spans if s["role"] == "guard"])
        self.assertEqual(result.errors, [tools.WRONG_TOOL])
        self.assertFalse(
            any(
                m["content"].startswith(guard.GUARD_REJECT_PREFIX)
                for m in backend.seen[1]
            )
        )

    def test_successful_call_never_retries(self):
        backend = SequencedBackend([action_text(GOOD_READ), action_text(FINAL)])
        result = self._turn(backend, (RETRY,))
        self.assertFalse([s for s in result.spans if s["role"] == "guard"])
        self.assertEqual(result.errors, [])

    def test_both_retryable_error_kinds_trigger_recovery(self):
        for name, action, error in (
            ("wrong_tool", BAD_TOOL, tools.WRONG_TOOL),
            ("bad_args", BAD_ARGS, tools.BAD_ARGS),
        ):
            with self.subTest(kind=name):
                backend = SequencedBackend(
                    [action_text(action), action_text(GOOD_READ), action_text(FINAL)]
                )
                result = self._turn(backend, (RETRY,))
                self.assertEqual(result.errors, [error])
                self.assertEqual(
                    len([s for s in result.spans if s["role"] == "guard"]), 1
                )

    def test_retry_budget_converges_under_endless_failures(self):
        backend = SequencedBackend([action_text(BAD_TOOL)])
        result = self._turn(backend, (RETRY,))
        self.assertEqual(
            len([s for s in result.spans if s["role"] == "guard"]),
            guard.MAX_GUARD_REJECTIONS,
        )
        self.assertEqual(result.termination, loop.TERMINATION_MAX_STEPS)


class TestRealRuns(unittest.TestCase):
    def test_only_recoverable_injected_fault_flips_and_intrinsic_defects_survive(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            off = {(r["task_id"], r["trial"]): r for r in run_rows((), p / "off")}
            on = {
                (r["task_id"], r["trial"]): r
                for r in run_rows((RETRY,), p / "on")
            }
        flips = [
            key for key in off if off[key]["passed"] != on[key]["passed"]
        ]
        self.assertEqual(flips, [("T01", 2)])
        self.assertEqual(off[("T01", 2)]["passed"], 0)
        self.assertEqual(on[("T01", 2)]["passed"], 1)
        # 脚本固有缺陷：错工具 / 缺参数，护栏没有越界去修
        for task_id, failure in (("T07", "wrong_tool"), ("T08", "bad_args")):
            with self.subTest(task=task_id):
                for trial in (1, 2, 3):
                    self.assertEqual(off[(task_id, trial)]["passed"], 0)
                    self.assertEqual(on[(task_id, trial)]["passed"], 0)
                self.assertEqual(on[(task_id, 1)]["failure_type"], failure)

    def test_invalid_calls_are_not_erased_by_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            off = analyze.summarize(run_rows((), p / "off"), 3)
            on = analyze.summarize(run_rows((RETRY,), p / "on"), 3)
        self.assertEqual(off["invalid_calls"], 7)
        self.assertEqual(on["invalid_calls"], 7)
        self.assertGreater(on["passk"], off["passk"])
        self.assertEqual(on["passk"], 0.20)

    def test_guard_spans_only_follow_tool_errors(self):
        """bad_json 不进 tools.call、duplicate_call 不报错，都不该触发重试。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "on"
            run_rows((RETRY,), p)
            spans_by_file = {
                f.stem: trace.read_jsonl(f)
                for f in sorted((p / "traces").glob("*.jsonl"))
            }
        for spans in spans_by_file.values():
            for span in spans:
                if span["role"] == "guard":
                    preceding = [
                        s for s in spans
                        if s["step"] < span["step"] and s["role"] == "tool"
                    ]
                    self.assertTrue(preceding, "guard span 前必须有工具调用")
                    self.assertIn(
                        preceding[-1]["observation"].get("error"),
                        guard.RETRYABLE_TOOL_ERRORS,
                    )


class TestSingleVariableContract(unittest.TestCase):
    def test_measurement_inputs_match_off_cell_exactly(self):
        off = exp_config.load_config(ROOT / OFF_CONFIG)
        on = exp_config.load_config(ROOT / RETRY_CONFIG)
        for key in MEASURE_KEYS:
            with self.subTest(field=key):
                self.assertEqual(getattr(off, key), getattr(on, key))
        self.assertEqual(off.guards, ())
        self.assertEqual(on.guards, (RETRY,))
        self.assertNotIn("guards", exp_config._core(off))
        self.assertEqual(exp_config._core(on)["guards"], [RETRY])

    def test_compare_points_to_the_off_archive(self):
        on = exp_config.load_config(ROOT / RETRY_CONFIG)
        self.assertEqual(
            on.compare,
            f"experiments/runs/{OFF_RID}/result_rows.jsonl",
        )
        self.assertTrue(exp_config.resolve_input(on.compare).is_file())

    def test_archived_report_and_snapshot_match_current_code(self):
        cfg = exp_config.load_config(ROOT / RETRY_CONFIG)
        rid = exp_config.run_id(cfg)
        rows = trace.read_jsonl(RUNS / rid / "result_rows.jsonl")
        compare_rows = trace.read_jsonl(exp_config.resolve_input(cfg.compare))
        expected = analyze.render_report(
            rows,
            runner.load_tasks(TASKS_PATH),
            cfg,
            rid,
            compare_rows,
            config_path=RETRY_CONFIG,
        )
        self.assertEqual(
            (RUNS / rid / "report.md").read_bytes(), expected.encode("utf-8")
        )
        snapshot = trace.dumps(exp_config.config_snapshot(cfg, rid)) + "\n"
        self.assertEqual(
            (RUNS / rid / "config.json").read_bytes(), snapshot.encode("utf-8")
        )

    def test_existing_archives_are_byte_identical_after_the_change(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            for rel, rid in EXISTING_RUNS:
                if rel == "experiments/calibration.toml":
                    continue  # 校准配置没有留档 report
                with self.subTest(config=rel):
                    tag = rel.replace("/", "_")
                    argv = [
                        "--config", rel,
                        "--results-dir", str(tmp / tag / "results"),
                        "--traces-dir", str(tmp / tag / "traces"),
                        "--runs-dir", str(tmp / "runs"),
                    ]
                    with redirect_stdout(io.StringIO()):
                        cli.main(argv)
                    self.assertEqual(
                        (RUNS / rid / "report.md").read_bytes(),
                        (tmp / "runs" / rid / "report.md").read_bytes(),
                    )
        # 两份只读汇总报告也必须仍与当前代码渲染一致
        self.assertEqual(
            (ROOT / "experiments" / "w3_guard_matrix.md").read_bytes(),
            guard_matrix.render_matrix(guard_matrix.build_matrix()).encode("utf-8"),
        )
        self.assertEqual(
            (ROOT / "experiments" / robustness.REPORT_NAME).read_bytes(),
            robustness.render_sweep(
                robustness.load_records(
                    (ROOT / "experiments" / robustness.EVIDENCE_NAME).read_text(
                        encoding="utf-8"
                    )
                )
            ).encode("utf-8"),
        )

    def test_reproduce_command_carries_config_and_guard(self):
        report = (RUNS / RETRY_RID / "report.md").read_text(encoding="utf-8")
        self.assertIn(f"--config {RETRY_CONFIG}", report)
        self.assertIn(f"--guard {RETRY}", report)
        self.assertIn("与对照组对照（A/B）", report)


class TestAbSweep(unittest.TestCase):
    def _records(self):
        return robustness.load_records(EVIDENCE.read_text(encoding="utf-8"))

    def test_collect_small_sample_shape(self):
        with tempfile.TemporaryDirectory() as d:
            records = ab_sweep.collect(
                (11, 1),
                treatment=RETRY,
                tasks_path=TASKS_PATH,
            )
        self.assertEqual(len(records), 4)
        self.assertEqual(
            [(r["seed"], r["cell"]) for r in records],
            [(11, "control"), (11, "treatment"), (1, "control"), (1, "treatment")],
        )
        for record in records:
            self.assertEqual(record["treatment"], RETRY)
            self.assertEqual(
                record["guards"], [] if record["cell"] == "control" else [RETRY]
            )
            for key in ("pass1", "passk", "invalid_calls", "total_steps",
                        "failure_counts", "task_count", "passk_hits"):
                self.assertIn(key, record)

    def test_missing_cell_raises_instead_of_zero_filling(self):
        records = self._records()[:1]
        with self.assertRaisesRegex(ValueError, "缺少格子"):
            ab_sweep.per_seed_pairs(records)

    def test_distribution_is_pure_and_zeros_noise(self):
        dist = ab_sweep.distribution([0.1, 0.0, -0.1, 1e-15, 0.2])
        self.assertEqual((dist["positive"], dist["zero"], dist["negative"]), (2, 2, 1))
        self.assertAlmostEqual(dist["mean"], 0.2 / 5)
        self.assertEqual(ab_sweep._signed(1e-15), "+0.00")

    def test_evidence_and_report_match_current_code(self):
        records = self._records()
        self.assertEqual(
            EVIDENCE.read_bytes(),
            robustness.dumps_records(records).encode("utf-8"),
        )
        self.assertEqual(
            REPORT.read_bytes(),
            ab_sweep.render_sweep(records, RETRY).encode("utf-8"),
        )

    def test_from_jsonl_and_two_cli_runs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            artifacts = []
            for index in (1, 2):
                target = out / str(index)
                with redirect_stdout(io.StringIO()):
                    ab_sweep.main(
                        ["--seeds", "11,1,2", "--out-dir", str(target)]
                    )
                artifacts.append(
                    (
                        (target / ab_sweep.evidence_name(RETRY)).read_bytes(),
                        (target / ab_sweep.report_name(RETRY)).read_bytes(),
                    )
                )
            self.assertEqual(artifacts[0], artifacts[1])
            target = out / "readonly"
            target.mkdir()
            (target / ab_sweep.evidence_name(RETRY)).write_bytes(artifacts[0][0])
            with redirect_stdout(io.StringIO()):
                ab_sweep.main(["--from-jsonl", "--out-dir", str(target)])
            self.assertEqual(
                (target / ab_sweep.report_name(RETRY)).read_bytes(),
                artifacts[0][1],
            )

    def test_full_sweep_distribution_and_seed11_match_archive(self):
        records = self._records()
        pairs = ab_sweep.per_seed_pairs(records)
        dist = ab_sweep.distribution([p["delta"] for p in pairs])
        self.assertEqual(len(pairs), len(robustness.DEFAULT_SEEDS))
        self.assertEqual(
            (dist["positive"], dist["zero"], dist["negative"]), EXPECTED_DIST
        )
        self.assertAlmostEqual(dist["mean"], EXPECTED_MEAN)
        self.assertEqual(dist["min"], 0.0)
        self.assertEqual(dist["max"], 0.1)
        headline = next(p for p in pairs if p["seed"] == 11)
        self.assertEqual(float(headline["control"]["passk"]), 0.10)
        self.assertEqual(float(headline["treatment"]["passk"]), 0.20)
        off_rows = trace.read_jsonl(RUNS / OFF_RID / "result_rows.jsonl")
        on_rows = trace.read_jsonl(RUNS / RETRY_RID / "result_rows.jsonl")
        self.assertEqual(analyze.summarize(off_rows, 3)["passk"], 0.10)
        self.assertEqual(analyze.summarize(on_rows, 3)["passk"], 0.20)

    def test_effect_seed_sets_are_not_nested(self):
        """作用位置不同：多覆盖一类错误不等于必然更优。"""
        rob = robustness.load_records(
            (ROOT / "experiments" / robustness.EVIDENCE_NAME).read_text(
                encoding="utf-8"
            )
        )
        schema_pos = {
            row["seed"]
            for row in robustness.per_seed_effects(rob)
            if row["effects"]["schema"] > 1e-9
        }
        retry_pos = {
            pair["seed"]
            for pair in ab_sweep.per_seed_pairs(self._records())
            if pair["delta"] > 1e-9
        }
        self.assertEqual(schema_pos, SCHEMA_POSITIVE_SEEDS)
        self.assertEqual(retry_pos, RETRY_POSITIVE_SEEDS)
        self.assertFalse(schema_pos <= retry_pos)
        self.assertEqual(retry_pos - schema_pos, {5, 13, 2048, 4096, 12345})
        self.assertEqual(schema_pos - retry_pos, {9001})

    def test_module_is_clock_free_and_anchors_exist(self):
        source = SWEEP_MODULE.read_text(encoding="utf-8")
        for banned in (
            "import time", "time.time", "import random", "random.",
            "os.environ", "uuid", "datetime",
        ):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)
        guard_src = (ROOT / "runtime" / "guard.py").read_text(encoding="utf-8")
        loop_src = (ROOT / "runtime" / "loop.py").read_text(encoding="utf-8")
        for phrase in (
            "GUARD_TOOL_RETRY", "RETRYABLE_TOOL_ERRORS", "tool_retry_message",
            "hashlib.sha256",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guard_src)
        for phrase in ("RETRYABLE_TOOL_ERRORS", "GUARD_TOOL_RETRY"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, loop_src)
        self.assertIn("下降不等于成功率提升", (ROOT / "eval" / "analyze.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
