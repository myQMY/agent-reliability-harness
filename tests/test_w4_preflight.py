"""W4 跑批前预检的守门测试：全部离线，零 API 请求、零花费。

钉死四件事：
1. 外部输入缺失时预检必须如实报 NOT READY，不许假装就绪；
2. 故意写坏配置（compare 失配 / 目录冲突 / 预算不足 / .env 缺键）必须被拦住；
3. 密钥值绝不进入报告或 stdout；
4. openai 后端缺少对照留档时，必须在读取密钥 / 发起调用之前就拒绝启动，
   而既有 scripted 路径不受这次改动影响。

所有涉及「就绪 / 未就绪」的断言都用临时目录与 patch **自己构造状态**，
不依赖本机此刻是否填了 `.env`、仓库里是否已有 OFF 组留档——W4 首轮
跑批完成后这两项都会从「缺失」变成「已生成」，写死环境会让测试失真。
"""
from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from eval import config as exp_config
from eval import run as cli
from eval import w4_preflight as preflight

ROOT = Path(__file__).resolve().parents[1]

SECRET = "sk-TEST-NEVER-PRINT-9f3c"


def current_checks(**kwargs):
    return preflight.run_checks(**kwargs)


class TestPreflightHonestState(unittest.TestCase):
    def test_absent_external_inputs_are_waiting_not_errors(self):
        # 两项外部输入都显式隔离：.env 指向不存在的路径，OFF 留档解析到
        # 临时目录里的空路径。这样无论真实仓库是否已跑过 W4，本测试都在
        # 验证同一件事：缺输入 = BLOCKED，不是 FAIL。
        with TemporaryDirectory() as d:
            base = Path(d)

            def fake_resolve(path_str: str) -> Path:
                if path_str.startswith("experiments/runs/"):
                    return base / "absent_archive.jsonl"
                return ROOT / path_str

            with patch.object(
                preflight.exp_config, "resolve_input", side_effect=fake_resolve
            ):
                checks, _ = current_checks(
                    env_file=base / "absent.env", max_calls=720
                )
        self.assertEqual(preflight.overall(checks), "NOT READY — 等待外部输入")
        self.assertFalse(any(item.status == preflight.FAIL for item in checks))
        blocked = [item.name for item in checks if item.status == preflight.BLOCKED]
        self.assertIn("OFF 组留档已生成", blocked)
        self.assertIn(".env 已配置三个必需键", blocked)

    def test_report_states_it_made_no_request_and_no_claim_about_results(self):
        checks, ctx = current_checks(max_calls=720)
        report = preflight.render_report(checks, ctx)
        # 不断言具体状态（READY 会随外部输入变化），只断言报告如实写出
        # 状态、三态口径，且明确声明自己不是实验结果。
        for phrase in (
            "不发起任何 API 请求", "不花一分钱", "不是 W4 的实验结果",
            "不属于冻结归档", "整体状态：", "三态口径", "单格不失控",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, report)
        self.assertNotIn(SECRET, report)

    def test_budget_scope_is_explicit_about_per_cell_and_total(self):
        checks, ctx = current_checks(max_calls=720)
        self.assertEqual(ctx["per_cell"]["calls_upper_bound"], 360)
        self.assertEqual(ctx["total"]["calls_upper_bound"], 720)
        self.assertEqual(ctx["total"]["total_tokens_upper_bound"], 1_224_000)
        report = preflight.render_report(checks, ctx)
        self.assertIn("1440", report)
        self.assertIn("不是总预算 720 次", report)

    def test_overall_distinguishes_error_from_waiting(self):
        mk = preflight.Check
        self.assertEqual(preflight.overall([mk("a", "PASS", "")]), "READY")
        self.assertEqual(
            preflight.overall([mk("a", "PASS", ""), mk("b", "BLOCKED", "")]),
            "NOT READY — 等待外部输入",
        )
        self.assertEqual(
            preflight.overall([mk("a", "FAIL", ""), mk("b", "BLOCKED", "")]),
            "NOT READY — 配置错误",
        )

    def test_strict_returns_nonzero_while_not_ready(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            out = base / "preflight.md"
            # 显式指向不存在的 .env，让「未就绪」成为测试自己构造的状态，
            # 而不是依赖本机密钥 / 归档的当前状态。
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = preflight.main(
                    ["--strict", "--out", str(out),
                     "--env-file", str(base / "absent.env")]
                )
            self.assertEqual(code, 1)
            self.assertIn("NOT READY", buf.getvalue())
            self.assertIn("未发起任何 API 请求", buf.getvalue())
            self.assertTrue(out.is_file())

    def test_taskset_hashes_match_the_existing_guard_test(self):
        source = (ROOT / "tests" / "test_w4_readiness.py").read_text(
            encoding="utf-8"
        )
        for name, digest in preflight.TASKSET_SHA256.items():
            with self.subTest(name=name):
                self.assertIn(digest, source)
                self.assertEqual(
                    digest, preflight._digest(ROOT / "tasks" / name)
                )


class TestPreflightCatchesBrokenConfig(unittest.TestCase):
    def test_changing_off_k_breaks_compare_link_and_single_variable(self):
        with TemporaryDirectory() as d:
            off = Path(d) / "off.toml"
            off.write_text(
                (ROOT / "experiments" / "w4_off_flash.toml").read_text(
                    encoding="utf-8"
                ).replace("k = 3", "k = 4"),
                encoding="utf-8",
            )
            checks, _ = current_checks(
                off_config=off,
                on_config=ROOT / "experiments" / "w4_all4_flash.toml",
            )
            failed = [item.name for item in checks if item.status == preflight.FAIL]
            self.assertIn("两格只差 [guards].enabled（单变量）", failed)
            self.assertIn("ALL4.compare 精确指向同后端 OFF 留档", failed)

    def test_shared_trace_directory_is_rejected(self):
        checks, _ = current_checks(
            dirs={**preflight.DEFAULT_DIRS, "on_traces": "traces/w4_off"}
        )
        names = [item.name for item in checks if item.status == preflight.FAIL]
        self.assertIn("两格 results / traces 目录互不冲突", names)

    def test_default_results_directory_is_rejected(self):
        checks, _ = current_checks(
            dirs={**preflight.DEFAULT_DIRS, "off_results": "results"}
        )
        names = [item.name for item in checks if item.status == preflight.FAIL]
        self.assertIn("两格 results / traces 目录互不冲突", names)

    def test_max_calls_below_single_cell_bound_is_rejected(self):
        checks, _ = current_checks(max_calls=100)
        names = [item.name for item in checks if item.status == preflight.FAIL]
        self.assertIn("预算闸门覆盖单格上界", names)

    def test_env_with_missing_keys_is_a_failure_not_a_wait(self):
        with TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("LLM_BASE_URL=https://example.invalid\n", encoding="utf-8")
            checks, _ = current_checks(env_file=env)
            item = next(
                c for c in checks if c.name == ".env 已配置三个必需键"
            )
            self.assertEqual(item.status, preflight.FAIL)
            self.assertIn("LLM_API_KEY", item.detail)

    def test_secret_value_never_leaks_and_ready_becomes_reachable(self):
        """补齐两个外部输入后应转 READY；密钥值不出现在报告 / stdout。"""
        with TemporaryDirectory() as d:
            base = Path(d)
            env = base / ".env"
            env.write_text(
                f"LLM_BASE_URL=https://example.invalid\n"
                f"LLM_API_KEY={SECRET}\n"
                f"LLM_MODEL=test-model\n",
                encoding="utf-8",
            )
            archive = base / "result_rows.jsonl"
            archive.write_text("{}\n", encoding="utf-8")
            off = exp_config.load_config(
                ROOT / "experiments" / "w4_off_flash.toml"
            )
            expect_compare = f"experiments/runs/{exp_config.run_id(off)}/result_rows.jsonl"
            on_path = base / "on.toml"
            on_path.write_text(
                (ROOT / "experiments" / "w4_all4_flash.toml")
                .read_text(encoding="utf-8")
                .replace(
                    "compare = \"experiments/runs/w4_off_flash-61491decf48c/result_rows.jsonl\"",
                    f'compare = "{expect_compare}"',
                ),
                encoding="utf-8",
            )

            def fake_resolve(path_str: str) -> Path:
                if path_str == expect_compare:
                    return archive
                return ROOT / path_str

            with patch.object(preflight.exp_config, "resolve_input", side_effect=fake_resolve):
                checks, ctx = current_checks(
                    on_config=on_path, env_file=env, max_calls=720
                )
                self.assertEqual(preflight.overall(checks), "READY")
                report = preflight.render_report(checks, ctx)
                self.assertNotIn(SECRET, report)

                # 同一隔离状态下 strict 应放行；密钥值仍不得进入 stdout / 报告。
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = preflight.main(
                        ["--strict", "--out", str(base / "report.md"),
                         "--env-file", str(env), "--on-config", str(on_path)]
                    )
            self.assertEqual(code, 0)
            self.assertNotIn(SECRET, buf.getvalue())
            self.assertNotIn(
                SECRET, (base / "report.md").read_text(encoding="utf-8")
            )


class TestRunRejectsMissingCompareBeforeSpending(unittest.TestCase):
    def test_openai_run_stops_before_reading_credentials(self):
        # 用隔离配置把 compare 指向不存在的文件，不依赖仓库里 OFF 留档
        # 是否已经生成（首轮跑批后它已存在）。
        with TemporaryDirectory() as d:
            cfg = Path(d) / "missing_compare.toml"
            cfg.write_text(
                (ROOT / "experiments" / "w4_all4_flash.toml")
                .read_text(encoding="utf-8")
                .replace(
                    "compare = \"experiments/runs/w4_off_flash-61491decf48c/result_rows.jsonl\"",
                    "compare = \"experiments/runs/absent-for-test/result_rows.jsonl\"",
                ),
                encoding="utf-8",
            )
            argv = ["--config", str(cfg)]
            with patch.object(
                cli, "config_from_env",
                side_effect=AssertionError("不该在对照缺失时读取密钥"),
            ):
                with self.assertRaises(SystemExit) as caught:
                    cli.main(argv)
        message = str(caught.exception)
        self.assertIn("对照组结果行不存在", message)
        self.assertIn("未发起任何模型调用", message)
        self.assertIn("--allow-missing-compare", message)

    def test_scripted_run_is_unaffected_by_the_new_guard(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            cfg = base / "cfg.toml"
            cfg.write_text(
                '[experiment]\nname = "preflight_smoke"\n'
                'tasks = "tasks/mvp_10.jsonl"\nk = 1\nbackend = "scripted"\n'
                'compare = "experiments/runs/does-not-exist/result_rows.jsonl"\n',
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                code = cli.main([
                    "--config", str(cfg),
                    "--results-dir", str(base / "results"),
                    "--traces-dir", str(base / "traces"),
                    "--runs-dir", str(base / "runs"),
                ])
            self.assertEqual(code, 0)
            report = list((base / "runs").rglob("report.md"))
            self.assertEqual(len(report), 1)
            self.assertNotIn("与对照组对照（A/B）", report[0].read_text(encoding="utf-8"))

    def test_new_cli_flag_does_not_change_run_id(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--allow-missing-compare"])
        self.assertTrue(args.allow_missing_compare)
        off = exp_config.load_config(ROOT / "experiments" / "w4_off_flash.toml")
        self.assertEqual(exp_config.run_id(off), "w4_off_flash-61491decf48c")


class TestDocsStayInSync(unittest.TestCase):
    def test_design_doc_budget_matches_computed_upper_bound(self):
        design = (ROOT / "docs" / "w4-design.md").read_text(encoding="utf-8")
        _, ctx = current_checks()
        total_calls = ctx["total"]["calls_upper_bound"]
        declared = re.search(r"调用次数上界.*?= \*\*(\d+)\*\*", design)
        self.assertIsNotNone(declared, "设计文档应写明调用次数上界")
        self.assertEqual(int(declared.group(1)), total_calls)
        for phrase in ("w4_preflight", "单次 `eval.run`", "1440"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, design)

    def test_readme_mentions_the_preflight(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("eval.w4_preflight", readme)


if __name__ == "__main__":
    unittest.main()
