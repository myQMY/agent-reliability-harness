"""W4 真实模型预算：离线成本估算 + 运行时硬闸门。

零第三方依赖、零网络、不读取密钥。做两件事：

1. ``estimate_plan`` 从任务集与实验配置推导**调用次数上界**
   （任务数 × k × MAX_STEPS × 实验格数），再乘使用者注入的单价给出费用。
   单价是 CLI 参数，本模块不硬编码任何服务商价格——价格会变，且不同
   服务商的 input / output / 缓存命中间口径不同，写死就会变成假事实。
2. ``BudgetedBackend`` 把预算变成运行时约束：整个 run 共享一份
   ``BudgetLedger``，在**每次模型调用之前**检查调用数 / token 数 / 估算费用，
   超限立即抛 ``BudgetExceeded``，停止继续产生费用。

口径边界：估算是上界而不是预测（多数 trial 会在 MAX_STEPS 之前结束）；
闸门在调用边界检查，因此最多多花一次已经发出的调用；脚本 / 模拟器
后端不产生真实调用，默认不包裹，既有留档逐字节不变。
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.model import Backend, ModelReply


class BudgetExceeded(RuntimeError):
    """预算闸门触发；调用方应立即停止，不要重试。"""


@dataclass(frozen=True)
class PriceCard:
    """每百万 token 的单价。currency 只用于展示，不做汇率换算。"""

    input_per_million: float
    output_per_million: float
    currency: str = "CNY"

    def validate(self) -> None:
        if self.input_per_million < 0 or self.output_per_million < 0:
            raise ValueError("单价不得为负")
        if not (self.input_per_million or self.output_per_million):
            raise ValueError("input / output 单价不得同时为 0")
        if not self.currency:
            raise ValueError("currency 不得为空")

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens / 1_000_000 * self.input_per_million
            + completion_tokens / 1_000_000 * self.output_per_million
        )


@dataclass(frozen=True)
class BudgetLimit:
    """一次真实模型跑批的硬上限；至少启用一项。"""

    max_calls: int | None = None
    max_total_tokens: int | None = None
    max_cost: float | None = None
    price: PriceCard | None = None

    def validate(self) -> None:
        if self.max_calls is None and self.max_total_tokens is None and self.max_cost is None:
            raise ValueError("BudgetLimit 至少要启用一项上限")
        for name, value in (
            ("max_calls", self.max_calls),
            ("max_total_tokens", self.max_total_tokens),
            ("max_cost", self.max_cost),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} 必须为正数")
        if self.max_cost is not None and self.price is None:
            raise ValueError("设置 max_cost 必须同时提供 price")
        if self.price is not None:
            self.price.validate()


@dataclass
class BudgetLedger:
    """跨整个 run 共享的累计器；per-trial 后端各自新建，累计不能跟着重置。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost(self, price: PriceCard) -> float:
        return price.cost(self.prompt_tokens, self.completion_tokens)


class BudgetedBackend:
    """Backend 装饰器：调用前检查、调用后记账，超限 fail-fast。

    ``usage`` 缺失时 token 记为 0（与 OpenAIBackend 的降级口径一致），
    此时 token / 费用闸门不再可靠，**调用数闸门仍然生效**——这是为什么
    ``eval.run`` 在真实模型路径下强制要求显式 ``--max-calls``。
    """

    def __init__(self, inner: Backend, ledger: BudgetLedger, limit: BudgetLimit):
        limit.validate()
        self._inner = inner
        self._ledger = ledger
        self._limit = limit

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    def _check(self) -> None:
        limit, ledger = self._limit, self._ledger
        if limit.max_calls is not None and ledger.calls >= limit.max_calls:
            raise BudgetExceeded(
                f"调用数达到上限 {limit.max_calls}，已停止；"
                f"累计 {ledger.calls} 次调用 / {ledger.tokens} tokens"
            )
        if (
            limit.max_total_tokens is not None
            and ledger.tokens >= limit.max_total_tokens
        ):
            raise BudgetExceeded(
                f"token 达到上限 {limit.max_total_tokens}，已停止；"
                f"累计 {ledger.calls} 次调用 / {ledger.tokens} tokens"
            )
        if limit.max_cost is not None and limit.price is not None:
            spent = ledger.cost(limit.price)
            if spent >= limit.max_cost:
                raise BudgetExceeded(
                    f"估算费用达到上限 {limit.max_cost:.4f} {limit.price.currency}，"
                    f"已停止；累计 {ledger.calls} 次调用 / {ledger.tokens} tokens"
                )

    def generate(self, messages: list[dict[str, str]]) -> ModelReply:
        self._check()
        reply = self._inner.generate(messages)
        self._ledger.calls += 1
        self._ledger.prompt_tokens += reply.prompt_tokens
        self._ledger.completion_tokens += reply.completion_tokens
        return reply


