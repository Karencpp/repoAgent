from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
TEST_TEMP_ROOT = PROJECT_ROOT / ".test-tmp"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from repo_agent.projects import (
    AmbiguousProjectSelectionError,
    DuplicateProjectNameError,
    DuplicateProjectPathError,
    InvalidProjectNameError,
    InvalidRepositoryError,
    PathOutsideRepositoryError,
    ProjectContextResolver,
    ProjectRegistry,
    ProjectSelectionRequiredError,
    RegistryCorruptedError,
    inspect_repository,
)


class ProjectModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_TEMP_ROOT / f"case-{uuid4().hex}"
        self.root.mkdir()
        self.registry = ProjectRegistry(self.root / "state" / "projects.json")
        self.resolver = ProjectContextResolver(self.registry)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def make_repo(self, name: str) -> Path:
        repo = self.root / name
        repo.mkdir()
        (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
        return repo

    def init_git_repo(self, name: str) -> Path:
        repo = self.make_repo(name)
        self.run_git(repo, "init")
        self.run_git(repo, "config", "user.email", "repo-agent@example.test")
        self.run_git(repo, "config", "user.name", "RepoAgent Test")
        self.run_git(repo, "add", "main.py")
        self.run_git(repo, "commit", "-m", "initial")
        return repo

    def run_git(self, repo: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip()

    def test_requires_explicit_selection(self) -> None:
        with self.assertRaises(ProjectSelectionRequiredError):
            self.resolver.resolve()

    def test_empty_repo_path_does_not_fall_back_to_current_directory(self) -> None:
        with self.assertRaises(InvalidRepositoryError):
            self.resolver.resolve(repo="")

    def test_rejects_ambiguous_selection(self) -> None:
        repo = self.make_repo("alpha")
        with self.assertRaises(AmbiguousProjectSelectionError):
            self.resolver.resolve(repo=repo, project="alpha")

    def test_adhoc_context_is_stable_and_not_persisted(self) -> None:
        repo = self.make_repo("alpha")
        first = self.resolver.resolve(repo=repo)
        second = self.resolver.resolve(repo=repo)

        self.assertEqual(first.project_id, second.project_id)
        self.assertTrue(first.project_id.startswith("adhoc-"))
        self.assertFalse(first.registered)
        self.assertEqual(self.registry.list(), ())

    def test_registered_projects_have_isolated_namespaces(self) -> None:
        first_repo = self.make_repo("alpha-repo")
        second_repo = self.make_repo("beta-repo")
        first_registration = self.registry.register(first_repo, "alpha")
        second_registration = self.registry.register(second_repo, "beta")

        first = self.resolver.resolve(project="alpha")
        second = self.resolver.resolve(project="beta")

        self.assertNotEqual(first_registration.project_id, second_registration.project_id)
        self.assertNotEqual(first.memory_namespace, second.memory_namespace)
        self.assertNotEqual(first.rag_namespace, second.rag_namespace)
        self.assertNotEqual(first.checkpoint_namespace, second.checkpoint_namespace)
        self.assertEqual(first.repo_root, first_repo.resolve())
        self.assertEqual(second.repo_root, second_repo.resolve())

    def test_registry_persists_and_resolves_by_id(self) -> None:
        repo = self.make_repo("alpha")
        registration = self.registry.register(repo, "alpha")
        reloaded_registry = ProjectRegistry(self.registry.storage_path)
        context = ProjectContextResolver(reloaded_registry).resolve(
            project=registration.project_id
        )

        self.assertEqual(context.project_id, registration.project_id)
        self.assertEqual(context.display_name, "alpha")

    def test_registration_is_idempotent_for_same_name_and_path(self) -> None:
        repo = self.make_repo("alpha")
        first = self.registry.register(repo, "alpha")
        second = self.registry.register(repo, "alpha")
        self.assertEqual(first.project_id, second.project_id)
        self.assertEqual(len(self.registry.list()), 1)

    def test_rejects_duplicate_name_for_different_path(self) -> None:
        self.registry.register(self.make_repo("alpha-one"), "alpha")
        with self.assertRaises(DuplicateProjectNameError):
            self.registry.register(self.make_repo("alpha-two"), "alpha")

    def test_rejects_duplicate_path_for_different_name(self) -> None:
        repo = self.make_repo("alpha")
        self.registry.register(repo, "alpha")
        with self.assertRaises(DuplicateProjectPathError):
            self.registry.register(repo, "other")

    def test_rejects_unsafe_project_name(self) -> None:
        repo = self.make_repo("alpha")
        for invalid_name in ("", "../alpha", "alpha beta", "-alpha"):
            with self.subTest(name=invalid_name):
                with self.assertRaises(InvalidProjectNameError):
                    self.registry.register(repo, invalid_name)

    def test_update_path_preserves_project_identity(self) -> None:
        original = self.make_repo("original")
        registration = self.registry.register(original, "alpha")
        moved = self.root / "moved"
        original.rename(moved)

        updated = self.registry.update_path("alpha", moved)
        context = self.resolver.resolve(project="alpha")

        self.assertEqual(updated.project_id, registration.project_id)
        self.assertEqual(context.project_id, registration.project_id)
        self.assertEqual(context.repo_root, moved.resolve())

    def test_repository_path_sandbox_blocks_escape(self) -> None:
        repo = self.make_repo("alpha")
        outside = self.root / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        context = self.resolver.resolve(repo=repo)

        self.assertEqual(context.resolve_repo_path("main.py"), repo / "main.py")
        with self.assertRaises(PathOutsideRepositoryError):
            context.resolve_repo_path("../secret.txt")
        with self.assertRaises(PathOutsideRepositoryError):
            context.resolve_repo_path(outside)

    def test_git_revision_distinguishes_clean_and_dirty(self) -> None:
        repo = self.init_git_repo("git-repo")
        clean = inspect_repository(repo)
        (repo / "main.py").write_text("print('changed')\n", encoding="utf-8")
        dirty = inspect_repository(repo)

        self.assertTrue(clean.is_git)
        self.assertEqual(clean.revision_kind, "git-clean")
        self.assertFalse(clean.is_dirty)
        self.assertEqual(dirty.revision_kind, "git-dirty")
        self.assertTrue(dirty.is_dirty)
        self.assertNotEqual(clean.revision, dirty.revision)

    def test_selected_subdirectory_remains_sandbox_boundary(self) -> None:
        repo = self.init_git_repo("monorepo")
        service = repo / "services" / "billing"
        service.mkdir(parents=True)
        (service / "billing.py").write_text("TOTAL = 0\n", encoding="utf-8")

        context = self.resolver.resolve(repo=service)

        self.assertTrue(context.revision_kind.startswith("git-"))
        self.assertEqual(context.git_root, repo.resolve())
        self.assertEqual(context.repo_root, service.resolve())
        with self.assertRaises(PathOutsideRepositoryError):
            context.resolve_repo_path(repo / "main.py")

    def test_corrupted_registry_fails_closed(self) -> None:
        self.registry.storage_path.parent.mkdir(parents=True)
        self.registry.storage_path.write_text(
            json.dumps({"schema_version": 999, "projects": []}),
            encoding="utf-8",
        )
        with self.assertRaises(RegistryCorruptedError):
            self.registry.list()

    def test_invalid_registry_entry_type_fails_closed(self) -> None:
        self.registry.storage_path.parent.mkdir(parents=True)
        self.registry.storage_path.write_text(
            json.dumps({"schema_version": 1, "projects": ["not-an-object"]}),
            encoding="utf-8",
        )
        with self.assertRaises(RegistryCorruptedError):
            self.registry.list()


if __name__ == "__main__":
    unittest.main()
