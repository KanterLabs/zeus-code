"""Bounded project discovery for local and SSH-hosted daemons."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from zeus_code.daemon import Daemon
from zeus_code import repository
from zeus_code.repository import RepositoryError, discover_projects


class ProjectDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zeus-discovery-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def repository(path: Path, *, worktree: bool = False) -> Path:
        path.mkdir(parents=True)
        marker = path / ".git"
        if worktree:
            marker.write_text("gitdir: /example/common/worktrees/example\n")
        else:
            marker.mkdir()
        return path

    async def test_discovers_git_directories_and_worktree_files_then_stops_descent(self) -> None:
        ordinary = self.repository(self.root / "projects" / "ordinary")
        worktree = self.repository(self.root / "teams" / "worktree", worktree=True)
        self.repository(ordinary / "nested-repository")
        self.repository(self.root / "node_modules" / "dependency")
        self.repository(self.root / "vendor" / "vendored")
        self.repository(self.root / "build" / "generated")
        self.repository(self.root / ".hidden" / "private")

        with mock.patch.object(repository, "_git", side_effect=AssertionError("discovery ran Git")):
            result = await discover_projects(str(self.root))

        self.assertEqual(result["root"], str(self.root.resolve()))
        self.assertEqual(
            result["projects"],
            [
                {"path": str(ordinary), "name": "ordinary"},
                {"path": str(worktree), "name": "worktree"},
            ],
        )
        self.assertFalse(result["truncated"])

    async def test_rpc_expands_and_canonicalizes_root_without_registering_projects(self) -> None:
        projects = self.root / "projects"
        repo = self.repository(projects / "remote-repo")
        alias = self.root / "project-alias"
        alias.symlink_to(projects, target_is_directory=True)
        daemon = Daemon(self.root / "state", providers={})
        self.assertTrue(await daemon.start())
        try:
            with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
                result = await daemon.dispatch("discover_projects", {"root": "~/project-alias"})
            self.assertEqual(result["root"], str(projects.resolve()))
            self.assertEqual(result["projects"], [{"path": str(repo), "name": "remote-repo"}])
            self.assertEqual(daemon.store.projects(), [])
        finally:
            await daemon.close()

    async def test_does_not_follow_directory_or_git_marker_symlinks(self) -> None:
        scan_root = self.root / "scan"
        scan_root.mkdir()
        outside = self.repository(self.root / "outside")
        (scan_root / "outside-link").symlink_to(outside, target_is_directory=True)
        (scan_root / "loop").symlink_to(scan_root, target_is_directory=True)
        fake = scan_root / "fake"
        fake.mkdir()
        (fake / ".git").symlink_to(outside / ".git", target_is_directory=True)

        result = await discover_projects(str(scan_root))

        self.assertEqual(result["projects"], [])
        self.assertFalse(result["truncated"])

    async def test_depth_directory_and_project_limits_report_truncation(self) -> None:
        deep = self.root / "deep"
        self.repository(deep / "one" / "two" / "three" / "four" / "allowed")
        depth_result = await discover_projects(str(deep))
        self.assertEqual(depth_result["projects"], [])
        self.assertTrue(depth_result["truncated"])

        many = self.root / "many"
        for index in range(3):
            self.repository(many / f"repo-{index}")
        with mock.patch.object(repository, "DISCOVERY_MAX_PROJECTS", 2):
            project_result = await discover_projects(str(many))
        self.assertEqual(len(project_result["projects"]), 2)
        self.assertTrue(project_result["truncated"])

        wide = self.root / "wide"
        for name in ("a", "b", "c"):
            (wide / name).mkdir(parents=True)
        with mock.patch.object(repository, "DISCOVERY_MAX_DIRECTORIES", 2):
            directory_result = await discover_projects(str(wide))
        self.assertEqual(directory_result["projects"], [])
        self.assertTrue(directory_result["truncated"])

    async def test_missing_or_nondirectory_root_has_actionable_error(self) -> None:
        missing = self.root / "missing"
        with self.assertRaisesRegex(RepositoryError, "does not exist"):
            await discover_projects(str(missing))

        file_root = self.root / "file"
        file_root.write_text("not a directory\n")
        with self.assertRaisesRegex(RepositoryError, "not a directory"):
            await discover_projects(str(file_root))


if __name__ == "__main__":
    unittest.main()
