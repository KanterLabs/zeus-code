from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from zeus_code.repository import (
    DIFF_LIMIT,
    RepositoryError,
    _UNTRACKED_STATS_LIMIT,
    create_worktree,
    get_diff,
    inspect_repository,
)


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "LC_ALL": "C"},
    )


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        work = Path(__file__).resolve().parents[1] / ".work"
        work.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=work)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Zeus Tests")
        git(self.root, "config", "user.email", "zeus@example.invalid")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def commit(self, name: str = "tracked.txt", content: str = "one\ntwo\n") -> None:
        (self.root / name).write_text(content)
        git(self.root, "add", "--", name)
        git(self.root, "commit", "-qm", "initial")

    async def test_inspect_finds_canonical_root_and_detached_head(self) -> None:
        self.commit()
        child = self.root / "nested"
        child.mkdir()
        result = await inspect_repository(str(child))
        self.assertEqual(result, {"path": str(self.root.resolve()), "branch": "main"})

        git(self.root, "checkout", "--detach", "-q")
        detached = await inspect_repository(str(self.root))
        self.assertRegex(detached["branch"], r"^detached@[0-9a-f]{12}$")

    async def test_worktree_creates_new_branch_and_refuses_existing_paths(self) -> None:
        self.commit()
        destination = Path(self.temporary.name) / "work tree"
        result = await create_worktree(str(self.root), str(destination), "feature/review")
        self.assertEqual(result, {"cwd": str(destination.resolve()), "branch": "feature/review"})
        self.assertTrue((destination / "tracked.txt").is_file())
        self.assertEqual(git(destination, "branch", "--show-current").stdout.strip(), "feature/review")

        marker = Path(self.temporary.name) / "occupied"
        marker.write_text("keep")
        with self.assertRaises(RepositoryError):
            await create_worktree(str(self.root), str(marker), "another")
        self.assertEqual(marker.read_text(), "keep")

    async def test_worktree_requires_a_commit(self) -> None:
        destination = Path(self.temporary.name) / "unborn-worktree"
        with self.assertRaises(RepositoryError):
            await create_worktree(str(self.root), str(destination), "feature")
        self.assertFalse(destination.exists())

    async def test_diff_combines_staged_unstaged_untracked_and_binary(self) -> None:
        self.commit(content="one\ntwo\nthree\n")
        tracked = self.root / "tracked.txt"
        tracked.write_text("ONE\ntwo\nthree\n")
        git(self.root, "add", "--", "tracked.txt")
        tracked.write_text("ONE\ntwo\nTHREE\n")
        (self.root / "new file.txt").write_text("alpha\nbeta\n")
        (self.root / "binary.bin").write_bytes(b"before\0after")

        result = await get_diff(str(self.root))
        by_path = {entry["path"]: entry for entry in result["files"]}
        self.assertEqual(by_path["tracked.txt"]["status"], "MM")
        self.assertEqual((by_path["tracked.txt"]["additions"], by_path["tracked.txt"]["deletions"]), (2, 2))
        self.assertEqual(by_path["new file.txt"]["status"], "??")
        self.assertEqual(by_path["new file.txt"]["additions"], 2)
        self.assertEqual(by_path["binary.bin"]["additions"], 0)
        self.assertIn("ONE", result["diff"])
        self.assertIn("THREE", result["diff"])
        self.assertIn("new file.txt", result["diff"])
        self.assertIn("Binary files", result["diff"])
        self.assertFalse(result["truncated"])

    async def test_unborn_repository_includes_index_worktree_and_untracked(self) -> None:
        staged = self.root / "staged.txt"
        staged.write_text("staged\n")
        git(self.root, "add", "--", "staged.txt")
        staged.write_text("current\n")
        (self.root / "loose.txt").write_text("loose\n")

        result = await get_diff(str(self.root))
        by_path = {entry["path"]: entry for entry in result["files"]}
        self.assertEqual(result["branch"], "main")
        self.assertEqual(by_path["staged.txt"]["additions"], 1)
        self.assertEqual(by_path["loose.txt"]["additions"], 1)
        self.assertIn("current", result["diff"])
        self.assertIn("loose", result["diff"])

    async def test_paths_with_spaces_quotes_and_newlines_are_parsed(self) -> None:
        self.commit()
        names = ['space name.txt', 'a"quote.txt', "line\nbreak.txt"]
        for name in names:
            (self.root / name).write_text("content\n")

        result = await get_diff(str(self.root))
        paths = {entry["path"] for entry in result["files"]}
        self.assertTrue(set(names).issubset(paths))
        selected = await get_diff(str(self.root), names[0])
        self.assertEqual([entry["path"] for entry in selected["files"]], [names[0]])

    async def test_rename_records_use_the_new_nul_delimited_path(self) -> None:
        self.commit(name="old name.txt", content="same\n")
        new_name = "new\nname.txt"
        git(self.root, "mv", "--", "old name.txt", new_name)

        result = await get_diff(str(self.root))
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["path"], new_name)
        self.assertEqual(result["files"][0]["status"], "R")
        self.assertEqual(result["files"][0]["additions"], 0)
        self.assertEqual(result["files"][0]["deletions"], 0)

    async def test_path_selection_rejects_escape_and_does_not_follow_symlink(self) -> None:
        self.commit()
        outside = Path(self.temporary.name) / "secret.txt"
        outside.write_text("do not expose\n")
        (self.root / "outside-link").symlink_to(outside)

        for unsafe in ("../secret.txt", str(outside.resolve())):
            with self.assertRaises(ValueError):
                await get_diff(str(self.root), unsafe)
        result = await get_diff(str(self.root))
        link = next(entry for entry in result["files"] if entry["path"] == "outside-link")
        self.assertEqual((link["additions"], link["deletions"]), (0, 0))
        self.assertNotIn("do not expose", result["diff"])

    async def test_diff_output_is_bounded(self) -> None:
        self.commit()
        (self.root / "large.txt").write_text("x" * (DIFF_LIMIT * 2))
        result = await get_diff(str(self.root))
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["diff"].encode()), DIFF_LIMIT)

    async def test_oversized_untracked_stats_are_explicit_and_file_is_selectable(self) -> None:
        self.commit()
        large = self.root / "oversized.bin"
        with large.open("wb") as stream:
            stream.seek(_UNTRACKED_STATS_LIMIT)
            stream.write(b"x")

        result = await get_diff(str(self.root))
        entry = next(item for item in result["files"] if item["path"] == large.name)
        self.assertIsNone(entry["additions"])
        self.assertIsNone(entry["deletions"])
        self.assertEqual(entry["stats_unavailable"], "safety_limit")
        self.assertTrue(entry["diff_omitted"])
        self.assertNotIn(large.name, result["diff"])

        selected = await get_diff(str(self.root), large.name)
        selected_entry = selected["files"][0]
        self.assertNotIn("diff_omitted", selected_entry)
        self.assertIn(large.name, selected["diff"])

    async def test_repository_diff_helpers_are_disabled(self) -> None:
        self.commit()
        attributes = self.root / ".gitattributes"
        attributes.write_text("tracked.txt diff=unsafe\n")
        git(self.root, "add", "--", ".gitattributes")
        git(self.root, "commit", "-qm", "attributes")
        marker = Path(self.temporary.name) / "helper-called"
        helper = Path(self.temporary.name) / "diff-helper"
        helper.write_text(f"#!/bin/sh\nprintf called > {marker}\nexit 1\n")
        helper.chmod(0o700)
        git(self.root, "config", "diff.external", str(helper))
        git(self.root, "config", "diff.unsafe.textconv", str(helper))
        (self.root / "tracked.txt").write_text("changed\n")

        result = await get_diff(str(self.root))
        self.assertIn("changed", result["diff"])
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
