from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.cli import build_parser


class RepoAgentCLITests(unittest.TestCase):
    def test_explain_question_can_be_read_from_console(self) -> None:
        arguments = build_parser().parse_args(
            [
                "explain",
                "--llm-provider",
                "deepseek",
                "--repo",
                "D:/target",
            ]
        )

        self.assertIsNone(arguments.question)
        self.assertEqual(arguments.command, "explain")

    def test_chat_keeps_explicit_project_selection(self) -> None:
        arguments = build_parser().parse_args(
            [
                "chat",
                "--project",
                "hospital-ai",
                "--llm-provider",
                "deepseek",
            ]
        )

        self.assertEqual(arguments.command, "chat")
        self.assertEqual(arguments.project, "hospital-ai")
        self.assertIsNone(arguments.repo)

    def test_eval_retrieval_command_is_registered(self) -> None:
        arguments = build_parser().parse_args(
            [
                "eval",
                "retrieval",
                "--dataset",
                "evals/retrieval/python-small.jsonl",
            ]
        )

        self.assertEqual(arguments.command, "eval")
        self.assertEqual(arguments.eval_command, "retrieval")
        self.assertEqual(arguments.mode, "hybrid")

    def test_resume_fix_requires_explicit_decision(self) -> None:
        arguments = build_parser().parse_args(
            [
                "resume-fix",
                "--repo",
                "D:/target",
                "--thread-id",
                "fix-thread",
                "--approve",
            ]
        )

        self.assertEqual(arguments.command, "resume-fix")
        self.assertTrue(arguments.approve)
        self.assertFalse(arguments.reject)


if __name__ == "__main__":
    unittest.main()
