"""CLI 入口：一条命令跑完全流程并产出证据。

默认零依赖、零成本、确定性：
    python -m eval.run --tasks tasks/mvp_10.jsonl --k 3

实验输入以 experiments/config.toml 为唯一事实来源，CLI 显式参数只覆盖对应字段；
最终生效配置决定 run_id。不传 --report 时留档到 <runs-dir>/<run_id>/
（report.md + config.json）；冻结基线 experiments/mvp_baseline.md 永不被写入。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from eval import analyze
from eval import config as exp_config
from eval import runner, trace
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
        "--backend", choices=("scripted", "openai"), default=None
    )
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--traces-dir", type=Path, default=Path("traces"))
    parser.add_argument(
        "--runs-dir", type=Path, default=Path("experiments/runs")
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report is not None and Path(args.report).resolve() == FROZEN_BASELINE:
        raise SystemExit(
            "拒绝写入冻结基线 experiments/mvp_baseline.md；"
            "请换一个 --report 路径或不传 --report 使用留档模式"
        )
    cfg = exp_config.with_overrides(
        exp_config.load_config(args.config),
        # as_posix() 保持分隔符稳定，避免 Windows 上 "/" 变 "\\" 后
        # 与 config.toml 里的同义路径算出不同的 run_id
        tasks=args.tasks.as_posix() if args.tasks is not None else None,
        k=args.k,
        backend=args.backend,
    )
    rid = exp_config.run_id(cfg)

    backend = (
        config_from_env(load_dotenv(args.env_file))
        if cfg.backend == "openai"
        else BackendConfig()
    )
    tasks_path = exp_config.resolve_input(cfg.tasks)
    run_config = runner.RunConfig(
        tasks_path=tasks_path,
        k=cfg.k,
        backend=backend,
        results_dir=args.results_dir,
        traces_dir=args.traces_dir,
        max_steps=cfg.max_steps,
        max_json_retries=cfg.max_json_retries,
    )
    rows = runner.run(run_config)
    tasks = runner.load_tasks(tasks_path)

    baseline_rows = None
    if cfg.compare:
        compare_path = exp_config.resolve_input(cfg.compare)
        if compare_path.is_file():
            baseline_rows = trace.read_jsonl(compare_path)

    report = analyze.render_report(rows, tasks, cfg, rid, baseline_rows)

    if args.report is not None:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(report.encode("utf-8"))
    else:
        run_dir = Path(args.runs_dir) / rid
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot = trace.dumps(exp_config.config_snapshot(cfg, rid)) + "\n"
        (run_dir / "config.json").write_bytes(snapshot.encode("utf-8"))
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
