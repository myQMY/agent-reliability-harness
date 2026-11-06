"""W4-c 装置守门测试：只读留档接得上、绝不回灌、既有产物一字节不动。

W4-b 剩下的开放项是「被替换掉的干净动作没留档，无法验证重发的是不是原本
那一个动作」。W4-c 只补这份证据，本文件把三件事钉死：

1. 留档内容确实来自**注入前**的模型输出，且拒绝后的响应仍是底层后端的一次真实调用；
2. ``clean_text`` 绝不进发往模型的 payload / trace 的被拒位置——否则
   「重发同一个动作」又变成 harness 代答；
3. 带不带 ``--clean-actions-dir``，既有 trace 8 字段、结果行、报告与
   全部既有 ``run_id`` 逐字节不变。

判定尺子另由离线校准（两路确定性夹具 + fpm=0 基准）与纯函数用例守门。
所有测试零网络、零真实模型调用。
"""
from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from eval import config as exp_config
from eval import guard_probe, runner, trace, w4_preflight, w4c_probe
from eval import run as cli
from runtime import guard, loop
from runtime.model import (
    SEEDED_FAULTS,
    BackendConfig,
    ModelReply,
    OpenAIBackend,
    OutputFaultBackend,
    build_backend,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS = REPO_ROOT / "tasks" / "mvp_10.jsonl"
EXPERIMENTS = REPO_ROOT / "experiments"

OFF_RID = "w4c_off_flash-b891da858ccb"
ON_RID = "w4c_all4_flash-b38a0c5106d6"
EXISTING_RUN_IDS = {
    "w4_off_flash.toml": "w4_off_flash-61491decf48c",
    "w4_all4_flash.toml": "w4_all4_flash-f6a4ea1674dc",
    "w4b_off_flash.toml": "w4b_off_flash-f6e556cd7201",
    "w4b_all4_flash.toml": "w4b_all4_flash-6d7222dc87af",
    "w4c_off_flash.toml": OFF_RID,
    "w4c_all4_flash.toml": ON_RID,
}

CLEAN_ACTION = json.dumps(
    {"tool": "get_order", "args": {"order_id": 1001}},
    ensure_ascii=False,
    sort_keys=True,
)
DIFFERENT_ACTION = json.dumps(
    {"tool": "get_order", "args": {"order_id": 2002}},
    ensure_ascii=False,
    sort_keys=True,
)
FINAL_ACTION = json.dumps({"final": "已处理"}, ensure_ascii=False, sort_keys=True)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self) -> bytes:
        return self._body


