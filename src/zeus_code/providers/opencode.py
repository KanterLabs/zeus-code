"""OpenCode provider adapter using its authenticated loopback HTTP API."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from ..process import spawn_supervised
from .base import ProviderError, RunContext


_READY = re.compile(r"opencode server listening on (http://\S+)")
_EVENT_TEXT_LIMIT = 64 * 1024
_SSE_EVENT_LIMIT = 1024 * 1024
_HTTP_BODY_LIMIT = 64 * 1024 * 1024


def _opencode_executable(executable: str) -> str | None:
    """Resolve one binary for readiness checks and runs over non-login SSH."""
    found = shutil.which(executable)
    if found is not None or executable != "opencode":
        return found
    home = Path.home()
    # Noninteractive SSH commonly omits user-local installers and Homebrew.
    # Explicit paths are never replaced with an unrelated installation.
    for directory in (
        home / ".local/bin",
        home / ".opencode/bin",
        home / ".bun/bin",
        home / ".npm-global/bin",
        home / ".volta/bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ):
        found = shutil.which(str(directory / "opencode"))
        if found is not None:
            return found
    return None


class _HTTPError(RuntimeError):
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        super().__init__(f"OpenCode HTTP {status}: {_error_text(body)}")


class _HTTPClient:
    """Small HTTP/1.1 client for one loopback OpenCode server."""

    def __init__(self, base_url: str, username: str, password: str, directory: str) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ProviderError(
                "OpenCode reported a non-loopback server address; refusing to connect"
            )
        if parsed.port is None:
            raise ProviderError("OpenCode reported a server address without a port")
        self.host = parsed.hostname
        self.port = parsed.port
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self.authorization = f"Basic {token}"
        self.directory = directory

    def path(self, route: str) -> str:
        return f"{route}?{urlencode({'directory': self.directory})}"

    async def request(
        self,
        method: str,
        route: str,
        body: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        async with asyncio.timeout(30.0):
            return await self._request(method, route, body, expected)

    async def _request(
        self,
        method: str,
        route: str,
        body: dict[str, Any] | None,
        expected: tuple[int, ...],
    ) -> Any:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = [
            f"{method} {self.path(route)} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            f"Authorization: {self.authorization}",
            "Accept: application/json",
            "Connection: close",
        ]
        if body is not None:
            headers.extend(("Content-Type: application/json", f"Content-Length: {len(raw)}"))
        try:
            writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + raw)
            await writer.drain()
            status, response_headers = await _read_headers(reader)
            response = await _read_body(reader, response_headers)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
        if status not in expected:
            raise _HTTPError(status, response)
        if not response:
            return None
        try:
            return json.loads(response)
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenCode returned an invalid JSON response") from exc

    async def events(self) -> AsyncGenerator[dict[str, Any], None]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        headers = [
            f"GET {self.path('/event')} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            f"Authorization: {self.authorization}",
            "Accept: text/event-stream",
            "Connection: close",
            "\r\n",
        ]
        try:
            writer.write("\r\n".join(headers).encode("ascii"))
            await writer.drain()
            status, response_headers = await _read_headers(reader)
            if status != 200:
                raise _HTTPError(status, await _read_body(reader, response_headers))
            data_lines: list[str] = []
            data_size = 0
            pending = b""
            async for chunk in _body_chunks(reader, response_headers):
                pending += chunk
                if len(pending) > _SSE_EVENT_LIMIT:
                    raise ProviderError("OpenCode sent an SSE event larger than 1 MiB")
                while b"\n" in pending:
                    raw_line, pending = pending.split(b"\n", 1)
                    line = raw_line.rstrip(b"\r").decode("utf-8", "replace")
                    if not line:
                        if data_lines:
                            payload = "\n".join(data_lines)
                            data_lines.clear()
                            data_size = 0
                            try:
                                event = json.loads(payload)
                            except json.JSONDecodeError as exc:
                                raise ProviderError("OpenCode sent an invalid SSE event") from exc
                            if isinstance(event, dict):
                                yield event
                        continue
                    if line.startswith("data:"):
                        data = line[5:].lstrip(" ")
                        data_size += len(data.encode("utf-8"))
                        if data_size > _SSE_EVENT_LIMIT:
                            raise ProviderError("OpenCode sent an SSE event larger than 1 MiB")
                        data_lines.append(data)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()


@dataclass
class _Server:
    process: asyncio.subprocess.Process
    url: str
    output_task: asyncio.Task[None]


class OpenCodeProvider:
    """Run each turn through a private OpenCode server owned by that turn."""

    def __init__(self, executable: str = "opencode", startup_timeout: float = 10.0) -> None:
        self.executable = executable
        self.startup_timeout = startup_timeout

    async def check(self) -> dict[str, Any]:
        executable = _opencode_executable(self.executable)
        if executable is None:
            return {
                "available": False,
                "detail": (
                    "OpenCode is not installed or is not on PATH; "
                    "install it to use this provider."
                ),
            }

        username, password = "zeus-code", secrets.token_urlsafe(32)
        server: _Server | None = None
        try:
            server = await self._start_server(os.getcwd(), executable, username, password)
            client = _HTTPClient(server.url, username, password, os.getcwd())
            health = await client.request("GET", "/global/health")
            providers = await client.request("GET", "/provider")
            connected = (
                set(providers.get("connected", []))
                if isinstance(providers, dict)
                else set()
            )
            models = _models(providers, connected)
            version = (
                str(health.get("version", "unknown"))
                if isinstance(health, dict)
                else "unknown"
            )
            if connected:
                names = ", ".join(sorted(connected))
                detail = f"OpenCode {version} ready; connected providers: {names}."
            else:
                detail = (
                    f"OpenCode {version} is installed, but no provider is authenticated. "
                    "Run `opencode auth login`."
                )
            return {
                "available": bool(connected),
                "version": version,
                "detail": detail,
                "models": models,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"available": False, "detail": f"OpenCode discovery failed: {exc}"}
        finally:
            if server is not None:
                await self._stop_server(server)

    async def run(self, context: RunContext, prompt: str) -> None:
        executable = _opencode_executable(self.executable)
        if executable is None:
            raise ProviderError("OpenCode is not installed or is not on PATH")

        username, password = "zeus-code", secrets.token_urlsafe(32)
        server: _Server | None = None
        client: _HTTPClient | None = None
        session_id: str | None = None
        events: AsyncGenerator[dict[str, Any], None] | None = None
        try:
            server = await self._start_server(context.cwd, executable, username, password)
            client = _HTTPClient(server.url, username, password, context.cwd)
            if context.session_id:
                session_id = context.session_id
                await client.request("GET", f"/session/{quote(session_id, safe='')}")
            else:
                created = await client.request(
                    "POST", "/session", {"title": f"Zeus Code: {context.thread_id}"}
                )
                session_id = created.get("id") if isinstance(created, dict) else None
                if not isinstance(session_id, str):
                    raise ProviderError("OpenCode did not return a session ID")

            # The daemon persists this callback before it returns. It must precede the prompt.
            await context.emit("provider_session", {"session_id": session_id})

            events = client.events()
            first_event = asyncio.create_task(anext(events))
            try:
                initial = await asyncio.wait_for(first_event, self.startup_timeout)
            except StopAsyncIteration as exc:
                raise ProviderError("OpenCode closed its event stream during startup") from exc

            body: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
            if context.model:
                provider_id, separator, model_id = context.model.partition("/")
                if not separator or not provider_id or not model_id:
                    raise ProviderError("OpenCode model must use the provider/model form")
                body["model"] = {"providerID": provider_id, "modelID": model_id}
            for setting in ("agent", "variant"):
                value = context.settings.get(setting)
                if isinstance(value, str) and value:
                    body[setting] = value

            await client.request(
                "POST",
                f"/session/{quote(session_id, safe='')}/prompt_async",
                body,
                expected=(204,),
            )
            await self._consume_turn(context, client, session_id, initial, events)
        except asyncio.CancelledError:
            if client is not None and session_id is not None:
                try:
                    await asyncio.wait_for(
                        client.request("POST", f"/session/{quote(session_id, safe='')}/abort"),
                        timeout=3.0,
                    )
                except (OSError, ProviderError, _HTTPError, asyncio.TimeoutError):
                    pass
            raise
        except _HTTPError as exc:
            if exc.status == 404 and context.session_id:
                raise ProviderError(
                    f"OpenCode session {context.session_id!r} was not found; start a new thread"
                ) from exc
            raise ProviderError(str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise ProviderError("Timed out waiting for the OpenCode server") from exc
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            raise ProviderError(f"Lost connection to the OpenCode server: {exc}") from exc
        finally:
            if events is not None:
                with contextlib.suppress(ConnectionError, OSError, RuntimeError):
                    await events.aclose()
            if server is not None:
                await self._stop_server(server)

    async def _consume_turn(
        self,
        context: RunContext,
        client: _HTTPClient,
        session_id: str,
        initial: dict[str, Any],
        events: AsyncIterator[dict[str, Any]],
    ) -> None:
        message_roles: dict[str, str] = {}
        part_types: dict[str, str] = {}
        tool_inputs: dict[str, dict[str, Any]] = {}

        async def consume(event: dict[str, Any]) -> bool:
            event_type = event.get("type")
            properties = event.get("properties")
            if not isinstance(properties, dict) or _event_session_id(properties) != session_id:
                return False

            if event_type == "message.updated":
                info = properties.get("info")
                if isinstance(info, dict) and isinstance(info.get("id"), str):
                    message_roles[info["id"]] = str(info.get("role", "assistant"))
                return False

            if (
                event_type == "message.part.delta"
                and properties.get("field") == "text"
                and part_types.get(str(properties.get("partID"))) == "text"
            ):
                delta = properties.get("delta")
                if isinstance(delta, str) and delta:
                    await context.emit(
                        "message_delta",
                        {
                            "item_id": str(
                                properties.get("partID", properties.get("messageID", ""))
                            ),
                            "text": delta,
                        },
                    )
                return False

            if event_type == "message.part.updated":
                part = properties.get("part")
                if not isinstance(part, dict):
                    return False
                if isinstance(part.get("id"), str) and isinstance(part.get("type"), str):
                    part_types[part["id"]] = part["type"]
                if part.get("type") == "tool":
                    await self._emit_tool(context, part, tool_inputs)
                elif (
                    part.get("type") == "text"
                    and isinstance(part.get("time"), dict)
                    and part["time"].get("end")
                ):
                    text = part.get("text")
                    if isinstance(text, str) and text and not part.get("synthetic"):
                        await context.emit(
                            "message",
                            {
                                "role": message_roles.get(str(part.get("messageID")), "assistant"),
                                "text": text,
                                "item_id": str(part.get("id", part.get("messageID", ""))),
                            },
                        )
                return False

            if event_type in {"permission.asked", "permission.v2.asked"}:
                await self._handle_permission(context, client, properties, tool_inputs)
                return False

            if event_type == "session.error":
                raise ProviderError(
                    f"OpenCode turn failed: {_clip(_event_error(properties.get('error')))}"
                )

            if event_type == "session.status":
                status = properties.get("status")
                if isinstance(status, dict) and status.get("type") == "retry":
                    await context.emit(
                        "status",
                        {"text": _clip(str(status.get("message", "OpenCode is retrying")))},
                    )
                return isinstance(status, dict) and status.get("type") == "idle"

            return event_type == "session.idle"

        if await consume(initial):
            return
        async for event in events:
            if await consume(event):
                return
        raise ProviderError("OpenCode closed its event stream before the turn completed")

    async def _emit_tool(
        self,
        context: RunContext,
        part: dict[str, Any],
        tool_inputs: dict[str, dict[str, Any]],
    ) -> None:
        state = part.get("state")
        if not isinstance(state, dict):
            return
        call_id = str(part.get("callID", part.get("id", "")))
        value = state.get("input")
        if isinstance(value, dict):
            tool_inputs[call_id] = value
        raw_status = str(state.get("status", "pending"))
        status = "failed" if raw_status == "error" else raw_status
        data: dict[str, Any] = {
            "item_id": str(part.get("id", call_id)),
            "title": _clip(str(state.get("title") or part.get("tool") or "tool")),
            "status": status,
        }
        output = state.get("output") if raw_status == "completed" else state.get("error")
        if isinstance(output, str) and output:
            data["text"] = output
        await context.emit("tool", data)

    async def _handle_permission(
        self,
        context: RunContext,
        client: _HTTPClient,
        properties: dict[str, Any],
        tool_inputs: dict[str, dict[str, Any]],
    ) -> None:
        request_id = properties.get("id") or properties.get("requestID")
        if not isinstance(request_id, str):
            raise ProviderError("OpenCode sent a permission request without an ID")
        permission = str(properties.get("permission") or properties.get("action") or "permission")
        command = _permission_command(properties, tool_inputs)
        if permission == "bash":
            kind = "command"
        elif permission in {"edit", "write", "patch"}:
            kind = "file_change"
        else:
            kind = "permission"
        decision = await context.approve(
            {
                "provider_request_id": request_id,
                "kind": kind,
                "command": command,
                "cwd": context.cwd,
                "details": properties,
            }
        )
        if decision not in {"allow", "reject"}:
            raise ProviderError(f"Invalid approval decision {decision!r}")
        await client.request(
            "POST",
            f"/permission/{quote(request_id, safe='')}/reply",
            {"reply": "once" if decision == "allow" else "reject"},
        )

    async def _start_server(
        self, cwd: str, executable: str, username: str, password: str
    ) -> _Server:
        environment = os.environ.copy()
        environment.update(
            {
                "OPENCODE_SERVER_USERNAME": username,
                "OPENCODE_SERVER_PASSWORD": password,
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
            }
        )
        process = await spawn_supervised(
            executable,
            "serve",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            cwd=cwd,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        assert process.stdout is not None
        lines: deque[str] = deque(maxlen=10)

        async def ready() -> str:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    raise ProviderError(
                        "OpenCode server exited before becoming ready"
                        + (f": {lines[-1]}" if lines else "")
                    )
                line = raw.decode("utf-8", "replace").strip()
                lines.append(line)
                match = _READY.search(line)
                if match:
                    return match.group(1)

        try:
            url = await asyncio.wait_for(ready(), timeout=self.startup_timeout)
        except BaseException:
            temporary = _Server(process, "", asyncio.create_task(_discard(process.stdout)))
            await self._stop_server(temporary)
            raise
        output_task = asyncio.create_task(_discard(process.stdout))
        return _Server(process, url, output_task)

    async def _stop_server(self, server: _Server) -> None:
        process = server.process
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 3.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        server.output_task.cancel()
        await asyncio.gather(server.output_task, return_exceptions=True)


async def _discard(reader: asyncio.StreamReader) -> None:
    while await reader.read(8192):
        pass


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionError("OpenCode closed the HTTP connection")
    pieces = status_line.decode("ascii", "replace").rstrip().split(" ", 2)
    if len(pieces) < 2 or not pieces[1].isdigit():
        raise ProviderError("OpenCode returned an invalid HTTP status line")
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in {b"\r\n", b"\n"}:
            break
        if not line:
            raise ConnectionError("OpenCode closed the HTTP response headers")
        name, separator, value = line.decode("iso-8859-1").partition(":")
        if not separator:
            raise ProviderError("OpenCode returned an invalid HTTP header")
        headers[name.lower()] = value.strip()
    return int(pieces[1]), headers


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in _body_chunks(reader, headers):
        size += len(chunk)
        if size > _HTTP_BODY_LIMIT:
            raise ProviderError("OpenCode returned an HTTP response larger than 64 MiB")
        chunks.append(chunk)
    return b"".join(chunks)


async def _body_chunks(
    reader: asyncio.StreamReader, headers: dict[str, str]
) -> AsyncIterator[bytes]:
    if "chunked" in headers.get("transfer-encoding", "").lower():
        while True:
            size_line = await reader.readline()
            if not size_line:
                raise ConnectionError("OpenCode closed a chunked HTTP response")
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError as exc:
                raise ProviderError("OpenCode returned an invalid HTTP chunk") from exc
            if size == 0:
                while await reader.readline() not in {b"\r\n", b"\n", b""}:
                    pass
                return
            if size > _HTTP_BODY_LIMIT:
                raise ProviderError("OpenCode returned an HTTP chunk larger than 64 MiB")
            remaining = size
            while remaining:
                piece = await reader.readexactly(min(remaining, 64 * 1024))
                remaining -= len(piece)
                yield piece
            if await reader.readexactly(2) != b"\r\n":
                raise ProviderError("OpenCode returned an invalid HTTP chunk terminator")
        return
    length = headers.get("content-length")
    if length is not None:
        try:
            remaining = int(length)
        except ValueError as exc:
            raise ProviderError("OpenCode returned an invalid Content-Length") from exc
        if remaining < 0 or remaining > _HTTP_BODY_LIMIT:
            raise ProviderError("OpenCode returned an HTTP response larger than 64 MiB")
        while remaining:
            piece = await reader.readexactly(min(remaining, 64 * 1024))
            remaining -= len(piece)
            yield piece
        return
    while chunk := await reader.read(65536):
        yield chunk


def _models(providers: Any, connected: set[str]) -> list[dict[str, Any]]:
    if not isinstance(providers, dict):
        return []
    defaults = providers.get("default")
    defaults = defaults if isinstance(defaults, dict) else {}
    output: list[dict[str, Any]] = []
    for provider in providers.get("all", []):
        if not isinstance(provider, dict) or provider.get("id") not in connected:
            continue
        provider_id = str(provider["id"])
        provider_default = defaults.get(provider_id)
        models = provider.get("models", {})
        if not isinstance(models, dict):
            continue
        for model_id, model in models.items():
            name = model.get("name", model_id) if isinstance(model, dict) else model_id
            entry: dict[str, Any] = {
                "id": f"{provider_id}/{model_id}",
                "name": str(name),
            }
            if isinstance(model, dict) and isinstance(model.get("variants"), dict):
                variants: list[str] = []
                for variant in model["variants"]:
                    if isinstance(variant, str) and variant and variant not in variants:
                        variants.append(variant)
                entry["variants"] = variants
            if isinstance(provider_default, str):
                entry["is_default"] = provider_default == str(model_id)
            output.append(entry)
    return sorted(output, key=lambda item: (item["name"].casefold(), item["id"]))


def _event_session_id(properties: dict[str, Any]) -> Any:
    direct = properties.get("sessionID")
    if direct is not None:
        return direct
    for key in ("info", "part"):
        nested = properties.get(key)
        if isinstance(nested, dict) and nested.get("sessionID") is not None:
            return nested["sessionID"]
    return None


def _permission_command(
    properties: dict[str, Any], tool_inputs: dict[str, dict[str, Any]]
) -> str:
    source = properties.get("tool") or properties.get("source")
    call_id = source.get("callID") if isinstance(source, dict) else None
    inputs = tool_inputs.get(str(call_id), {})
    metadata = properties.get("metadata")
    metadata_command = metadata.get("command") if isinstance(metadata, dict) else None
    for value in (inputs.get("command"), metadata_command):
        if isinstance(value, str) and value:
            return value
    resources = properties.get("patterns") or properties.get("resources") or []
    if isinstance(resources, list):
        return "\n".join(str(item) for item in resources)
    return str(resources)


def _event_error(error: Any) -> str:
    if not isinstance(error, dict):
        return "unknown error" if error is None else str(error)
    data = error.get("data")
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return data["message"]
    if isinstance(error.get("message"), str):
        return error["message"]
    return str(error.get("name", "unknown error"))


def _error_text(body: bytes) -> str:
    if not body:
        return "empty response"
    try:
        return _event_error(json.loads(body))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body.decode("utf-8", "replace")[:500]


def _clip(value: str) -> str:
    if len(value) <= _EVENT_TEXT_LIMIT:
        return value
    return value[:_EVENT_TEXT_LIMIT] + "\n[output truncated by Zeus Code]"
