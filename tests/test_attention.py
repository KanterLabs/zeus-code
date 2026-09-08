from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zeus_code.workspace import Workspace


def thread(thread_id: str, state: str, project_id: str = "project-1") -> dict:
    return {
        "id": thread_id,
        "project_id": project_id,
        "title": f"Thread {thread_id}",
        "state": state,
        "archived": False,
    }


def event(seq: int, thread_id: str, kind: str, data: dict, run_id: str | None = None) -> dict:
    value = {"seq": seq, "thread_id": thread_id, "kind": kind, "data": data}
    if run_id is not None:
        value["run_id"] = run_id
    return value


class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temporary.name) / "client.json"
        self.workspace = Workspace(cache_path=self.cache_path)
        machine = self.workspace.machines["local"]
        machine["server_id"] = "server-a"
        machine["snapshot"] = {
            "server_id": "server-a",
            "projects": [{"id": "project-1", "name": "Zeus"}],
            "threads": [thread("thread-1", "completed"), thread("thread-2", "running")],
            "approvals": [],
            "last_seq": 0,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def apply(self, value: dict, machine_id: str = "local") -> None:
        Workspace._apply_event(self.workspace.machines[machine_id], value)
        self.workspace._changed(force=True)

    def test_counts_distinct_unread_results_without_transport_noise(self):
        self.apply(event(1, "thread-1", "message", {"role": "user", "text": "go"}, "run-1"))
        self.apply(event(2, "thread-1", "status", {"phase": "thinking"}, "run-1"))
        self.apply(event(3, "thread-1", "message_delta", {"text": "part one"}, "run-1"))
        self.apply(event(4, "thread-1", "message_delta", {"text": "part two"}, "run-1"))
        self.apply(event(5, "thread-1", "run_state", {"state": "completed"}, "run-1"))
        self.apply(event(6, "thread-1", "message", {"role": "assistant", "text": "another"}, "run-2"))
        self.apply(event(7, "thread-2", "tool", {"title": "no badge"}, "run-3"))

        self.assertEqual(self.workspace.unread_count("local", "thread-1"), 2)
        self.assertEqual(self.workspace.unread_count("local", "thread-2"), 0)
        items = self.workspace.attention_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(
            items[0],
            {
                "machine_id": "local",
                "thread_id": "thread-1",
                "title": "Thread thread-1",
                "project_name": "Zeus",
                "machine_name": "local",
                "state": "completed",
                "unread_count": 2,
                "needs_approval": False,
                "stale": True,
            },
        )

    def test_seen_marker_advances_only_after_visible_selected_live_tail_draw(self):
        self.apply(event(1, "thread-1", "message_delta", {"text": "answer"}, "run-1"))
        self.workspace.switch("local", "project-1", "thread-2")
        self.assertFalse(self.workspace.mark_thread_seen("local", "thread-1", visible=True))
        self.workspace.switch("local", "project-1", "thread-1")
        self.workspace.set_draft("preserve this")

        self.assertFalse(self.workspace.mark_thread_seen("local", "thread-1", visible=False))
        self.workspace.set_scroll(5)
        self.assertFalse(self.workspace.mark_thread_seen("local", "thread-1", visible=True))
        self.workspace.set_scroll(0)
        self.workspace.machines["local"]["seen_threads"]["thread-1"] = {
            "server_id": "server-a",
            "seq": "invalid",
        }
        self.assertTrue(self.workspace.mark_thread_seen("local", "thread-1", visible=True))
        self.assertFalse(self.workspace.mark_thread_seen("local", "thread-1", visible=True))
        self.assertEqual(self.workspace.unread_count("local", "thread-1"), 0)

        self.apply(event(2, "thread-1", "message_delta", {"text": "later"}, "run-2"))
        self.assertFalse(self.workspace.mark_thread_seen("local", "thread-1", visible=False))
        self.assertEqual(self.workspace.unread_count("local", "thread-1"), 1)

        restored = Workspace(cache_path=self.cache_path)
        self.assertEqual(restored.thread_view("thread-1", "local")["draft"], "preserve this")
        self.assertEqual(restored.unread_count("local", "thread-1"), 1)

    def test_attention_items_span_machines(self):
        remote_id = self.workspace.add_machine("buildbox", "buildbox")
        remote = self.workspace.machines[remote_id["id"]]
        remote["server_id"] = "remote-server"
        remote["stale"] = False
        remote["snapshot"] = {
            "server_id": "remote-server",
            "projects": [{"id": "remote-project", "name": "Backend"}],
            "threads": [thread("remote-thread", "failed", "remote-project")],
            "approvals": [],
            "last_seq": 1,
        }
        self.apply(
            event(1, "remote-thread", "run_state", {"state": "failed"}, "remote-run"),
            remote_id["id"],
        )

        self.assertEqual(
            self.workspace.attention_items(),
            [
                {
                    "machine_id": remote_id["id"],
                    "thread_id": "remote-thread",
                    "title": "Thread remote-thread",
                    "project_name": "Backend",
                    "machine_name": "buildbox",
                    "state": "failed",
                    "unread_count": 1,
                    "needs_approval": False,
                    "stale": False,
                }
            ],
        )

    def test_server_identity_scopes_seen_sequence_and_preserves_draft(self):
        self.apply(event(9, "thread-1", "run_state", {"state": "completed"}, "old-run"))
        self.workspace.switch("local", "project-1", "thread-1")
        self.workspace.set_draft("do not replay or discard")
        self.assertTrue(self.workspace.mark_thread_seen("local", "thread-1", visible=True))

        machine = self.workspace.machines["local"]
        machine["server_id"] = "server-b"
        machine["events"] = {}
        self.apply(event(1, "thread-1", "run_state", {"state": "failed"}, "new-run"))

        self.assertEqual(self.workspace.unread_count("local", "thread-1"), 1)
        self.assertEqual(self.workspace.thread_view()["draft"], "do not replay or discard")
        self.assertTrue(self.workspace.mark_thread_seen("local", "thread-1", visible=True))
        self.assertEqual(
            machine["seen_threads"]["thread-1"],
            {"server_id": "server-b", "seq": 1},
        )

    def test_pending_approval_remains_attention_after_events_are_seen(self):
        machine = self.workspace.machines["local"]
        machine["snapshot"]["threads"][0]["state"] = "awaiting_approval"
        machine["snapshot"]["approvals"] = [
            {"id": "approval-1", "thread_id": "thread-1", "state": "pending"}
        ]
        self.apply(
            event(
                1,
                "thread-1",
                "approval",
                {"id": "approval-1", "approval_state": "pending"},
                "run-1",
            )
        )
        self.workspace.switch("local", "project-1", "thread-1")
        self.assertTrue(self.workspace.mark_thread_seen("local", "thread-1", visible=True))

        item = self.workspace.attention_items()[0]
        self.assertEqual(item["unread_count"], 0)
        self.assertTrue(item["needs_approval"])
        machine["snapshot"]["approvals"][0]["state"] = "resolved"
        self.assertEqual(self.workspace.attention_items(), [])

    def test_additive_cache_default_accepts_existing_version_one_state(self):
        state = self.workspace.state
        state["machines"]["local"].pop("seen_threads")
        state["thread_views"] = {
            "local:thread-1": {"draft": "kept", "scroll": 0, "anchor_seq": None}
        }
        self.cache_path.write_text(json.dumps(state), encoding="utf-8")

        restored = Workspace(cache_path=self.cache_path)
        self.assertEqual(restored.machines["local"]["seen_threads"], {})
        self.assertEqual(restored.thread_view("thread-1", "local")["draft"], "kept")


if __name__ == "__main__":
    unittest.main()
