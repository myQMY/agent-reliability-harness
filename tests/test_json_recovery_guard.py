"""W3 第四条护栏 json_recovery 与「可达上限分析」的验收。

本文件钉死六件事：
1. ``json_recovery`` 覆盖四类注入故障里最后一条没有护栏覆盖的 ``bad_json``：
   **保留 W1 的 RETRY_HINT 证据**（grader 归因链路不变），只额外追加 guard
   span 与 ``[GUARD-REJECT]`` 消息触发重发；固有连续坏 JSON 仍失败，护栏不越界；
2. 新 A/B 与 OFF 组的测量输入逐字段相同，唯一自变量是 ``[guards].enabled``；
   既有 6 个 run_id、6 份归档报告、矩阵 / 稳健性 / tool_retry 扫描报告与
   冻结基线一字节未动；
3. **单 seed 会骗人**：``seed=11`` 单开是 0.10 → 0.10 的零效应，
   30-seed 分布也只有正 5 / 零 25 / 负 0；但四条护栏全开把 8 个 seed
   补到上限，30/30 等于「关掉注入故障 + 四条全开」；
4. **两个口径陷阱**：``fpm=0`` 不是天花板（0.20 < 0.30，因为幂等护栏还修了
   脚本固有的重复写）；剩余 7 个任务的 gap 是固有缺陷，不该由护栏去修；
5. 上限分析从证据自动推导不可恢复任务、不硬编码任务 ID，逐字节可复现；
6. 剩余两条路线表护栏在当前模拟器上没有可观测的测量输入，W3 护栏阶段收口。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from env import db
from eval import ab_sweep, analyze, ceiling, guard_matrix, robustness, runner, trace
from eval import config as exp_config
from eval import run as cli
from runtime import guard, loop
from runtime.model import (
    FAULT_BAD_JSON,
    BackendConfig,
    ModelReply,
    SEEDED_FAULTS,
    SeededBackend,
)

ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "tasks" / "mvp_10.jsonl"
EXPERIMENTS = ROOT / "experiments"
RUNS = EXPERIMENTS / "runs"
JR = guard.GUARD_JSON_RECOVERY

JR_CONFIG = "experiments/guard_ab_json_recovery.toml"
OFF_CONFIG = "experiments/guard_ab_off.toml"
OFF_RID = "w3_ab_off-346ab5e98382"
JR_RID = "w3_json_recovery_on-c507312a3e9a"

# (配置文件, 冻结 run_id)；新增护栏不得改变其中任何一个
EXISTING_RUNS = (
    ("experiments/config.toml", "baseline_scripted-a346e8acef15"),
    ("experiments/calibration.toml", "calibration_seeded-ea22719ba7c2"),
    (OFF_CONFIG, OFF_RID),
    ("experiments/guard_ab_on.toml", "w3_ab_on-fa17143eb761"),
    ("experiments/guard_ab_idem.toml", "w3_idem_on-a4e1925e1619"),
    ("experiments/guard_ab_both.toml", "w3_both_on-3c15f3bd94fd"),
    ("experiments/guard_ab_retry.toml", "w3_retry_on-5c178cab56a0"),
)

MEASURE_KEYS = (
    "tasks", "k", "backend", "max_steps", "max_json_retries",
    "seed", "fault_per_mille",
)

# 同一 30-seed 集上的实测结果；改 seed 集或改护栏都会让这里失败
EXPECTED_DIST = (5, 25, 0)
EXPECTED_MEAN = 5 * 0.1 / 30
EXPECTED_POSITIVE_SEEDS = {1, 7, 23, 88, 8192}
LIFTED_BY_ALL4 = {1, 3, 7, 23, 88, 99, 3001, 8192}
INTRINSIC_TASKS = {"T02", "T04", "T06", "T07", "T08", "T09", "T10"}

JR_EVIDENCE = EXPERIMENTS / ab_sweep.evidence_name(JR)
JR_REPORT = EXPERIMENTS / ab_sweep.report_name(JR)
CEILING_EVIDENCE = EXPERIMENTS / ceiling.EVIDENCE_NAME
CEILING_REPORT = EXPERIMENTS / ceiling.REPORT_NAME


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


def task_script(task_id: str) -> list:
    for task in runner.load_tasks(TASKS_PATH):
        if task["id"] == task_id:
            return list(task["script"])
    raise AssertionError(f"任务不存在：{task_id}")


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


class _LoopCase(unittest.TestCase):
    def _turn(self, backend, guards):
        task = next(t for t in runner.load_tasks(TASKS_PATH) if t["id"] == "T01")
        template = db.build_template(task["initial_db"])
        conn = db.reset(template)
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


class TestJsonRecoveryRules(_LoopCase):
    def test_guard_is_registered_last_without_reordering_predecessors(self):
        self.assertEqual(JR, "json_recovery")
        self.assertEqual(
            guard.GUARD_NAMES,
            (
                guard.GUARD_SCHEMA_VALIDATION,
                guard.GUARD_IDEMPOTENCY,
                guard.GUARD_TOOL_RETRY,
                JR,
            ),
        )

    def test_recoverable_reasons_match_parse_action_output(self):
        self.assertEqual(
            guard.RECOVERABLE_FORMAT_REASONS, ("invalid_json", "missing_fields")
        )
        self.assertEqual(loop.parse_action("{not-json")[1], "invalid_json")
        self.assertEqual(loop.parse_action('{"action": 1}')[1], "missing_fields")
        self.assertIn("invalid_json", guard.RECOVERABLE_FORMAT_REASONS)

    def test_hint_is_deterministic_prefixed_and_explains_the_skipped_action(self):
        hint = guard.json_recovery_hint("invalid_json")
        self.assertTrue(hint.startswith(guard.GUARD_REJECT_PREFIX))
        self.assertIn("invalid_json", hint)
        self.assertIn("还没有执行", hint)
        self.assertEqual(hint, guard.json_recovery_hint("invalid_json"))
        self.assertNotEqual(hint, guard.json_recovery_hint("missing_fields"))

    def test_four_injected_faults_have_four_guard_mechanisms(self):
        self.assertEqual(
            set(SEEDED_FAULTS),
            {"bad_json", "bad_args", "wrong_tool", "duplicate_call"},
        )
        self.assertEqual(len(guard.GUARD_NAMES), 4)
        self.assertEqual(
            set(guard.RETRYABLE_TOOL_ERRORS), {"wrong_tool", "bad_args"}
        )

    def test_config_accepts_json_recovery_and_still_rejects_unknown(self):
        cfg = exp_config.load_config(ROOT / JR_CONFIG)
        self.assertEqual(cfg.guards, (JR,))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.toml"
            path.write_text('[guards]\nenabled = ["json_recoveryy"]\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                exp_config.load_config(path)
        parsed = cli.build_parser().parse_args(["--guard", JR])
        self.assertEqual(parsed.guard, [JR])

    def test_all_existing_run_ids_stay_frozen(self):
        for rel, rid in EXISTING_RUNS:
            with self.subTest(config=rel):
                self.assertEqual(
                    exp_config.run_id(exp_config.load_config(ROOT / rel)), rid
                )
        self.assertEqual(
            exp_config.run_id(exp_config.load_config(ROOT / JR_CONFIG)), JR_RID
        )
        self.assertNotIn(JR_RID, {rid for _, rid in EXISTING_RUNS})


class TestLoopJsonRecovery(_LoopCase):
    def _seeded_bad_json(self):
        script = [
            {"tool": "get_order", "args": {"order_id": "O-100"}},
            {"final": "done"},
        ]
        return SeededBackend(
            BackendConfig(name="seeded", seed=11, fault_per_mille=0),
            script,
            task_id="T01",
            trial=1,
            force_fault=FAULT_BAD_JSON,
        )

    def test_recovery_replays_the_skipped_clean_action(self):
        result = self._turn(self._seeded_bad_json(), (JR,))
        tool_spans = [s for s in result.spans if s["role"] == "tool"]
        self.assertEqual(len(tool_spans), 1, "被跳过的 get_order 必须被重发执行")
        self.assertEqual(tool_spans[0]["tool"], "get_order")
        self.assertEqual(result.termination, loop.TERMINATION_FINAL)

    def test_guard_off_skips_the_clean_action(self):
        result = self._turn(self._seeded_bad_json(), ())
        self.assertFalse([s for s in result.spans if s["role"] == "tool"])
        self.assertEqual(result.termination, loop.TERMINATION_FINAL)

    def test_retry_hint_evidence_is_preserved_so_attribution_is_unchanged(self):
        for guards in ((), (JR,)):
            with self.subTest(guards=guards):
                result = self._turn(self._seeded_bad_json(), guards)
                user_spans = [
                    s for s in result.spans
                    if s["role"] == "user"
                    and str(s["observation"]).startswith(loop.RETRY_HINT)
                ]
                self.assertEqual(len(user_spans), 1)
                self.assertEqual(user_spans[0]["observation"],
                                 f"{loop.RETRY_HINT} (invalid_json)")

    def test_guard_span_is_added_only_when_enabled_and_last_message_is_reject(self):
        backend = SequencedBackend(["{not-json", '{"final": "done"}'])
        result = self._turn(backend, (JR,))
        guard_spans = [s for s in result.spans if s["role"] == "guard"]
        self.assertEqual(len(guard_spans), 1)
        self.assertTrue(
            guard_spans[0]["observation"].startswith(guard.GUARD_REJECT_PREFIX)
        )
        self.assertTrue(
            backend.seen[1][-1]["content"].startswith(guard.GUARD_REJECT_PREFIX)
        )
        off = self._turn(SequencedBackend(["{not-json", '{"final": "done"}']), ())
        self.assertFalse([s for s in off.spans if s["role"] == "guard"])

    def test_intrinsic_consecutive_bad_json_still_fails(self):
        """T06 的 3 个连续坏 JSON 没有干净动作可重发，护栏不越界。"""
        backend = SeededBackend(
            BackendConfig(name="seeded", seed=11, fault_per_mille=0),
            task_script("T06"),
            task_id="T06",
            trial=1,
        )
        result = self._turn(backend, (JR,))
        self.assertEqual(result.termination, loop.TERMINATION_JSON_FAILURE)
        self.assertLessEqual(
            len([s for s in result.spans if s["role"] == "guard"]),
            guard.MAX_GUARD_REJECTIONS,
        )

    def test_recovery_budget_converges_under_endless_bad_json(self):
        result = self._turn(SequencedBackend(["{not-json"]), (JR,))
        self.assertEqual(result.termination, loop.TERMINATION_JSON_FAILURE)
        self.assertLessEqual(
            len([s for s in result.spans if s["role"] == "guard"]),
            guard.MAX_GUARD_REJECTIONS,
        )

    def test_other_guards_do_not_touch_the_json_failure_branch(self):
        backend = SequencedBackend(["{not-json", '{"final": "done"}'])
        result = self._turn(
            backend,
            (
                guard.GUARD_SCHEMA_VALIDATION,
                guard.GUARD_IDEMPOTENCY,
                guard.GUARD_TOOL_RETRY,
            ),
        )
        self.assertFalse([s for s in result.spans if s["role"] == "guard"])


class TestSingleVariableContract(unittest.TestCase):
    def test_measurement_inputs_match_off_cell_exactly(self):
        off = exp_config.load_config(ROOT / OFF_CONFIG)
        on = exp_config.load_config(ROOT / JR_CONFIG)
        for key in MEASURE_KEYS:
            with self.subTest(field=key):
                self.assertEqual(getattr(off, key), getattr(on, key))
        self.assertEqual(off.guards, ())
        self.assertEqual(on.guards, (JR,))
        self.assertNotIn("guards", exp_config._core(off))
        self.assertEqual(exp_config._core(on)["guards"], [JR])

    def test_compare_points_to_the_off_archive(self):
        on = exp_config.load_config(ROOT / JR_CONFIG)
        self.assertEqual(
            on.compare, f"experiments/runs/{OFF_RID}/result_rows.jsonl"
        )
        self.assertTrue(exp_config.resolve_input(on.compare).is_file())

    def test_archived_report_and_snapshot_match_current_code(self):
        cfg = exp_config.load_config(ROOT / JR_CONFIG)
        rid = exp_config.run_id(cfg)
        rows = trace.read_jsonl(RUNS / rid / "result_rows.jsonl")
        compare_rows = trace.read_jsonl(exp_config.resolve_input(cfg.compare))
        expected = analyze.render_report(
            rows, runner.load_tasks(TASKS_PATH), cfg, rid, compare_rows,
            config_path=JR_CONFIG,
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
        # 既有只读汇总报告也必须仍与当前代码渲染一致
        self.assertEqual(
            (EXPERIMENTS / "w3_guard_matrix.md").read_bytes(),
            guard_matrix.render_matrix(guard_matrix.build_matrix()).encode("utf-8"),
        )
        self.assertEqual(
            (EXPERIMENTS / robustness.REPORT_NAME).read_bytes(),
            robustness.render_sweep(
                robustness.load_records(
                    (EXPERIMENTS / robustness.EVIDENCE_NAME).read_text(encoding="utf-8")
                )
            ).encode("utf-8"),
        )
        retry_records = robustness.load_records(
            (EXPERIMENTS / ab_sweep.evidence_name(guard.GUARD_TOOL_RETRY)).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            (EXPERIMENTS / ab_sweep.report_name(guard.GUARD_TOOL_RETRY)).read_bytes(),
            ab_sweep.render_sweep(retry_records, guard.GUARD_TOOL_RETRY).encode("utf-8"),
        )

    def test_existing_caveat_branches_are_unchanged(self):
        """四种既有护栏组合的 mechanism 措辞不能被新分支抢走。"""
        cases = [
            (OFF_CONFIG, None),
            ("experiments/guard_ab_on.toml", "下降不等于成功率提升"),
            ("experiments/guard_ab_idem.toml", "与成功率没有必然联系"),
            ("experiments/guard_ab_retry.toml", "工具层报错之后"),
            ("experiments/guard_ab_both.toml", "与成功率没有必然联系"),
        ]
        for rel, phrase in cases:
            with self.subTest(config=rel):
                text = "\n".join(analyze._caveats(exp_config.load_config(ROOT / rel)))
                if phrase is not None:
                    self.assertIn(phrase, text)
                self.assertNotIn("`json_recovery` 修的是", text)
        new_text = "\n".join(analyze._caveats(exp_config.load_config(ROOT / JR_CONFIG)))
        self.assertIn("`json_recovery` 修的是", new_text)

    def test_reproduce_command_carries_config_and_guard(self):
        report = (RUNS / JR_RID / "report.md").read_text(encoding="utf-8")
        self.assertIn(f"--config {JR_CONFIG}", report)
        self.assertIn(f"--guard {JR}", report)
        self.assertIn("与对照组对照（A/B）", report)


class TestSweepEvidence(unittest.TestCase):
    def _records(self):
        return robustness.load_records(JR_EVIDENCE.read_text(encoding="utf-8"))

    def test_seed_set_is_identical_to_robustness(self):
        records = self._records()
        self.assertEqual(len(records), 2 * len(robustness.DEFAULT_SEEDS))
        pairs = ab_sweep.per_seed_pairs(records)
        self.assertEqual(
            tuple(p["seed"] for p in pairs), robustness.DEFAULT_SEEDS
        )
        for record in records:
            self.assertEqual(record["treatment"], JR)

    def test_distribution_and_seed11_zero_effect(self):
        pairs = ab_sweep.per_seed_pairs(self._records())
        dist = ab_sweep.distribution([p["delta"] for p in pairs])
        self.assertEqual(
            (dist["positive"], dist["zero"], dist["negative"]), EXPECTED_DIST
        )
        self.assertAlmostEqual(dist["mean"], EXPECTED_MEAN)
        positive = {p["seed"] for p in pairs if p["delta"] > 1e-9}
        self.assertEqual(positive, EXPECTED_POSITIVE_SEEDS)
        # 单 seed 的 A/B 完全测不到这条护栏：留档那一格是 0.10 → 0.10
        headline = next(p for p in pairs if p["seed"] == 11)
        self.assertEqual(float(headline["control"]["passk"]), 0.10)
        self.assertEqual(float(headline["treatment"]["passk"]), 0.10)
        off_rows = trace.read_jsonl(RUNS / OFF_RID / "result_rows.jsonl")
        on_rows = trace.read_jsonl(RUNS / JR_RID / "result_rows.jsonl")
        self.assertEqual(analyze.summarize(off_rows, 3)["passk"], 0.10)
        self.assertEqual(analyze.summarize(on_rows, 3)["passk"], 0.10)

    def test_evidence_and_report_match_current_code(self):
        records = self._records()
        self.assertEqual(
            JR_EVIDENCE.read_bytes(),
            robustness.dumps_records(records).encode("utf-8"),
        )
        self.assertEqual(
            JR_REPORT.read_bytes(),
            ab_sweep.render_sweep(records, JR).encode("utf-8"),
        )
        text = JR_REPORT.read_text(encoding="utf-8")
        self.assertIn("可达上限分析", text)
        self.assertIn("bad_json", text)

    def test_tool_retry_report_still_renders_byte_identically(self):
        records = robustness.load_records(
            (EXPERIMENTS / ab_sweep.evidence_name(guard.GUARD_TOOL_RETRY)).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            (EXPERIMENTS / ab_sweep.report_name(guard.GUARD_TOOL_RETRY)).read_bytes(),
            ab_sweep.render_sweep(records, guard.GUARD_TOOL_RETRY).encode("utf-8"),
        )

    def test_from_jsonl_and_two_cli_runs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            artifacts = []
            for index in (1, 2):
                target = out / str(index)
                with redirect_stdout(io.StringIO()):
                    ab_sweep.main(
                        ["--seeds", "11,1,2", "--treatment", JR,
                         "--out-dir", str(target)]
                    )
                artifacts.append(
                    (
                        (target / ab_sweep.evidence_name(JR)).read_bytes(),
                        (target / ab_sweep.report_name(JR)).read_bytes(),
                    )
                )
            self.assertEqual(artifacts[0], artifacts[1])
            target = out / "readonly"
            target.mkdir()
            (target / ab_sweep.evidence_name(JR)).write_bytes(artifacts[0][0])
            with redirect_stdout(io.StringIO()):
                ab_sweep.main(
                    ["--from-jsonl", "--treatment", JR, "--out-dir", str(target)]
                )
            self.assertEqual(
                (target / ab_sweep.report_name(JR)).read_bytes(), artifacts[0][1]
            )


class TestCeilingEvidence(unittest.TestCase):
    def _records(self):
        return robustness.load_records(CEILING_EVIDENCE.read_text(encoding="utf-8"))

    def test_nine_cells_times_thirty_seeds(self):
        records = self._records()
        self.assertEqual(len(records), 9 * len(robustness.DEFAULT_SEEDS))
        self.assertEqual(
            {r["cell"] for r in records}, set(ceiling.CELL_NAMES)
        )
        self.assertEqual(
            {int(r["seed"]) for r in records}, set(robustness.DEFAULT_SEEDS)
        )
        with tempfile.TemporaryDirectory() as d:
            small = ceiling.collect((11, 1), tasks_path=TASKS_PATH)
        self.assertEqual(len(small), 18)
        self.assertEqual(small[0]["cell"], "off")
        self.assertEqual(small[1]["cell"], "schema")

    def test_all_four_guards_reach_the_attainable_ceiling(self):
        records = self._records()
        equal, total, mismatch = ceiling.equal_seed_count(
            records, "all4", "no_fault_all4"
        )
        self.assertEqual((equal, total, mismatch), (30, 30, []))
        self.assertAlmostEqual(ceiling.cell_summary(records, "all4")["passk_mean"], 0.30)
        self.assertAlmostEqual(
            ceiling.cell_summary(records, "all3")["passk_mean"], 0.27
        )
        all3_equal, all3_total, _ = ceiling.equal_seed_count(
            records, "all3", "no_fault_all4"
        )
        self.assertEqual((all3_equal, all3_total), (22, 30))
        lifted = ceiling.per_seed_delta(records, "all3", "all4")
        self.assertEqual(
            {seed for seed, delta in lifted if delta > 1e-9}, LIFTED_BY_ALL4
        )
        self.assertFalse([seed for seed, delta in lifted if delta < -1e-9])

    def test_fpm_zero_is_not_a_ceiling(self):
        records = self._records()
        no_fault = ceiling.cell_summary(records, "no_fault")["passk_mean"]
        all4 = ceiling.cell_summary(records, "all4")["passk_mean"]
        off = ceiling.cell_summary(records, "off")["passk_mean"]
        self.assertAlmostEqual(no_fault, 0.20)
        self.assertAlmostEqual(all4, 0.30)
        self.assertLess(no_fault, all4, "fpm=0 不是天花板：幂等护栏还修固有重复写")
        self.assertGreater(all4, off)
        # 诊断参照与 seed 无关（RNG 未被使用）
        for cell in ("no_fault", "no_fault_all4"):
            values = {
                float(r["passk"]) for r in ceiling.cell_records(records, cell)
            }
            self.assertEqual(len(values), 1, f"{cell} 不应随 seed 变化")

    def test_unrecoverable_tasks_are_derived_not_hardcoded(self):
        records = self._records()
        intrinsic = ceiling.unrecoverable_tasks(records)
        self.assertEqual({item["task_id"] for item in intrinsic}, INTRINSIC_TASKS)
        self.assertEqual(len(intrinsic), 7)
        known_failures = {
            "intent_misread", "wrong_tool", "bad_args", "policy_violation",
            "loop_or_timeout", "output_format", "unknown",
        }
        for item in intrinsic:
            self.assertIn(item["no_fault_failure"], known_failures)
            self.assertIn(item["all4_failure"], known_failures)
        # 能被护栏修好的三个任务不在清单里
        self.assertFalse(INTRINSIC_TASKS & {"T01", "T03", "T05"})
        source = (ROOT / "eval" / "ceiling.py").read_text(encoding="utf-8")
        self.assertNotIn('"T02"', source)
        self.assertNotIn('"T06"', source)

    def test_single_guard_positive_seeds_are_not_the_combination_gap(self):
        records = self._records()
        single = {
            seed
            for seed, delta in ceiling.per_seed_delta(records, "off", "json_recovery")
            if delta > 1e-9
        }
        lifted = {
            seed
            for seed, delta in ceiling.per_seed_delta(records, "all3", "all4")
            if delta > 1e-9
        }
        self.assertEqual(single, EXPECTED_POSITIVE_SEEDS)
        self.assertNotEqual(single, lifted)
        self.assertTrue(single <= lifted)

    def test_pure_functions_reject_missing_cells_and_zero_noise(self):
        records = self._records()[:1]
        with self.assertRaisesRegex(ValueError, "缺少格子"):
            ceiling.per_seed_delta(records, "off", "all4")
        with self.assertRaisesRegex(ValueError, "拒绝静默补零"):
            ceiling.cell_summary(records, "all4")
        counts = ceiling.sign_counts([(1, 0.1), (2, 0.0), (3, -0.1), (4, 1e-15)])
        self.assertEqual(
            (counts["positive"], counts["zero"], counts["negative"]), (1, 2, 1)
        )
        self.assertEqual(ceiling._signed(1e-15), "+0.00")

    def test_evidence_and_report_match_current_code(self):
        records = self._records()
        self.assertEqual(
            CEILING_EVIDENCE.read_bytes(),
            robustness.dumps_records(records).encode("utf-8"),
        )
        self.assertEqual(
            CEILING_REPORT.read_bytes(),
            ceiling.render_report(records).encode("utf-8"),
        )
        text = CEILING_REPORT.read_text(encoding="utf-8")
        for phrase in (
            "不是天花板", "剩余 gap 不该由护栏修", "30/30 个 seed",
            "上下文压缩", "token / 步数预算", "w3_ceiling.jsonl",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_from_jsonl_and_two_cli_runs_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            artifacts = []
            for index in (1, 2):
                target = out / str(index)
                with redirect_stdout(io.StringIO()):
                    ceiling.main(
                        ["--seeds", "11,1,2", "--out-dir", str(target)]
                    )
                artifacts.append(
                    (
                        (target / ceiling.EVIDENCE_NAME).read_bytes(),
                        (target / ceiling.REPORT_NAME).read_bytes(),
                    )
                )
            self.assertEqual(artifacts[0], artifacts[1])
            target = out / "readonly"
            target.mkdir()
            (target / ceiling.EVIDENCE_NAME).write_bytes(artifacts[0][0])
            with redirect_stdout(io.StringIO()):
                ceiling.main(["--from-jsonl", "--out-dir", str(target)])
            self.assertEqual(
                (target / ceiling.REPORT_NAME).read_bytes(), artifacts[0][1]
            )

    def test_module_is_clock_free_and_does_not_add_guards(self):
        source = (ROOT / "eval" / "ceiling.py").read_text(encoding="utf-8")
        for banned in (
            "import time", "time.time", "import random", "random.",
            "os.environ", "uuid", "datetime",
        ):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)
        for phrase in ("no_fault_all4", "unrecoverable_tasks", "fault_per_mille"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)
        guard_src = (ROOT / "runtime" / "guard.py").read_text(encoding="utf-8")
        loop_src = (ROOT / "runtime" / "loop.py").read_text(encoding="utf-8")
        for phrase in ("GUARD_JSON_RECOVERY", "json_recovery_hint",
                       "RECOVERABLE_FORMAT_REASONS"):
            self.assertIn(phrase, guard_src)
            self.assertIn(phrase, loop_src)


class TestDocumentation(unittest.TestCase):
    def test_readme_states_ceiling_and_closure(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for phrase in (
            "json_recovery", "可达上限分析", "w3_ceiling.md",
            "fpm=0 不是天花板", "W3 护栏阶段收口",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_tech_stack_states_why_remaining_guards_are_not_measurable(self):
        text = (ROOT / "docs" / "tech-stack.md").read_text(encoding="utf-8")
        for phrase in (
            "json_recovery", "可达上限分析", "上下文压缩",
            "token / 步数预算", "真实上下文窗口",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
