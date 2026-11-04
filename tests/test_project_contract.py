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


if __name__ == "__main__":
    unittest.main()
