"""W4-c 只读判定：拒绝后重发的，是不是原本那一个动作？

W4-b 证明了真实模型在**输出层**能响应 ``[GUARD-REJECT]``（17/17
responsive），但留下一个更窄的开放项：注入器只把 ``ModelReply.text`` 换成
坏文本，模型当次原本生成的动作不进 trace，因此无法验证「重发的是不是原本
那一个动作」。W3 靠 ``SeededBackend._pending_correct`` 硬编码重发有明确参照，
W4-b 拒绝代答，参照也一起没了。

W4-c 的装置改动只有一处：``OutputFaultBackend`` 在每次注入时把**替换前的
干净文本**留进 ``clean_actions``，由 ``eval.runner`` 写到独立 side-car 目录
（不改 trace 既有 8 字段、结果行、判分、run_id）。本模块纯只读地把两份证据
对上：

1. 复用 ``eval.guard_probe.probe_spans`` 取拒绝事件（同一把尺子）；
2. 从既有 trace 取该 guard span 之前最近的 assistant span = 被拒文本；
3. 在 side-car 里按 ``(trial_id, injected_text == rejected_text)`` 找留档；
4. 比较 ``clean_text`` 与 ``reply_text`` 解析出的动作。

**防代答红线**：留档是事后观测。本模块只读，运行时绝不把 ``clean_text``
喂回模型；``OutputFaultBackend.generate`` 也不会读它来构造响应。

``--dry-run`` 是零 API 请求、零花费的离线装置校准：两路确定性夹具分别
「被拒后给另一个合法动作」与「被拒后重发同一个动作」，加上 ``fpm=0``
基准，机器证明尺子既能数出 different、也能数出 identical，且配对不漏。
校准数字**不是实验结论**，不与实测值相减或比较。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from env import db
from eval import guard_probe, runner, trace, w4b_probe
from runtime import guard, loop
from runtime.model import ModelReply, OutputFaultBackend

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = REPO_ROOT / "tasks" / "mvp_10.jsonl"
DEFAULT_OUT = REPO_ROOT / "experiments" / "w4c_probe.md"
DEFAULT_DRYRUN_OUT = REPO_ROOT / "experiments" / "w4c_probe_dryrun.md"

# 与 W4-b 两格配置一致：注入判定函数同源，装置参数不是模型能力轴。
DEFAULT_FPM = 300
DEFAULT_SEED = 11
DEFAULT_K = 3

# 校准夹具「给不同动作」时返回的固定读工具调用：合法 schema、必定与任何
# clean_text 不同、不写状态、不会触发 idempotency / tool_retry 的额外拒绝。
DISTINCT_ACTION = json.dumps(
    {"tool": "list_customer_orders", "args": {"customer_id": "__w4c_calibration__"}},
    ensure_ascii=False,
    sort_keys=True,
)

IDENTICAL = "identical"
DIFFERENT = "different"
FINAL_INSTEAD = "final_instead"
UNPARSEABLE = "unparseable"
IDEMPOTENCY_SKIPPED = "idempotency_skipped"
CHAINED = "chained"
MISSING = "missing"
VERDICTS = (
    IDENTICAL, DIFFERENT, FINAL_INSTEAD, UNPARSEABLE,
    IDEMPOTENCY_SKIPPED, CHAINED, MISSING,
)
COMPARABLE = (IDENTICAL, DIFFERENT, FINAL_INSTEAD)


@dataclass(frozen=True)
class Comparison:
    """一次拒绝事件与一份干净动作留档的对照结果。"""

    trial_id: str
    step: int
    kind: str
    fault_kind: str
    cursor: int
    verdict: str
    clean_text: str
    injected_text: str
    reply_text: str


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalized_action(text: str) -> tuple[str | None, str | None]:
    """解析成 (kind, 规范化动作)。工具比较只认 tool + args，忽略多余字段。

    ``parse_action`` 已保证 tool 是 str、args 是 dict；final 单独成一类。
    规范化用 sort_keys + 紧凑分隔符，避免键序差异造成假的「不同」。
    """
    action, _ = loop.parse_action(text)
    if action is None:
        return None, None
    if "final" in action and isinstance(action["final"], str):
        return "final", None
    tool, args = action.get("tool"), action.get("args", {})
    if not isinstance(tool, str) or not isinstance(args, dict):
        return None, None
    normalized = json.dumps(
        {"tool": tool, "args": args},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "tool", normalized


def rejected_text(spans: Sequence[dict[str, Any]], step: int) -> str:
    """该 guard span 之前最近的 assistant span 文本 = 被拒的那一次输出。

    注入后的坏文本正是这样落盘的，因此它是 side-car 配对的自然 key。
    """
    ordered = sorted(spans, key=lambda span: span.get("step", 0))
    index = next(
        (
            i
            for i, span in enumerate(ordered)
            if span.get("step") == step and span.get("role") == "guard"
        ),
        None,
    )
    if index is None:
        return ""
    for back in range(index - 1, -1, -1):
        if ordered[back].get("role") == "assistant":
            return _as_text(ordered[back].get("observation", ""))
    return ""


def _verdict(event: guard_probe.RejectionEvent, record: dict[str, Any]) -> str:
    # idempotency 的拒绝文本要求「跳过该动作」，本来就不该期待重发同一个，
    # 因此单列、不进主分母（口径写死在 docs/w4c-design.md §3.1）。
    if event.kind == guard.GUARD_IDEMPOTENCY:
        return IDEMPOTENCY_SKIPPED
    clean_kind, clean_norm = _normalized_action(str(record.get("clean_text", "")))
    reply_kind, reply_norm = _normalized_action(event.reply_text)
    if clean_kind is None or reply_kind is None:
        return UNPARSEABLE
    if reply_kind == "final":
        return FINAL_INSTEAD
    if clean_kind == "final":
        # 注入只发生在合法 tool 动作上；出现即装置异常，如实单列。
        return UNPARSEABLE
    return IDENTICAL if clean_norm == reply_norm else DIFFERENT


def compare_events(
    events: Sequence[guard_probe.RejectionEvent],
    spans_by_trial: dict[str, list[dict[str, Any]]],
    clean_by_trial: dict[str, list[dict[str, Any]]],
) -> list[Comparison]:
    """纯函数核心：把拒绝事件配回干净动作留档并判定。

    同一 trial 里出现多条相同坏文本时按 cursor 顺序消费。配不到留档时，
    先判是不是「连锁拒绝」（被拒文本 = 之前某次拒绝的响应动作）：那是模型
    自己刚才的输出被再次拦下，不是注入直接产生，单列 chained、不算装置
    故障。其余记 missing——它是装置没接上的唯一硬信号。
    """
    used: dict[str, set[int]] = {}
    seen_replies: dict[str, set[str]] = {}
    results: list[Comparison] = []
    ordered = sorted(events, key=lambda event: (event.trial_id, event.step))
    for event in ordered:
        tid = event.trial_id
        rejected = rejected_text(spans_by_trial.get(tid, []), event.step)
        candidates = clean_by_trial.get(tid, [])
        consumed = used.setdefault(tid, set())
        match = next(
            (
                i
                for i, record in enumerate(candidates)
                if i not in consumed
                and str(record.get("injected_text", "")) == rejected
            ),
            -1,
        )
        replies = seen_replies.setdefault(tid, set())
        if match >= 0:
            consumed.add(match)
            record = candidates[match]
            results.append(
                Comparison(
                    trial_id=tid,
                    step=event.step,
                    kind=event.kind,
                    fault_kind=str(record.get("fault_kind", "")),
                    cursor=int(record.get("cursor", -1)),
                    verdict=_verdict(event, record),
                    clean_text=str(record.get("clean_text", "")),
                    injected_text=str(record.get("injected_text", "")),
                    reply_text=event.reply_text,
                )
            )
        else:
            verdict = CHAINED if rejected and rejected in replies else MISSING
            results.append(
                Comparison(
                    trial_id=tid,
                    step=event.step,
                    kind=event.kind,
                    fault_kind="",
                    cursor=-1,
                    verdict=verdict,
                    clean_text="",
                    injected_text=rejected,
                    reply_text=event.reply_text,
                )
            )
        if event.reply_text:
            replies.add(event.reply_text)
    return results


def summarize(comps: Sequence[Comparison]) -> dict[str, Any]:
    counts = {name: sum(1 for c in comps if c.verdict == name) for name in VERDICTS}
    by_kind: dict[str, dict[str, int]] = {}
    for kind in (*guard.GUARD_NAMES, guard_probe.UNKNOWN_KIND):
        subset = [c for c in comps if c.kind == kind]
        if not subset:
            continue
        by_kind[kind] = {
            "total": len(subset),
            "comparable": sum(1 for c in subset if c.verdict in COMPARABLE),
            "identical": sum(1 for c in subset if c.verdict == IDENTICAL),
            "different": sum(1 for c in subset if c.verdict == DIFFERENT),
        }
    return {
        "total": len(comps),
        "counts": counts,
        "comparable": sum(counts[name] for name in COMPARABLE),
        "by_kind": by_kind,
    }


def load_spans(path: Path) -> dict[str, list[dict[str, Any]]]:
    """只读既有 trace 目录；文件名 stem 即 trial_id（与 runner 一致）。"""
    by_trial: dict[str, list[dict[str, Any]]] = {}
    for file in sorted(Path(path).glob("*.jsonl")):
        by_trial.setdefault(file.stem, []).extend(trace.read_jsonl(file))
    return by_trial


def load_clean_actions(path: Path) -> dict[str, list[dict[str, Any]]]:
    """只读独立 side-car 目录；按 cursor 排序供顺序消费。"""
    by_trial: dict[str, list[dict[str, Any]]] = {}
    for file in sorted(Path(path).glob("*.jsonl")):
        for record in trace.read_jsonl(file):
            tid = str(record.get("trial_id") or file.stem)
            by_trial.setdefault(tid, []).append(record)
    for records in by_trial.values():
        records.sort(key=lambda record: int(record.get("cursor", 0)))
    return by_trial


def compare_group(traces_dir: Path, clean_dir: Path) -> list[Comparison]:
    """既有 trace + side-car 两份只读证据，产出该组的判定列表。"""
    spans_by_trial = load_spans(traces_dir)
    clean_by_trial = load_clean_actions(clean_dir)
    events: list[guard_probe.RejectionEvent] = []
    for spans in spans_by_trial.values():
        events.extend(guard_probe.probe_spans(spans))
    return compare_events(events, spans_by_trial, clean_by_trial)


class _CalibrationBackend:
    """离线校准专用确定性夹具；绝不用于真实跑批。

    ``distinct`` 模式被拒后返回一个固定的、必定不同的合法读工具调用；
    ``same`` 模式被拒后返回上一次被替换掉的干净动作（模拟 W3 的
    ``_pending_correct``，但只作为**尺子的输入**，不是 harness 代答装置）。
    两路对称地证明判定器既不漏报也不误报。
    """

    def __init__(self, script: Sequence[str], *, mode: str):
        if mode not in ("distinct", "same"):
            raise ValueError(f"未知校准模式：{mode}")
        self._script = list(script)
        self._mode = mode
        self._cursor = 0
        self._last_clean = ""

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        if messages and messages[-1].get("content", "").startswith(
            guard.GUARD_REJECT_PREFIX
        ):
            if self._mode == "same":
                return ModelReply(text=self._last_clean)
            return ModelReply(text=DISTINCT_ACTION)
        text = self._script[min(self._cursor, len(self._script) - 1)]
        self._cursor += 1
        self._last_clean = text
        return ModelReply(text=text)


def calibration_group(
    tasks: Sequence[dict[str, Any]],
    mode: str,
    *,
    seed: int,
    fault_per_mille: int,
    k: int,
) -> tuple[list[Comparison], int, int]:
    """跑一遍确定性单轮循环，返回 (判定列表, 注入次数, 拒绝事件数)。"""
    spans_by_trial: dict[str, list[dict[str, Any]]] = {}
    clean_by_trial: dict[str, list[dict[str, Any]]] = {}
    events: list[guard_probe.RejectionEvent] = []
    injected = 0
    for task in tasks:
        template = db.build_template(task["initial_db"])
        script = w4b_probe.clean_script(task)
        for trial in range(1, k + 1):
            tid = f"{task['id']}-t{trial}"
            conn = db.reset(template)
            backend = OutputFaultBackend(
                _CalibrationBackend(script, mode=mode),
                seed=seed,
                fault_per_mille=fault_per_mille,
                task_id=task["id"],
                trial=trial,
            )
            messages = [{"role": "user", "content": task["user_utterances"][0]}]
            result = loop.run_turn(
                messages,
                backend,
                conn,
                trial_id=tid,
                counter=loop.StepCounter(),
                budget=guard.StepBudget(),
                guards=guard.GUARD_NAMES,
                guard_state=guard.GuardState(),
            )
            spans_by_trial[tid] = result.spans
            clean_by_trial[tid] = list(backend.clean_actions)
            injected += len(backend.clean_actions)
            events.extend(guard_probe.probe_spans(result.spans))
            conn.close()
        template.close()
    comps = compare_events(events, spans_by_trial, clean_by_trial)
    return comps, injected, len(events)


def calibration(
    tasks_path: Path, *, seed: int, fault_per_mille: int, k: int
) -> dict[str, Any]:
    tasks = runner.load_tasks(Path(tasks_path))
    distinct, distinct_inj, distinct_events = calibration_group(
        tasks, "distinct", seed=seed, fault_per_mille=fault_per_mille, k=k
    )
    same, same_inj, same_events = calibration_group(
        tasks, "same", seed=seed, fault_per_mille=fault_per_mille, k=k
    )
    baseline, baseline_inj, baseline_events = calibration_group(
        tasks, "distinct", seed=seed, fault_per_mille=0, k=k
    )
    return {
        "distinct": summarize(distinct),
        "same": summarize(same),
        "baseline": summarize(baseline),
        "injected": {"distinct": distinct_inj, "same": same_inj, "baseline": baseline_inj},
        "events": {"distinct": distinct_events, "same": same_events, "baseline": baseline_events},
        "task_count": len(tasks),
        "trial_count": len(tasks) * k,
    }


def assert_calibration(stats: dict[str, Any]) -> None:
    """跑批前的强制断言；任何一条不过都不许花真实模型的钱。"""
    if stats["events"]["baseline"] or stats["injected"]["baseline"]:
        raise SystemExit(
            "校准失败：fpm=0 基准组仍出现拒绝 / 留档，干净脚本混入了固有缺陷"
        )
    distinct, same = stats["distinct"], stats["same"]
    for label, group in (("distinct", distinct), ("same", same)):
        if group["counts"][MISSING]:
            raise SystemExit(
                f"校准失败：{label} 路有 {group['counts'][MISSING]} 个事件配不到留档"
            )
        if not group["comparable"]:
            raise SystemExit(f"校准失败：{label} 路没有可比事件")
    if distinct["counts"][IDENTICAL] or distinct["counts"][DIFFERENT] != distinct["comparable"]:
        raise SystemExit(
            "校准失败：distinct 路应把全部可比事件判为 different，却数到了 identical / 其它"
        )
    if same["counts"][IDENTICAL] != same["comparable"]:
        raise SystemExit(
            "校准失败：same 路应把全部可比事件判为 identical，尺子把「重发同一动作」漏掉了吗"
        )


def _short(text: str, limit: int = 64) -> str:
    flat = text.replace("\n", " ").replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _summary_table(groups: Sequence[tuple[str, list[Comparison]]]) -> list[str]:
    lines = [
        "## 汇总",
        "",
        "| 组 | 拒绝事件 | comparable | identical | different | final_instead | "
        "unparseable | idempotency_skipped | chained | missing |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, comps in groups:
        s = summarize(comps)
        c = s["counts"]
        lines.append(
            f"| {label} | {s['total']} | {s['comparable']} | "
            f"{c[IDENTICAL]} | {c[DIFFERENT]} | {c[FINAL_INSTEAD]} | "
            f"{c[UNPARSEABLE]} | {c[IDEMPOTENCY_SKIPPED]} | "
            f"{c[CHAINED]} | {c[MISSING]} |"
        )
    return lines


def _kind_table(groups: Sequence[tuple[str, list[Comparison]]]) -> list[str]:
    lines = [
        "",
        "## 按护栏类型拆分",
        "",
        "| 组 | 护栏 | 事件 | comparable | identical | different |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for label, comps in groups:
        for kind, row in summarize(comps)["by_kind"].items():
            lines.append(
                f"| {label} | `{kind}` | {row['total']} | {row['comparable']} | "
                f"{row['identical']} | {row['different']} |"
            )
    return lines


def _detail_table(groups: Sequence[tuple[str, list[Comparison]]]) -> list[str]:
    lines = [
        "",
        "## 逐事件明细（只读，可人工核对）",
        "",
        "| 组 | trial | step | 护栏 | 故障 | 判定 | 被替换的干净动作 | 拒绝后的响应 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    any_row = False
    for label, comps in groups:
        for c in comps:
            any_row = True
            lines.append(
                f"| {label} | `{c.trial_id}` | {c.step} | `{c.kind}` | "
                f"`{c.fault_kind or '-'}` | `{c.verdict}` | "
                f"`{_short(c.clean_text) or '-'}` | `{_short(c.reply_text) or '-'}` |"
            )
    if not any_row:
        lines.append("| — | — | — | — | — | — | — | — |")
    return lines


_CAVEATS = [
    "",
    "## 口径",
    "",
    "- **输出被注入扰动，不是纯净模型行为**。`fault_per_mille` 是装置参数，"
    "不是模型能力轴；本结果不与 W3 / W4 首轮 / W4-b 的任何数字相减或拼曲线。",
    "- 主分母 `comparable = identical + different + final_instead`；"
    "`idempotency_skipped`（拒绝文本要求「跳过该动作」）、`unparseable`、"
    "`chained`（连锁拒绝，不是注入直接产生）、`missing` 全部单列、如实统计。",
    "- `identical` 的含义是「模型按同一策略重新推导出了同一个合法调用」，"
    "**不是**回忆起被拿走的动作：模型从未见过 `clean_text`，对话里只有坏文本 + 拒绝消息。",
    "- `different` 只说明给了另一个合法动作，不等于改错；`final_instead` 不是重发。",
    "- 本判定不读 `passed`、不进结果行、不改 grader；论效果仍然只认 `pass^k`，"
    "而本组 `pass^k` 无分辨率。机制证据不是护栏有效性的证据。",
    "",
]


def render_report(groups: Sequence[tuple[str, list[Comparison]]]) -> str:
    head = [
        "# W4-c 只读判定：拒绝后重发的是不是原本那一个动作",
        "",
        "> 由 `python -m eval.w4c_probe` 生成；只读既有 trace 与独立 `clean_actions/` "
        "side-car，不重跑模型、不重算判分、不改任何归档。",
        "> **本报告涉及的输出都被注入了输出级故障，不是纯净模型行为。** "
        "本组只判定一条输出层机制链，不判定任务成功、不判定护栏有效。",
        "",
    ]
    return "\n".join(
        head + _summary_table(groups) + _kind_table(groups)
        + _detail_table(groups) + _CAVEATS
    )


def render_dryrun(stats: dict[str, Any], *, seed: int, fpm: int, k: int) -> str:
    d, s = stats["distinct"], stats["same"]
    lines = [
        "# W4-c 前置：干净动作留档与判定尺子离线校准（零成本、零网络）",
        "",
        "> 由 `python -m eval.w4c_probe --dry-run` 生成。**全程没有发起任何模型 API 请求，",
        "> 没有花一分钱，不跑判分、不改任何归档。**",
        "> **这些数字只证明留档与判定链路接得上，不是 W4-c 的实验结论。**",
        "> 校准用的是确定性脚本夹具，不是语言模型；它的判定分布不预示真实模型的结果。",
        "",
        f"校准输入：`tasks/mvp_10.jsonl`（{stats['task_count']} 任务 × k={k} = "
        f"{stats['trial_count']} trial、单轮）、`seed={seed}`、"
        f"`fault_per_mille={fpm}`，四条护栏全开。",
        "",
        "> 校准脚本先按 `eval/w4b_probe.clean_script` 过滤任务集固有缺陷（含成对重复写），"
        "只保留合法工具调用。不过滤时 `fpm=60` 会「注入 0 次却数到 7 次拒绝」，"
        "那是脚本固有缺陷，不是装置生效。",
        "",
        "## 装置是否成立（三路强制断言）",
        "",
        "| 路 | 夹具在拒绝后 | 注入次数 | 拒绝事件 | comparable | identical | different | missing |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        f"| `fpm=0` 基准 | 给不同动作 | {stats['injected']['baseline']} | "
        f"{stats['events']['baseline']} | {stats['baseline']['comparable']} | "
        f"{stats['baseline']['counts'][IDENTICAL]} | "
        f"{stats['baseline']['counts'][DIFFERENT]} | "
        f"{stats['baseline']['counts'][MISSING]} |",
        f"| A：给不同动作 | 固定的另一个合法读工具调用 | {stats['injected']['distinct']} | "
        f"{stats['events']['distinct']} | {d['comparable']} | "
        f"{d['counts'][IDENTICAL]} | {d['counts'][DIFFERENT]} | {d['counts'][MISSING]} |",
        f"| B：重发同一动作 | 返回上一次被替换的干净动作 | {stats['injected']['same']} | "
        f"{stats['events']['same']} | {s['comparable']} | "
        f"{s['counts'][IDENTICAL]} | {s['counts'][DIFFERENT]} | {s['counts'][MISSING]} |",
        "",
        "- `fpm=0` 基准必须 0 注入 / 0 拒绝 / 0 留档，否则「分母只来自注入」不成立。",
        "- A 路把全部可比事件判为 `different`（`identical = 0`）；"
        "B 路把全部可比事件判为 `identical`。两路一起证明尺子既不误报也不漏报。",
        "- 两路 `missing` 必须为 0：每个由注入造成的拒绝都能配回一份干净动作留档。",
        "- `idempotency_skipped` / `chained` 按口径单列、不进主分母，"
        "B 路里出现它们是脚本夹具的机械性质，不是装置故障。",
        "",
        "## 防代答红线（本组成立的前提）",
        "",
        "- `clean_text` 只在注入时被读进内存留档，运行时绝不写进 `messages` / "
        "system prompt / 拒绝文本，也绝不读它来构造响应；"
        "`OutputFaultBackend` 拒绝后照常调用底层真实模型、只跳过注入。",
        "- 留档写在独立 `clean_actions/` 目录，不进 `experiments/runs/` 归档三件、"
        "不改 trace 既有 8 字段、结果行、判分或 `run_id`。",
        "- 校准里的「重发同一动作」由夹具产生，只用于检验尺子；"
        "真实跑批里没有任何代码替模型重发动作。",
        "",
    ]
    return "\n".join(lines + _CAVEATS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.w4c_probe",
        description="W4-c 只读判定：把被替换的干净动作与拒绝后的响应做对照",
    )
    parser.add_argument("--dry-run", action="store_true", help="离线装置校准（零 API 请求）")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--fault-per-mille", type=int, default=DEFAULT_FPM)
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument(
        "--traces-dir", action="append", type=Path, default=None,
        help="只读模式：既有 trace 目录，可重复传入",
    )
    parser.add_argument(
        "--clean-actions-dir", action="append", type=Path, default=None,
        help="只读模式：与 --traces-dir 一一对应的 side-car 目录",
    )
    parser.add_argument(
        "--label", action="append", default=None,
        help="与 --traces-dir 一一对应的组名",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out = args.out or (DEFAULT_DRYRUN_OUT if args.dry_run else DEFAULT_OUT)

    if args.dry_run:
        if args.traces_dir or args.clean_actions_dir or args.label:
            raise SystemExit("--dry-run 不接受 --traces-dir / --clean-actions-dir / --label")
        stats = calibration(
            args.tasks,
            seed=args.seed,
            fault_per_mille=args.fault_per_mille,
            k=args.k,
        )
        assert_calibration(stats)
        report = render_dryrun(
            stats, seed=args.seed, fpm=args.fault_per_mille, k=args.k
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(report.encode("utf-8"))
        print(f"已写出：{out.as_posix()}")
        print(
            f"  fpm=0 基准: 注入 {stats['injected']['baseline']}，"
            f"拒绝 {stats['events']['baseline']}"
        )
        print(
            f"  A 给不同动作: identical {stats['distinct']['counts'][IDENTICAL]} / "
            f"different {stats['distinct']['counts'][DIFFERENT]} / "
            f"missing {stats['distinct']['counts'][MISSING]}"
        )
        print(
            f"  B 重发同一动作: identical {stats['same']['counts'][IDENTICAL]} / "
            f"comparable {stats['same']['comparable']} / "
            f"missing {stats['same']['counts'][MISSING]}"
        )
        print("本命令未发起任何 API 请求，未花费任何费用。")
        return 0

    traces = args.traces_dir or []
    cleans = args.clean_actions_dir or []
    labels = args.label or []
    if not traces:
        raise SystemExit("只读模式需要至少一组 --traces-dir / --clean-actions-dir / --label")
    if len(cleans) != len(traces) or len(labels) != len(traces):
        raise SystemExit("--traces-dir / --clean-actions-dir / --label 数量必须一致")
    for directory in (*traces, *cleans):
        if not Path(directory).is_dir():
            raise SystemExit(f"目录不存在：{directory}。只读模式不猜测路径，也不静默当作空组")
    groups = [
        (label, compare_group(traces_dir, clean_dir))
        for label, traces_dir, clean_dir in zip(labels, traces, cleans)
    ]
    report = render_report(groups)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(report.encode("utf-8"))
    print(f"已写出：{out.as_posix()}")
    for label, comps in groups:
        s = summarize(comps)
        print(
            f"  {label}: 拒绝 {s['total']}，comparable {s['comparable']}，"
            f"identical {s['counts'][IDENTICAL]}，different {s['counts'][DIFFERENT]}，"
            f"missing {s['counts'][MISSING]}"
        )
    print("本命令未发起任何 API 请求，未重跑模型。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
