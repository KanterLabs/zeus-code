"""Cross-component release scenarios, using deterministic structured providers."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from zeus_code.daemon import Daemon


class ControlledProvider:
    instances = {}

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.decision = None

    async def check(self):
        return {"available": True, "detail": "Deterministic test provider"}

    async def run(self, context, prompt):
        self.context = context
        self.instances[context.thread_id] = self
        await context.emit("provider_session", {"session_id": context.session_id or "session-" + context.thread_id})
        await context.emit("message_delta", {"item_id": "answer", "text": "Working independently. "})
        self.started.set()
        try:
            if prompt == "approval":
                self.decision = await context.approve({"provider_request_id": "command-1", "kind": "command",
                                                       "command": "printf approved", "cwd": context.cwd})
                await context.emit("message_delta", {"item_id": "answer", "text": self.decision})
            elif prompt == "oversized-approval":
                await context.approve({"provider_request_id": "huge", "details": "x" * 40000})
            else:
                await self.release.wait()
            await context.emit("message_delta", {"item_id": "answer", "text": "Finished."})
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="zeus-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        ControlledProvider.instances = {}
        self.daemon = Daemon(self.root / "state", providers={"codex": ControlledProvider, "opencode": ControlledProvider})
        self.assertTrue(await self.daemon.start())
        self.project = await self.daemon.dispatch("add_project", {"path": str(self.repo)})

    async def asyncTearDown(self):
        await self.daemon.close()
        self.tmp.cleanup()

    async def thread(self, provider="codex", title="Task"):
        return await self.daemon.dispatch("create_thread", {"project_id": self.project["id"], "title": title, "provider": provider})

    async def send(self, thread, prompt="hold", key="request-1"):
        return await self.daemon.dispatch("send", {"thread_id": thread["id"], "prompt": prompt, "request_id": key})

    async def wait_for(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(.005)
        self.fail("Timed out waiting for deterministic provider state")

    async def wire(self):
        reader, writer = await asyncio.open_unix_connection(str(self.root / "state/server.sock"))
        return reader, writer

    async def rpc(self, pair, method, params=None, version=1):
        reader, writer = pair
        writer.write(json.dumps({"id": "test", "version": version, "method": method, "params": params or {}}).encode() + b"\n")
        await writer.drain()
        return json.loads(await reader.readline())

    async def test_concurrent_runs_background_approval_and_isolated_cancel(self):
        codex = await self.thread("codex", "Approval task")
        other = await self.thread("opencode", "Independent task")
        run1 = await self.send(codex, "approval", "codex-turn")
        run2 = await self.send(other, "hold", "opencode-turn")
        await self.wait_for(lambda: len(self.daemon.store.approvals()) == 1 and other["id"] in ControlledProvider.instances)
        approval = self.daemon.store.approvals()[0]
        self.assertEqual(approval["thread_id"], codex["id"])
        self.assertEqual(approval["payload"]["cwd"], str(self.repo))
        self.assertEqual(self.daemon.store.thread(codex["id"])["state"], "awaiting_approval")
        await self.daemon.dispatch("approve", {"request_id": approval["id"], "decision": "reject"})
        await self.wait_for(lambda: self.daemon.store.run(run1["id"])["state"] == "completed")
        self.assertEqual(ControlledProvider.instances[codex["id"]].decision, "reject")
        self.assertEqual(self.daemon.store.run(run2["id"])["state"], "running")
        await self.daemon.dispatch("cancel", {"thread_id": other["id"], "run_id": run2["id"]})
        self.assertTrue(ControlledProvider.instances[other["id"]].cancelled)
        self.assertEqual(self.daemon.store.run(run2["id"])["state"], "cancelled")
        self.assertEqual(self.daemon.store.run(run1["id"])["state"], "completed")

    async def test_socket_disconnect_and_idempotent_reconnect_preserve_run(self):
        thread = await self.thread()
        pair = await self.wire()
        params = {"thread_id": thread["id"], "prompt": "hold", "request_id": "stable-submit"}
        response = await self.rpc(pair, "send", params)
        run_id = response["result"]["id"]
        pair[1].close()
        await pair[1].wait_closed()
        await self.wait_for(lambda: thread["id"] in ControlledProvider.instances)
        self.assertEqual(self.daemon.store.run(run_id)["state"], "running")
        second = await self.wire()
        retried = await self.rpc(second, "send", params)
        self.assertEqual(retried["result"]["id"], run_id)
        first_page = (await self.rpc(second, "events", {"after": 0, "limit": 2}))["result"]
        second_page = (await self.rpc(second, "events", {"after": first_page["last_seq"], "limit": 500}))["result"]
        all_events = first_page["events"] + second_page["events"]
        self.assertEqual(len([e for e in all_events if e["kind"] == "message" and e["data"].get("role") == "user"]), 1)
        self.assertEqual(len({e["seq"] for e in all_events}), len(all_events))
        ControlledProvider.instances[thread["id"]].release.set()
        await self.wait_for(lambda: self.daemon.store.run(run_id)["state"] == "completed")
        second[1].close()
        await second[1].wait_closed()

    async def test_immediate_cancel_before_provider_starts_is_terminal(self):
        thread = await self.thread()
        run = await self.send(thread)
        await self.daemon.dispatch("cancel", {"thread_id": thread["id"]})
        self.assertEqual(self.daemon.store.run(run["id"])["state"], "cancelled")
        self.assertNotIn(run["id"], self.daemon.tasks)

    async def test_duplicate_start_does_not_recover_live_runs(self):
        thread = await self.thread()
        run = await self.send(thread)
        other = Daemon(self.root / "state", providers={"codex": ControlledProvider})
        self.assertFalse(await other.start())
        self.assertEqual(self.daemon.store.run(run["id"])["state"], "running")
        self.assertTrue((self.root / "state/server.sock").exists())

    async def test_protocol_mismatch_and_unknown_method_are_actionable(self):
        pair = await self.wire()
        response = await self.rpc(pair, "hello", version=200)
        self.assertEqual(response["error"]["code"], "version_mismatch")
        response = await self.rpc(pair, "not-a-method")
        self.assertEqual(response["error"]["code"], "unknown_method")
        response = await self.rpc(pair, "hello")
        self.assertEqual(response["result"]["server_id"], self.daemon.store.server_id)
        pair[1].close()
        await pair[1].wait_closed()

    async def test_draft_history_and_session_survive_daemon_restart(self):
        thread = await self.thread()
        run = await self.send(thread)
        await self.wait_for(lambda: thread["id"] in ControlledProvider.instances)
        await self.daemon.dispatch("update_thread", {"thread_id": thread["id"], "draft": "Remember my draft", "scroll": 12})
        server_id = self.daemon.store.server_id
        await self.daemon.close()
        self.daemon = Daemon(self.root / "state", providers={"codex": ControlledProvider})
        await self.daemon.start()
        restored = self.daemon.store.thread(thread["id"])
        self.assertEqual(self.daemon.store.server_id, server_id)
        self.assertEqual(restored["draft"], "Remember my draft")
        self.assertEqual(restored["scroll"], 12)
        self.assertTrue(restored["session_id"])
        self.assertEqual(self.daemon.store.run(run["id"])["state"], "cancelled")
        self.assertTrue(self.daemon.store.history(thread["id"])["events"])
        self.assertFalse(self.daemon.tasks)

    async def test_disk_limit_preserves_history_and_refuses_new_runs(self):
        thread = await self.thread()
        from unittest.mock import patch
        with patch.dict(os.environ, {"ZEUS_CODE_MAX_STORAGE_MB": "1"}):
            for _ in range(20):
                self.daemon.store.append_event(thread["id"], None, "message", {"text": "x" * 65536})
            before = self.daemon.store.last_seq
            with self.assertRaisesRegex(ValueError, "History storage limit"):
                await self.send(thread)
            self.assertEqual(self.daemon.store.last_seq, before)

    async def test_oversized_approval_fails_without_poisoning_snapshot(self):
        thread = await self.thread()
        run = await self.send(thread, "oversized-approval")
        await self.wait_for(lambda: self.daemon.store.run(run["id"])["state"] == "failed")
        self.assertFalse(self.daemon.store.approvals())
        self.assertIn("32 KiB", self.daemon.store.run(run["id"])["error"])
        pair = await self.wire()
        self.assertIn("result", await self.rpc(pair, "snapshot"))
        pair[1].close()
        await pair[1].wait_closed()

    async def test_large_snapshot_pages_preserve_every_thread(self):
        expected = []
        for index in range(8):
            thread = await self.thread(title=f"Large draft {index}")
            expected.append(thread["id"])
            await self.daemon.dispatch("update_thread", {"thread_id": thread["id"], "draft": "x" * 128000})
        actual, offset, pages = [], 0, 0
        while True:
            page = await self.daemon.dispatch("snapshot", {"offset": offset})
            self.assertLess(len(json.dumps(page).encode()), 1024 * 1024)
            actual.extend(thread["id"] for thread in page["threads"])
            pages += 1
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertGreater(pages, 1)
        self.assertCountEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
