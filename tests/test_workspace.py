import asyncio
import json
import stat
import tempfile
import unittest
from pathlib import Path

from zeus_code.client import RPCError
from zeus_code.workspace import EVENT_PAGE_SIZE, MAX_EVENTS_PER_THREAD, Workspace


def thread(thread_id="t1", state="running"):
    return {
        "id": thread_id,
        "project_id": "p1",
        "title": "Build feature",
        "provider": "codex",
        "cwd": "/repo",
        "branch": "main",
        "state": state,
        "archived": False,
        "updated_at": "2026-09-07T00:00:00Z",
    }


class FakeRPC:
    def __init__(self, events=None):
        self.connected = False
        self.closed = False
        self.fail_snapshot = False
        self.events = list(events or [])
        self.calls = []
        self.send_failures = []
        self.sent_request_ids = []
        self.sent_prompts = []
        self.send_started = None
        self.send_gate = None
        self.server_id = "server-a"
        self.snapshot_started = None
        self.snapshot_gate = None
        self.add_project_started = None
        self.add_project_gate = None
        self.create_thread_started = None
        self.create_thread_gate = None

    async def connect(self):
        await asyncio.sleep(0)
        self.connected = True

    async def close(self):
        self.closed = True

    async def call(self, method, params=None):
        params = params or {}
        self.calls.append((method, params))
        if method == "snapshot":
            if self.snapshot_started is not None:
                self.snapshot_started.set()
            if self.snapshot_gate is not None:
                await self.snapshot_gate.wait()
            if self.fail_snapshot:
                raise ConnectionError("host vanished")
            return {
                "server_id": self.server_id,
                "projects": [{"id": "p1", "name": "Repo", "path": "/repo"}],
                "threads": [thread()],
                "approvals": [],
                "last_seq": len(self.events),
            }
        if method == "providers":
            return {"codex": {"available": True, "detail": "ready"}}
        if method == "events":
            after = params["after"]
            page = [event for event in self.events if event["seq"] > after][: params["limit"]]
            return {"events": page, "last_seq": page[-1]["seq"] if page else after}
        if method == "send":
            self.sent_request_ids.append(params["request_id"])
            self.sent_prompts.append(params["prompt"])
            if self.send_started is not None:
                self.send_started.set()
            if self.send_gate is not None:
                await self.send_gate.wait()
            if self.send_failures:
                raise self.send_failures.pop(0)
            return {"id": "run1", "thread_id": params["thread_id"], "state": "running"}
        if method == "add_project":
            if self.add_project_started is not None:
                self.add_project_started.set()
            if self.add_project_gate is not None:
                await self.add_project_gate.wait()
            return {
                "id": "p-new",
                "name": params.get("name") or "new-repo",
                "path": params["path"],
                "branch": "main",
                "created_at": "2026-09-07T00:01:00Z",
                "updated_at": "2026-09-07T00:01:00Z",
            }
        if method == "create_thread":
            if self.create_thread_started is not None:
                self.create_thread_started.set()
            if self.create_thread_gate is not None:
                await self.create_thread_gate.wait()
            return {
                **thread("t-new", "idle"),
                "title": params["title"],
                "provider": params["provider"],
                "model": params.get("model"),
                "settings": params.get("settings"),
                "isolated": params["worktree"],
            }
        if method == "update_thread":
            return {**thread(params["thread_id"]), **params}
        if method == "history":
            return {"events": [], "has_more": False}
        raise AssertionError(f"unexpected RPC method {method}")


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temporary.name) / "state" / "client.json"

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def make_workspace(self, rpc):
        return Workspace(cache_path=self.cache_path, rpc_factory=lambda **_: rpc, poll_interval=0.01)

    async def test_cache_preserves_machine_drafts_scroll_and_selection_privately(self):
        rpc = FakeRPC()
        workspace = self.make_workspace(rpc)
        await workspace.sync_machine("local")
        workspace.switch("local", "p1", "t1")
        workspace.set_draft("unsent follow-up")
        workspace.set_scroll(37)
        remote = workspace.add_machine("homelab", "homelab")
        workspace.switch(remote["id"])
        workspace.switch("local", "p1", "t1")
        self.assertEqual(
            workspace.thread_view(),
            {"draft": "unsent follow-up", "scroll": 37, "anchor_seq": None, "history_events": None},
        )
        await workspace.close()

        self.assertEqual(stat.S_IMODE(self.cache_path.stat().st_mode), 0o600)
        raw = json.loads(self.cache_path.read_text())
        self.assertIn(remote["id"], raw["machines"])
        restored = self.make_workspace(FakeRPC())
        self.assertEqual(
            restored.thread_view(),
            {"draft": "unsent follow-up", "scroll": 37, "anchor_seq": None, "history_events": None},
        )
        # A prior connected status is never trusted after client restart.
        self.assertEqual(restored.selected_machine["connection"], "disconnected")
        self.assertTrue(restored.selected_machine["stale"])
        await restored.close()

    async def test_event_replay_drains_pages_deduplicates_and_bounds_each_thread(self):
        count = EVENT_PAGE_SIZE * 2 + 17
        events = [
            {"seq": seq, "thread_id": "t1", "run_id": "r1", "kind": "message_delta", "data": {"text": str(seq)}}
            for seq in range(1, count + 1)
        ]
        rpc = FakeRPC(events)
        workspace = self.make_workspace(rpc)
        await workspace.sync_machine("local")
        self.assertEqual(workspace.selected_machine["cursor"], count)
        self.assertGreaterEqual(sum(1 for method, _ in rpc.calls if method == "events"), 4)
        cached = workspace.thread_events("t1")
        self.assertEqual(len(cached), min(count, MAX_EVENTS_PER_THREAD))
        self.assertEqual(cached[-1]["seq"], count)

        # Replaying an overlapping page cannot duplicate events or move cursor back.
        await workspace._drain_events("local", rpc)
        self.assertEqual(len(workspace.thread_events("t1")), min(count, MAX_EVENTS_PER_THREAD))
        self.assertEqual(workspace.selected_machine["cursor"], count)
        await workspace.close()

    async def test_disconnect_marks_snapshot_stale_without_failing_running_thread(self):
        rpc = FakeRPC()
        workspace = self.make_workspace(rpc)
        await workspace.sync_machine("local")
        self.assertEqual(workspace.threads()[0]["state"], "running")
        rpc.fail_snapshot = True
        await workspace.sync_machine("local")
        self.assertEqual(workspace.selected_machine["connection"], "disconnected")
        self.assertTrue(workspace.selected_machine["stale"])
        self.assertEqual(workspace.threads()[0]["state"], "running")
        await workspace.close()

    async def test_uncertain_send_keeps_draft_and_explicit_retry_reuses_request_id(self):
        rpc = FakeRPC()
        workspace = self.make_workspace(rpc)
        await workspace.sync_machine("local")
        workspace.switch("local", "p1", "t1")
        workspace.set_draft("do the work")
        rpc.send_failures.append(ConnectionError("reply lost"))
        with self.assertRaises(ConnectionError):
            await workspace.send_prompt()
        self.assertEqual(workspace.thread_view()["draft"], "do the work")
        request_id = rpc.sent_request_ids[-1]

        await workspace.retry_uncertain()
        self.assertEqual(rpc.sent_request_ids, [request_id, request_id])
        self.assertEqual(workspace.thread_view()["draft"], "")
        self.assertFalse(workspace.state["uncertain_sends"])
        await workspace.close()

    async def test_definitive_rpc_error_is_not_recorded_as_uncertain(self):
        rpc = FakeRPC()
        workspace = self.make_workspace(rpc)
        await workspace.sync_machine("local")
        workspace.switch("local", "p1", "t1")
        rpc.send_failures.append(RPCError("busy", "thread already running"))
        with self.assertRaises(RPCError):
            await workspace.send_prompt("next")
        self.assertFalse(workspace.state["uncertain_sends"])
        await workspace.close()

    async def test_inflight_send_never_clears_followup_or_redirects_after_switch(self):
        rpc = FakeRPC()
        rpc.send_started = asyncio.Event()
        rpc.send_gate = asyncio.Event()
        workspace = self.make_workspace(rpc)
        await workspace.sync_machine("local")
        workspace.switch("local", "p1", "t1")
        workspace.set_draft("first prompt")
        task = asyncio.create_task(
            workspace.send_prompt("first prompt", machine_id="local", thread_id="t1")
        )
        await rpc.send_started.wait()
        workspace.set_draft("follow-up typed while sending")
        remote = workspace.add_machine("box", "box")
        workspace.switch(remote["id"])
        rpc.send_gate.set()
        await task
        self.assertEqual(rpc.sent_prompts, ["first prompt"])
        self.assertEqual(
            workspace.thread_view("t1", "local")["draft"],
            "follow-up typed while sending",
        )
        self.assertEqual(workspace.selected_machine_id, remote["id"])
        await workspace.close()

    async def test_invalid_or_future_cache_is_preserved_and_rejected(self):
        self.cache_path.parent.mkdir(parents=True)
        self.cache_path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "preserved"):
            self.make_workspace(FakeRPC())
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), "{broken")

        self.cache_path.write_text('{"version":999}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "schema version 999"):
            self.make_workspace(FakeRPC())
        self.assertEqual(self.cache_path.read_text(encoding="utf-8"), '{"version":999}')

    async def test_changed_server_identity_resets_event_sequence_but_keeps_drafts(self):
        rpc = FakeRPC([
            {"seq": seq, "thread_id": "t1", "kind": "message", "data": {"role": "assistant", "text": f"old {seq}"}}
            for seq in range(1, 4)
        ])
        workspace = self.make_workspace(rpc)
        await workspace.sync_machine("local")
        workspace.switch("local", "p1", "t1")
        workspace.set_draft("keep me")
        self.assertEqual(workspace.selected_machine["cursor"], 3)

        rpc.server_id = "server-b"
        rpc.events = [
            {"seq": seq, "thread_id": "t1", "kind": "message", "data": {"role": "assistant", "text": f"new {seq}"}}
            for seq in range(1, 3)
        ]
        await workspace.sync_machine("local")
        self.assertEqual(workspace.selected_machine["server_id"], "server-b")
        self.assertEqual(workspace.selected_machine["cursor"], 2)
        self.assertEqual([event["data"]["text"] for event in workspace.thread_events("t1")], ["new 1", "new 2"])
        self.assertEqual(workspace.thread_view()["draft"], "keep me")
        await workspace.close()

    async def test_concurrent_client_requests_create_only_one_transport(self):
        created = []
        def factory(**_):
            client = FakeRPC()
            created.append(client)
            return client
        workspace = Workspace(cache_path=self.cache_path, rpc_factory=factory)
        first, second = await asyncio.gather(workspace._client("local"), workspace._client("local"))
        self.assertIs(first, second)
        self.assertEqual(len(created), 1)
        await workspace.close()

    async def test_healthy_poll_does_not_flicker_connected_state(self):
        rpc = FakeRPC()
        workspace = self.make_workspace(rpc)
        await workspace.sync_machine("local")
        self.assertEqual(workspace.selected_machine["connection"], "connected")
        rpc.snapshot_started = asyncio.Event()
        rpc.snapshot_gate = asyncio.Event()
        polling = asyncio.create_task(workspace.sync_machine("local"))
        await rpc.snapshot_started.wait()
        self.assertEqual(workspace.selected_machine["connection"], "connected")
        rpc.snapshot_gate.set()
        await polling
        await workspace.close()

    async def test_first_project_is_cached_and_selected_without_full_sync(self):
        rpc = FakeRPC()
        workspace = self.make_workspace(rpc)

        result = await workspace.add_project("/new-repo", "New Repo")

        self.assertEqual(workspace.projects(), [result])
        self.assertEqual(workspace.selected_project, result)
        self.assertIsNone(workspace.selected_thread)
        self.assertEqual([method for method, _ in rpc.calls], ["add_project"])
        await workspace.close()

    async def test_first_thread_is_cached_and_opened_without_full_sync(self):
        rpc = FakeRPC()
        workspace = self.make_workspace(rpc)
        workspace.selected_machine["snapshot"]["projects"] = [
            {"id": "p1", "name": "Repo", "path": "/repo"}
        ]
        workspace.selected_machine["snapshot"]["threads"] = []
        workspace.switch("local", "p1")

        result = await workspace.create_thread("p1", "New conversation", "codex")

        self.assertEqual(workspace.threads(), [result])
        self.assertEqual(workspace.selected_thread, result)
        self.assertEqual([method for method, _ in rpc.calls], ["create_thread"])
        await workspace.close()

    async def test_inflight_project_creation_does_not_steal_new_machine_selection(self):
        rpc = FakeRPC()
        rpc.add_project_started = asyncio.Event()
        rpc.add_project_gate = asyncio.Event()
        workspace = self.make_workspace(rpc)
        task = asyncio.create_task(workspace.add_project("/new-repo", machine_id="local"))
        await rpc.add_project_started.wait()

        remote = workspace.add_machine("box", "box")
        workspace.switch(remote["id"])
        rpc.add_project_gate.set()
        result = await task

        self.assertEqual(workspace.projects("local"), [result])
        self.assertEqual(workspace.selected_machine_id, remote["id"])
        self.assertIsNone(workspace.selected_project)
        await workspace.close()

    async def test_inflight_thread_creation_does_not_steal_new_thread_selection(self):
        rpc = FakeRPC()
        rpc.create_thread_started = asyncio.Event()
        rpc.create_thread_gate = asyncio.Event()
        workspace = self.make_workspace(rpc)
        workspace.selected_machine["snapshot"]["projects"] = [
            {"id": "p1", "name": "Repo", "path": "/repo"}
        ]
        workspace.selected_machine["snapshot"]["threads"] = [thread("t1"), thread("t2")]
        workspace.switch("local", "p1", "t1")
        task = asyncio.create_task(
            workspace.create_thread("p1", "New conversation", "codex", machine_id="local")
        )
        await rpc.create_thread_started.wait()

        workspace.switch("local", "p1", "t2")
        rpc.create_thread_gate.set()
        result = await task

        self.assertIn(result, workspace.threads())
        self.assertEqual(workspace.selected_thread["id"], "t2")
        await workspace.close()

    async def test_inflight_sync_cannot_erase_a_newly_cached_thread(self):
        rpc = FakeRPC()
        rpc.snapshot_started = asyncio.Event()
        rpc.snapshot_gate = asyncio.Event()
        workspace = self.make_workspace(rpc)
        workspace.selected_machine["snapshot"]["projects"] = [
            {"id": "p1", "name": "Repo", "path": "/repo"}
        ]
        workspace.selected_machine["snapshot"]["threads"] = [thread("t1")]
        workspace.switch("local", "p1", "t1")
        syncing = asyncio.create_task(workspace.sync_machine("local"))
        await rpc.snapshot_started.wait()

        result = await workspace.create_thread("p1", "New conversation", "codex")
        rpc.snapshot_gate.set()
        await syncing

        self.assertIn(result, workspace.threads())
        self.assertEqual(workspace.selected_thread, result)
        await workspace.close()

    async def test_scrolled_history_window_survives_more_than_live_cache_limit(self):
        workspace = self.make_workspace(FakeRPC())
        await workspace.sync_machine("local")
        workspace.switch("local", "p1", "t1")
        machine = workspace.selected_machine
        for seq in range(1, MAX_EVENTS_PER_THREAD + 1):
            workspace._apply_event(machine, {"seq": seq, "thread_id": "t1", "kind": "message", "data": {"role": "assistant", "text": str(seq)}})
        workspace.anchor_scroll()
        workspace.set_scroll(20)
        frozen = workspace.view_events()

        final_seq = MAX_EVENTS_PER_THREAD * 2 + 100
        for seq in range(MAX_EVENTS_PER_THREAD + 1, final_seq + 1):
            workspace._apply_event(machine, {"seq": seq, "thread_id": "t1", "kind": "message", "data": {"role": "assistant", "text": str(seq)}})
        self.assertEqual(workspace.view_events(), frozen)
        self.assertEqual(workspace.thread_events()[-1]["seq"], final_seq)
        workspace.set_scroll(0)
        self.assertEqual(workspace.view_events()[-1]["seq"], final_seq)
        self.assertEqual(len(workspace.view_events()), MAX_EVENTS_PER_THREAD)
        await workspace.close()


if __name__ == "__main__":
    unittest.main()