def count_tasks(path: Path) -> int:
    """数非空行；不解析 JSON，避免与 runner 形成循环导入。"""
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def estimate_plan(
    *,
    task_count: int,
    k: int,
    max_steps: int,
    avg_prompt_tokens: int,
    avg_completion_tokens: int,
    cells: int = 1,
    price: PriceCard | None = None,
) -> dict[str, Any]:
    """按**最坏情况**估算：每个 trial 都跑满 MAX_STEPS。

    返回值里的 calls 是上界，不是期望值；avg_* 是使用者给的单次调用
    平均量级假设，不是实测。真实用量要等跑完从响应的 usage 读取。
    """
    for name, value in (
        ("task_count", task_count),
        ("k", k),
        ("max_steps", max_steps),
        ("avg_prompt_tokens", avg_prompt_tokens),
        ("avg_completion_tokens", avg_completion_tokens),
        ("cells", cells),
    ):
        if value <= 0:
            raise ValueError(f"{name} 必须为正数")
    if price is not None:
        price.validate()

    calls = task_count * k * max_steps * cells
    prompt = calls * avg_prompt_tokens
    completion = calls * avg_completion_tokens
    plan: dict[str, Any] = {
        "task_count": task_count,
        "k": k,
        "max_steps": max_steps,
        "cells": cells,
        "avg_prompt_tokens": avg_prompt_tokens,
        "avg_completion_tokens": avg_completion_tokens,
        "calls_upper_bound": calls,
        "prompt_tokens_upper_bound": prompt,
        "completion_tokens_upper_bound": completion,
        "total_tokens_upper_bound": prompt + completion,
    }
    if price is not None:
        plan["currency"] = price.currency
        plan["input_cost"] = price.cost(prompt, 0)
        plan["output_cost"] = price.cost(0, completion)
        plan["total_cost"] = price.cost(prompt, completion)
    return plan


def render_estimate(plan: dict[str, Any], *, sources: list[str]) -> str:
    """渲染成可 git diff 的 Markdown；不含时间戳、绝对路径或机器信息。"""
    lines = [
        "# W4 真实模型预算估算（离线上界）",
        "",
        "> 本表由 `python -m eval.budget` 生成，**没有发起任何网络请求**。",
        "> 调用次数是「每个 trial 都跑满 MAX_STEPS」的最坏情况上界，"
        "不是期望值；单价由命令行注入，价格会变，以服务商账单为准。",
        "",
        "## 输入",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 任务集 | {', '.join(f'`{s}`' for s in sources)} |",
        f"| 任务数（合计） | {plan['task_count']} |",
        f"| k | {plan['k']} |",
        f"| MAX_STEPS | {plan['max_steps']} |",
        f"| 实验格数 | {plan['cells']} |",
        f"| 单次调用平均 prompt tokens（假设） | {plan['avg_prompt_tokens']} |",
        f"| 单次调用平均 completion tokens（假设） | {plan['avg_completion_tokens']} |",
        "",
        "## 上界",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        f"| 模型调用次数上界 | {plan['calls_upper_bound']} |",
        f"| prompt tokens 上界 | {plan['prompt_tokens_upper_bound']} |",
        f"| completion tokens 上界 | {plan['completion_tokens_upper_bound']} |",
        f"| 合计 tokens 上界 | {plan['total_tokens_upper_bound']} |",
    ]
    if "total_cost" in plan:
        currency = plan["currency"]
        lines += [
            f"| input 费用上界 | {plan['input_cost']:.4f} {currency} |",
            f"| output 费用上界 | {plan['output_cost']:.4f} {currency} |",
            f"| **合计费用上界** | **{plan['total_cost']:.4f} {currency}** |",
        ]
    lines += [
        "",
        "## 使用方式",
        "",
        f"- 把 `--max-calls {plan['calls_upper_bound']}` 传给 `python -m eval.run`，"
        "闸门会在达到该调用数时停止，不会静默继续烧钱。",
        "- 实际 trial 通常在 4~8 步就给出 final 或触发终止，实际调用数会明显低于上界；"
        "跑完请如实记录 `usage` 实测值，不要用本表当实测成本。",
        "- 若模型开启 thinking mode，completion tokens 可能远高于假设值，"
        "此时应以 `--max-cost` 配合单价一起兜底。",
        "",
    ]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.budget",
        description="离线估算 W4 真实模型跑批的调用次数与费用上界（不联网、不花钱）",
    )
    parser.add_argument(
        "--tasks", action="append", type=Path, default=None,
        help="任务集路径，可重复传入；只数非空行",
    )
    parser.add_argument("--task-count", type=int, default=None, help="直接给任务数")
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--cells", type=int, default=2, help="实验格数（off / all4）")
    parser.add_argument("--avg-prompt", type=int, default=1500)
    parser.add_argument("--avg-completion", type=int, default=200)
    parser.add_argument("--price-in", type=float, default=None, help="每百万 input tokens 单价")
    parser.add_argument("--price-out", type=float, default=None, help="每百万 output tokens 单价")
    parser.add_argument("--currency", default="CNY")
    parser.add_argument("--out", type=Path, default=None, help="写出 Markdown；不传则打印到 stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sources: list[str] = []
    if args.tasks:
        task_count = 0
        for path in args.tasks:
            task_count += count_tasks(path)
            sources.append(path.as_posix())
    else:
        task_count = args.task_count if args.task_count is not None else 10
        sources.append(f"直接指定 task_count={task_count}")
    price = None
    if args.price_in is not None or args.price_out is not None:
        if args.price_in is None or args.price_out is None:
            raise SystemExit("--price-in 与 --price-out 必须同时给出")
        price = PriceCard(args.price_in, args.price_out, args.currency)
    plan = estimate_plan(
        task_count=task_count,
        k=args.k,
        max_steps=args.max_steps,
        avg_prompt_tokens=args.avg_prompt,
        avg_completion_tokens=args.avg_completion,
        cells=args.cells,
        price=price,
    )
    report = render_estimate(plan, sources=sources)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(report.encode("utf-8"))
        print(f"已写出：{args.out.as_posix()}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
