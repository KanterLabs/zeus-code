import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zeus_code.workspace import CACHE_VERSION, Workspace


def project(project_id: str, name: str, updated_at: str = "2026-09-01T00:00:00Z") -> dict:
    return {"id": project_id, "name": name, "path": f"/work/{project_id}", "updated_at": updated_at}


def thread(
    thread_id: str,
    project_id: str,
    title: str,
    *,
    archived: bool = False,
    state: str = "idle",
    updated_at: str = "2026-09-01T00:00:00Z",
) -> dict:
    return {
        "id": thread_id,
        "project_id": project_id,
        "title": title,
        "provider": "codex",
        "state": state,
        "archived": archived,
        "updated_at": updated_at,
    }


class SendRPC:
    def __init__(
        self, failures: list[Exception] | None = None, *, server_id: str = "server-a"
    ) -> None:
        self.failures = list(failures or [])
        self.server_id = server_id
        self.hello: dict[str, str] | None = None
        self.calls: list[tuple[str, dict]] = []
        self.request_ids: list[str] = []

    async def connect(self) -> dict[str, str]:
        self.hello = {"server_id": self.server_id}
        return self.hello

    async def close(self) -> None:
        self.hello = None

    async def call(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        self.calls.append((method, params))
        if method == "send":
            self.request_ids.append(params["request_id"])
            if self.failures:
                raise self.failures.pop(0)
            return {
                "id": "run-accepted",
                "thread_id": params["thread_id"],
                "prompt": params["prompt"],
                "request_id": params["request_id"],
                "state": "running",
                "created_at": "2026-09-09T00:00:00Z",
                "updated_at": "2026-09-09T00:00:00Z",
            }
        raise AssertionError(f"unexpected RPC method {method}")


class MovingTailRPC:
    """A snapshot client whose first event page already crosses its high-water."""

    def __init__(self) -> None:
        self.event_calls = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def call(self, method: str, params: dict | None = None) -> dict:
        if method == "snapshot":
            return {
                "server_id": "server-moving",
                "projects": [project("p1", "One")],
                "threads": [thread("t1", "p1", "Busy", state="running")],
                "approvals": [],
                "last_seq": 1,
            }
        if method == "providers":
            return {"codex": {"available": True}}
        if method == "events":
            self.event_calls += 1
            if self.event_calls > 1:
                raise AssertionError("catch-up waited for an empty page past the snapshot high-water")
            return {
                "events": [
                    {
                        "seq": 1,
                        "thread_id": "t1",
                        "run_id": "run-moving",
                        "kind": "message",
                        "data": {"role": "user", "text": "keep producing"},
                        "created_at": "2026-09-09T00:00:00Z",
                    },
                    {
                        "seq": 2,
                        "thread_id": "t1",
                        "run_id": "run-moving",
                        "kind": "run_state",
                        "data": {"state": "completed"},
                        "created_at": "2026-09-09T00:00:01Z",
                    },
                ],
                "last_seq": 2,
            }
        raise AssertionError(f"unexpected RPC method {method}")


class GatedSendRPC(SendRPC):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, method: str, params: dict | None = None) -> dict:
        if method != "send":
            return await super().call(method, params)
        params = params or {}
        self.calls.append((method, params))
        self.request_ids.append(params["request_id"])
        self.started.set()
        await self.release.wait()
        return {
            "id": "run-gated",
            "thread_id": params["thread_id"],
            "prompt": params["prompt"],
            "request_id": params["request_id"],
            "state": "running",
        }


class DiffRPC:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> dict:
        return {"server_id": "server-a"}

    async def close(self) -> None:
        self.closed = True

    async def call(self, method: str, params: dict | None = None) -> dict:
        self.calls.append((method, params or {}))
        if method == "diff":
            return {"diff": "+preview", "files": []}
        raise AssertionError(f"unexpected RPC method {method}")


class DailyWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temporary.name) / "state" / "client.json"

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def workspace(self, rpc: object | None = None) -> Workspace:
        return Workspace(cache_path=self.cache_path, rpc_factory=lambda **_: rpc)

    @staticmethod
    def populate(workspace: Workspace) -> None:
        machine = workspace.machines["local"]
        machine["server_id"] = "server-a"
        machine["snapshot"] = {
            "server_id": "server-a",
            "projects": [
                project("p1", "One", "2026-09-01T00:00:00Z"),
                project("p2", "Two", "2026-09-02T00:00:00Z"),
                project("p3", "Three", "2026-09-03T00:00:00Z"),
            ],
            "threads": [
                thread("t1", "p1", "First", updated_at="2026-09-01T00:00:00Z"),
                thread("t2", "p2", "Second", updated_at="2026-09-02T00:00:00Z"),
                thread("t3", "p3", "Archived result", archived=True, updated_at="2026-09-03T00:00:00Z"),
            ],
            "approvals": [],
            "last_seq": 0,
        }
        machine["providers"] = {"codex": {"available": True}}

    async def test_populated_cache_roundtrip_preserves_selection_views_and_preferences(self) -> None:
        workspace = self.workspace()
        self.populate(workspace)
        workspace.switch("local", "p1", "t1")
        workspace.set_draft("unsent daily work")
        workspace.set_scroll(29)
        workspace.set_project_pinned("local", "p1")
        workspace.set_thread_pinned("local", "t1")
        workspace.set_project_collapsed("local", "p2", True)
        workspace.state["future_extension"] = {"preserve": True}
        await workspace.close()

        restored = self.workspace()
        self.assertEqual(restored.state["version"], CACHE_VERSION)
        self.assertEqual(restored.selected_machine_id, "local")
        self.assertEqual(restored.selected_project["id"], "p1")
        self.assertEqual(restored.selected_thread["id"], "t1")
        self.assertEqual(restored.thread_view()["draft"], "unsent daily work")
        self.assertEqual(restored.thread_view()["scroll"], 29)
        self.assertTrue(restored.project_pinned("local", "p1"))
        self.assertTrue(restored.thread_pinned("local", "t1"))
        self.assertTrue(restored.project_collapsed("local", "p2"))
        self.assertEqual(restored.state["future_extension"], {"preserve": True})
        self.assertEqual(restored.selected_machine["connection"], "disconnected")
        self.assertTrue(restored.selected_machine["stale"])
        await restored.close()

    async def test_offline_restore_keeps_unverified_selection_draft_and_run_state(self) -> None:
        self.cache_path.parent.mkdir(parents=True)
        cached = {
            "version": CACHE_VERSION,
            "machines": {
                "local": {
                    "id": "local",
                    "alias": "local",
                    "host": None,
                    "connection": "connected",
                    "stale": False,
                    "snapshot": {},
                    "providers": {},
                    "events": {},
                    "runs": {"remembered-thread": {"id": "run-live", "state": "running", "_last_seq": 0}},
                    "seen_threads": {},
                    "cursor": 0,
                    "server_id": "server-a",
                    "last_error": None,
                }
            },
            "selected_machine": "local",
            "selected_project": "remembered-project",
            "selected_thread": "remembered-thread",
            "thread_views": {
                "local:remembered-thread": {"draft": "offline draft", "scroll": 11}
            },
            "unknown_cache_field": "kept",
        }
        self.cache_path.write_text(json.dumps(cached), encoding="utf-8")

        restored = self.workspace()
        self.assertEqual(restored.state["selected_project"], "remembered-project")
        self.assertEqual(restored.state["selected_thread"], "remembered-thread")
        self.assertEqual(restored.thread_view("remembered-thread", "local")["draft"], "offline draft")
        self.assertEqual(restored.thread_view("remembered-thread", "local")["scroll"], 11)
        self.assertEqual(restored.machines["local"]["runs"]["remembered-thread"]["state"], "running")
        self.assertEqual(restored.state["unknown_cache_field"], "kept")
        self.assertEqual(restored.selected_machine["connection"], "disconnected")
        self.assertTrue(restored.selected_machine["stale"])
        await restored.close()

    async def test_pins_then_recency_sort_and_archived_search_are_cached(self) -> None:
        workspace = self.workspace()
        self.populate(workspace)
        with patch("zeus_code.workspace.time.time", side_effect=[100.0, 200.0]):
            workspace.switch("local", "p1", "t1")
            workspace.switch("local", "p2", "t2")

        self.assertEqual([item["id"] for item in workspace.ordered_projects()], ["p2", "p1", "p3"])
        self.assertEqual([item["id"] for item in workspace.ordered_threads()], ["t2", "t1"])
        workspace.set_project_pinned("local", "p1")
        workspace.set_thread_pinned("local", "t1")
        self.assertEqual([item["id"] for item in workspace.ordered_projects()], ["p1", "p2", "p3"])
        self.assertEqual([item["id"] for item in workspace.ordered_threads()], ["t1", "t2"])
        self.assertEqual(workspace.search_threads("Archived"), [])
        self.assertEqual(
            [item["thread"]["id"] for item in workspace.search_threads("Archived", include_archived=True)],
            ["t3"],
        )
        await workspace.close()

        restored = self.workspace()
        self.assertEqual([item["id"] for item in restored.ordered_projects()], ["p1", "p2", "p3"])
        self.assertEqual([item["id"] for item in restored.ordered_threads()], ["t1", "t2"])
        await restored.close()

    async def test_failed_prompt_recovery_preserves_identity_and_newer_draft(self) -> None:
        workspace = self.workspace()
        self.populate(workspace)
        workspace.switch("local", "p1", "t1")
        Workspace._apply_run_ack(
            workspace.machines["local"],
            "t1",
            {
                "id": "run-failed",
                "state": "failed",
                "prompt": "repair the build",
                "request_id": "request-failed",
                "error": "provider exited",
                "created_at": "2026-09-09T00:00:00Z",
                "updated_at": "2026-09-09T00:01:00Z",
            },
        )
        recovery = workspace.failed_prompt()
        self.assertEqual(
            {key: recovery[key] for key in ("machine_id", "thread_id", "run_id", "request_id", "acceptance")},
            {
                "machine_id": "local",
                "thread_id": "t1",
                "run_id": "run-failed",
                "request_id": "request-failed",
                "acceptance": "accepted",
            },
        )

        workspace.set_draft("newer follow-up")
        with self.assertRaisesRegex(ValueError, "newer draft"):
            workspace.recover_failed_prompt()
        self.assertEqual(workspace.thread_view()["draft"], "newer follow-up")
        workspace.set_draft("")
        self.assertEqual(workspace.recover_failed_prompt()["run_id"], "run-failed")
        self.assertEqual(workspace.thread_view()["draft"], "repair the build")
        await workspace.close()

    async def test_uncertain_acceptance_never_replays_and_retry_keeps_request_identity(self) -> None:
        first_rpc = SendRPC([ConnectionError("reply lost")])
        workspace = self.workspace(first_rpc)
        self.populate(workspace)
        workspace.switch("local", "p1", "t1")
        workspace.set_draft("possibly accepted")
        with self.assertRaises(ConnectionError):
            await workspace.send_prompt()
        request_id = first_rpc.request_ids[0]
        self.assertEqual(workspace.uncertain_send()["request_id"], request_id)
        self.assertEqual(workspace.uncertain_send()["acceptance"], "unknown")
        await workspace.close()

        second_rpc = SendRPC()
        restored = self.workspace(second_rpc)
        self.assertEqual(second_rpc.calls, [])
        dismissed = restored.dismiss_uncertain()
        self.assertEqual(dismissed["request_id"], request_id)
        self.assertEqual(dismissed["acceptance"], "unknown")
        restored.set_draft("possibly accepted")
        with self.assertRaisesRegex(ValueError, "acceptance is still unknown"):
            await restored.send_prompt()
        self.assertEqual(second_rpc.request_ids, [])

        await restored.retry_uncertain()
        self.assertEqual(second_rpc.request_ids, [request_id])
        self.assertIsNone(restored.uncertain_send())
        self.assertEqual(restored.state["send_recovery_evidence"][request_id]["acceptance"], "accepted")
        await restored.close()

    async def test_sync_stops_at_snapshot_high_water_and_applies_crossing_page(self) -> None:
        rpc = MovingTailRPC()
        workspace = self.workspace(rpc)

        await asyncio.wait_for(workspace.sync_machine("local"), timeout=1)

        self.assertEqual(rpc.event_calls, 1)
        self.assertEqual(workspace.selected_machine["connection"], "connected")
        self.assertFalse(workspace.selected_machine["stale"])
        self.assertEqual(workspace.selected_machine["cursor"], 2)
        self.assertEqual(workspace.threads()[0]["state"], "completed")
        self.assertIsInstance(workspace.selected_machine["last_connected_at"], float)

        workspace.cache._dirty = False
        with patch.object(workspace.cache, "mark_dirty") as marked_dirty:
            await workspace.sync_machine("local")
        marked_dirty.assert_not_called()
        self.assertEqual(rpc.event_calls, 1)
        await workspace.close()

    async def test_retry_refuses_replacement_server_and_preserves_unknown_evidence(self) -> None:
        rpc = SendRPC([ConnectionError("reply lost")])
        workspace = self.workspace(rpc)
        self.populate(workspace)
        workspace.switch("local", "p1", "t1")
        workspace.set_draft("may have run on the old server")
        with self.assertRaises(ConnectionError):
            await workspace.send_prompt()
        request_id = rpc.request_ids[0]
        self.assertEqual(workspace.uncertain_send()["server_id"], "server-a")

        replacement = SendRPC(server_id="server-replacement")
        await workspace._drop_client("local")
        workspace.rpc_factory = lambda **_: replacement
        workspace.machines["local"]["server_id"] = "server-replacement"
        workspace.machines["local"]["snapshot"]["server_id"] = "server-replacement"
        recovery = workspace.uncertain_send()
        self.assertEqual(recovery["server_scope"], "server_changed")
        self.assertFalse(recovery["retryable"])
        with self.assertRaisesRegex(ValueError, "previous server instance"):
            await workspace.retry_uncertain()

        self.assertEqual(rpc.request_ids, [request_id])
        self.assertEqual(replacement.request_ids, [])
        self.assertEqual(workspace.uncertain_send()["acceptance"], "unknown")
        self.assertEqual(workspace.thread_view()["draft"], "may have run on the old server")
        self.assertEqual(
            workspace.state["send_recovery_evidence"][request_id]["acceptance"], "unknown"
        )
        await workspace.close()

    async def test_send_ack_cannot_erase_an_aba_followup_draft(self) -> None:
        rpc = GatedSendRPC()
        workspace = self.workspace(rpc)
        self.populate(workspace)
        workspace.switch("local", "p1", "t1")
        workspace.set_draft("same text")

        sending = asyncio.create_task(workspace.send_prompt())
        await rpc.started.wait()
        workspace.set_draft("a newer follow-up")
        workspace.set_draft("same text")
        rpc.release.set()
        await sending

        self.assertEqual(workspace.thread_view()["draft"], "same text")
        await workspace.close()

    async def test_independent_diff_does_not_replace_or_block_poll_transport(self) -> None:
        clients: list[DiffRPC] = []

        def factory(**_: object) -> DiffRPC:
            client = DiffRPC()
            clients.append(client)
            return client

        workspace = Workspace(cache_path=self.cache_path, rpc_factory=factory)
        self.populate(workspace)
        workspace.switch("local", "p1", "t1")
        shared = await workspace._client("local")

        result = await workspace.get_diff(independent=True)

        self.assertEqual(result["diff"], "+preview")
        self.assertEqual(len(clients), 2)
        self.assertIs(workspace.clients["local"], shared)
        self.assertFalse(shared.closed)
        self.assertTrue(clients[1].closed)
        self.assertEqual(clients[1].calls, [("diff", {"thread_id": "t1"})])
        await workspace.close()


if __name__ == "__main__":
    unittest.main()
