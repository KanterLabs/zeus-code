from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from zeus_code.providers.base import RunContext
from zeus_code.providers.opencode import OpenCodeProvider


class OpenCodeFixture:
    def __init__(self, session_id: str = "ses_fixture") -> None:
        self.session_id = session_id
        self.server: asyncio.Server | None = None
        self.event_writer: asyncio.StreamWriter | None = None
        self.requests: list[tuple[str, str, dict | None]] = []
        self.prompt_seen = asyncio.Event()
        self.abort_seen = asyncio.Event()
        self.reply_seen = asyncio.Event()
        self.expected_directory = os.getcwd()
        self.username = "zeus-code"
        self.password: str | None = None
        self.resume = False
        self.turn_events = True
        self.connected = ["opencode"]

    async def start(self) -> str:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        if self.event_writer is not None:
            self.event_writer.close()
            await self.event_writer.wait_closed()
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def send_event(self, event: dict) -> None:
        assert self.event_writer is not None
        payload = f"data: {json.dumps(event)}\n\n".encode()
        # The installed server uses HTTP chunking; split an event across chunks.
        split = max(1, len(payload) // 2)
        for piece in (payload[:split], payload[split:]):
            self.event_writer.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            await self.event_writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode("ascii").rstrip()
        method, target, _ = request_line.split(" ", 2)
        headers: dict[str, str] = {}
        while (line := await reader.readline()) not in {b"\r\n", b"\n", b""}:
            name, value = line.decode("iso-8859-1").split(":", 1)
            headers[name.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = json.loads(await reader.readexactly(length)) if length else None
        parsed = urlsplit(target)
        query = parse_qs(parsed.query)
        assert query.get("directory") == [self.expected_directory]
        token = base64.b64decode(headers["authorization"].removeprefix("Basic ")).decode()
        assert token == f"{self.username}:{self.password}"
        self.requests.append((method, parsed.path, body))

        if parsed.path == "/event":
            self.event_writer = writer
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
            )
            await writer.drain()
            await self.send_event({"type": "server.connected", "properties": {}})
            return
        if parsed.path == "/global/health":
            await self._json(writer, 200, {"healthy": True, "version": "1.18.26"})
            return
        if parsed.path == "/provider":
            await self._json(
                writer,
                200,
                {
                    "connected": self.connected,
                    "default": {"opencode": "big-pickle"},
                    "all": [
                        {
                            "id": "opencode",
                            "models": {
                                "big-pickle": {
                                    "name": "Big Pickle",
                                    "variants": {
                                        "low": {"reasoningEffort": "low"},
                                        "high": {"reasoningEffort": "high"},
                                    },
                                },
                                "steady": {"name": "Steady"},
                            },
                        },
                        {"id": "unconfigured", "models": {"paid": {"name": "Paid"}}},
                    ],
                },
            )
            return
        if parsed.path == "/session" and method == "POST":
            await self._json(writer, 200, {"id": self.session_id})
            return
        if parsed.path == f"/session/{self.session_id}" and method == "GET":
            await self._json(writer, 200, {"id": self.session_id})
            return
        if parsed.path == f"/session/{self.session_id}/prompt_async":
            await self._empty(writer, 204)
            self.prompt_seen.set()
            if self.turn_events:
                await self._turn_prefix()
            return
        if parsed.path == "/permission/per_allow/reply":
            await self._json(writer, 200, True)
            self.reply_seen.set()
            await self._turn_suffix()
            return
        if parsed.path == f"/session/{self.session_id}/abort":
            await self._json(writer, 200, True)
            self.abort_seen.set()
            return
        await self._json(writer, 404, {"name": "NotFound", "data": {"message": parsed.path}})

    async def _turn_prefix(self) -> None:
        await self.send_event(
            {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": "ses_someone_else",
                    "messageID": "msg_other",
                    "partID": "prt_other",
                    "field": "text",
                    "delta": "ignore me",
                },
            }
        )
        await self.send_event(
            {
                "type": "message.updated",
                "properties": {
                    "info": {
                        "id": "msg_assistant",
                        "sessionID": self.session_id,
                        "role": "assistant",
                    },
                },
            }
        )
        await self.send_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "sessionID": self.session_id,
                    "part": {
                        "id": "prt_reasoning",
                        "messageID": "msg_assistant",
                        "type": "reasoning",
                        "text": "",
                        "time": {"start": 1},
                    },
                },
            }
        )
        await self.send_event(
            {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": self.session_id,
                    "messageID": "msg_assistant",
                    "partID": "prt_reasoning",
                    "field": "text",
                    "delta": "private reasoning",
                },
            }
        )
        await self.send_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "prt_tool",
                        "sessionID": self.session_id,
                        "messageID": "msg_assistant",
                        "callID": "call_bash",
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "running",
                            "input": {"command": "git status --short"},
                            "time": {"start": 1},
                        },
                    },
                },
            }
        )
        await self.send_event(
            {
                "type": "permission.asked",
                "properties": {
                    "id": "per_allow",
                    "sessionID": self.session_id,
                    "permission": "bash",
                    "patterns": ["git status --short"],
                    "metadata": {},
                    "always": ["git status*"],
                    "tool": {"messageID": "msg_assistant", "callID": "call_bash"},
                },
            }
        )

    async def _turn_suffix(self) -> None:
        await self.send_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "prt_tool",
                        "sessionID": self.session_id,
                        "messageID": "msg_assistant",
                        "callID": "call_bash",
                        "type": "tool",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "git status --short"},
                            "output": "clean",
                            "title": "git status --short",
                            "metadata": {},
                            "time": {"start": 1, "end": 2},
                        },
                    },
                },
            }
        )
        await self.send_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "prt_text",
                        "sessionID": self.session_id,
                        "messageID": "msg_assistant",
                        "type": "text",
                        "text": "",
                        "time": {"start": 1},
                    },
                },
            }
        )
        await self.send_event(
            {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": self.session_id,
                    "messageID": "msg_assistant",
                    "partID": "prt_text",
                    "field": "text",
                    "delta": "done",
                },
            }
        )
        await self.send_event(
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "prt_text",
                        "sessionID": self.session_id,
                        "messageID": "msg_assistant",
                        "type": "text",
                        "text": "done",
                        "time": {"start": 1, "end": 2},
                    },
                },
            }
        )
        await self.send_event(
            {"type": "session.idle", "properties": {"sessionID": self.session_id}}
        )

    @staticmethod
    async def _json(writer: asyncio.StreamWriter, status: int, data: object) -> None:
        body = json.dumps(data).encode()
        writer.write(
            f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    @staticmethod
    async def _empty(writer: asyncio.StreamWriter, status: int) -> None:
        writer.write(
            f"HTTP/1.1 {status} Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()


class FixtureProvider(OpenCodeProvider):
    def __init__(self, fixture: OpenCodeFixture) -> None:
        super().__init__(sys.executable)
        self.fixture = fixture
        self.stops = 0

    async def _start_server(self, cwd, executable, username, password):
        self.fixture.expected_directory = cwd
        self.fixture.username = username
        self.fixture.password = password
        return SimpleNamespace(url=await self.fixture.start())

    async def _stop_server(self, server) -> None:
        self.stops += 1
        await self.fixture.close()


class OpenCodeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_streams_filtered_events_and_awaits_approval(self) -> None:
        fixture = OpenCodeFixture()
        provider = FixtureProvider(fixture)
        emitted: list[tuple[str, dict]] = []
        approvals: list[dict] = []

        async def emit(kind: str, data: dict) -> None:
            emitted.append((kind, data))

        async def approve(request: dict) -> str:
            approvals.append(request)
            return "allow"

        context = RunContext(
            thread_id="thread-1",
            run_id="run-1",
            cwd=os.getcwd(),
            session_id=None,
            model="opencode/big-pickle",
            settings={"agent": "build", "variant": "high"},
            emit=emit,
            approve=approve,
        )
        await provider.run(context, "check the tree")

        self.assertEqual(emitted[0], ("provider_session", {"session_id": "ses_fixture"}))
        self.assertNotIn("ignore me", repr(emitted))
        self.assertNotIn("private reasoning", repr(emitted))
        self.assertIn(("message_delta", {"item_id": "prt_text", "text": "done"}), emitted)
        self.assertIn(
            ("message", {"role": "assistant", "text": "done", "item_id": "prt_text"}),
            emitted,
        )
        self.assertEqual(approvals[0]["provider_request_id"], "per_allow")
        self.assertEqual(approvals[0]["kind"], "command")
        self.assertEqual(approvals[0]["command"], "git status --short")
        prompt = next(
            body
            for method, path, body in fixture.requests
            if path.endswith("prompt_async")
        )
        self.assertEqual(
            prompt,
            {
                "parts": [{"type": "text", "text": "check the tree"}],
                "model": {"providerID": "opencode", "modelID": "big-pickle"},
                "agent": "build",
                "variant": "high",
            },
        )
        reply = next(body for _, path, body in fixture.requests if path.endswith("/reply"))
        self.assertEqual(reply, {"reply": "once"})
        self.assertEqual(provider.stops, 1)

    async def test_resume_validates_and_emits_session_before_prompt(self) -> None:
        fixture = OpenCodeFixture()
        provider = FixtureProvider(fixture)
        timeline: list[str] = []

        async def emit(kind: str, data: dict) -> None:
            timeline.append(kind)
            if kind == "provider_session":
                self.assertFalse(fixture.prompt_seen.is_set())

        context = RunContext(
            "thread-1",
            "run-1",
            os.getcwd(),
            "ses_fixture",
            None,
            {},
            emit,
            lambda request: asyncio.sleep(0, result="allow"),
        )
        await provider.run(context, "resume")
        paths = [(method, path) for method, path, _ in fixture.requests]
        self.assertIn(("GET", "/session/ses_fixture"), paths)
        self.assertNotIn(("POST", "/session"), paths)
        self.assertEqual(timeline[0], "provider_session")

    async def test_cancellation_aborts_only_the_session_then_cleans_up(self) -> None:
        fixture = OpenCodeFixture()
        fixture.turn_events = False
        provider = FixtureProvider(fixture)
        context = RunContext(
            "thread-1",
            "run-1",
            os.getcwd(),
            None,
            None,
            {},
            lambda kind, data: asyncio.sleep(0),
            lambda request: asyncio.sleep(0, result="reject"),
        )
        task = asyncio.create_task(provider.run(context, "wait"))
        await asyncio.wait_for(fixture.prompt_seen.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(fixture.abort_seen.is_set())
        self.assertEqual(provider.stops, 1)
        self.assertIn(
            ("POST", "/session/ses_fixture/abort"),
            [(method, path) for method, path, _ in fixture.requests],
        )

    async def test_check_discovers_version_and_connected_models_without_a_turn(self) -> None:
        fixture = OpenCodeFixture()
        provider = FixtureProvider(fixture)
        result = await provider.check()
        self.assertEqual(result["version"], "1.18.26")
        self.assertEqual(
            result["models"],
            [
                {
                    "id": "opencode/big-pickle",
                    "name": "Big Pickle",
                    "variants": ["low", "high"],
                    "is_default": True,
                },
                {
                    "id": "opencode/steady",
                    "name": "Steady",
                    "is_default": False,
                },
            ],
        )
        self.assertFalse(any(path.endswith("prompt_async") for _, path, _ in fixture.requests))

    async def test_missing_binary_is_isolated_discovery_failure(self) -> None:
        result = await OpenCodeProvider("definitely-not-an-opencode-binary").check()
        self.assertFalse(result["available"])
        self.assertIn("not installed", result["detail"])

    async def test_check_explains_missing_model_authentication(self) -> None:
        fixture = OpenCodeFixture()
        fixture.connected = []
        result = await FixtureProvider(fixture).check()
        self.assertFalse(result["available"])
        self.assertEqual(result["models"], [])
        self.assertIn("opencode auth login", result["detail"])

    async def test_protocol_bounded_text_is_forwarded_without_truncation(self) -> None:
        provider = OpenCodeProvider(sys.executable)
        emitted: list[tuple[str, dict]] = []
        text = "x" * 70_000

        async def stream():
            yield {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "prt_large",
                        "sessionID": "ses_fixture",
                        "messageID": "msg_large",
                        "type": "text",
                        "text": "",
                        "time": {"start": 1},
                    }
                },
            }
            yield {
                "type": "message.part.delta",
                "properties": {
                    "sessionID": "ses_fixture",
                    "messageID": "msg_large",
                    "partID": "prt_large",
                    "field": "text",
                    "delta": text,
                },
            }
            yield {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "prt_large",
                        "sessionID": "ses_fixture",
                        "messageID": "msg_large",
                        "type": "text",
                        "text": text,
                        "time": {"start": 1, "end": 2},
                    }
                },
            }
            yield {
                "type": "session.idle",
                "properties": {"sessionID": "ses_fixture"},
            }

        context = RunContext(
            "thread-1",
            "run-1",
            os.getcwd(),
            "ses_fixture",
            None,
            {},
            lambda kind, data: _capture(emitted, kind, data),
            lambda request: asyncio.sleep(0, result="reject"),
        )
        await provider._consume_turn(
            context,
            None,  # type: ignore[arg-type] -- no permission event in this fixture
            "ses_fixture",
            {"type": "server.connected", "properties": {}},
            stream(),
        )
        self.assertEqual(
            next(data["text"] for kind, data in emitted if kind == "message_delta"),
            text,
        )
        self.assertEqual(next(data["text"] for kind, data in emitted if kind == "message"), text)


async def _capture(events: list[tuple[str, dict]], kind: str, data: dict) -> None:
    events.append((kind, data))


if __name__ == "__main__":
    unittest.main()
