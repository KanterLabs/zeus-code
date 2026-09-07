from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from zeus_code.client import MAX_MESSAGE_BYTES, RPCClient, RPCError


WORK = Path(__file__).resolve().parents[1] / ".work"


def temporary_directory() -> tempfile.TemporaryDirectory[str]:
    WORK.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=WORK)


class RPCClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.calls: list[dict] = []
        self.server_id = str(uuid.uuid4())
        self.snapshot_mode = "normal"

        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while line := await reader.readline():
                    request = json.loads(line)
                    self.calls.append(request)
                    if request["method"] == "hello":
                        result = {
                            "protocol_version": 1,
                            "version": "1.0.0",
                            "server_id": self.server_id,
                        }
                        reply = {"id": request["id"], "result": result}
                    elif request["method"] == "snapshot":
                        offset = request["params"].get("offset", 0)
                        if offset == 0:
                            result = {
                                "protocol_version": 1,
                                "server_id": self.server_id,
                                "last_seq": 10,
                                "projects": [{"id": "project-0"}],
                                "threads": [{"id": "thread-0"}],
                                "approvals": [],
                                "next_offset": 2,
                            }
                        elif offset == 2:
                            result = {
                                "protocol_version": 1,
                                "server_id": (
                                    "different-server" if self.snapshot_mode == "identity" else self.server_id
                                ),
                                "last_seq": 20,
                                "projects": [{"id": "project-2"}],
                                "threads": [],
                                "approvals": [{"id": "approval-2"}],
                                "next_offset": 2 if self.snapshot_mode == "nonadvancing" else 5,
                            }
                        else:
                            result = {
                                "protocol_version": 1,
                                "server_id": self.server_id,
                                "last_seq": 30,
                                "projects": [],
                                "threads": [{"id": "thread-5"}],
                                "approvals": [],
                                "next_offset": None,
                            }
                        reply = {"id": request["id"], "result": result}
                    elif request["method"] == "fail":
                        reply = {"id": request["id"], "error": {"code": "bad", "message": "nope"}}
                    else:
                        await asyncio.sleep(0.01)
                        reply = {"id": request["id"], "result": request["params"]}
                    writer.write(json.dumps(reply).encode() + b"\n")
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        self.server = await asyncio.start_unix_server(handler, path=str(self.data_dir / "server.sock"))
        self.addAsyncCleanup(self._close_server)

    async def _close_server(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def test_connect_negotiates_and_calls_use_uuid_ids(self) -> None:
        async with RPCClient(self.data_dir) as client:
            self.assertEqual(await client.call("echo", {"x": 3}), {"x": 3})
        self.assertEqual([call["method"] for call in self.calls], ["hello", "echo"])
        self.assertTrue(all(uuid.UUID(call["id"]) for call in self.calls))
        self.assertTrue(all(call["version"] == 1 for call in self.calls))

    async def test_rpc_error_preserves_code_and_connection(self) -> None:
        async with RPCClient(self.data_dir) as client:
            with self.assertRaises(RPCError) as caught:
                await client.call("fail")
            self.assertEqual(caught.exception.code, "bad")
            self.assertEqual(caught.exception.message, "nope")
            self.assertEqual(await client.call("echo", {"still": "open"}), {"still": "open"})

    async def test_calls_are_serialized(self) -> None:
        async with RPCClient(self.data_dir) as client:
            results = await asyncio.gather(
                client.call("one", {"n": 1}),
                client.call("two", {"n": 2}),
            )
        self.assertEqual(results, [{"n": 1}, {"n": 2}])

    async def test_oversize_request_is_refused(self) -> None:
        async with RPCClient(self.data_dir) as client:
            with self.assertRaisesRegex(ValueError, "exceeds"):
                await client.call("echo", {"payload": "x" * MAX_MESSAGE_BYTES})

    async def test_snapshot_pages_are_transparently_assembled(self) -> None:
        async with RPCClient(self.data_dir) as client:
            snapshot = await client.call("snapshot")
        self.assertEqual(
            [project["id"] for project in snapshot["projects"]],
            ["project-0", "project-2"],
        )
        self.assertEqual(
            [thread["id"] for thread in snapshot["threads"]],
            ["thread-0", "thread-5"],
        )
        self.assertEqual(snapshot["approvals"], [{"id": "approval-2"}])
        self.assertEqual(snapshot["last_seq"], 10)
        self.assertIsNone(snapshot["next_offset"])
        snapshot_calls = [call for call in self.calls if call["method"] == "snapshot"]
        self.assertEqual([call["params"] for call in snapshot_calls], [{}, {"offset": 2}, {"offset": 5}])

    async def test_explicit_snapshot_offset_returns_one_page(self) -> None:
        async with RPCClient(self.data_dir) as client:
            page = await client.call("snapshot", {"offset": 2})
        self.assertEqual(page["next_offset"], 5)
        self.assertEqual([call["params"] for call in self.calls if call["method"] == "snapshot"], [{"offset": 2}])

    async def test_snapshot_rejects_nonadvancing_offset(self) -> None:
        self.snapshot_mode = "nonadvancing"
        async with RPCClient(self.data_dir) as client:
            with self.assertRaisesRegex(ConnectionError, "did not advance"):
                await client.call("snapshot")

    async def test_snapshot_rejects_server_identity_change(self) -> None:
        self.snapshot_mode = "identity"
        async with RPCClient(self.data_dir) as client:
            with self.assertRaisesRegex(ConnectionError, "identity changed"):
                await client.call("snapshot")

    def test_rejects_unsafe_ssh_destination(self) -> None:
        for host in ("-oProxyCommand=bad", "good host", "host;bad"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                RPCClient(self.data_dir, host=host)


class BadHelloTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_server_id_is_rejected(self) -> None:
        with temporary_directory() as directory:
            async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                request = json.loads(await reader.readline())
                writer.write(json.dumps({"id": request["id"], "result": {"protocol_version": 1}}).encode() + b"\n")
                await writer.drain()
                writer.close()

            server = await asyncio.start_unix_server(handler, path=str(Path(directory) / "server.sock"))
            try:
                with self.assertRaisesRegex(ConnectionError, "server id"):
                    await RPCClient(directory).connect()
            finally:
                server.close()
                await server.wait_closed()
