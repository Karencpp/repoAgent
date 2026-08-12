from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.evals import EvalDatasetError, load_patch_cases, load_retrieval_cases


class EvalDatasetTests(unittest.TestCase):
    def test_committed_retrieval_dataset_loads(self) -> None:
        cases = load_retrieval_cases(
            PROJECT_ROOT / "evals" / "retrieval" / "python-small.jsonl",
            fixtures_root=PROJECT_ROOT / "evals" / "fixtures",
        )

        self.assertEqual(len(cases), 5)
        self.assertEqual(cases[0].relevant_line_ranges[0].path, "src/calculator.py")

    def test_duplicate_case_id_is_rejected(self) -> None:
        root = PROJECT_ROOT / ".test-tmp" / "eval-duplicates"
        root.mkdir(parents=True, exist_ok=True)
        dataset = root / "bad.jsonl"
        dataset.write_text(
            "\n".join(
                [
                    '{"case_id":"same","repo_fixture":"calculator_repo","query":"q","relevant_paths":["src/calculator.py"]}',
                    '{"case_id":"same","repo_fixture":"calculator_repo","query":"q","relevant_paths":["src/calculator.py"]}',
                ]
            ),
            encoding="utf-8",
        )

        with self.assertRaises(EvalDatasetError):
            load_retrieval_cases(dataset)

    def test_bad_patch_path_is_rejected(self) -> None:
        root = PROJECT_ROOT / ".test-tmp" / "eval-bad-path"
        root.mkdir(parents=True, exist_ok=True)
        dataset = root / "bad.jsonl"
        dataset.write_text(
            '{"case_id":"bad","repo_fixture":"failing_pytest_repo","objective":"x",'
            '"target_tests":["tests/test_calculator.py"],'
            '"regression_tests":["tests"],'
            '"expected_changed_paths":["../outside.py"]}\n',
            encoding="utf-8",
        )

        with self.assertRaises(EvalDatasetError):
            load_patch_cases(dataset)


if __name__ == "__main__":
    unittest.main()
