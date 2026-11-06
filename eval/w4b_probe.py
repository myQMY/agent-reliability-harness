"""W4-b 装置校准：零成本证明「输出级故障注入能制造 [GUARD-REJECT] 分母」。

W4 首轮的教训是分母为 0 白跑一批：真实模型在正常路径下不产生四类输出级
故障，护栏一次都没触发。W4-b 给 openai 后端接上注入器后，**必须先离线证明
装置确实能提出问题**，再花任何钱。

本模块零网络、零 API 请求、不判分、不写归档：把任务脚本**过滤掉固有缺陷**
后当作「一个正常工作的模型」，套上 ``OutputFaultBackend``，用
``loop.run_turn`` 跑一遍，再把 span 喂回 W4 首轮那把 ``eval.guard_probe`` 尺子。

为什么必须过滤：任务集里故意留了 6 类固有缺陷（坏 JSON / 错工具 / 缺参数 /
超退…），它们在 W3 里本身就会触发护栏。若直接拿原始脚本校准，数到的
guard span 分不清是「注入制造的」还是「脚本本来就坏的」——实测 fpm=60 时
注入 0 次却数到 7 次拒绝，正是这个混淆。校准脚本只保留合法工具调用，
因此 **OFF 组必须是 0 个 guard span**，ALL4 组多出来的全部来自注入。

**这不是实验结论**。ScriptedBackend 按 cursor 前进，不会真正理解拒绝消息；
这里的 responsive 数字只证明「注入链路接得上、探针数得到、四类都能分类」。
真实模型能否读懂 ``[GUARD-REJECT]``，只能由 W4-b 两格实测回答。

真实跑批的结果分析不在这里：直接用
``python -m eval.guard_probe --traces-dir ... --label ...`` 复用同一把尺子。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from env import db, tools
from eval import guard_probe
from runtime import guard, loop
from runtime.model import OutputFaultBackend, ScriptedBackend

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TASKS = REPO_ROOT / "tasks" / "mvp_10.jsonl"
DEFAULT_OUT = REPO_ROOT / "experiments" / "w4b_probe_dryrun.md"

# 与 W4-b 两格配置保持一致。W3 归因实验用 60；这里取 300 是因为真实模型
# 的动作序列短（首轮实测平均约 3 步），注入机会远少于脚本后端，需要更高
# 注入率才能在 30 trial 上覆盖四类护栏。这是装置参数，不是模型能力轴。
DEFAULT_FPM = 300
DEFAULT_SEED = 11
DEFAULT_K = 3

# 校准脚本末尾的占位 final：不参与判分，也不是任务的目标话术。
PLACEHOLDER_FINAL = "完成"


def clean_script(task: dict[str, Any]) -> list[str]:
    """只保留合法工具调用，末尾补一个占位 final。

    过滤条件与工具层同源：工具名在 TOOL_SCHEMAS 里、args 通过
    validate_args。业务策略违规工具层不拦，会保留并成功执行，不产生 guard
    span，因此不构成混淆源。
    """
    actions: list[str] = []
    seen_writes: set[str] = set()
    for item in task.get("script", ()):
        obj: Any = item
        if isinstance(item, str):
            try:
                obj = json.loads(item)
            except (ValueError, TypeError):
                continue
        if not isinstance(obj, dict) or "tool" not in obj:
            continue
        name, args = obj["tool"], obj.get("args", {})
        if not isinstance(name, str) or name not in tools.TOOL_SCHEMAS:
            continue
        if tools.validate_args(name, args) is not None:
            continue
        text = json.dumps(
            {"tool": name, "args": args},
            ensure_ascii=False,
            sort_keys=True,
        )
        # 脚本里成对的重复写动作也是固有缺陷（W3 的 idempotency 修的就是
        # 它），留着会额外制造 guard span，让校准数不清注入的贡献。
        if name in tools.WRITE_TOOLS:
            if text in seen_writes:
                continue
            seen_writes.add(text)
        actions.append(text)
    actions.append(
        json.dumps({"final": PLACEHOLDER_FINAL}, ensure_ascii=False, sort_keys=True)
    )
    return actions


def dry_run(
    tasks_path: Path,
    guards: Sequence[str],
    *,
    seed: int = DEFAULT_SEED,
    fault_per_mille: int = DEFAULT_FPM,
    k: int = DEFAULT_K,
) -> dict[str, Any]:
    """跑一遍注入后的单轮循环，返回探针事件与装置级计数。"""
    tasks = [
        json.loads(line)
        for line in Path(tasks_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events: list[guard_probe.RejectionEvent] = []
    guard_spans = 0
    injected: list[tuple[int, str]] = []
    for task in tasks:
        template = db.build_template(task["initial_db"])
        script = clean_script(task)
        for trial in range(1, k + 1):
            conn = db.reset(template)
            backend = OutputFaultBackend(
                ScriptedBackend(script),
                seed=seed,
                fault_per_mille=fault_per_mille,
                task_id=task["id"],
                trial=trial,
            )
            messages = [{"role": "user", "content": task["user_utterances"][0]}]
            guard_state = (
                guard.GuardState() if guard.GUARD_IDEMPOTENCY in guards else None
            )
            result = loop.run_turn(
                messages,
                backend,
                conn,
                trial_id=f"{task['id']}-t{trial}",
                counter=loop.StepCounter(),
                budget=guard.StepBudget(),
                guards=tuple(guards),
                guard_state=guard_state,
            )
            events.extend(guard_probe.probe_spans(result.spans))
            guard_spans += sum(
                1 for span in result.spans if span["role"] == "guard"
            )
            injected.extend(backend.injected)
            conn.close()
        template.close()
    return {
        "events": events,
        "guard_spans": guard_spans,
        "injected": injected,
        "task_count": len(tasks),
        "trial_count": len(tasks) * k,
    }


def render(
    off: dict[str, Any],
    on: dict[str, Any],
    *,
    seed: int,
    fault_per_mille: int,
    k: int,
) -> str:
    off_s = guard_probe.summarize(off["events"])
    on_s = guard_probe.summarize(on["events"])
    kinds = sorted({kind for _, kind in on["injected"]})
    lines = [
        "# W4-b 前置：注入装置离线校准（零成本、零网络）",
        "",
        "> 由 `python -m eval.w4b_probe` 生成。**全程没有发起任何模型 API 请求，",
        "> 没有花一分钱，不跑判分、不改任何归档。**",
        "> **这些数字只证明装置接得上，不是 W4-b 的实验结论。** 校准用的是",
        "> `ScriptedBackend + OutputFaultBackend`：脚本后端按 cursor 前进、不会理解",
        "> 拒绝消息，它的 responsive 不是语言模型的表现。真实模型能否读懂",
        "> `[GUARD-REJECT]`，只能由 W4-b 两格实测的探针回答。",
        "",
        f"校准输入：`tasks/mvp_10.jsonl`（{off['task_count']} 任务 × k={k} = "
        f"{off['trial_count']} trial、单轮）、`seed={seed}`、"
        f"`fault_per_mille={fault_per_mille}`，两格只差 `[guards].enabled`。",
        "",
        "> **校准脚本先过滤掉了任务集里的 6 类固有缺陷**（含成对的重复写动作），"
        "只保留合法工具调用，末尾补一个不参与判分的占位 `final`。"
        "因此 OFF 组必须为 0 个 guard span，ALL4 组数到的拒绝只能来自注入。"
        "CLI 另跑一组 `fpm=0` 的 ALL4 基准并**强制断言其 guard span 为 0**——"
        "这是「干净脚本本身不触发护栏」的机器证明。不过滤时 fpm=60 会"
        "「注入 0 次却数到 7 次拒绝」，那是脚本固有缺陷，不是装置生效。",
        "",
        "## 装置是否成立",
        "",
        "| 组 | 注入次数 | guard span | 拒绝数 | responsive |",
        "| --- | --- | --- | --- | --- |",
        f"| OFF（护栏全关） | {len(off['injected'])} | {off['guard_spans']} | "
        f"{off_s['total']} | {off_s['responsive']} |",
        f"| ALL4（四条全开） | {len(on['injected'])} | {on['guard_spans']} | "
        f"{on_s['total']} | {on_s['responsive']} |",
        "",
        "- OFF 组必须是 **0 个 guard span / 0 次拒绝**；ALL4 组必须 > 0，"
        "否则注入器与护栏没接上，不准发真实调用。",
        "- **本校准里 responsive 恒等于拒绝数，是脚本后端的机械性质**：",
        "  `ScriptedBackend` 被拒后总返回脚本下一项，必然「合法且不同」。",
        "  它**不预示真实模型的 responsive 率**，也不与 W4-b 实测值相减或比较。",
        f"- 本次注入覆盖的故障类型：`{', '.join(kinds) or '无'}`"
        "（四类见 `runtime/model.py` 的 `SEEDED_FAULTS`）。",
        f"- ALL4 拒绝护栏类型分布：`{', '.join(on_s['kinds_seen']) or '无'}`。",
        "- 两格注入次数可能不同：护栏拦下后会多出几次「响应拒绝」的 generate，",
        "  每次都消耗 cursor 且按协议跳过注入。这是护栏的处理效应，不是自变量不同。",
        "",
        "## 按护栏类型拆分（只读探针，同一把尺子）",
        "",
        "| 护栏 | 拒绝数 | responsive | 后续 tool 成功 |",
        "| --- | --- | --- | --- |",
    ]
    for kind, stats in on_s["by_kind"].items():
        if not stats["total"]:
            continue
        lines.append(
            f"| `{kind}` | {stats['total']} | {stats['responsive']} | "
            f"{stats['followup_tool_ok']} |"
        )
    lines += [
        "",
        "## 口径与边界",
        "",
        "- 注入参数 `fault_per_mille` 是**装置参数**，不是模型能力轴；W3 归因实验",
        "  用 60，这里为在较短的动作序列上取得足够分母而另设，",
        "  **两组的任何数字都不可相减、不可拼曲线**。",
        "- `responsive` 只是输出层面的机制证据，不是任务成功；论效果只认 `pass^k`，",
        "  而 W4-b 的主读数是探针分母，**不是** `pass^k`。",
        "- 注入只替换模型输出层的文本；终态判分、任务集、护栏实现、grader 全部未改。",
        "- 真实跑批的产物分析用 `python -m eval.guard_probe --traces-dir`，",
        "  与本校准共用 `probe_spans` / `summarize`，不存在两把尺子。",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.w4b_probe",
        description="W4-b 注入装置离线校准：零 API 请求，证明探针有分母",
    )
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--fault-per-mille", type=int, default=DEFAULT_FPM)
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    common = dict(
        tasks_path=args.tasks,
        seed=args.seed,
        fault_per_mille=args.fault_per_mille,
        k=args.k,
    )
    off = dry_run(guards=(), **common)
    on = dry_run(guards=guard.GUARD_NAMES, **common)
    # fpm=0 基准：干净脚本 + 四条全开必须 0 guard span，否则 ALL4 组数到的
    # 拒绝里混了脚本固有缺陷，「分母来自注入」这句话就不成立。
    baseline = dry_run(
        guards=guard.GUARD_NAMES, **{**common, "fault_per_mille": 0}
    )
    if baseline["guard_spans"] or baseline["events"]:
        raise SystemExit(
            "校准失败：fpm=0 基准组仍出现 guard span，说明干净脚本混入了固有缺陷"
        )
    if off["guard_spans"] or off["events"]:
        raise SystemExit("校准失败：护栏全关时不应出现 guard span / 拒绝事件")
    if not on["injected"] or not on["guard_spans"] or not on["events"]:
        raise SystemExit("校准失败：全开组没数到拒绝，注入装置没接上")
    report = render(
        off, on, seed=args.seed, fault_per_mille=args.fault_per_mille, k=args.k
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(report.encode("utf-8"))
    print(f"已写出：{args.out.as_posix()}")
    print(
        f"  OFF : 注入 {len(off['injected'])}，guard span {off['guard_spans']}"
    )
    print(
        f"  ALL4: 注入 {len(on['injected'])}，guard span {on['guard_spans']}，"
        f"拒绝 {len(on['events'])}，responsive "
        f"{guard_probe.summarize(on['events'])['responsive']}"
    )
    print("本命令未发起任何 API 请求，未花费任何费用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
