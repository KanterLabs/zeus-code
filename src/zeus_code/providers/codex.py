"""Codex app-server provider adapter.

Each Zeus run owns one app-server subprocess.  Codex persists the conversation
itself; Zeus persists the returned thread id and starts a new app-server process
to resume it on the next turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from typing import Any

from ..process import spawn_supervised
from .base import ProviderError, RunContext


_JSON_LINE_LIMIT = 1024 * 1024
_STDERR_LIMIT = 32 * 1024
_EVENT_TEXT_LIMIT = 64 * 1024
_PENDING_BYTES_LIMIT = 4 * 1024 * 1024
_PENDING_MESSAGE_LIMIT = 4096
_REQUEST_TIMEOUT = 20.0
_CLEANUP_TIMEOUT = 2.0


def _clip(value: str, limit: int = _EVENT_TEXT_LIMIT) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    marker = b"\n[output truncated by Zeus Code]"
    prefix = encoded[: max(0, limit - len(marker))]
    return prefix.decode("utf-8", errors="ignore") + marker.decode()


def _json_text(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    return text


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            info = error.get("codexErrorInfo")
            if info:
                return _clip(f"{message} ({_json_text(info)})", 8192)
            return _clip(message, 8192)
        nested = error.get("error")
        if nested is not None:
            return _error_text(nested)
    if isinstance(error, str) and error:
        return _clip(error, 8192)
    return _clip(_json_text(error), 8192)


class _AppServer:
    """A bounded JSONL connection to one owned app-server process."""

    def __init__(self, executable: str, line_limit: int) -> None:
        self.executable = executable
        self.line_limit = line_limit
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._stderr = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            self.process = await spawn_supervised(
                self.executable,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.line_limit,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                "Codex CLI was not found. Install Codex or configure its executable path."
            ) from exc
        except OSError as exc:
            raise ProviderError(f"Could not start Codex app-server: {exc}") from exc

        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                return
            self._stderr.extend(chunk)
            if len(self._stderr) > _STDERR_LIMIT:
                del self._stderr[: len(self._stderr) - _STDERR_LIMIT]

    def stderr_text(self) -> str:
        return self._stderr.decode("utf-8", errors="replace").strip()

    async def send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise ProviderError("Codex app-server is not running.")
        try:
            payload = json.dumps(
                message, ensure_ascii=False, separators=(",", ":")
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                f"Codex protocol request {message.get('method', 'response')!r} "
                "contains a value that is not JSON serializable."
            ) from exc
        if len(payload) + 1 > self.line_limit:
            raise ProviderError(
                f"Codex protocol request exceeded the {self.line_limit}-byte safety limit."
            )
        try:
            process.stdin.write(payload + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            detail = self.stderr_text()
            suffix = f" Details: {detail}" if detail else ""
            raise ProviderError(f"Codex app-server closed its input.{suffix}") from exc

    async def read(self) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise ProviderError("Codex app-server is not running.")
        try:
            line = await process.stdout.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise ProviderError(
                f"Codex app-server sent a JSON line larger than {self.line_limit} bytes."
            ) from exc
        if not line:
            await process.wait()
            detail = self.stderr_text()
            suffix = f" Details: {detail}" if detail else ""
            raise ProviderError(
                f"Codex app-server exited unexpectedly with status {process.returncode}.{suffix}"
            )
        if len(line) > self.line_limit:
            raise ProviderError(
                f"Codex app-server sent a JSON line larger than {self.line_limit} bytes."
            )
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = line[:200].decode("utf-8", errors="replace")
            raise ProviderError(f"Codex app-server sent invalid JSON: {preview!r}") from exc
        if not isinstance(message, dict):
            raise ProviderError("Codex app-server sent a non-object protocol message.")
        return message

    async def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        pending: list[dict[str, Any]] | None = None,
        timeout: float = _REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        await self.send({"method": method, "id": request_id, "params": params})

        async def wait_for_response() -> dict[str, Any]:
            pending_bytes = 0
            if pending:
                pending_bytes = sum(
                    len(
                        json.dumps(
                            item, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
                    for item in pending
                )
            while True:
                message = await self.read()
                if message.get("id") == request_id and "method" not in message:
                    if "error" in message:
                        raise ProviderError(
                            f"Codex {method} failed: {_error_text(message['error'])}"
                        )
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise ProviderError(
                            f"Codex {method} returned an invalid response."
                        )
                    return result
                if "method" in message and "id" in message and pending is None:
                    await self.reject_request(message, "request arrived before a turn was active")
                    raise ProviderError(
                        f"Codex sent unsupported early request {message.get('method')!r}."
                    )
                if pending is not None:
                    pending_bytes += len(
                        json.dumps(
                            message, ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
                    if (
                        len(pending) >= _PENDING_MESSAGE_LIMIT
                        or pending_bytes > _PENDING_BYTES_LIMIT
                    ):
                        raise ProviderError(
                            "Codex emitted more than 4 MiB of events before "
                            f"responding to {method}."
                        )
                    pending.append(message)

        try:
            return await asyncio.wait_for(wait_for_response(), timeout)
        except TimeoutError as exc:
            raise ProviderError(f"Timed out waiting for Codex {method}.") from exc

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "zeus_code",
                    "title": "Zeus Code",
                    "version": "1.0.0",
                }
            },
        )
        await self.send({"method": "initialized", "params": {}})

    async def reject_request(self, message: dict[str, Any], reason: str) -> None:
        if "id" not in message:
            return
        await self.send(
            {
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": f"Zeus Code cannot handle this server request: {reason}",
                },
            }
        )

    async def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 0.5)
        except TimeoutError:
            self._signal_group(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), _CLEANUP_TIMEOUT)
            except TimeoutError:
                self._signal_group(signal.SIGKILL)
                await process.wait()
        # A misbehaving descendant can keep inherited pipes open after the app-server
        # parent exits. The group was created by this connection and is safe to reap.
        self._signal_group(signal.SIGTERM, even_if_parent_exited=True)
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, 1.0)
            except TimeoutError:
                self._stderr_task.cancel()
                await asyncio.gather(self._stderr_task, return_exceptions=True)

    def _signal_group(
        self, sig: signal.Signals, *, even_if_parent_exited: bool = False
    ) -> None:
        process = self.process
        if process is None or (process.returncode is not None and not even_if_parent_exited):
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return


class CodexProvider:
    """Translate the Codex app-server protocol into Zeus provider events."""

    def __init__(
        self,
        executable: str = "codex",
        *,
        line_limit: int = _JSON_LINE_LIMIT,
        request_timeout: float = _REQUEST_TIMEOUT,
    ) -> None:
        self.executable = executable
        self.line_limit = line_limit
        self.request_timeout = request_timeout

    def _connection(self) -> _AppServer:
        return _AppServer(self.executable, self.line_limit)

    async def run(self, context: RunContext, prompt: str) -> None:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("Codex requires a non-empty prompt.")
        if not os.path.isabs(context.cwd):
            raise ProviderError("Codex working directory must be an absolute path.")
        if not os.path.isdir(context.cwd):
            raise ProviderError(f"Codex working directory does not exist: {context.cwd}")

        server = self._connection()
        provider_thread_id: str | None = None
        turn_id: str | None = None
        try:
            await server.start()
            await server.initialize()

            thread_method = "thread/resume" if context.session_id else "thread/start"
            thread_params = self._thread_params(context)
            if context.session_id:
                thread_params["threadId"] = context.session_id
            pending: list[dict[str, Any]] = []
            thread_result = await server.request(
                thread_method,
                thread_params,
                pending=pending,
                timeout=self.request_timeout,
            )
            thread = thread_result.get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise ProviderError(f"Codex {thread_method} did not return a thread id.")
            provider_thread_id = thread["id"]
            await context.emit(
                "provider_session", {"session_id": provider_thread_id}
            )

            turn_params = self._turn_params(context, provider_thread_id, prompt)
            turn_result = await server.request(
                "turn/start",
                turn_params,
                pending=pending,
                timeout=self.request_timeout,
            )
            turn = turn_result.get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise ProviderError("Codex turn/start did not return a turn id.")
            turn_id = turn["id"]

            for message in pending:
                done = await self._handle_message(
                    server, context, message, provider_thread_id, turn_id
                )
                if done:
                    return
            while True:
                message = await server.read()
                if await self._handle_message(
                    server, context, message, provider_thread_id, turn_id
                ):
                    return
        except asyncio.CancelledError:
            if provider_thread_id and turn_id:
                try:
                    await asyncio.shield(
                        server.request(
                            "turn/interrupt",
                            {"threadId": provider_thread_id, "turnId": turn_id},
                            timeout=min(self.request_timeout, 3.0),
                        )
                    )
                except (ProviderError, asyncio.CancelledError):
                    pass
            raise
        finally:
            await asyncio.shield(server.close())

    def _thread_params(self, context: RunContext) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": context.cwd}
        if context.model:
            params["model"] = context.model
        settings = context.settings
        approval = settings.get("approval_policy", settings.get("approvalPolicy"))
        if approval is not None:
            aliases = {
                "onRequest": "on-request",
                "unlessTrusted": "untrusted",
                "on_request": "on-request",
            }
            params["approvalPolicy"] = (
                aliases.get(approval, approval) if isinstance(approval, str) else approval
            )
        sandbox = settings.get("sandbox")
        if sandbox is not None:
            aliases = {
                "readOnly": "read-only",
                "workspaceWrite": "workspace-write",
                "dangerFullAccess": "danger-full-access",
            }
            params["sandbox"] = (
                aliases.get(sandbox, sandbox) if isinstance(sandbox, str) else sandbox
            )
        personality = settings.get("personality")
        if personality is not None:
            params["personality"] = personality
        service_tier = settings.get("service_tier", settings.get("serviceTier"))
        if service_tier is not None:
            params["serviceTier"] = service_tier
        return params

    def _turn_params(
        self, context: RunContext, thread_id: str, prompt: str
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        settings = context.settings
        effort = settings.get("reasoning_effort", settings.get("effort"))
        if effort is not None:
            params["effort"] = effort
        summary = settings.get("reasoning_summary", settings.get("summary"))
        if summary is not None:
            params["summary"] = summary
        return params

    async def _handle_message(
        self,
        server: _AppServer,
        context: RunContext,
        message: dict[str, Any],
        thread_id: str,
        turn_id: str,
    ) -> bool:
        method = message.get("method")
        if isinstance(method, str) and "id" in message:
            await self._handle_server_request(
                server, context, message, thread_id, turn_id
            )
            return False
        if not isinstance(method, str):
            return False
        params = message.get("params")
        if not isinstance(params, dict):
            return False
        message_thread = params.get("threadId")
        if message_thread is not None and message_thread != thread_id:
            return False
        message_turn = params.get("turnId")
        if message_turn is not None and message_turn != turn_id:
            return False

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            item_id = params.get("itemId")
            if isinstance(delta, str) and isinstance(item_id, str):
                await context.emit(
                    "message_delta", {"item_id": item_id, "text": delta}
                )
        elif method == "item/commandExecution/outputDelta":
            delta = params.get("delta")
            item_id = params.get("itemId")
            if isinstance(delta, str) and isinstance(item_id, str):
                await context.emit(
                    "tool",
                    {
                        "item_id": item_id,
                        "title": "Command",
                        "status": "running",
                        "text": delta,
                        "delta": True,
                    },
                )
        elif method in {
            "item/fileChange/outputDelta",
            "item/mcpToolCall/progress",
        }:
            text = params.get("delta", params.get("message"))
            item_id = params.get("itemId")
            if isinstance(text, str) and isinstance(item_id, str):
                title = (
                    "File changes"
                    if method == "item/fileChange/outputDelta"
                    else "MCP tool"
                )
                is_delta = method == "item/fileChange/outputDelta"
                await context.emit(
                    "tool",
                    {
                        "item_id": item_id,
                        "title": title,
                        "status": "running",
                        "text": text,
                        "delta": is_delta,
                    },
                )
        elif method == "item/fileChange/patchUpdated":
            item_id = params.get("itemId")
            changes = params.get("changes")
            if isinstance(item_id, str) and isinstance(changes, list):
                await context.emit(
                    "tool",
                    {
                        "item_id": item_id,
                        "title": "File changes",
                        "status": "running",
                        "text": _json_text(changes),
                    },
                )
        elif method == "item/plan/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                await context.emit("status", {"text": _clip(delta)})
        elif method in {"item/started", "item/completed"}:
            item = params.get("item")
            if isinstance(item, dict):
                await self._emit_item(
                    context, item, completed=method == "item/completed"
                )
        elif method in {"warning", "configWarning"}:
            text = params.get("message", params.get("summary"))
            if isinstance(text, str):
                await context.emit("status", {"text": _clip(text)})
        elif method == "turn/plan/updated":
            plan = params.get("plan")
            if isinstance(plan, list):
                steps = [
                    str(row.get("step"))
                    for row in plan
                    if isinstance(row, dict) and row.get("step")
                ]
                if steps:
                    await context.emit("status", {"text": _clip("Plan: " + "; ".join(steps))})
        elif method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise ProviderError("Codex sent an invalid turn/completed event.")
            status = turn.get("status")
            if status == "completed":
                return True
            if status == "interrupted":
                raise ProviderError("Codex interrupted the turn before completion.")
            if status == "failed":
                raise ProviderError(
                    f"Codex turn failed: {_error_text(turn.get('error'))}"
                )
            raise ProviderError(f"Codex turn ended with unknown status {status!r}.")
        return False

    async def _emit_item(
        self, context: RunContext, item: dict[str, Any], *, completed: bool
    ) -> None:
        item_type = item.get("type")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not isinstance(item_type, str):
            return
        if item_type == "agentMessage":
            if completed and isinstance(item.get("text"), str):
                await context.emit(
                    "message",
                    {
                        "role": "assistant",
                        "text": item["text"],
                        "item_id": item_id,
                    },
                )
            return
        if item_type in {"userMessage", "reasoning", "hookPrompt"}:
            return
        if item_type == "plan":
            if completed and isinstance(item.get("text"), str):
                await context.emit("status", {"text": _clip(item["text"])})
            return

        title, text = self._tool_display(item_type, item)
        raw_status = item.get("status")
        status_aliases = {
            "inProgress": "running",
            "completed": "completed",
            "failed": "failed",
            "declined": "declined",
        }
        status = status_aliases.get(raw_status, "completed" if completed else "running")
        event: dict[str, Any] = {
            "item_id": item_id,
            "title": _clip(title, 2048),
            "status": status,
        }
        if text:
            event["text"] = text
        await context.emit("tool", event)

    def _tool_display(self, item_type: str, item: dict[str, Any]) -> tuple[str, str]:
        if item_type == "commandExecution":
            return str(item.get("command") or "Command"), str(
                item.get("aggregatedOutput") or ""
            )
        if item_type == "fileChange":
            changes = item.get("changes")
            return "File changes", _json_text(changes) if changes else ""
        if item_type == "mcpToolCall":
            title = f"{item.get('server', 'MCP')}: {item.get('tool', 'tool')}"
            body = item.get("error") or item.get("result")
            return title, _json_text(body) if body is not None else ""
        if item_type == "dynamicToolCall":
            body = item.get("contentItems")
            return str(item.get("tool") or "Tool"), _json_text(body) if body else ""
        if item_type == "collabAgentToolCall":
            return str(item.get("tool") or "Agent"), str(item.get("prompt") or "")
        if item_type == "webSearch":
            return "Web search", str(item.get("query") or "")
        if item_type == "imageView":
            return "View image", str(item.get("path") or "")
        if item_type == "imageGeneration":
            return "Generate image", _json_text(item.get("result"))
        if item_type == "functionCallOutput":
            return str(item.get("name") or "Tool output"), _json_text(item.get("output"))
        return item_type, _json_text(
            {key: value for key, value in item.items() if key not in {"id", "type"}}
        )

    async def _handle_server_request(
        self,
        server: _AppServer,
        context: RunContext,
        message: dict[str, Any],
        thread_id: str,
        turn_id: str,
    ) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            await server.reject_request(message, "malformed request")
            raise ProviderError("Codex sent a malformed server request.")
        if params.get("threadId", params.get("conversationId")) not in {
            None,
            thread_id,
        } or params.get("turnId") not in {None, turn_id}:
            await server.reject_request(message, "request belongs to another turn")
            raise ProviderError("Codex sent an approval for another turn.")

        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        }:
            await self._handle_approval(server, context, message, method, params)
            return
        await server.reject_request(message, f"unsupported method {method}")
        raise ProviderError(
            f"Codex requested {method}, which Zeus Code cannot answer safely."
        )

    async def _handle_approval(
        self,
        server: _AppServer,
        context: RunContext,
        message: dict[str, Any],
        method: str,
        params: dict[str, Any],
    ) -> None:
        request_id = message["id"]
        if method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
            kind = "file_change"
        elif method == "item/permissions/requestApproval":
            kind = "permission"
        else:
            kind = "command"
        command: Any
        if kind == "command":
            command = params.get("command")
        elif kind == "file_change":
            command = params.get("grantRoot") or "file changes"
        else:
            command = params.get("permissions")
        cwd = params.get("cwd") or context.cwd
        approval = {
            "provider_request_id": str(request_id),
            "kind": kind,
            "command": command,
            "cwd": cwd,
            "details": params.copy(),
        }
        decision = await context.approve(approval)
        if decision not in {"allow", "reject"}:
            await self._send_approval_response(
                server, request_id, method, params, "reject"
            )
            raise ProviderError(
                f"Zeus approval callback returned invalid decision {decision!r}."
            )
        await self._send_approval_response(
            server, request_id, method, params, decision
        )

    async def _send_approval_response(
        self,
        server: _AppServer,
        request_id: Any,
        method: str,
        params: dict[str, Any],
        decision: str,
    ) -> None:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            result = {"decision": "accept" if decision == "allow" else "decline"}
        elif method == "item/permissions/requestApproval":
            permissions = params.get("permissions") if decision == "allow" else {}
            result = {"permissions": permissions or {}, "scope": "turn"}
        else:
            result = {
                "decision": "approved"
                if decision == "allow"
                else {"denied": {"rejection": "Rejected in Zeus Code"}}
            }
        await server.send({"id": request_id, "result": result})

    async def check(self) -> dict[str, Any]:
        executable = shutil.which(self.executable)
        if executable is None:
            return {
                "available": False,
                "detail": "Codex CLI is not installed or is not on PATH.",
            }
        version = await self._version(executable)
        server = _AppServer(executable, self.line_limit)
        try:
            await server.start()
            await server.initialize()
            account_result = await server.request(
                "account/read",
                {"refreshToken": False},
                timeout=self.request_timeout,
            )
            models = await self._models(server)
            account = account_result.get("account")
            requires_auth = account_result.get("requiresOpenaiAuth") is True
            if account is None and requires_auth:
                return {
                    "available": False,
                    "version": version,
                    "detail": "Codex is installed but not authenticated. Run `codex login`.",
                    "models": models,
                }
            if isinstance(account, dict):
                account_type = str(account.get("type") or "configured account")
                detail = f"Codex is installed and authenticated with {account_type}."
            else:
                detail = "Codex is installed; the selected provider does not require OpenAI auth."
            return {
                "available": True,
                "version": version,
                "detail": detail,
                "models": models,
            }
        except ProviderError as exc:
            return {
                "available": False,
                "version": version,
                "detail": str(exc),
            }
        finally:
            await asyncio.shield(server.close())

    async def _version(self, executable: str) -> str:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=8192,
            )
            assert process.stdout is not None
            output = await asyncio.wait_for(process.stdout.readline(), 5.0)
            if len(output) > 8192:
                raise ValueError("oversized version output")
            await asyncio.wait_for(process.wait(), 5.0)
        except (TimeoutError, ValueError, asyncio.LimitOverrunError):
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return "unknown"
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            raise
        except OSError:
            return "unknown"
        text = output.decode("utf-8", errors="replace").strip()
        return text or "unknown"

    async def _models(self, server: _AppServer) -> list[dict[str, str]]:
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            result = await server.request(
                "model/list", params, timeout=self.request_timeout
            )
            data = result.get("data")
            if not isinstance(data, list):
                raise ProviderError("Codex model/list returned an invalid model list.")
            for row in data:
                if not isinstance(row, dict):
                    continue
                model_id = row.get("id", row.get("model"))
                if not isinstance(model_id, str) or model_id in seen:
                    continue
                name = row.get("displayName")
                models.append(
                    {"id": model_id, "name": name if isinstance(name, str) else model_id}
                )
                seen.add(model_id)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return models
            cursor = next_cursor
        raise ProviderError("Codex model discovery exceeded 10 result pages.")