class _Opener:
    """按序返回模型文本；用尽后重复最后一项；记录发往模型的 HTTP payload。"""

    def __init__(self, texts):
        self._texts = list(texts)
        self._index = 0
        self.requests: list[bytes] = []

    def __call__(self, request):
        self.requests.append(request.data)
        text = self._texts[min(self._index, len(self._texts) - 1)]
        self._index += 1
        body = json.dumps(
            {
                "choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return _FakeResponse(body)


class _FixedBackend:
    """固定返回一段干净动作，并数调用次数；用来证明「拒绝后真的再调一次」。"""

    def __init__(self, text: str = CLEAN_ACTION):
        self.text = text
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        return ModelReply(
            text=self.text, prompt_tokens=100, completion_tokens=20, latency_ms=1
        )


def _faulted(inner=None, **kwargs) -> OutputFaultBackend:
    options = dict(seed=11, fault_per_mille=1000, task_id="T01", trial=1)
    options.update(kwargs)
    return OutputFaultBackend(inner or _FixedBackend(), **options)


def _load(name: str) -> exp_config.ExperimentConfig:
    return exp_config.load_config(EXPERIMENTS / name)


class TestCleanActionRecords(unittest.TestCase):
    """留档内容必须精确来自注入前的那一次输出。"""

    def test_record_fields_are_exact(self):
        backend = _faulted()
        reply = backend.generate([{"role": "user", "content": "hi"}])
        self.assertEqual(len(backend.clean_actions), 1)
        record = backend.clean_actions[0]
        self.assertEqual(record["clean_text"], CLEAN_ACTION)
        self.assertEqual(record["injected_text"], reply.text)
        self.assertNotEqual(reply.text, CLEAN_ACTION)
        self.assertEqual(record["cursor"], 0)
        self.assertIn(record["fault_kind"], SEEDED_FAULTS)
        self.assertEqual(record["task_id"], "T01")
        self.assertEqual(record["trial"], 1)
        self.assertEqual(record["trial_id"], "T01-t1")
        # 只替换 text，token / latency 观测原样保留
        self.assertEqual(reply.prompt_tokens, 100)
        self.assertEqual(reply.latency_ms, 1)

    def test_trial_id_matches_runner_and_is_empty_without_task_id(self):
        self.assertEqual(_faulted().trial_id, "T01-t1")
        self.assertEqual(_faulted(task_id="").trial_id, "")

    def test_no_record_when_no_fault_was_injected(self):
        backend = _faulted(fault_per_mille=0)
        reply = backend.generate([{"role": "user", "content": "hi"}])
        self.assertEqual(reply.text, CLEAN_ACTION)
        self.assertEqual(backend.clean_actions, [])
        self.assertEqual(backend.injected, [])

    def test_final_and_bad_model_output_are_not_injected(self):
        for text in (FINAL_ACTION, "{not-json"):
            backend = _faulted(_FixedBackend(text))
            self.assertEqual(backend.generate([]).text, text)
            self.assertEqual(backend.clean_actions, [])

    def test_rejection_response_is_a_real_call_and_not_a_new_record(self):
        inner = _FixedBackend()
        backend = _faulted(inner)
        backend.generate([{"role": "user", "content": "hi"}])
        before = list(backend.clean_actions)
        reply = backend.generate(
            [
                {"role": "user", "content": "hi"},
                {"role": "user", "content": guard.GUARD_REJECT_PREFIX + " 请重发"},
            ]
        )
        self.assertEqual(reply.text, CLEAN_ACTION)  # 底层后端当次真实输出
        self.assertEqual(inner.calls, 2)
        self.assertEqual(backend.clean_actions, before)
        self.assertEqual(len(backend.injected), 1)

    def test_fpm_zero_does_not_wrap_openai_backend(self):
        config = BackendConfig(
            name="openai", base_url="https://fake", api_key="FAKE",
            model="fake", seed=11, fault_per_mille=0,
        )
        backend = build_backend(config, ())
        self.assertNotIsInstance(backend, OutputFaultBackend)
        self.assertIsInstance(backend, OpenAIBackend)
        self.assertFalse(hasattr(backend, "clean_actions"))

    def test_injection_recorder_is_built_for_nonzero_fpm(self):
        config = BackendConfig(
            name="openai", base_url="https://fake", api_key="FAKE",
            model="fake", seed=11, fault_per_mille=300,
            system_prompt="ctx",
        )
        backend = build_backend(config, (), task_id="T01", trial=2)
        self.assertIsInstance(backend, OutputFaultBackend)
        self.assertEqual(backend.trial_id, "T01-t2")


class TestNoAnswerFeeding(unittest.TestCase):
    """防代答红线：运行时绝不把留档回灌给模型。"""

    def test_generate_returns_injected_text_not_clean_text(self):
        backend = _faulted()
        reply = backend.generate([])
        self.assertEqual(reply.text, backend.clean_actions[0]["injected_text"])
        self.assertNotEqual(reply.text, backend.clean_actions[0]["clean_text"])

    def test_recording_does_not_mutate_messages(self):
        backend = _faulted()
        messages = [{"role": "user", "content": "hi"}]
        snapshot = [dict(message) for message in messages]
        backend.generate(messages)
        self.assertEqual(messages, snapshot)

    def test_fake_opener_payload_never_contains_the_replaced_clean_action(self):
        opener = _Opener([CLEAN_ACTION, DIFFERENT_ACTION])
        real = OpenAIBackend("https://fake", "FAKE", "fake", opener=opener)
        backend = OutputFaultBackend(
            real, seed=11, fault_per_mille=1000, task_id="T01", trial=1
        )
        first = backend.generate([{"role": "user", "content": "hi"}])
        self.assertEqual(backend.clean_actions[0]["clean_text"], CLEAN_ACTION)
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": first.text},
            {"role": "user", "content": guard.GUARD_REJECT_PREFIX + " 请重发"},
        ]
        second = backend.generate(messages)
        self.assertEqual(second.text, DIFFERENT_ACTION)
        self.assertEqual(len(opener.requests), 2)
        # 第二次请求的 payload 里带的是坏文本 + 拒绝消息，不是被拿走的干净动作。
        for raw in opener.requests:
            payload = json.loads(raw.decode("utf-8"))
            self.assertNotIn(
                CLEAN_ACTION,
                json.dumps(payload["messages"], ensure_ascii=False),
            )

    def test_side_car_is_post_hoc_only_and_backend_never_reads_it_to_respond(self):
        # 清空 / 篡改留档都不该改变后续 generate 的行为：响应只来自底层后端。
        inner = _FixedBackend()
        backend = _faulted(inner)
        backend.generate([])
        backend.clean_actions.clear()
        reply = backend.generate(
            [{"role": "user", "content": guard.GUARD_REJECT_PREFIX + " 请重发"}]
        )
        self.assertEqual(reply.text, inner.text)
        self.assertEqual(inner.calls, 2)


class TestExistingArtifactsUntouched(unittest.TestCase):
    """「测量能力升级不得偷偷改变测量结果」在 W4-c 的代码级证明。"""

    @staticmethod
    def _run_once(base: Path, tag: str, clean_dir):
        opener = _Opener([CLEAN_ACTION])
        config = BackendConfig(
            name="openai", base_url="https://fake", api_key="FAKE",
            model="fake", seed=11, fault_per_mille=1000,
        )
        with patch("runtime.model.urllib.request.urlopen", opener), patch(
            "runtime.model.time.perf_counter", return_value=0.0
        ):
            runner.run(
                runner.RunConfig(
                    tasks_path=TASKS,
                    k=1,
                    backend=config,
                    results_dir=base / tag / "results",
                    traces_dir=base / tag / "traces",
                    guards=guard.GUARD_NAMES,
                    clean_actions_dir=clean_dir,
                )
            )
        return opener

    def test_rows_and_traces_are_byte_identical_with_or_without_side_car(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._run_once(base, "without", None)
            self._run_once(base, "with", base / "with" / "clean")
            self.assertEqual(
                (base / "without" / "results" / "result_rows.jsonl").read_bytes(),
                (base / "with" / "results" / "result_rows.jsonl").read_bytes(),
            )
            a_traces = sorted((base / "without" / "traces").glob("*.jsonl"))
            b_traces = sorted((base / "with" / "traces").glob("*.jsonl"))
            self.assertEqual([p.name for p in a_traces], [p.name for p in b_traces])
            self.assertTrue(a_traces)
            for left, right in zip(a_traces, b_traces):
                self.assertEqual(left.read_bytes(), right.read_bytes(), left.name)
            self.assertFalse((base / "without" / "clean").exists())

    def test_side_car_records_injected_text_visible_in_trace_but_not_clean_text(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._run_once(base, "grp", base / "grp" / "clean")
            clean_files = sorted((base / "grp" / "clean").glob("*.jsonl"))
            self.assertTrue(clean_files)
            for file in clean_files:
                records = trace.read_jsonl(file)
                self.assertTrue(records)
                spans = trace.read_jsonl(base / "grp" / "traces" / file.name)
                assistant_texts = [
                    str(span.get("observation", ""))
                    for span in spans
                    if span.get("role") == "assistant"
                ]
                for record in records:
                    self.assertIn(record["injected_text"], assistant_texts)
                    self.assertNotEqual(record["clean_text"], record["injected_text"])
                # 不能断言 clean_text 从不出现在整个 trial 的 trace 里：这个
                # 夹具每次都返回同一动作，模型在**未被注入**的步骤里当然也会
                # 自己再输出一次，那不是回灌。真正要守的是「被拒的那一个位置
                # 落盘的是坏文本」——由 compare_events 的配对结果直接证明。
                comps = w4c_probe.compare_events(
                    guard_probe.probe_spans(spans),
                    {file.stem: spans},
                    {file.stem: records},
                )
                for comp in comps:
                    if comp.verdict in (w4c_probe.CHAINED, w4c_probe.MISSING):
                        continue
                    self.assertEqual(
                        comp.injected_text,
                        w4c_probe.rejected_text(spans, comp.step),
                    )

    def test_side_car_pairs_back_with_every_injected_rejection(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._run_once(base, "grp", base / "grp" / "clean")
            comps = w4c_probe.compare_group(
                base / "grp" / "traces", base / "grp" / "clean"
            )
            self.assertTrue(comps)
            stats = w4c_probe.summarize(comps)
            self.assertEqual(stats["counts"][w4c_probe.MISSING], 0)

    def test_clean_actions_dir_is_not_part_of_run_id(self):
        cfg = _load("w4c_off_flash.toml")
        snapshot = exp_config.config_snapshot(cfg, exp_config.run_id(cfg))["config"]
        self.assertNotIn("clean_actions_dir", snapshot)
        self.assertNotIn("clean_actions", snapshot)

    def test_all_existing_run_ids_are_unchanged_and_w4c_ones_are_new(self):
        for name, expected in EXISTING_RUN_IDS.items():
            cfg = _load(name)
            self.assertEqual(exp_config.run_id(cfg), expected, name)
        self.assertNotIn(OFF_RID, list(EXISTING_RUN_IDS.values())[:4])
        self.assertNotEqual(OFF_RID, ON_RID)

    def test_two_tasksets_sha256_unchanged(self):
        for name, expected in w4_preflight.TASKSET_SHA256.items():
            actual = hashlib.sha256(
                (REPO_ROOT / "tasks" / name).read_bytes()
            ).hexdigest()
            self.assertEqual(actual, expected, name)

    def test_cli_rejects_side_car_dir_when_no_injector_exists(self):
        with self.assertRaises(SystemExit):
            cli.main(
                [
                    "--config", exp_config.DEFAULT_CONFIG_PATH.as_posix(),
                    "--clean-actions-dir", "clean_actions/unused",
                ]
            )

    def test_cli_forwards_side_car_dir_into_run_config(self):
        captured: dict[str, runner.RunConfig] = {}

        def fake_run(cfg):
            captured["cfg"] = cfg
            raise RuntimeError("STOP before rendering")

        env = {"LLM_BASE_URL": "x", "LLM_API_KEY": "x", "LLM_MODEL": "x"}
        with patch.object(cli, "load_dotenv", return_value=env), patch.object(
            cli.runner, "run", side_effect=fake_run
        ):
            with self.assertRaisesRegex(RuntimeError, "STOP"):
                cli.main(
                    [
                        "--config", (EXPERIMENTS / "w4c_off_flash.toml").as_posix(),
                        "--max-calls", "720",
                        "--clean-actions-dir", "clean_actions/w4c_off",
                        "--traces-dir", "traces/w4c_off",
                        "--results-dir", "results/w4c_off",
                    ]
                )
        self.assertEqual(
            captured["cfg"].clean_actions_dir, Path("clean_actions/w4c_off")
        )


def _event(
    reply: str,
    *,
    step: int = 2,
    kind: str = guard.GUARD_SCHEMA_VALIDATION,
) -> guard_probe.RejectionEvent:
    return guard_probe.RejectionEvent(
        trial_id="T01-t1",
        step=step,
        kind=kind,
        has_reply=bool(reply),
        parse_ok=True,
        changed=True,
        responsive=True,
        followup_tool_ok=False,
        reply_text=reply,
    )


def _spans(rejected: str, *, step: int = 2) -> list[dict]:
    return [
        loop.make_span("T01-t1", 1, "assistant", observation=rejected),
        loop.make_span("T01-t1", step, "guard", observation="[GUARD-REJECT] x"),
    ]


def _clean(clean_text: str, injected: str = "{not-json", cursor: int = 0) -> dict:
    return {
        "task_id": "T01",
        "trial": 1,
        "trial_id": "T01-t1",
        "cursor": cursor,
        "fault_kind": "bad_json",
        "clean_text": clean_text,
        "injected_text": injected,
    }


class TestVerdictRules(unittest.TestCase):
    """判定分层必须按 docs/w4c-design.md §3.1 跑批前写死的口径。"""

    def test_normalized_action_ignores_key_order_but_not_values(self):
        a = w4c_probe._normalized_action(CLEAN_ACTION)
        b = w4c_probe._normalized_action(
            '{"args":{"order_id":1001},"tool":"get_order"}'
        )
        self.assertEqual(a, b)
        self.assertNotEqual(a, w4c_probe._normalized_action(DIFFERENT_ACTION))
        self.assertEqual(w4c_probe._normalized_action(FINAL_ACTION)[0], "final")
        self.assertEqual(w4c_probe._normalized_action("{not-json"), (None, None))

    def test_rejected_text_picks_nearest_assistant_before_guard(self):
        spans = _spans("{not-json")
        self.assertEqual(w4c_probe.rejected_text(spans, 2), "{not-json")
        self.assertEqual(w4c_probe.rejected_text(spans, 99), "")

    def _one(self, reply, **kwargs):
        record = kwargs.pop("record", _clean(CLEAN_ACTION))
        event = _event(reply, **kwargs)
        return w4c_probe.compare_events(
            [event], {"T01-t1": _spans(record["injected_text"])},
            {"T01-t1": [record]},
        )[0]

    def test_identical_when_reply_is_the_replaced_action(self):
        comp = self._one(CLEAN_ACTION)
        self.assertEqual(comp.verdict, w4c_probe.IDENTICAL)
        self.assertEqual(comp.clean_text, CLEAN_ACTION)

    def test_different_when_reply_is_another_legal_action(self):
        self.assertEqual(self._one(DIFFERENT_ACTION).verdict, w4c_probe.DIFFERENT)

    def test_final_instead(self):
        self.assertEqual(self._one(FINAL_ACTION).verdict, w4c_probe.FINAL_INSTEAD)

    def test_unparseable_reply(self):
        self.assertEqual(self._one("{not-json").verdict, w4c_probe.UNPARSEABLE)

    def test_idempotency_is_reported_separately_even_if_reply_matches(self):
        comp = self._one(CLEAN_ACTION, kind=guard.GUARD_IDEMPOTENCY)
        self.assertEqual(comp.verdict, w4c_probe.IDEMPOTENCY_SKIPPED)
        self.assertEqual(
            w4c_probe.summarize([comp])["comparable"], 0
        )

    def test_missing_when_rejection_cannot_be_paired(self):
        comps = w4c_probe.compare_events(
            [_event(DIFFERENT_ACTION)], {"T01-t1": _spans("UNKNOWN-BAD-TEXT")},
            {"T01-t1": [_clean(CLEAN_ACTION)]},
        )
        self.assertEqual(comps[0].verdict, w4c_probe.MISSING)

    def test_chained_rejection_is_not_a_device_failure(self):
        spans = [
            loop.make_span("T01-t1", 1, "assistant", observation="{not-json"),
            loop.make_span("T01-t1", 2, "guard", observation="[GUARD-REJECT] a"),
            loop.make_span("T01-t1", 3, "assistant", observation=DIFFERENT_ACTION),
            loop.make_span("T01-t1", 4, "guard", observation="[GUARD-REJECT] b"),
        ]
        events = [
            _event(DIFFERENT_ACTION, step=2),
            _event(FINAL_ACTION, step=4),
        ]
        comps = w4c_probe.compare_events(
            events, {"T01-t1": spans},
            {"T01-t1": [_clean(CLEAN_ACTION)]},
        )
        self.assertEqual([c.verdict for c in comps], ["different", "chained"])
        self.assertEqual(w4c_probe.summarize(comps)["counts"]["missing"], 0)

    def test_duplicate_injected_text_is_consumed_in_cursor_order(self):
        spans = [
            loop.make_span("T01-t1", 1, "assistant", observation="{not-json"),
            loop.make_span("T01-t1", 2, "guard", observation="[GUARD-REJECT] a"),
            loop.make_span("T01-t1", 3, "assistant", observation="{not-json"),
            loop.make_span("T01-t1", 4, "guard", observation="[GUARD-REJECT] b"),
        ]
        # 两次响应都给 CLEAN_ACTION：只有按 cursor 顺序消费，才会得到
        # [identical, different]；若顺序颠倒，两个判定会整体翻转。
        events = [_event(CLEAN_ACTION, step=2), _event(CLEAN_ACTION, step=4)]
        records = [
            _clean(CLEAN_ACTION, cursor=0),
            _clean(DIFFERENT_ACTION, cursor=3),
        ]
        comps = w4c_probe.compare_events(
            events, {"T01-t1": spans}, {"T01-t1": records}
        )
        self.assertEqual([c.cursor for c in comps], [0, 3])
        self.assertEqual(
            [c.verdict for c in comps],
            [w4c_probe.IDENTICAL, w4c_probe.DIFFERENT],
        )

    def test_load_clean_actions_sorts_by_cursor(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            trace.write_jsonl(
                directory / "T01-t1.jsonl",
                [_clean("b", cursor=7), _clean("a", cursor=2)],
            )
            records = w4c_probe.load_clean_actions(directory)["T01-t1"]
            self.assertEqual([r["cursor"] for r in records], [2, 7])

    def test_offline_calibration_asserts_pass(self):
        stats = w4c_probe.calibration(TASKS, seed=11, fault_per_mille=300, k=3)
        w4c_probe.assert_calibration(stats)  # 不抛即通过
        self.assertEqual(stats["events"]["baseline"], 0)
        self.assertEqual(stats["injected"]["baseline"], 0)
        self.assertEqual(stats["distinct"]["counts"]["identical"], 0)
        self.assertGreater(stats["distinct"]["counts"]["different"], 0)
        self.assertEqual(
            stats["same"]["counts"]["identical"], stats["same"]["comparable"]
        )
        for label in ("distinct", "same"):
            self.assertEqual(stats[label]["counts"]["missing"], 0, label)

    def test_reports_carry_the_discipline_wording(self):
        report = w4c_probe.render_report([("grp", [])])
        self.assertIn("输出被注入扰动，不是纯净模型行为", report)
        self.assertIn("comparable", report)
        self.assertIn("idempotency_skipped", report)
        stats = w4c_probe.calibration(TASKS, seed=11, fault_per_mille=300, k=3)
        dryrun = w4c_probe.render_dryrun(stats, seed=11, fpm=300, k=3)
        self.assertIn("不是 W4-c 的实验结论", dryrun)
        self.assertIn("防代答红线", dryrun)
        self.assertIn("零成本", dryrun)


class TestW4cConfigs(unittest.TestCase):
    """两格仍是单变量 A/B；装置参数与 W4-b 一致，run_id 全部隔离。"""

    def test_single_variable_and_compare_chain(self):
        off, on = _load("w4c_off_flash.toml"), _load("w4c_all4_flash.toml")
        for field in w4_preflight._MEASURED_FIELDS:
            self.assertEqual(getattr(off, field), getattr(on, field), field)
        self.assertEqual(off.guards, ())
        self.assertEqual(on.guards, guard.GUARD_NAMES)
        self.assertEqual(off.compare, "")
        self.assertEqual(
            on.compare, f"experiments/runs/{OFF_RID}/result_rows.jsonl"
        )

    def test_injection_params_match_w4b_exactly(self):
        off_c = _load("w4c_off_flash.toml")
        off_b = _load("w4b_off_flash.toml")
        for field in ("tasks", "k", "backend", "max_steps", "max_json_retries",
                      "seed", "fault_per_mille"):
            self.assertEqual(getattr(off_c, field), getattr(off_b, field), field)
        self.assertNotEqual(off_c.name, off_b.name)
        self.assertNotEqual(exp_config.run_id(off_c), exp_config.run_id(off_b))

    def test_design_doc_has_prewritten_rules_and_stop_conditions(self):
        text = (REPO_ROOT / "docs" / "w4c-design.md").read_text(encoding="utf-8")
        for phrase in (
            "成功判据不是 `pass^k` 提升",
            "防代答红线",
            "判定规则（跑批前写死）",
            "comparable",
            "停止条件",
            "不是实验结论",
        ):
            self.assertIn(phrase, text, phrase)


if __name__ == "__main__":
    unittest.main()
