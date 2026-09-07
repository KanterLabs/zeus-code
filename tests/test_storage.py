from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from zeus_code.storage import INTERRUPTED_ERROR, Store, StoreError


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        Path(".work").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=".work")
        self.data_dir = Path(self.temp.name) / "data"
        self.store = Store(self.data_dir)
        self.project = self.store.add_project("/repo", "Repo", "main")
        self.thread = self.store.create_thread(
            self.project["id"], "Thread", "codex", "/repo", "main", False
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_duplicate_send_is_stable_and_overlap_is_rejected(self) -> None:
        run, created = self.store.create_run(self.thread["id"], "hello", "request-1")
        self.assertEqual(run, self.store.find_run_by_request("request-1"))
        self.assertIsNone(self.store.find_run_by_request("missing-request"))
        again, created_again = self.store.create_run(
            self.thread["id"], "hello", "request-1"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(run["id"], again["id"])
        self.assertEqual(2, len(self.store.events()["events"]))
        self.assertEqual(
            {"role": "user", "text": "hello"},
            self.store.events()["events"][0]["data"],
        )
        with self.assertRaises(ValueError):
            self.store.create_run(self.thread["id"], "changed", "request-1")
        with self.assertRaises(ValueError):
            self.store.create_run(self.thread["id"], "other", "request-2")

    def test_concurrent_run_guard_allows_exactly_one_start(self) -> None:
        def start(number: int) -> tuple[str, bool] | str:
            try:
                run, created = self.store.create_run(
                    self.thread["id"], f"prompt-{number}", f"request-{number}"
                )
                return run["id"], created
            except ValueError as exc:
                return str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, (1, 2)))
        successes = [result for result in results if isinstance(result, tuple)]
        failures = [result for result in results if isinstance(result, str)]
        self.assertEqual(1, len(successes))
        self.assertEqual(["thread already has an active run"], failures)

    def test_event_cursor_pages_without_skipping(self) -> None:
        run, _ = self.store.create_run(self.thread["id"], "hello", "request-1")
        for number in range(5):
            self.store.append_event(
                self.thread["id"], run["id"], "output", {"number": number}
            )
        seen: list[int] = []
        cursor = 0
        while True:
            page = self.store.events(after=cursor, limit=2)
            if not page["events"]:
                break
            seen.extend(event["seq"] for event in page["events"])
            cursor = page["last_seq"]
        self.assertEqual(list(range(1, 8)), seen)
        history = self.store.history(self.thread["id"], limit=2)
        self.assertTrue(history["has_more"])
        self.assertEqual([6, 7], [item["seq"] for item in history["events"]])

    def test_wire_byte_budget_preserves_all_events_across_pages(self) -> None:
        run, _ = self.store.create_run(self.thread["id"], "hello", "request-1")
        for number in range(70):
            self.store.append_event(
                self.thread["id"], run["id"], "output", {"text": "x" * 8192, "n": number}
            )
        first = self.store.events(limit=200)
        self.assertLess(len(first["events"]), 72)
        self.assertLess(len(json.dumps(first).encode()), 530 * 1024)
        seen: list[int] = []
        cursor = 0
        while True:
            page = self.store.events(after=cursor, limit=200)
            if not page["events"]:
                break
            seen.extend(item["seq"] for item in page["events"])
            cursor = page["last_seq"]
        self.assertEqual(list(range(1, 73)), seen)
        history = self.store.history(self.thread["id"], limit=200)
        self.assertTrue(history["has_more"])
        self.assertLess(len(json.dumps(history).encode()), 530 * 1024)

    def test_approval_resolution_is_exact_and_idempotent(self) -> None:
        run, _ = self.store.create_run(self.thread["id"], "hello", "request-1")
        first = self.store.create_approval(
            run["id"], {"provider_request_id": "approval-1"}
        )
        second = self.store.create_approval(
            run["id"], {"provider_request_id": "approval-2"}
        )
        self.assertNotEqual("approval-1", first["id"])
        uuid.UUID(first["id"])
        self.assertEqual(
            first,
            self.store.create_approval(
                run["id"], {"provider_request_id": "approval-1"}
            ),
        )
        self.assertEqual("awaiting_approval", self.store.thread(self.thread["id"])["state"])
        resolved = self.store.resolve_approval(first["id"], "allow")
        self.assertEqual("resolved", resolved["state"])
        self.assertEqual("awaiting_approval", self.store.run(run["id"])["state"])
        self.assertEqual(resolved, self.store.resolve_approval(first["id"], "allow"))
        with self.assertRaises(ValueError):
            self.store.resolve_approval(first["id"], "reject")
        self.store.resolve_approval(second["id"], "reject")
        self.assertEqual("running", self.store.run(run["id"])["state"])
        self.assertEqual([], self.store.approvals())

    def test_finish_invalidates_only_its_approvals(self) -> None:
        other = self.store.create_thread(
            self.project["id"], "Other", "opencode", "/repo", "main", False
        )
        run, _ = self.store.create_run(self.thread["id"], "one", "request-1")
        other_run, _ = self.store.create_run(other["id"], "two", "request-2")
        approval = self.store.create_approval(run["id"], {"id": "approval-1"})
        other_approval = self.store.create_approval(
            other_run["id"], {"id": "approval-2"}
        )
        self.store.finish_run(run["id"], "cancelled")
        self.assertEqual("invalidated", self.store.approval(approval["id"])["state"])
        self.assertEqual("pending", self.store.approval(other_approval["id"])["state"])
        with self.assertRaises(ValueError):
            self.store.resolve_approval(approval["id"], "allow")

    def test_restart_preserves_data_and_recovery_never_replays(self) -> None:
        self.store.update_thread(
            self.thread["id"], draft="saved", scroll=17, session_id="session-1"
        )
        run, _ = self.store.create_run(self.thread["id"], "hello", "request-1")
        self.store.create_approval(run["id"], {"id": "approval-1"})
        server_id = self.store.server_id
        self.store.close()

        self.store = Store(self.data_dir)
        self.assertEqual(server_id, self.store.server_id)
        self.assertEqual("saved", self.store.thread(self.thread["id"])["draft"])
        self.assertEqual(run["id"], self.store.find_run_by_request("request-1")["id"])
        self.assertEqual(1, self.store.recover_interrupted())
        recovered = self.store.run(run["id"])
        self.assertEqual("failed", recovered["state"])
        self.assertEqual(INTERRUPTED_ERROR, recovered["error"])
        self.assertEqual("session-1", self.store.thread(self.thread["id"])["session_id"])
        self.assertEqual([], self.store.approvals())
        self.assertEqual(0, self.store.recover_interrupted())

    def test_reopens_populated_version_zero_database_without_reset(self) -> None:
        project_id = self.project["id"]
        self.store.close()
        db = sqlite3.connect(self.store.path)
        db.execute("PRAGMA user_version = 0")
        db.commit()
        db.close()
        self.store = Store(self.data_dir)
        self.assertEqual(project_id, self.store.project(project_id)["id"])

    def test_refuses_newer_schema(self) -> None:
        self.store.close()
        db = sqlite3.connect(self.store.path)
        db.execute("PRAGMA user_version = 99")
        db.commit()
        db.close()
        with self.assertRaises(StoreError):
            Store(self.data_dir)
        db = sqlite3.connect(self.store.path)
        self.assertEqual(99, db.execute("PRAGMA user_version").fetchone()[0])
        db.close()

    def test_backup_is_verified_and_refuses_overwrite(self) -> None:
        target = Path(self.temp.name) / "backup.db"
        self.assertEqual(target, self.store.backup(target))
        self.assertEqual(0o600, os.stat(target).st_mode & 0o777)
        db = sqlite3.connect(target)
        self.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(1, db.execute("SELECT COUNT(*) FROM projects").fetchone()[0])
        db.close()
        with self.assertRaises(FileExistsError):
            self.store.backup(target)

    def test_permissions_and_update_whitelist(self) -> None:
        self.assertEqual(0o700, os.stat(self.data_dir).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(self.store.path).st_mode & 0o777)
        with self.assertRaises(ValueError):
            self.store.update_thread(self.thread["id"], provider="opencode")


if __name__ == "__main__":
    unittest.main()
