"""W4 跑批前离线预检：把执行清单里靠人肉记住的约束变成机器校验。

零网络、零 API 请求、不读取密钥值、不跑模型、不判分、不改任何归档。
只回答一个问题：现在按 ``docs/w4-design.md`` §5 按下那几条命令，会不会
因为配置问题静默产出一份「没有 A/B 对照」的假报告，或者把预算闸门的
作用域理解错。

三态语义（报告里必须能区分「写错了」和「还没轮到」）：

- ``PASS``：条件满足。
- ``FAIL``：配置 / 代码写错，必须修，跑批前就该拦住。
- ``BLOCKED``：外部输入尚未到位（``.env``、OFF 组留档），不是错误。

任何 FAIL 或 BLOCKED 都意味着 NOT READY；``--strict`` 下返回非零退出码。
本模块产出的是**环境状态快照**，每次运行覆盖更新，不属于冻结归档，
也不含任何模型分数 / 实测 token / latency。
"""
from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from eval import analyze, budget
from eval import config as exp_config
from eval import run as cli
from runtime import guard

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OFF_CONFIG = REPO_ROOT / "experiments" / "w4_off_flash.toml"
DEFAULT_ON_CONFIG = REPO_ROOT / "experiments" / "w4_all4_flash.toml"
DEFAULT_OUT = REPO_ROOT / "experiments" / "w4_preflight.md"

# 与 runtime.model.config_from_env 实际读取的键一一对应；只读键名，不读值。
REQUIRED_ENV_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")

# 两个已提交 fixture 的 sha256，与 tests/test_w4_readiness.py 钉死的常量一致。
TASKSET_SHA256 = {
    "mvp_10.jsonl": "0a3e2af34af872f1c26e976585b961f86458b1ec48368134bc5229bde26b62da",
    "mvp_30.jsonl": "33d25c59410268c5d7417237871b9c750a98499f70883e4c1458bde59108a255",
}

# §5 建议的两格产物目录。trace 文件名是 <trial_id>.jsonl，两格共用目录会
# 互相覆盖，纠正探针随后会读到错的那一组；也不能退回默认 results / traces。
DEFAULT_DIRS: dict[str, str] = {
    "off_results": "results/w4_off",
    "off_traces": "traces/w4_off",
    "on_results": "results/w4_all4",
    "on_traces": "traces/w4_all4",
}
SHARED_DIRS = ("results", "traces")

# 与 eval.budget 的默认假设一致：单次调用 1500 prompt / 200 completion tokens。
AVG_PROMPT_TOKENS = 1500
AVG_COMPLETION_TOKENS = 200
CELLS = 2

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"

