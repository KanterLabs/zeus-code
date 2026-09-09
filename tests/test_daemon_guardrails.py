"""Regressions for daemon-side provider and snapshot admission guards."""

import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from zeus_code import PROTOCOL_VERSION
from zeus_code.daemon import MAX_LINE, Daemon


class UnavailableProvider:
    run_calls = 0

    async def check(self):
        return {"available": False, "detail": "Fixture provider is signed out."}

    async def run(self, context, prompt):
        type(self).run_calls += 1


class HoldingProvider:
    run_calls = 0
    started: asyncio.Event
    release: asyncio.Event

    async def check(self):
        return {"available": True, "detail": "Fixture provider is ready."}

    async def run(self, context, prompt):
        type(self).run_calls += 1
        type(self).started.set()
        await type(self).release.wait()


class DaemonGuardrailTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="zeus-guardrails-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)], check=True
        )
        UnavailableProvider.run_calls = 0
        HoldingProvider.run_calls = 0
        HoldingProvider.started = asyncio.Event()
        HoldingProvider.release = asyncio.Event()
        self.daemon = Daemon(
            self.root / "state",
            providers={"codex": UnavailableProvider, "opencode": HoldingProvider},
        )
        self.assertTrue(await self.daemon.start())
        self.project = await self.daemon.dispatch(
            "add_project", {"path": str(self.repo)}
        )
        self._request_id = 0

    async def asyncTearDown(self):
        HoldingProvider.release.set()
        await self.daemon.close()
        self.tmp.cleanup()

    async def wait_for(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.005)
        self.fail("Timed out waiting for deterministic daemon state")

    async def wire(self):
        return await asyncio.open_unix_connection(
            str(self.root / "state" / "server.sock"), limit=MAX_LINE + 1
        )

    async def rpc(self, pair, method, params=None):
        reader, writer = pair
        self._request_id += 1
        writer.write(
            json.dumps(
                {
                    "id": self._request_id,
                    "version": PROTOCOL_VERSION,
                    "method": method,
                    "params": params or {},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        return json.loads(await reader.readline())

    async def create_thread(self, provider="codex", title="Task"):
        return await self.daemon.dispatch(
            "create_thread",
            {
                "project_id": self.project["id"],
                "title": title,
                "provider": provider,
            },
        )

    async def test_send_rejects_provider_known_to_be_unavailable(self):
        await self.wait_for(
            lambda: self.daemon._provider_cache.get("codex", {}).get("available")
            is False
        )
        thread = await self.create_thread()
        await self.daemon.dispatch(
            "update_thread", {"thread_id": thread["id"], "draft": "keep this"}
        )
        before_seq = self.daemon.store.last_seq
        pair = await self.wire()
        try:
            response = await self.rpc(
                pair,
                "send",
                {
                    "thread_id": thread["id"],
                    "prompt": "must not run",
                    "request_id": "unavailable-request",
                },
            )
        finally:
            pair[1].close()
            await pair[1].wait_closed()

        self.assertEqual(response["error"]["code"], "provider_unavailable")
        self.assertIn("signed out", response["error"]["message"])
        self.assertEqual(UnavailableProvider.run_calls, 0)
        self.assertIsNone(
            self.daemon.store.find_run_by_request("unavailable-request")
        )
        self.assertFalse(self.daemon.tasks)
        self.assertEqual(self.daemon.store.last_seq, before_seq)
        stored = self.daemon.store.thread(thread["id"])
        self.assertEqual(stored["state"], "idle")
        self.assertEqual(stored["draft"], "keep this")

    async def test_provider_status_change_does_not_hide_accepted_retry(self):
        thread = await self.create_thread(provider="opencode")
        params = {
            "thread_id": thread["id"],
            "prompt": "run once",
            "request_id": "accepted-request",
        }
        accepted = await self.daemon.dispatch("send", params)
        await asyncio.wait_for(HoldingProvider.started.wait(), timeout=1)
        self.daemon._provider_cache["opencode"] = {
            "available": False,
            "detail": "Fixture provider became unavailable.",
        }

        retried = await self.daemon.dispatch("send", params)

        self.assertEqual(retried["id"], accepted["id"])
        self.assertEqual(HoldingProvider.run_calls, 1)
        HoldingProvider.release.set()
        await self.wait_for(
            lambda: self.daemon.store.run(accepted["id"])["state"] == "completed"
        )

    async def test_snapshot_pages_are_frozen_for_one_connection(self):
        threads = []
        for index in range(8):
            thread = await self.create_thread(
                provider="opencode", title=f"Large draft {index}"
            )
            thread = await self.daemon.dispatch(
                "update_thread",
                {"thread_id": thread["id"], "draft": "x" * 128000},
            )
            threads.append(thread)
        expected_threads = [
            thread["id"]
            for thread in sorted(
                threads, key=lambda item: (item["created_at"], item["id"])
            )
        ]
        second_repo = self.root / "second-repo"
        second_repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(second_repo)], check=True
        )
        pair = await self.wire()
        try:
            first = (await self.rpc(pair, "snapshot"))["result"]
            self.assertIsNotNone(first["next_offset"])
            added = await self.daemon.dispatch(
                "add_project", {"path": str(second_repo)}
            )

            pages = [first]
            offset = first["next_offset"]
            while offset is not None:
                page = (
                    await self.rpc(pair, "snapshot", {"offset": offset})
                )["result"]
                pages.append(page)
                offset = page["next_offset"]

            actual_threads = [
                thread["id"] for page in pages for thread in page["threads"]
            ]
            actual_projects = [
                project["id"] for page in pages for project in page["projects"]
            ]
            self.assertEqual(actual_threads, expected_threads)
            self.assertEqual(actual_projects, [self.project["id"]])
            self.assertTrue(
                all(page["last_seq"] == first["last_seq"] for page in pages)
            )

            fresh_pages = []
            page = (await self.rpc(pair, "snapshot"))["result"]
            while True:
                fresh_pages.append(page)
                if page["next_offset"] is None:
                    break
                page = (
                    await self.rpc(
                        pair, "snapshot", {"offset": page["next_offset"]}
                    )
                )["result"]
            fresh_projects = [
                project["id"]
                for page in fresh_pages
                for project in page["projects"]
            ]
            fresh_threads = [
                thread["id"]
                for page in fresh_pages
                for thread in page["threads"]
            ]
            self.assertCountEqual(
                fresh_projects, [self.project["id"], added["id"]]
            )
            self.assertEqual(fresh_threads, expected_threads)
        finally:
            pair[1].close()
            await pair[1].wait_closed()


if __name__ == "__main__":
    unittest.main()
