"""CLI 入口：一条命令跑完全流程并产出证据。

默认零依赖、零成本、确定性：
    python -m eval.run --tasks tasks/mvp_10.jsonl --k 3

实验输入以 experiments/config.toml 为唯一事实来源，CLI 显式参数只覆盖对应字段；
最终生效配置决定 run_id。不传 --report 时留档到 <runs-dir>/<run_id>/
（report.md + config.json + result_rows.jsonl）；冻结基线 experiments/mvp_baseline.md 永不被写入。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from eval import analyze, budget
from eval import config as exp_config
from eval import runner, trace
from runtime.guard import GUARD_NAMES
from runtime.model import BackendConfig, config_from_env

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN_BASELINE = REPO_ROOT / "experiments" / "mvp_baseline.md"


def load_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 读取，避免为三个变量引入第三方依赖。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.run",
        description="跑一遍评测，产出结果行、全链路 trace、留档配置与 Markdown 结果表",
    )
    parser.add_argument(
        "--config", type=Path, default=exp_config.DEFAULT_CONFIG_PATH
    )
    # None 表示「未指定」，由配置文件决定；一旦覆盖就会改变 run_id
    parser.add_argument("--tasks", type=Path, default=None)
    parser.add_argument("--k", type=int, default=None)
    parser.add_argument(
        "--backend", choices=("scripted", "openai", "seeded"), default=None
    )
    # 仅 backend = seeded 时生效；不传则由 [seeded] 配置段决定
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--fault-per-mille", type=int, default=None)
    # W3 起的可开关护栏；与 [guards].enabled 合并去重，白名单由 choices 与 validate 双重把守
    parser.add_argument(
        "--guard", action="append", choices=GUARD_NAMES, default=None
    )
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--traces-dir", type=Path, default=Path("traces"))
    parser.add_argument(
        "--runs-dir", type=Path, default=Path("experiments/runs")
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    # W4 起真实模型专用预算闸门：不进 run_id（不是实验自变量），但必须显式
    # 给出，避免忘设上限时一次跑批静默烧钱。确定性后端传这些参数直接报错。
    parser.add_argument("--max-calls", type=int, default=None)
    parser.add_argument("--max-total-tokens", type=int, default=None)
    parser.add_argument("--max-cost", type=float, default=None)
    parser.add_argument("--price-in", type=float, default=None)
    parser.add_argument("--price-out", type=float, default=None)
    parser.add_argument("--currency", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report is not None and Path(args.report).resolve() == FROZEN_BASELINE:
        raise SystemExit(
            "拒绝写入冻结基线 experiments/mvp_baseline.md；"
            "请换一个 --report 路径或不传 --report 使用留档模式"
        )
    base_cfg = exp_config.load_config(args.config)
    merged_guards = tuple(dict.fromkeys([*base_cfg.guards, *(args.guard or ())]))
    cfg = exp_config.with_overrides(
        base_cfg,
        # as_posix() 保持分隔符稳定，避免 Windows 上 "/" 变 "\\" 后
        # 与 config.toml 里的同义路径算出不同的 run_id
        tasks=args.tasks.as_posix() if args.tasks is not None else None,
        k=args.k,
        backend=args.backend,
        seed=args.seed,
        fault_per_mille=args.fault_per_mille,
        guards=merged_guards,
    )
    rid = exp_config.run_id(cfg)

    budget_limit: budget.BudgetLimit | None = None
    budget_args = (
        args.max_calls,
        args.max_total_tokens,
        args.max_cost,
        args.price_in,
        args.price_out,
        args.currency,
    )
    if cfg.backend == "openai":
        if args.max_calls is None:
            raise SystemExit(
                "接真实模型前必须显式设置预算：至少传 --max-calls <调用数上界>。"
                "先用 `python -m eval.budget --tasks <任务集> --k 3 --cells 2` "
                "离线上算，再据此确定上限；预算闸门会在超限调用前停止。"
            )
        price = None
        if args.price_in is not None or args.price_out is not None:
            if args.price_in is None or args.price_out is None:
                raise SystemExit("--price-in 与 --price-out 必须成对给出")
            price = budget.PriceCard(
                args.price_in, args.price_out, args.currency or "CNY"
            )
        if args.max_cost is not None and price is None:
            raise SystemExit(
                "--max-cost 需要同时给出 --price-in / --price-out 才能换算"
            )
        budget_limit = budget.BudgetLimit(
            max_calls=args.max_calls,
            max_total_tokens=args.max_total_tokens,
            max_cost=args.max_cost,
            price=price,
        )
        budget_limit.validate()
        backend = config_from_env(load_dotenv(args.env_file))
    elif any(value is not None for value in budget_args):
        raise SystemExit(
            "预算参数仅在 --backend openai 下生效；确定性后端不产生真实调用，"
            "传了也不该假装生效"
        )
    elif cfg.backend == "seeded":
        backend = BackendConfig(
            name="seeded",
            seed=cfg.seed,
            fault_per_mille=cfg.fault_per_mille,
        )
    else:
        backend = BackendConfig()
    tasks_path = exp_config.resolve_input(cfg.tasks)
    run_config = runner.RunConfig(
        tasks_path=tasks_path,
        k=cfg.k,
        backend=backend,
        results_dir=args.results_dir,
        traces_dir=args.traces_dir,
        max_steps=cfg.max_steps,
        max_json_retries=cfg.max_json_retries,
        guards=cfg.guards,
        budget_limit=budget_limit,
    )
    try:
        rows = runner.run(run_config)
    except budget.BudgetExceeded as exc:
        raise SystemExit(
            f"预算闸门触发，已停止产生新的模型调用：{exc}。"
            "已经发出的调用仍会计费；请核对累计用量后再决定是否提高上限。"
        ) from exc
    tasks = runner.load_tasks(tasks_path)

    baseline_rows = None
    if cfg.compare:
        compare_path = exp_config.resolve_input(cfg.compare)
        if compare_path.is_file():
            baseline_rows = trace.read_jsonl(compare_path)

    # 非默认配置文件必须写进报告的复现命令，否则重跑会算出另一个 run_id；
    # 只写仓库内相对路径，报告里不出现绝对路径或机器信息。
    config_path: str | None = None
    chosen = Path(args.config).resolve()
    if chosen != exp_config.DEFAULT_CONFIG_PATH.resolve():
        try:
            config_path = chosen.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            config_path = None

    report = analyze.render_report(
        rows, tasks, cfg, rid, baseline_rows, config_path=config_path
    )

    if args.report is not None:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(report.encode("utf-8"))
    else:
        run_dir = Path(args.runs_dir) / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot = trace.dumps(exp_config.config_snapshot(cfg, rid)) + "\n"
        (run_dir / "config.json").write_bytes(snapshot.encode("utf-8"))
        # 结果行作为可复现证据入库，也让后续实验能把本次当作对照组
        # （compare 指向 <run_id>/result_rows.jsonl 即为同后端 A/B）。
        (run_dir / "result_rows.jsonl").write_bytes(
            (args.results_dir / "result_rows.jsonl").read_bytes()
        )
        report_path = run_dir / "report.md"
        report_path.write_bytes(report.encode("utf-8"))

    summary = analyze.summarize(rows, cfg.k)
    print(f"run_id: {rid}")
    print(
        f"完成 {summary['trial_count']} 个 trial："
        f"pass¹={summary['pass1']:.2f}, pass^{cfg.k}={summary['passk']:.2f}"
    )
    print(f"结果行：{args.results_dir / 'result_rows.jsonl'}")
    print(f"trace：{args.traces_dir}/<trial_id>.jsonl")
    print(f"结果表：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