_MEASURED_FIELDS = ("tasks", "k", "backend", "max_steps", "max_json_retries")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def overall(checks: Sequence[Check]) -> str:
    """任何一项没过就不能开跑；先区分「写错了」与「还在等」。"""
    if any(item.status == FAIL for item in checks):
        return "NOT READY — 配置错误"
    if any(item.status == BLOCKED for item in checks):
        return "NOT READY — 等待外部输入"
    return "READY"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_checks(
    *,
    off_config: Path = DEFAULT_OFF_CONFIG,
    on_config: Path = DEFAULT_ON_CONFIG,
    env_file: Path | None = None,
    dirs: dict[str, str] | None = None,
    max_calls: int | None = None,
    max_total_tokens: int | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
    currency: str = "USD",
) -> tuple[list[Check], dict[str, Any]]:
    """逐项静态检查；不发起任何网络请求，也不写文件。"""
    checks: list[Check] = []
    ctx: dict[str, Any] = {}
    chosen_dirs = dict(DEFAULT_DIRS if dirs is None else dirs)
    env_path = REPO_ROOT / ".env" if env_file is None else Path(env_file)

    # 1) 配置可读——后续每一项都依赖它，读不了就直接收工。
    try:
        off = exp_config.load_config(Path(off_config))
        on = exp_config.load_config(Path(on_config))
    except (OSError, ValueError) as exc:
        checks.append(Check("两份 W4 配置可读且字段合法", FAIL, f"加载失败：{exc}"))
        return checks, ctx
    checks.append(
        Check(
            "两份 W4 配置可读且字段合法",
            PASS,
            "validate() 通过；`w4_off_flash.toml` / `w4_all4_flash.toml`",
        )
    )

    # 2) 单变量：测量链路上的字段必须逐字相同。
    mismatched = [
        field for field in _MEASURED_FIELDS
        if getattr(off, field) != getattr(on, field)
    ]
    checks.append(
        Check(
            "两格只差 [guards].enabled（单变量）",
            FAIL if mismatched else PASS,
            f"测量字段不一致：{mismatched}" if mismatched
            else "tasks / k / backend / max_steps / max_json_retries 全部相同",
        )
    )

    # 3) 真实模型后端——W4 的定义就是接 OpenAIBackend。
    both_openai = off.backend == on.backend == "openai"
    checks.append(
        Check(
            "两格后端均为 openai",
            PASS if both_openai else FAIL,
            f"off={off.backend}, all4={on.backend}" if not both_openai
            else "确定性后端不产生真实调用，不能用来验证 [GUARD-REJECT]",
        )
    )

    # 4) 护栏开关：OFF 全关、ALL4 四条全开。
    guards_ok = off.guards == () and on.guards == guard.GUARD_NAMES
    checks.append(
        Check(
            "护栏为唯一自变量（OFF 空 / ALL4 全开）",
            PASS if guards_ok else FAIL,
            f"off={list(off.guards)}, all4={list(on.guards)}" if not guards_ok
            else f"ALL4 = {list(guard.GUARD_NAMES)}",
        )
    )

    # 5) run_id 不撞车，否则第二格会覆盖第一格的报告。
    off_rid = exp_config.run_id(off)
    on_rid = exp_config.run_id(on)
    ctx["off_run_id"] = off_rid
    ctx["on_run_id"] = on_rid
    checks.append(
        Check(
            "两格 run_id 不同（留档不会互相覆盖）",
            PASS if off_rid != on_rid else FAIL,
            f"{off_rid} / {on_rid}",
        )
    )

    # 6) compare 链接：硬编码字符串必须等于按 OFF run_id 推导出的留档路径，
    #    且绝不是 W1 冻结基线（那是 scripted 后端，跨后端不能作因果解读）。
    expected = f"experiments/runs/{off_rid}/result_rows.jsonl"
    ctx["expected_compare"] = expected
    compare_ok = (
        off.compare == ""
        and on.compare == expected
        and on.compare != analyze.FROZEN_COMPARE
    )
    checks.append(
        Check(
            "ALL4.compare 精确指向同后端 OFF 留档",
            PASS if compare_ok else FAIL,
            f"期望 `{expected}`，实际 `{on.compare}`；OFF.compare=`{off.compare}`"
            if not compare_ok else expected,
        )
    )

    # 7) OFF 留档是否已生成：没跑 OFF 就跑 ALL4，run.py 现在会明确拒绝
    #    （过去是静默跳过 A/B 表）。这里提前如实标成 BLOCKED 而非错误。
    archive = exp_config.resolve_input(on.compare)
    if archive.is_file():
        checks.append(Check("OFF 组留档已生成", PASS, f"`{on.compare}` 存在"))
    else:
        checks.append(
            Check(
                "OFF 组留档已生成",
                BLOCKED,
                f"`{on.compare}` 不存在；按 §5 先跑对照组，再跑实验组",
            )
        )

    # 8) 产物目录不冲突。
    values = list(chosen_dirs.values())
    conflict = len(set(values)) != len(values) or any(
        value in SHARED_DIRS for value in values
    )
    checks.append(
        Check(
            "两格 results / traces 目录互不冲突",
            FAIL if conflict else PASS,
            f"目录重复或退回默认目录：{chosen_dirs}" if conflict
            else " / ".join(f"{k}={v}" for k, v in chosen_dirs.items()),
        )
    )

    # 9) 已提交 fixture 未被改动（用户明确要求：一律不改两个任务集）。
    for name, expected_sha in TASKSET_SHA256.items():
        path = REPO_ROOT / "tasks" / name
        actual = _digest(path) if path.is_file() else "文件缺失"
        checks.append(
            Check(
                f"任务集 {name} sha256 未变",
                PASS if actual == expected_sha else FAIL,
                "与守门测试钉死的常量一致" if actual == expected_sha
                else f"期望 {expected_sha}，实际 {actual}",
            )
        )

    # 10) 密钥文件：文件不存在属于「还没轮到」；存在却缺键是写错了。
    #     只检查键名，绝不把值写进报告或 stdout。
    if not env_path.is_file():
        checks.append(
            Check(
                ".env 已配置三个必需键",
                BLOCKED,
                "`.env` 不存在；复制 `.env.example` 并填 "
                + " / ".join(REQUIRED_ENV_KEYS),
            )
        )
    else:
        values = cli.load_dotenv(env_path)
        missing = [key for key in REQUIRED_ENV_KEYS if not values.get(key)]
        checks.append(
            Check(
                ".env 已配置三个必需键",
                FAIL if missing else PASS,
                f"缺少或为空：{missing}" if missing
                else "三个键均已设置（值不读取、不展示）",
            )
        )

    # 11) 预算：分别算「单格」与「两格合计」，并讲清闸门作用域。
    tasks_path = exp_config.resolve_input(off.tasks)
    task_count = budget.count_tasks(tasks_path)
    price = None
    if price_in is not None or price_out is not None:
        if price_in is None or price_out is None:
            price = None
        else:
            price = budget.PriceCard(price_in, price_out, currency)
    common = dict(
        task_count=task_count,
        k=off.k,
        max_steps=off.max_steps,
        avg_prompt_tokens=AVG_PROMPT_TOKENS,
        avg_completion_tokens=AVG_COMPLETION_TOKENS,
        price=price,
    )
    per_cell = budget.estimate_plan(cells=1, **common)
    total = budget.estimate_plan(cells=CELLS, **common)
    ctx["per_cell"] = per_cell
    ctx["total"] = total
    ctx["max_calls"] = max_calls

    problems: list[str] = []
    if max_calls is not None and max_calls < per_cell["calls_upper_bound"]:
        problems.append(
            f"--max-calls {max_calls} < 单格上界 {per_cell['calls_upper_bound']}"
        )
    if (
        max_total_tokens is not None
        and max_total_tokens < per_cell["total_tokens_upper_bound"]
    ):
        problems.append(
            f"--max-total-tokens {max_total_tokens} < 单格上界 "
            f"{per_cell['total_tokens_upper_bound']}"
        )
    detail = (
        f"单格上界 {per_cell['calls_upper_bound']} 次 / "
        f"{per_cell['total_tokens_upper_bound']} tokens；"
        f"两格计划合计 {total['calls_upper_bound']} 次 / "
        f"{total['total_tokens_upper_bound']} tokens。"
    )
    if max_calls is not None:
        detail += (
            f" `--max-calls {max_calls}` 是**单次 eval.run** 的闸门，两格各自独立，"
            f"合计硬上限 {max_calls * CELLS} 次（不是总预算 {total['calls_upper_bound']}）。"
        )
    else:
        detail += f" 建议每格传 `--max-calls {total['calls_upper_bound']}`（留 2× 余量）。"
    checks.append(Check("预算闸门覆盖单格上界", FAIL if problems else PASS,
                        "；".join(problems) if problems else detail))

    return checks, ctx


