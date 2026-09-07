"""The newline-delimited RPC transport used by the CLI and TUI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION
from .paths import default_data_dir, socket_path


MAX_MESSAGE_BYTES = 1024 * 1024
CONNECT_TIMEOUT = 10.0
CALL_TIMEOUT = 60.0
SSH_CLOSE_TIMEOUT = 2.0
STDERR_TAIL_BYTES = 64 * 1024


class RPCError(Exception):
    """An error returned by the Zeus Code daemon."""

    def __init__(self, code: int | str | None, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _validate_host(host: str) -> str:
    # ``--`` protects OpenSSH options; the character allow-list also keeps a
    # destination from being interpreted differently by unusual ssh wrappers.
    if not host or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:%+\[\]-]+", host):
        raise ValueError(f"Unsafe SSH host or alias: {host!r}")
    return host


class RPCClient:
    """A single serialized connection to a local or SSH-hosted daemon."""

    def __init__(
        self,
        data_dir: Path | str | None = None,
        host: str | None = None,
        remote_command: str = "zeus-code",
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir is not None else default_data_dir()
        self.host = _validate_host(host) if host is not None else None
        if not remote_command or "\x00" in remote_command or "\n" in remote_command or "\r" in remote_command:
            raise ValueError("remote_command must be a non-empty single-line string")
        self.remote_command = remote_command
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._call_lock = asyncio.Lock()
        self.hello: dict[str, Any] | None = None

    async def __aenter__(self) -> RPCClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def connect(self) -> dict[str, Any]:
        """Open the transport and perform protocol negotiation."""
        if self._reader is not None:
            assert self.hello is not None
            return self.hello

        self._stderr_tail.clear()
        try:
            if self.host is None:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_unix_connection(
                        str(socket_path(self.data_dir)), limit=MAX_MESSAGE_BYTES + 1
                    ),
                    timeout=CONNECT_TIMEOUT,
                )
                self._reader, self._writer = reader, writer
            else:
                command = f"{shlex.quote(self.remote_command)} bridge"
                self._process = await asyncio.wait_for(
                    asyncio.create_subprocess_exec(
                        "ssh",
                        "-T",
                        "-o",
                        "BatchMode=yes",
                        "--",
                        self.host,
                        command,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=MAX_MESSAGE_BYTES + 1,
                    ),
                    timeout=CONNECT_TIMEOUT,
                )
                assert self._process.stdout is not None and self._process.stdin is not None
                self._reader = self._process.stdout
                self._writer = self._process.stdin
                assert self._process.stderr is not None
                self._stderr_task = asyncio.create_task(self._read_stderr(self._process.stderr))

            hello = await self._request("hello", {}, timeout=CONNECT_TIMEOUT)
            self._validate_hello(hello)
            self.hello = hello
            return hello
        except BaseException:
            await self.close()
            raise

    async def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """Make one RPC call. Calls are serialized to preserve reply ordering."""
        if self._reader is None or self._writer is None:
            raise RuntimeError("RPC client is not connected; call connect() first")
        async with self._call_lock:
            try:
                request_params = dict(params or {})
                result = await self._request(method, request_params, timeout=CALL_TIMEOUT)
                if method == "snapshot" and "offset" not in request_params:
                    return await self._complete_snapshot(result, request_params)
                return result
            except (asyncio.CancelledError, asyncio.TimeoutError, ConnectionError, OSError):
                # A timeout, cancellation, or malformed reply makes this stream's
                # request/reply position unknowable. Never retry a mutation.
                await self.close()
                raise

    async def _complete_snapshot(
        self, first_page: Any, base_params: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Assemble server-bounded snapshot pages on the same connection."""
        if not isinstance(first_page, dict):
            raise ConnectionError("Daemon returned a non-object snapshot page")
        server_id = first_page.get("server_id")
        if not isinstance(server_id, str) or not server_id:
            raise ConnectionError("Daemon snapshot page is missing a server id")

        assembled = dict(first_page)
        arrays: dict[str, list[Any]] = {}
        for field in ("projects", "threads", "approvals"):
            value = first_page.get(field)
            if not isinstance(value, list):
                raise ConnectionError(f"Daemon snapshot page has invalid {field}")
            arrays[field] = list(value)
            assembled[field] = arrays[field]

        offset = 0
        next_offset = self._snapshot_next_offset(first_page)
        pages = 1
        while next_offset is not None:
            if next_offset <= offset:
                raise ConnectionError("Daemon snapshot pagination did not advance")
            if pages >= 1000:
                raise ConnectionError("Daemon snapshot exceeds the 1000-page safety limit")
            offset = next_offset
            follow_params = {**base_params, "offset": offset}
            page = await self._request("snapshot", follow_params, timeout=CALL_TIMEOUT)
            pages += 1
            if not isinstance(page, dict):
                raise ConnectionError("Daemon returned a non-object snapshot page")
            if page.get("server_id") != server_id:
                raise ConnectionError("Daemon identity changed during snapshot pagination")
            for field, target in arrays.items():
                value = page.get(field)
                if not isinstance(value, list):
                    raise ConnectionError(f"Daemon snapshot page has invalid {field}")
                target.extend(value)
            next_offset = self._snapshot_next_offset(page)

        # Metadata, especially last_seq, deliberately remains from the first
        # page so event replay starts at the snapshot's low-water mark.
        assembled["next_offset"] = None
        return assembled

    @staticmethod
    def _snapshot_next_offset(page: Mapping[str, Any]) -> int | None:
        value = page.get("next_offset")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConnectionError("Daemon snapshot page has an invalid next_offset")
        return value

    async def close(self) -> None:
        """Close this client and, for SSH, only the subprocess it created."""
        writer, self._writer = self._writer, None
        self._reader = None
        self.hello = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=SSH_CLOSE_TIMEOUT)

        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=SSH_CLOSE_TIMEOUT)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=SSH_CLOSE_TIMEOUT)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

        stderr_task, self._stderr_task = self._stderr_task, None
        if stderr_task is not None:
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task

    async def _request(self, method: str, params: Mapping[str, Any], *, timeout: float) -> Any:
        assert self._reader is not None and self._writer is not None
        request_id = str(uuid.uuid4())
        request = {
            "id": request_id,
            "version": PROTOCOL_VERSION,
            "method": method,
            "params": dict(params),
        }
        try:
            payload = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ValueError(f"RPC parameters are not JSON serializable: {exc}") from exc
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError(f"RPC request exceeds the {MAX_MESSAGE_BYTES}-byte limit")

        self._writer.write(payload)
        await asyncio.wait_for(self._writer.drain(), timeout=timeout)
        try:
            line = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
        except ValueError as exc:
            raise ConnectionError(f"RPC response exceeds the {MAX_MESSAGE_BYTES}-byte limit") from exc
        if not line:
            raise ConnectionError(await self._closed_message())
        if len(line) > MAX_MESSAGE_BYTES:
            raise ConnectionError(f"RPC response exceeds the {MAX_MESSAGE_BYTES}-byte limit")
        if not line.endswith(b"\n"):
            raise ConnectionError("RPC connection closed during a response")
        try:
            response = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConnectionError("Daemon returned malformed JSON") from exc
        if not isinstance(response, dict):
            raise ConnectionError("Daemon returned a non-object RPC response")
        if response.get("id") != request_id:
            raise ConnectionError("Daemon returned a response with the wrong request id")
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict):
                raise ConnectionError("Daemon returned a malformed RPC error")
            raise RPCError(error.get("code"), str(error.get("message", "RPC request failed")))
        if "result" not in response:
            raise ConnectionError("Daemon response contains neither result nor error")
        return response["result"]

    @staticmethod
    def _validate_hello(hello: Any) -> None:
        if not isinstance(hello, dict):
            raise ConnectionError("Daemon hello response is not an object")
        protocol = hello.get("protocol_version")
        if not isinstance(protocol, int) or isinstance(protocol, bool) or protocol != PROTOCOL_VERSION:
            raise ConnectionError(
                f"Incompatible protocol version {protocol!r}; this client requires major version {PROTOCOL_VERSION}"
            )
        if not isinstance(hello.get("server_id"), str) or not hello["server_id"].strip():
            raise ConnectionError("Daemon hello response is missing a server id")

    async def _read_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > STDERR_TAIL_BYTES:
                del self._stderr_tail[:-STDERR_TAIL_BYTES]

    async def _closed_message(self) -> str:
        if self._stderr_task is not None and not self._stderr_task.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.1)
        detail = bytes(self._stderr_tail).decode("utf-8", "replace").strip()
        if self.host is not None:
            base = f"SSH connection to {self.host} closed before the daemon replied"
            return f"{base}: {detail}" if detail else f"{base}. Check the SSH alias and run 'zeus-code serve' remotely."
        return "Local Zeus Code server closed the connection"


