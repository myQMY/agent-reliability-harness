"""W4 的 ``[GUARD-REJECT]`` 纠正探针：只从 trace 读数，不改判分口径。

W4 要回答的不是「护栏有没有让通过率变高」（那是 pass^k 的活），而是更靠前
的一问：**真实模型收到一条拒绝消息后，下一次输出有没有作出有效响应**。
本模块把这件事做成可复用的纯函数：给一段 span 列表，逐条找 role=guard 且
以 GUARD_REJECT_PREFIX 开头的拒绝，检查紧随其后的 assistant 输出。

四态口径（相对「被拒的那一次输出」）：

- ``no_reply``：拒绝之后没有下一次模型输出（步数/重试耗尽）。
- ``unparseable``：下一次输出仍不是合法的单个动作 JSON。
- ``repeated``：输出合法，但与被拒那次逐字相同（等于没听懂）。
- ``responsive``：输出合法且已改变——**只说明输出层面作出了响应**。

``followup_tool_ok`` 是更强的证据：拒绝之后出现过一次成功执行的 tool span。
但 ``idempotency`` 的正确响应恰恰是「跳过该写动作」，不该期待有新的写工具
执行，所以这个字段按护栏类型分别解读，不汇总成单一成功率。

**这是机制证据，不是效果指标**：responsive 高不代表任务成功，也不代表
护栏有效；论效果仍然只认 pass^k。CLI 的默认 dry-run 用 SeededBackend
跑两格来校准探针本身——模拟器的 ``_pending_correct`` 是硬编码重发，
所以 dry-run 的数字只证明「探针数得对」，**绝不能当作 W4 的实验结论**。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Sequence

from eval import runner, trace
from runtime import guard, loop
from runtime.model import BackendConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = REPO_ROOT / "tasks" / "mvp_10.jsonl"
DEFAULT_OUT = REPO_ROOT / "experiments" / "w4_probe_dryrun.md"

# 与 runtime/guard.py 的四条拒绝文本一一对应；测试钉死四类都能被识别。
KIND_MARKERS = (
    ("schema 校验", guard.GUARD_SCHEMA_VALIDATION),
    ("幂等键", guard.GUARD_IDEMPOTENCY),
    ("工具层失败", guard.GUARD_TOOL_RETRY),
    ("不是合法的单个 JSON 对象", guard.GUARD_JSON_RECOVERY),
)
UNKNOWN_KIND = "unknown"

# dry-run 的校准参数版本化写死，与 W3 的 seed=11 / fpm=60 保持一致
DRYRUN_SEED = 11
DRYRUN_FPM = 60


@dataclass(frozen=True)
class RejectionEvent:
    trial_id: str
    step: int
    kind: str
    has_reply: bool
    parse_ok: bool
    changed: bool
    responsive: bool
    followup_tool_ok: bool
    reply_text: str


def classify(observation: str) -> str:
    """按拒绝文本判定护栏类型；认不出来的如实记 unknown，不猜。"""
    for marker, kind in KIND_MARKERS:
        if marker in observation:
            return kind
    return UNKNOWN_KIND


def _observation_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _tool_ok(span: dict[str, Any]) -> bool:
    observation = span.get("observation")
    return isinstance(observation, dict) and observation.get("ok") is True


def probe_spans(spans: Sequence[dict[str, Any]]) -> list[RejectionEvent]:
    """逐条分析 guard 拒绝；spans 必须按 step 递增，返回顺序与输入一致。"""
    ordered = sorted(spans, key=lambda span: span.get("step", 0))
    events: list[RejectionEvent] = []
    for index, span in enumerate(ordered):
        if span.get("role") != "guard":
            continue
        text = _observation_text(span.get("observation", ""))
        if not text.startswith(guard.GUARD_REJECT_PREFIX):
            continue

        # 被拒的那一次输出 = 本 guard span 之前最近的一条 assistant span
        previous = ""
        for back in range(index - 1, -1, -1):
            if ordered[back].get("role") == "assistant":
                previous = _observation_text(ordered[back].get("observation", ""))
                break

        reply_text = ""
        has_reply = False
        for ahead in range(index + 1, len(ordered)):
            if ordered[ahead].get("role") == "assistant":
                reply_text = _observation_text(ordered[ahead].get("observation", ""))
                has_reply = True
                break
        action, _ = loop.parse_action(reply_text) if has_reply else (None, "")
        parse_ok = action is not None
        changed = has_reply and parse_ok and reply_text != previous

        # 拒绝之后、下一条拒绝之前是否出现过成功执行的工具调用
        followup_tool_ok = False
        for ahead in range(index + 1, len(ordered)):
            role = ordered[ahead].get("role")
            if role == "guard" and _observation_text(
                ordered[ahead].get("observation", "")
            ).startswith(guard.GUARD_REJECT_PREFIX):
                break
            if role == "tool" and _tool_ok(ordered[ahead]):
                followup_tool_ok = True
                break

        events.append(
            RejectionEvent(
                trial_id=str(span.get("trial_id", "")),
                step=int(span.get("step", 0)),
                kind=classify(text),
                has_reply=has_reply,
                parse_ok=parse_ok,
                changed=changed,
                responsive=bool(changed),
                followup_tool_ok=followup_tool_ok,
                reply_text=reply_text,
            )
        )
    return events


def summarize(events: Sequence[RejectionEvent]) -> dict[str, Any]:
    kinds = [event.kind for event in events]
    by_kind: dict[str, dict[str, int]] = {}
    for kind in (*[name for _, name in KIND_MARKERS], UNKNOWN_KIND):
        subset = [event for event in events if event.kind == kind]
        by_kind[kind] = {
            "total": len(subset),
            "responsive": sum(1 for event in subset if event.responsive),
            "parse_ok": sum(1 for event in subset if event.parse_ok),
            "followup_tool_ok": sum(1 for event in subset if event.followup_tool_ok),
        }
    return {
        "total": len(events),
        "responsive": sum(1 for event in events if event.responsive),
        "parse_ok": sum(1 for event in events if event.parse_ok),
        "changed": sum(1 for event in events if event.changed),
        "has_reply": sum(1 for event in events if event.has_reply),
        "followup_tool_ok": sum(1 for event in events if event.followup_tool_ok),
        "kinds_seen": sorted(set(kinds)),
        "by_kind": by_kind,
    }


def dry_run_events(
    tasks_path: Path,
    guards: tuple[str, ...],
    *,
    seed: int = DRYRUN_SEED,
    fault_per_mille: int = DRYRUN_FPM,
) -> list[RejectionEvent]:
    """用确定性模拟器跑一遍并把 trace 喂回探针；只写临时目录，零成本零网络。"""
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        runner.run(
            runner.RunConfig(
                tasks_path=Path(tasks_path),
                k=1,
                backend=BackendConfig(
                    name="seeded", seed=seed, fault_per_mille=fault_per_mille
                ),
                results_dir=base / "results",
                traces_dir=base / "traces",
                guards=guards,
            )
        )
        events: list[RejectionEvent] = []
        for path in sorted((base / "traces").glob("*.jsonl")):
            events.extend(probe_spans(trace.read_jsonl(path)))
    return sorted(events, key=lambda event: (event.trial_id, event.step))


def events_from_dir(path: Path) -> list[RejectionEvent]:
    """W4 真实跑批后复用：只读已落盘的 trace 目录，不重跑评测。"""
    events: list[RejectionEvent] = []
    for trace_file in sorted(Path(path).glob("*.jsonl")):
        events.extend(probe_spans(trace.read_jsonl(trace_file)))
    return sorted(events, key=lambda event: (event.trial_id, event.step))


def _tables(groups: Sequence[tuple[str, Sequence[RejectionEvent]]]) -> list[str]:
    lines = [
        "## 汇总",
        "",
        "| 组 | 拒绝数 | 有下一次输出 | 输出合法 | 已改变 | responsive | 后续 tool 成功 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, events in groups:
        s = summarize(events)
        lines.append(
            f"| {label} | {s['total']} | {s['has_reply']} | {s['parse_ok']} | "
            f"{s['changed']} | {s['responsive']} | {s['followup_tool_ok']} |"
        )
    lines += [
        "",
        "## 按护栏类型拆分",
        "",
        "| 组 | 护栏 | 拒绝数 | responsive | 后续 tool 成功 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for label, events in groups:
        s = summarize(events)
        for kind, stats in s["by_kind"].items():
            if not stats["total"]:
                continue
            lines.append(
                f"| {label} | `{kind}` | {stats['total']} | "
                f"{stats['responsive']} | {stats['followup_tool_ok']} |"
            )
    return lines


def render_report(groups: Sequence[tuple[str, Sequence[RejectionEvent]]]) -> str:
    head = [
        "# W4 `[GUARD-REJECT]` 纠正探针",
        "",
        "> 由 `python -m eval.guard_probe` 生成；只读取 trace span，不重跑判分。",
        "> **responsive 只是输出层面的响应，不是任务成功、不是护栏有效**；"
        "论效果只认 `pass^k`。",
        "",
    ]
    tail = [
        "",
        "## 口径",
        "",
        "- `no_reply` / `unparseable` / `repeated` 三类都不计入 responsive；"
        "只要下一次输出合法且与被拒那次不同，才算作出了有效响应。",
        "- `idempotency` 的正确响应是**跳过**重复写动作，「后续 tool 成功」"
        "对它不适用，不能拿这一列横向比较四条护栏。",
        "- 探针不读 `passed` 字段，不进结果行，不改 grader；它只回答"
        "「拒绝消息有没有被读懂」这一层机制问题。",
        "",
    ]
    return "\n".join(head + _tables(groups) + tail)


def render_dryrun(groups: Sequence[tuple[str, Sequence[RejectionEvent]]]) -> str:
    """dry-run 报告必须把「这只是校准」写在最显眼的位置。"""
    off = summarize(groups[0][1])
    on = summarize(groups[1][1]) if len(groups) > 1 else None
    lines = [
        "# W4 前置：纠正探针离线校准（零成本、零网络）",
        "",
        "> 本文件由 `python -m eval.guard_probe` 用 **SeededBackend** 生成，"
        "全程没有发起任何模型 API 请求，也没有花一分钱。",
        "> **这些数字只证明探针数得对，不构成 W4 的实验结论。** "
        "模拟器的 `_pending_correct` 是硬编码的「收到拒绝就重发干净动作」，"
        "它当然能读懂——真实模型能不能读懂，必须等 W4 接真实模型才知道。",
        "",
        f"校准输入：`tasks/mvp_10.jsonl`，k=1，seed={DRYRUN_SEED}，"
        f"fault_per_mille={DRYRUN_FPM}，两格只差 `[guards].enabled`。",
        "",
        f"- OFF 组拒绝数 **{off['total']}**（护栏全关时探针不应凭空数出拒绝）；",
    ]
    if on is not None:
        lines += [
            f"- 四条全开组拒绝数 **{on['total']}**，其中 "
            f"responsive **{on['responsive']}**、后续 tool 成功 "
            f"**{on['followup_tool_ok']}**；类型分布 "
            f"{', '.join(on['kinds_seen']) or '无'}。",
        ]
    lines += [
        "",
        "这组数字回答的是「尺子有没有装反」：OFF 组必须是 0，全开组必须能"
        "把四类拒绝分别归类。它**不回答**真实模型是否理解 `[GUARD-REJECT]`，"
        "也不与任何 W3 留档的分数混用。W4 跑完用 "
        "`python -m eval.guard_probe --traces-dir <off> --label w4_off "
        "--traces-dir <all4> --label w4_all4` 只读复用同一把尺子。",
        "",
        *_tables(groups),
        "",
        "## 口径",
        "",
        "- `no_reply` / `unparseable` / `repeated` 三类都不计入 responsive；"
        "只要下一次输出合法且与被拒那次不同，才算作出了有效响应。",
        "- `idempotency` 的正确响应是**跳过**重复写动作，「后续 tool 成功」"
        "对它不适用，不能拿这一列横向比较四条护栏。",
        "- 探针不读 `passed` 字段，不进结果行，不改 grader；它只回答"
        "「拒绝消息有没有被读懂」这一层机制问题。",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.guard_probe",
        description="[GUARD-REJECT] 纠正探针：离线校准或只读分析已落盘 trace",
    )
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--traces-dir", action="append", type=Path, default=None,
        help="只读模式：已落盘的 trace 目录，可重复传入",
    )
    parser.add_argument(
        "--label", action="append", default=None,
        help="与 --traces-dir 一一对应的组名",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.traces_dir:
        labels = args.label or []
        if len(labels) != len(args.traces_dir):
            raise SystemExit("--label 必须与 --traces-dir 数量一致")
        groups = [
            (label, events_from_dir(path))
            for label, path in zip(labels, args.traces_dir)
        ]
        report = render_report(groups)
    else:
        off = dry_run_events(args.tasks, ())
        all4 = dry_run_events(args.tasks, tuple(guard.GUARD_NAMES))
        groups = [("seeded_off", off), ("seeded_all4", all4)]
        if off:
            raise SystemExit("校准失败：护栏全关时不应出现 [GUARD-REJECT] span")
        if not all4:
            raise SystemExit("校准失败：四条全开时应能数到 [GUARD-REJECT] span")
        report = render_dryrun(groups)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(report.encode("utf-8"))
    print(f"已写出：{args.out.as_posix()}")
    for label, events in groups:
        s = summarize(events)
        print(f"  {label}: 拒绝 {s['total']}，responsive {s['responsive']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
