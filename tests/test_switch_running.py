"""Regression coverage for navigating away from an active daemon-owned run."""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest

from zeus_code.client import RPCClient
from zeus_code.daemon import Daemon
from zeus_code.workspace import EVENT_PAGE_SIZE, Workspace


WORK = Path(__file__).resolve().parents[1] / ".work"


class BurstingProvider:
    """A deterministic provider that emits a multi-page tail after a switch."""

    backlog_size = EVENT_PAGE_SIZE * 2 + 17
    started: asyncio.Event
    emit_backlog: asyncio.Event
    backlog_emitted: asyncio.Event
    release: asyncio.Event
    run_calls = 0
    cancellations = 0
    context = None

    @classmethod
    def reset(cls) -> None:
        cls.started = asyncio.Event()
        cls.emit_backlog = asyncio.Event()
        cls.backlog_emitted = asyncio.Event()
        cls.release = asyncio.Event()
        cls.run_calls = 0
        cls.cancellations = 0
        cls.context = None

    async def check(self) -> dict[str, object]:
        return {"available": True, "detail": "Deterministic switch regression provider"}

    async def run(self, context, prompt: str) -> None:
        type(self).run_calls += 1
        type(self).context = context
        try:
            await context.emit("provider_session", {"session_id": "switch-session"})
            await context.emit(
                "status", {"text": "Running before the client changes threads"}
            )
            type(self).started.set()
            await type(self).emit_backlog.wait()
            for index in range(type(self).backlog_size):
                await context.emit(
                    "message_delta",
                    {"item_id": "answer", "text": f"burst-{index:04d}\n"},
                )
            type(self).backlog_emitted.set()
            await type(self).release.wait()
            await context.emit(
                "message",
                {
                    "item_id": "answer",
                    "role": "assistant",
                    "text": "Finished exactly once.",
                },
            )
        except asyncio.CancelledError:
            type(self).cancellations += 1
            raise


class TailChasingRPCClient(RPCClient):
    """Keep the daemon event tail moving immediately before every read."""

    chase_tail = False
    injected = 0

    async def call(self, method, params=None):
        if method == "events" and type(self).chase_tail:
            context = BurstingProvider.context
            if context is None:
                raise AssertionError("provider context is unavailable")
            type(self).injected += 1
            await context.emit(
                "status", {"text": f"moving-tail-{type(self).injected:04d}"}
            )
        return await super().call(method, params)


class SwitchRunningThreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        WORK.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)], check=True
        )

        BurstingProvider.reset()
        self.daemon = Daemon(
            self.root / "state", providers={"codex": BurstingProvider}
        )
        self.assertTrue(await self.daemon.start())
        # Hundreds of individually durable provider events would make this
        # regression unnecessarily slow on fsync-heavy CI filesystems.
        self.daemon.store._db.execute("PRAGMA synchronous = OFF")
        TailChasingRPCClient.chase_tail = False
        TailChasingRPCClient.injected = 0
        self.workspace = Workspace(
            data_dir=self.root / "state",
            cache_path=self.root / "client.json",
            rpc_factory=TailChasingRPCClient,
        )
        await self._wait_for_provider()

        self.project = await self.workspace.add_project(str(self.repo))
        self.thread_a = await self.workspace.create_thread(
            self.project["id"], "Long-running A", "codex"
        )
        self.thread_b = await self.workspace.create_thread(
            self.project["id"], "Viewed B", "codex"
        )

    async def asyncTearDown(self) -> None:
        await self.workspace.close()
        await self.daemon.close()
        self.temp.cleanup()

    async def _wait_for_provider(self) -> None:
        for _ in range(200):
            await self.workspace.sync_machine("local")
            status = self.workspace.selected_machine.get("providers", {}).get("codex", {})
            if status.get("available") is True:
                return
            await asyncio.sleep(0.005)
        self.fail("Deterministic provider discovery did not finish")

    async def _wait_for_completed(self, run_id: str) -> None:
        for _ in range(200):
            await self.workspace.sync_machine("local")
            if self.daemon.store.run(run_id)["state"] == "completed":
                return
            await asyncio.sleep(0.005)
        self.fail("Long-running provider did not complete")

    async def test_switch_poll_and_reconnect_preserve_active_run_without_replay(self) -> None:
        self.workspace.switch(
            "local", self.project["id"], self.thread_a["id"]
        )
        run = await self.workspace.send_prompt(
            "Keep running while I view B", request_id="stable-switch-send"
        )
        await asyncio.wait_for(BurstingProvider.started.wait(), timeout=1)

        self.workspace.switch(
            "local", self.project["id"], self.thread_b["id"]
        )
        BurstingProvider.emit_backlog.set()
        await asyncio.wait_for(BurstingProvider.backlog_emitted.wait(), timeout=5)

        # Polling while B is visible still drains machine-wide event pages for A.
        await self.workspace.sync_machine("local")
        self.assertEqual(self.workspace.selected_thread["id"], self.thread_b["id"])
        self.assertFalse(
            any(
                event.get("thread_id") == self.thread_a["id"]
                for event in self.workspace.view_events()
            )
        )
        burst = [
            event
            for event in self.workspace.thread_events(self.thread_a["id"])
            if event["kind"] == "message_delta"
        ]
        self.assertEqual(
            [event["data"]["text"] for event in burst],
            [f"burst-{index:04d}\n" for index in range(BurstingProvider.backlog_size)],
        )
        self.assertEqual(self.daemon.store.run(run["id"])["state"], "running")
        self.assertEqual(BurstingProvider.cancellations, 0)

        # Replacing the RPC connection is transport-only; daemon provider work
        # must remain alive and an idempotent retry must not replay the turn.
        first_client = self.workspace.clients["local"]
        await self.workspace._drop_client("local")
        self.assertEqual(self.daemon.store.run(run["id"])["state"], "running")
        await self.workspace.sync_machine("local")
        self.assertIsNot(self.workspace.clients["local"], first_client)
        self.assertEqual(self.workspace.selected_thread["id"], self.thread_b["id"])
        retried = await self.workspace.send_prompt(
            "Keep running while I view B",
            request_id="stable-switch-send",
            machine_id="local",
            thread_id=self.thread_a["id"],
        )
        self.assertEqual(retried["id"], run["id"])
        self.assertEqual(BurstingProvider.run_calls, 1)

        BurstingProvider.release.set()
        await self._wait_for_completed(run["id"])
        self.assertEqual(BurstingProvider.run_calls, 1)
        self.assertEqual(BurstingProvider.cancellations, 0)
        self.assertEqual(self.workspace.selected_thread["id"], self.thread_b["id"])

        events = self.workspace.thread_events(self.thread_a["id"])
        self.assertEqual(len({event["seq"] for event in events}), len(events))
        self.assertEqual(
            sum(
                event["kind"] == "message"
                and event["data"].get("role") == "user"
                for event in events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event["kind"] == "message"
                and event["data"].get("text") == "Finished exactly once."
                for event in events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event["kind"] == "run_state"
                and event["data"].get("state") == "completed"
                for event in events
            ),
            1,
        )

        sequences = [event["seq"] for event in events]
        await self.workspace.sync_machine("local")
        await self.workspace.sync_machine("local")
        self.assertEqual(
            [event["seq"] for event in self.workspace.thread_events(self.thread_a["id"])],
            sequences,
        )

    async def test_moving_event_tail_cannot_starve_snapshot_commit(self) -> None:
        self.workspace.switch(
            "local", self.project["id"], self.thread_a["id"]
        )
        run = await self.workspace.send_prompt(
            "Keep producing while I view B", request_id="moving-tail-send"
        )
        await asyncio.wait_for(BurstingProvider.started.wait(), timeout=1)
        self.workspace.switch(
            "local", self.project["id"], self.thread_b["id"]
        )

        # Each read causes a real provider event to be persisted first, so an
        # empty-page catch-up loop can never terminate. Snapshot high-water
        # catch-up must consume the whole returned page, including the newly
        # committed event beyond that boundary, and then return.
        TailChasingRPCClient.chase_tail = True
        try:
            await asyncio.wait_for(self.workspace.sync_machine("local"), timeout=1)
        finally:
            TailChasingRPCClient.chase_tail = False

        self.assertGreater(TailChasingRPCClient.injected, 0)
        self.assertEqual(self.workspace.selected_machine["connection"], "connected")
        self.assertFalse(self.workspace.selected_machine["stale"])
        self.assertEqual(self.workspace.selected_thread["id"], self.thread_b["id"])
        self.assertEqual(self.daemon.store.run(run["id"])["state"], "running")
        self.assertEqual(BurstingProvider.cancellations, 0)
        self.assertTrue(
            any(
                event["kind"] == "status"
                and event["data"].get("text", "").startswith("moving-tail-")
                for event in self.workspace.thread_events(self.thread_a["id"])
            )
        )


if __name__ == "__main__":
    unittest.main()