def render_report(checks: Sequence[Check], ctx: dict[str, Any]) -> str:
    """可 git diff 的 Markdown；不含时钟、绝对路径、密钥值或模型分数。"""
    status = overall(checks)
    counts = {
        key: sum(1 for item in checks if item.status == key)
        for key in (PASS, FAIL, BLOCKED)
    }
    lines = [
        "# W4 跑批前预检（就绪度快照）",
        "",
        "> 由 `python -m eval.w4_preflight` 生成。**本命令不发起任何 API 请求、不花一分钱、"
        "不跑模型、不判分。**",
        "> 本文件是环境 / 配置状态快照，每次运行覆盖更新；**不属于冻结归档，"
        "也不是 W4 的实验结果**，不含任何模型分数、实测 token 或 latency。",
        "",
        f"整体状态：**{status}**"
        f"（{counts[FAIL]} FAIL / {counts[BLOCKED]} BLOCKED / {counts[PASS]} PASS）",
        "",
        "| # | 检查项 | 状态 | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for index, item in enumerate(checks, 1):
        lines.append(
            f"| {index} | {item.name} | {item.status} | "
            f"{item.detail.replace('|', '\\|')} |"
        )
    lines += ["", "## 预算作用域（最容易被读错的一条）", ""]
    total = ctx.get("total")
    per_cell = ctx.get("per_cell")
    if total and per_cell:
        lines += [
            f"- 实验计划总量（两格）= `{total['calls_upper_bound']}` 次调用、"
            f"`{total['total_tokens_upper_bound']}` tokens 上界。",
            f"- 单次 `eval.run` 的上界 = `{per_cell['calls_upper_bound']}` 次调用、"
            f"`{per_cell['total_tokens_upper_bound']}` tokens。",
            f"- `--max-calls` 作用于**单次 `eval.run`**，两格是两次独立进程、"
            f"各有一份账本；传 `{total['calls_upper_bound']}` 时合计硬上限是 "
            f"`{total['calls_upper_bound'] * CELLS}`，**不是总预算 720 次**。"
            "闸门保证的是「单格不失控」，不是「整轮不超某个总额」。",
            "- 估算用上界不是预测；`avg_prompt` / `avg_completion` 是假设不是实测；"
            "单价由命令行注入，代码不硬编码任何服务商价格。",
        ]
    lines += [
        "",
        "## 三态口径",
        "",
        "- `FAIL`：配置或代码写错，必须先修；`--strict` 会以此返回非零退出码。",
        "- `BLOCKED`：外部输入尚未到位（`.env` 未填、OFF 组留档未生成），不是错误。",
        "- `PASS`：该项就绪，**不等于 W4 可以开跑**——只有整体 `READY` 才是。",
        "",
        "## 跑批前必须补齐",
        "",
    ]
    pending = [item for item in checks if item.status != PASS]
    if pending:
        for item in pending:
            lines.append(f"- [{item.status}] {item.name}：{item.detail}")
    else:
        lines.append("- 无。全部检查通过；按 `docs/w4-design.md` §5 执行命令。")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.w4_preflight",
        description="W4 跑批前离线预检：零 API 请求、零花费、只读配置与文件状态",
    )
    parser.add_argument("--off-config", type=Path, default=DEFAULT_OFF_CONFIG)
    parser.add_argument("--on-config", type=Path, default=DEFAULT_ON_CONFIG)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--strict", action="store_true",
        help="非 READY 时返回非零退出码（供跑批脚本串联）",
    )
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--price-in", type=float, default=None)
    parser.add_argument("--price-out", type=float, default=None)
    parser.add_argument("--currency", default="USD")
    for key, default in DEFAULT_DIRS.items():
        parser.add_argument(f"--{key.replace('_', '-')}", default=default)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dirs = {key: getattr(args, key) for key in DEFAULT_DIRS}
    checks, ctx = run_checks(
        off_config=args.off_config,
        on_config=args.on_config,
        env_file=args.env_file,
        dirs=dirs,
        max_calls=args.max_calls,
        max_total_tokens=args.max_total_tokens,
        price_in=args.price_in,
        price_out=args.price_out,
        currency=args.currency,
    )
    report = render_report(checks, ctx)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_bytes(report.encode("utf-8"))
    status = overall(checks)
    print(f"W4 预检：{status}")
    for item in checks:
        print(f"  [{item.status:7s}] {item.name}")
    print(f"报告：{Path(args.out).as_posix()}")
    print("本命令未发起任何 API 请求，未花费任何费用。")
    return 0 if status == "READY" or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
