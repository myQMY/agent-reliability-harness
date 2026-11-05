"""仓库契约测试：守住项目基础约定，防止关键文件与规则被误删或改弱。

只依赖 Python 标准库，不需要安装任何依赖：

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def compact(text: str) -> str:
    """去掉全部空白，让「Git commit」和「Gitcommit」这类差异不影响断言。"""
    return "".join(text.split())


class TestAgentsRules(unittest.TestCase):
    """AGENTS.md 是协作约定本身，条款不得被删掉或改弱。"""

    def test_agents_md_exists(self) -> None:
        self.assertTrue((ROOT / "AGENTS.md").is_file(), "缺少 AGENTS.md")

    def test_all_clauses_present(self) -> None:
        text = compact(read("AGENTS.md"))
        clauses = (
            "注意事项",
            "每次改动完成后",
            "Gitcommit",
            "以便后续追踪和回滚",
            "每次改动后",
            "编写或更新相关测试",
            "在交付给用户前",
            "确保所有测试和验证全部通过",
        )
        for clause in clauses:
            with self.subTest(clause=clause):
                self.assertIn(compact(clause), text, f"AGENTS.md 缺少条款：{clause}")


class TestRepoSkeleton(unittest.TestCase):
    REQUIRED_FILES = ("README.md", ".gitignore", ".gitattributes", ".env.example")
    REQUIRED_DIRS = ("tasks", "env", "runtime", "eval", "web", "experiments", "tests")

    def test_required_files_exist(self) -> None:
        for rel_path in self.REQUIRED_FILES:
            with self.subTest(file=rel_path):
                self.assertTrue((ROOT / rel_path).is_file(), f"缺少文件 {rel_path}")

    def test_required_dirs_exist(self) -> None:
        for rel_path in self.REQUIRED_DIRS:
            with self.subTest(dir=rel_path):
                self.assertTrue((ROOT / rel_path).is_dir(), f"缺少目录 {rel_path}")


class TestGitHygiene(unittest.TestCase):
    def test_secrets_and_artifacts_ignored(self) -> None:
        ignore = read(".gitignore")
        for pattern in (".env", "/traces/", "/results/", "__pycache__/"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignore, f".gitignore 未忽略 {pattern}")

    def test_taskset_not_ignored(self) -> None:
        """任务集是评测标尺，必须入库，不能被批量规则排除。"""
        ignore = read(".gitignore")
        self.assertNotIn("*.jsonl", ignore, "任务集 jsonl 不应被忽略")
        self.assertNotIn("tasks/", ignore, "tasks/ 目录不应被忽略")

    def test_env_example_has_no_real_key(self) -> None:
        for line in read(".env.example").splitlines():
            if line.startswith("LLM_API_KEY="):
                self.assertEqual(
                    line.strip(), "LLM_API_KEY=", ".env.example 不得填入真实密钥"
                )
                return
        self.fail(".env.example 缺少 LLM_API_KEY 空占位")

    def test_out_of_scope_config_not_active(self) -> None:
        """并发与预算护栏属于 W3/W5 自变量，不能以生效配置形式出现在 MVP。"""
        for line in read(".env.example").splitlines():
            stripped = line.strip()
            for key in (
                "EVAL_CONCURRENCY=",
                "MAX_TOKENS_PER_TASK=",
                "EVAL_DB_TEMPLATE=",
            ):
                with self.subTest(key=key):
                    self.assertFalse(
                        stripped.startswith(key),
                        f"{key} 属于 W3/W5 范围，MVP 阶段不得生效",
                    )


class TestDesignDoc(unittest.TestCase):
    """设计文档是范围共识的载体：防止有人删掉「非目标」悄悄扩范围，
    或改掉确定性判分 / ScriptedBackend 这类关键选型。"""

    DOC = "docs/mvp-design.md"

    def test_design_doc_exists(self) -> None:
        self.assertTrue((ROOT / self.DOC).is_file(), f"缺少 {self.DOC}")

    def test_readme_links_design_doc(self) -> None:
        self.assertIn(
            "docs/mvp-design.md",
            read("README.md"),
            "README 应指向设计文档，保证范围文档可被找到",
        )

    def test_scope_contract_present(self) -> None:
        text = compact(read(self.DOC))
        anchors = (
            "定位与目标",
            "设计原则",
            "MVP 主要功能",
            "ScriptedBackend",
            "终态判分",
            "确定性判分",
            "明确不做",
            "LLM-as-Judge",
            "验收标准",
            "pass¹",
        )
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                self.assertIn(compact(anchor), text, f"设计文档缺少关键锚点：{anchor}")


class TestTechStackDoc(unittest.TestCase):
    """技术方案是选型共识的载体：防止有人悄悄引入框架 / ORM / 第三方依赖，
    或删掉「逐字节一致」这类硬约束，把可复现实验装置退化成普通 Agent demo。
    """

    DOC = "docs/tech-stack.md"

    def test_tech_stack_doc_exists(self) -> None:
        self.assertTrue((ROOT / self.DOC).is_file(), f"缺少 {self.DOC}")

    def test_readme_links_tech_stack_doc(self) -> None:
        self.assertIn(
            "docs/tech-stack.md",
            read("README.md"),
            "README 应指向技术方案，保证实现口径可被找到",
        )

    def test_core_choices_present(self) -> None:
        text = compact(read(self.DOC))
        anchors = (
            "零第三方依赖",
            "Python3.11+",
            "typing.Protocol",
            "ScriptedBackend",
            "OpenAIBackend",
            "逐字节一致",
            "trial_id",
            "tomllib",
            "argparse",
            "unittest",
            "按阶段引入",
            "pydantic",
            "IO收敛原则",
        )
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                self.assertIn(compact(anchor), text, f"技术方案缺少关键锚点：{anchor}")

    def test_exclusions_present(self) -> None:
        """「不引入」的措辞必须保留，防止把被测对象换成第三方框架。"""
        text = compact(read(self.DOC))
        exclusions = (
            "明确不引入Agent框架",
            "全程不引入",
            "不用ORM",
            "LLM-as-Judge",
            "LangGraph",
        )
        for phrase in exclusions:
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), text, f"技术方案缺少排除项：{phrase}")


class TestSeededCalibrationContract(unittest.TestCase):
    """W2.5 校准器：确定性、零成本、口径诚实，且不得悄悄改变冻结基线。"""

    def test_seeded_backend_sources_are_deterministic(self):
        source = read("runtime/model.py")
        for phrase in (
            "SeededBackend",
            "GUARD_REJECT_PREFIX",
            "hashlib.sha256",
            "fault_per_mille",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)
        self.assertNotIn("uuid", source)
        self.assertNotIn("random.seed", source)

    def test_calibration_config_is_versioned(self):
        self.assertTrue((ROOT / "experiments/calibration.toml").is_file())
        self.assertIn('backend = "seeded"', read("experiments/calibration.toml"))
        self.assertIn("[seeded]", read("experiments/calibration.toml"))

    def test_default_config_has_no_seeded_section_values(self):
        """默认配置里写 [seeded] 值会改变 run_id，冻结留档随即失效。"""
        text = read("experiments/config.toml")
        self.assertNotIn("seed =", text)
        self.assertNotIn("fault_per_mille =", text)

    def test_docs_describe_calibration_honestly(self):
        readme = compact(read("README.md"))
        stack = compact(read("docs/tech-stack.md"))
        for phrase in ("SeededBackend", "pass^k", "校准"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), readme, f"README 缺少 {phrase}")
        for phrase in ("SeededBackend", "伪随机", "不构成"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), stack, f"技术方案缺少 {phrase}")


class TestExperimentRecords(unittest.TestCase):
    """P0 实验记录体系是 W3 对照实验的地基：配置、冻结基线、留档约定不得回潮。"""

    def test_required_record_files_exist(self):
        for rel_path in (
            "experiments/config.toml",
            "experiments/baseline/result_rows.jsonl",
            "experiments/mvp_baseline.md",
            "eval/config.py",
        ):
            with self.subTest(file=rel_path):
                self.assertTrue((ROOT / rel_path).is_file(), f"缺少 {rel_path}")

    def test_cli_never_writes_frozen_baseline_by_default(self):
        source = read("eval/run.py")
        self.assertIn("FROZEN_BASELINE", source, "CLI 必须显式守住冻结基线")
        self.assertNotIn(
            'default=Path("experiments/mvp_baseline.md")',
            source,
            "--report 默认值不得指回冻结基线，否则重跑会覆盖对照组",
        )

    def test_frozen_baseline_is_not_ignored(self):
        ignore = read(".gitignore")
        self.assertNotIn("experiments/baseline/", ignore)
        self.assertNotIn("experiments/config.toml", ignore)

    def test_readme_documents_run_id_and_freeze(self):
        text = compact(read("README.md"))
        for phrase in ("run_id", "冻结基线", "experiments/config.toml"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), text, f"README 缺少 {phrase}")

    def test_tech_stack_documents_archive_mode(self):
        text = compact(read("docs/tech-stack.md"))
        for phrase in ("run_id", "冻结基线", "config.toml"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), text, f"技术方案缺少 {phrase}")


class TestW2MeasurementContract(unittest.TestCase):
    """W2 升级的是测量能力而非测量结果：守住观测来源、归因与话术的契约。"""

    def test_model_reply_is_the_only_observation_source(self):
        source = read("runtime/model.py")
        for phrase in ("ModelReply", "prompt_tokens", "completion_tokens", "usage"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)
        self.assertNotIn(
            "SCRIPTED_TOKENS",
            read("runtime/guard.py"),
            "护栏层不得再提供假的成本常量",
        )

    def test_attribution_reads_trace_not_just_errors(self):
        source = read("eval/grader.py")
        for phrase in ("trace_events", "first_divergence", "unknown", "errors_fallback"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)

    def test_conditional_utterances_are_validated(self):
        source = read("eval/user_sim.py")
        for phrase in ("TurnContext", "utterance_for", "parse_rules"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)

    def test_docs_describe_w2_honestly(self):
        readme = compact(read("README.md"))
        stack = compact(read("docs/tech-stack.md"))
        for phrase in ("first_divergence", "usage", "unknown"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), readme, f"README 缺少 {phrase}")
        for phrase in ("ModelReply", "首个可判定偏离点", "usage"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), stack, f"技术方案缺少 {phrase}")


class TestW3GuardContract(unittest.TestCase):
    """W3 第一条护栏：开关必须可配置、对照必须单变量、口径必须诚实。"""

    def test_guard_switch_is_config_driven(self):
        for path, phrases in (
            ("runtime/guard.py", ("GUARD_NAMES", "GUARD_REJECT_PREFIX", "validate_args", "MAX_GUARD_REJECTIONS")),
            ("runtime/loop.py", ("guards", "rejection_message", "\"guard\"")),
            ("eval/config.py", ("guards", "GUARD_NAMES")),
            ("eval/run.py", ("--guard", "merged_guards")),
        ):
            source = read(path)
            for phrase in phrases:
                with self.subTest(path=path, phrase=phrase):
                    self.assertIn(phrase, source, f"{path} 缺少 {phrase}")

    def test_ab_configs_are_versioned_and_differ_only_in_enabled(self):
        off = read("experiments/guard_ab_off.toml")
        on = read("experiments/guard_ab_on.toml")
        self.assertIn("enabled = []", off)
        self.assertIn('enabled = ["schema_validation"]', on)
        self.assertNotIn("schema_validation", off, "对照组不得开启任何护栏")
        for text in (off, on):
            self.assertIn('backend = "seeded"', text)
            self.assertIn("seed = 11", text)
            self.assertIn("fault_per_mille = 60", text)

    def test_default_config_keeps_all_guards_off(self):
        """默认路径开启护栏会悄悄改变 W1 冻结基线的可比性。"""
        self.assertNotIn("enabled = [\"", read("experiments/config.toml"))

    def test_report_does_not_sell_invalid_calls_as_improvement(self):
        source = read("eval/analyze.py")
        for phrase in ("下降不等于成功率提升", "与对照组对照（A/B）", "单变量对照"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)

    def test_docs_describe_w3_honestly(self):
        readme = compact(read("README.md"))
        stack = compact(read("docs/tech-stack.md"))
        for phrase in ("schema_validation", "单变量", "与对照组对照", "guard_ab_on.toml"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), readme, f"README 缺少 {phrase}")
        for phrase in ("schema_validation", "单变量", "不构成"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), stack, f"技术方案缺少 {phrase}")


class TestW3FactorialContract(unittest.TestCase):
    """W3 第二条护栏与 2×2 全因子：单一事实来源、确定性、诚实口径不得回潮。"""

    def test_four_cells_and_matrix_report_are_versioned(self):
        for rel in (
            "experiments/guard_ab_off.toml",
            "experiments/guard_ab_on.toml",
            "experiments/guard_ab_idem.toml",
            "experiments/guard_ab_both.toml",
            "experiments/w3_guard_matrix.md",
            "eval/guard_matrix.py",
            "tests/test_idempotency_guard.py",
        ):
            with self.subTest(path=rel):
                self.assertTrue((ROOT / rel).is_file(), f"缺少 {rel}")

    def test_write_tool_list_has_one_definition(self):
        self.assertIn("WRITE_TOOLS", read("env/tools.py"))
        self.assertIn("from env import db, tools", read("eval/grader.py"))
        self.assertIn("from env import tools", read("runtime/model.py"))
        self.assertIn("from env import tools", read("runtime/guard.py"))

    def test_idempotency_key_is_hash_based_and_clock_free(self):
        source = read("runtime/guard.py")
        for phrase in (
            "GUARD_IDEMPOTENCY", "GuardState", "idempotency_key",
            "hashlib.sha256", "record_write", "sort_keys=True",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)
        for banned in ("uuid", "time.time", "random.random"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_matrix_module_is_read_only_and_clock_free(self):
        source = read("eval/guard_matrix.py")
        for phrase in (
            "effects_of", "build_matrix", "render_matrix",
            "difference-in-differences", "不是显著性检验",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, source)
        self.assertNotIn("datetime", source)
        self.assertNotIn("time.", source)

    def test_guard_state_is_wired_into_loop_and_runner(self):
        self.assertIn("guard_state", read("runtime/loop.py"))
        self.assertIn("guard_state", read("eval/runner.py"))

    def test_docs_describe_factorial_honestly(self):
        readme = compact(read("README.md"))
        stack = compact(read("docs/tech-stack.md"))
        for phrase in ("2×2", "idempotency", "w3_guard_matrix.md", "交互"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), readme, f"README 缺少 {phrase}")
        for phrase in ("idempotency", "2×2", "交互", "不构成"):
            with self.subTest(phrase=phrase):
                self.assertIn(compact(phrase), stack, f"技术方案缺少 {phrase}")


if __name__ == "__main__":
    unittest.main()
