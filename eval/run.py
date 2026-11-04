"""CLI 入口：一条命令跑完全流程并产出证据。

默认零依赖、零成本、确定性：
    python -m eval.run --tasks tasks/mvp_10.jsonl --k 3
"""
from __future__ import annotations

import argparse
from pathlib import Path

from eval import analyze, runner
from runtime.model import BackendConfig, config_from_env


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
        description="跑一遍 MVP 评测，产出结果行、全链路 trace 与 Markdown 结果表",
    )
    parser.add_argument(
        "--tasks", type=Path, default=Path("tasks/mvp_10.jsonl")
    )
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument(
        "--backend", choices=("scripted", "openai"), default="scripted"
    )
    parser.add_argument("--results-dir", type=Path, default=Path("results"))
    parser.add_argument("--traces-dir", type=Path, default=Path("traces"))
    parser.add_argument(
        "--report", type=Path, default=Path("experiments/mvp_baseline.md")
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.k < 1:
        raise SystemExit("--k 必须 >= 1")

    backend = (
        config_from_env(load_dotenv(args.env_file))
        if args.backend == "openai"
        else BackendConfig()
    )
    config = runner.RunConfig(
        tasks_path=args.tasks,
        k=args.k,
        backend=backend,
        results_dir=args.results_dir,
        traces_dir=args.traces_dir,
    )
    rows = runner.run(config)
    tasks = runner.load_tasks(args.tasks)
    report = analyze.render_report(rows, tasks, config)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes(report.encode("utf-8"))

    summary = analyze.summarize(rows, args.k)
    print(
        f"完成 {summary['trial_count']} 个 trial："
        f"pass¹={summary['pass1']:.2f}, pass^{args.k}={summary['passk']:.2f}"
    )
    print(f"结果行：{args.results_dir / 'result_rows.jsonl'}")
    print(f"trace：{args.traces_dir}/<trial_id>.jsonl")
    print(f"结果表：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