async def bridge(data_dir: Path | str | None = None) -> None:
    """Relay stdio to a local daemon without affecting daemon lifecycle."""
    path = Path(data_dir).expanduser().resolve() if data_dir is not None else default_data_dir()
    socket_reader, socket_writer = await asyncio.open_unix_connection(
        str(socket_path(path)), limit=MAX_MESSAGE_BYTES + 1
    )
    loop = asyncio.get_running_loop()
    stdin_reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    stdin_protocol = asyncio.StreamReaderProtocol(stdin_reader)
    stdin_transport, _ = await loop.connect_read_pipe(lambda: stdin_protocol, sys.stdin.buffer)
    stdout_protocol = asyncio.streams.FlowControlMixin(loop=loop)
    stdout_transport, _ = await loop.connect_write_pipe(lambda: stdout_protocol, sys.stdout.buffer)
    stdout_writer = asyncio.StreamWriter(stdout_transport, stdout_protocol, None, loop)

    async def stdin_to_socket() -> None:
        while chunk := await stdin_reader.read(64 * 1024):
            socket_writer.write(chunk)
            await socket_writer.drain()
        socket_writer.close()
        with contextlib.suppress(Exception):
            await socket_writer.wait_closed()

    async def socket_to_stdout() -> None:
        while chunk := await socket_reader.read(64 * 1024):
            stdout_writer.write(chunk)
            await stdout_writer.drain()

    tasks = {asyncio.create_task(stdin_to_socket()), asyncio.create_task(socket_to_stdout())}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        stdin_transport.close()
        socket_writer.close()
        with contextlib.suppress(Exception):
            await socket_writer.wait_closed()
        stdout_writer.close()
        with contextlib.suppress(Exception):
            await stdout_writer.wait_closed()
