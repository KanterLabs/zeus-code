"""Per-machine durable session owner. Clients may come and go independently."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import stat
import time
from typing import Any, Callable
import uuid

from . import PROTOCOL_VERSION, __version__
from .paths import default_data_dir, secure_directory, socket_path
from .providers.base import ProviderError, RunContext
from .repository import create_worktree, discover_projects, get_diff, inspect_repository
from .storage import Store

MAX_LINE = 1024 * 1024
MAX_PROMPT = 128 * 1024
MAX_CLIENTS = 64
MAX_RUNS = 8
ACTIVE_STATES = {"running", "awaiting_approval"}


class RequestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _string(params: dict, key: str, *, maximum: int = 4096, optional: bool = False) -> str | None:
    value = params.get(key)
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise RequestError("invalid_request", f"{key} must be nonempty text, at most {maximum} characters.")
    return value


def _integer(params: dict, key: str, default: int, maximum: int) -> int:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise RequestError("invalid_request", f"{key} must be an integer between 0 and {maximum}.")
    return value


class Daemon:
    def __init__(self, data_dir: Path, *, providers: dict[str, Callable] | None = None):
        self.data_dir = secure_directory(Path(data_dir))
        self.store: Store | None = None
        self.providers = providers
        self.tasks: dict[str, asyncio.Task] = {}
        self.approval_waiters: dict[str, asyncio.Future] = {}
        self.server: asyncio.AbstractServer | None = None
        self.stopping = asyncio.Event()
        self.clients: set[asyncio.StreamWriter] = set()
        self.client_tasks: set[asyncio.Task] = set()
        self._lock_fd: int | None = None
        self._provider_cache: tuple[float, dict] | None = None
        self._closing = False

    def _provider_factories(self) -> dict[str, Callable]:
        if self.providers is not None:
            return self.providers
        from .providers.codex import CodexProvider
        from .providers.opencode import OpenCodeProvider
        return {"codex": CodexProvider, "opencode": OpenCodeProvider}

    async def start(self) -> bool:
        """Acquire the sole-owner lock before touching the database or socket."""
        path = self.data_dir / "server.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._lock_fd = fd
        try:
            sock = socket_path(self.data_dir)
            if sock.exists() or sock.is_symlink():
                info = sock.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise RuntimeError(f"Refusing to replace unexpected path {sock}. Choose another --data-dir.")
                sock.unlink()
            self.store = Store(self.data_dir)
            self.store.recover_interrupted()
            self.server = await asyncio.start_unix_server(self._handle_client, path=str(sock), limit=MAX_LINE + 1)
            sock.chmod(0o600)
            metadata = self.data_dir / "server.json"
            metadata.write_text(json.dumps(self.hello()) + "\n")
            metadata.chmod(0o600)
            return True
        except BaseException:
            await self.close()
            raise

    def hello(self) -> dict:
        assert self.store is not None
        return {"protocol_version": PROTOCOL_VERSION, "version": __version__,
                "server_id": self.store.server_id, "hostname": socket.gethostname(), "pid": os.getpid()}

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        if len(self.clients) >= MAX_CLIENTS or self._closing:
            writer.close()
            return
        self.clients.add(writer)
        if current is not None:
            self.client_tasks.add(current)
        try:
            while not self._closing:
                request_id = None
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=300)
                    if not line:
                        break
                    if len(line) > MAX_LINE:
                        raise RequestError("too_large", "Request exceeds the 1 MiB protocol limit.")
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise RequestError("invalid_request", "Expected a JSON request object.")
                    request_id = request.get("id")
                    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool) or len(str(request_id)) > 128:
                        request_id = None
                        raise RequestError("invalid_request", "A short request id is required.")
                    if request.get("version") != PROTOCOL_VERSION:
                        raise RequestError("version_mismatch", f"Protocol {PROTOCOL_VERSION} is required. Install matching Zeus Code versions on client and server.")
                    method = request.get("method")
                    params = request.get("params", {})
                    if not isinstance(method, str) or not isinstance(params, dict):
                        raise RequestError("invalid_request", "method must be text and params must be an object.")
                    result = await self.dispatch(method, params)
                    response = {"id": request_id, "result": result}
                except (asyncio.TimeoutError, ConnectionError):
                    break
                except (ValueError, KeyError, RuntimeError, OSError, sqlite3.Error) as exc:
                    if isinstance(exc, KeyError):
                        code, message = "not_found", "Requested project, thread, run, or approval does not exist. Refresh and try again."
                    elif isinstance(exc, RequestError):
                        code, message = exc.code, str(exc)
                    elif isinstance(exc, sqlite3.Error):
                        code, message = "storage_error", "Cannot persist state. Check free disk space and state-directory permissions before retrying."
                    else:
                        code, message = "request_failed", str(exc)
                    response = {"id": request_id, "error": {"code": code, "message": message}}
                encoded = json.dumps(response, ensure_ascii=False).encode() + b"\n"
                if len(encoded) > MAX_LINE:
                    encoded = json.dumps({"id": request_id, "error": {
                        "code": "too_large", "message": "Response exceeds 1 MiB. Request a smaller page or a single diff file."}}).encode() + b"\n"
                writer.write(encoded)
                await asyncio.wait_for(writer.drain(), timeout=15)
        except (ConnectionError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            self.clients.discard(writer)
            if current is not None:
                self.client_tasks.discard(current)
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    def _check_storage_budget(self) -> None:
        raw = os.environ.get("ZEUS_CODE_MAX_STORAGE_MB", "1024")
        try:
            budget = max(1, int(raw)) * 1024 * 1024
        except ValueError:
            raise RequestError("configuration", "ZEUS_CODE_MAX_STORAGE_MB must be a positive integer.") from None
        assert self.store is not None
        size = sum(p.stat().st_size for p in self.data_dir.glob(self.store.path.name + "*") if p.is_file())
        if size >= budget:
            raise RequestError("storage_limit", "History storage limit reached. Back up the state and increase ZEUS_CODE_MAX_STORAGE_MB before starting more work. Existing history is preserved.")

    async def dispatch(self, method: str, params: dict) -> Any:
        assert self.store is not None
        s = self.store
        if method == "hello":
            return self.hello()
        if method == "snapshot":
            last = s.last_seq() if callable(s.last_seq) else s.last_seq
            offset = _integer(params, "offset", 0, 2**31 - 1)
            records = [("projects", p) for p in s.projects()]
            records.extend(("threads", t) for t in sorted(s.threads(), key=lambda t: (t["created_at"], t["id"])))
            records.extend(("approvals", a) for a in s.approvals())
            result = {**self.hello(), "projects": [], "threads": [], "approvals": [],
                      "last_seq": last, "next_offset": None}
            used = 0
            for index in range(offset, len(records)):
                category, record = records[index]
                size = len(json.dumps(record, ensure_ascii=False).encode("utf-8")) + 2
                if used and used + size > 512 * 1024:
                    result["next_offset"] = index
                    break
                result[category].append(record)
                used += size
            return result
        if method == "providers":
            return await self._check_providers()
        if method == "discover_projects":
            return await discover_projects(_string(params, "root"))
        if method == "add_project":
            repo = await inspect_repository(_string(params, "path"))
            name = _string(params, "name", maximum=120, optional=True) or Path(repo["path"]).name
            return s.add_project(repo["path"], name, repo["branch"])
        if method == "create_thread":
            project = s.project(_string(params, "project_id", maximum=128))
            title = _string(params, "title", maximum=200)
            provider = _string(params, "provider", maximum=30)
            if provider not in self._provider_factories():
                raise RequestError("unknown_provider", "Choose codex or opencode when creating a thread.")
            model = _string(params, "model", maximum=200, optional=True)
            settings = self._settings(params.get("settings", {}))
            isolated = params.get("worktree", False)
            if not isinstance(isolated, bool):
                raise RequestError("invalid_request", "worktree must be true or false.")
            repo = await inspect_repository(project["path"])
            cwd, branch = repo["path"], repo["branch"]
            if isolated:
                worktree_id = uuid.uuid4().hex
                destination = self.data_dir / "worktrees" / project["id"] / worktree_id
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                created = await create_worktree(cwd, str(destination), "zeus/" + worktree_id[:16])
                cwd, branch = created["cwd"], created["branch"]
            return s.create_thread(project["id"], title, provider, cwd, branch, isolated, model=model, settings=settings)
        if method == "update_thread":
            thread_id = _string(params, "thread_id", maximum=128)
            thread = s.thread(thread_id)
            changes = {k: v for k, v in params.items() if k != "thread_id"}
            if not changes or set(changes) - {"title", "archived", "draft", "scroll", "model", "settings"}:
                raise RequestError("invalid_request", "Only title, archived, draft, scroll, model and settings can be updated.")
            if "title" in changes:
                _string(changes, "title", maximum=200)
            if "model" in changes:
                _string(changes, "model", maximum=200, optional=True)
            if "settings" in changes:
                self._settings(changes["settings"])
            if "archived" in changes and not isinstance(changes["archived"], bool):
                raise RequestError("invalid_request", "archived must be true or false.")
            if "draft" in changes and (not isinstance(changes["draft"], str) or len(changes["draft"]) > MAX_PROMPT):
                raise RequestError("invalid_request", "Draft exceeds the 128 KiB limit.")
            if "scroll" in changes:
                _integer(changes, "scroll", 0, 2**31 - 1)
            if thread["state"] in ACTIVE_STATES and any(k in changes for k in ("archived", "model", "settings")):
                raise RequestError("run_active", "Wait for or cancel this run before archiving or changing its model settings.")
            return s.update_thread(thread_id, **changes)
        if method == "send":
            thread_id = _string(params, "thread_id", maximum=128)
            prompt = _string(params, "prompt", maximum=MAX_PROMPT)
            request_id = _string(params, "request_id", maximum=128)
            accepted = s.find_run_by_request(request_id)
            if accepted:
                if accepted["thread_id"] != thread_id or accepted["prompt"] != prompt:
                    raise RequestError("request_conflict", "request_id was already used for a different send.")
                return accepted
            self._check_storage_budget()
            thread = s.thread(thread_id)
            if thread["archived"]:
                raise RequestError("archived", "Unarchive this thread before sending a new message.")
            # Check duplicates before capacity: an accepted request must remain retryable.
            if len(self.tasks) >= MAX_RUNS:
                existing = s.active_run(thread_id)
                if not existing or existing.get("request_id") != request_id:
                    raise RequestError("capacity", f"This machine already has {MAX_RUNS} active runs. Wait or cancel one.")
            if not Path(thread["cwd"]).is_dir():
                raise RequestError("missing_directory", "This thread's working directory is missing. Restore it before resuming.")
            run, created = s.create_run(thread_id, prompt, request_id)
            if created:
                self.tasks[run["id"]] = asyncio.create_task(self._execute(thread, run, prompt), name="zeus-run-" + run["id"])
            return run
        if method == "cancel":
            thread_id = _string(params, "thread_id", maximum=128)
            s.thread(thread_id)
            run = s.active_run(thread_id)
            wanted = _string(params, "run_id", maximum=128, optional=True)
            if wanted and (not run or run["id"] != wanted):
                raise RequestError("stale_run", "That run is no longer the active run. Refresh before cancelling.")
            if not run:
                return {"cancelled": False}
            task = self.tasks.get(run["id"])
            if task:
                task.cancel()
                # State is acknowledged after cleanup, so the next send cannot overlap.
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                if s.run(run["id"])["state"] in ACTIVE_STATES:
                    s.finish_run(run["id"], "cancelled")
                self.tasks.pop(run["id"], None)
            else:
                s.finish_run(run["id"], "cancelled")
            return {"cancelled": True}
        if method == "approve":
            request_id = _string(params, "request_id", maximum=128)
            decision = _string(params, "decision", maximum=10)
            if decision not in ("allow", "reject"):
                raise RequestError("invalid_request", "Approval decision must be allow or reject.")
            approval = s.approval(request_id)
            if approval["state"] == "pending" and request_id not in self.approval_waiters:
                raise RequestError("stale_approval", "This approval no longer has an active provider request. Refresh the thread.")
            record = s.resolve_approval(request_id, decision)
            waiter = self.approval_waiters.get(request_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(decision)
            return record
        if method == "events":
            return s.events(after=_integer(params, "after", 0, 2**63 - 1),
                            limit=max(1, _integer(params, "limit", 200, 500)),
                            thread_id=_string(params, "thread_id", maximum=128, optional=True))
        if method == "history":
            before = params.get("before")
            if before is not None:
                _integer(params, "before", 0, 2**63 - 1)
            return s.history(_string(params, "thread_id", maximum=128), before=before,
                             limit=max(1, _integer(params, "limit", 200, 500)))
        if method == "diff":
            thread = s.thread(_string(params, "thread_id", maximum=128))
            result = await get_diff(thread["cwd"], _string(params, "path", optional=True))
            s.update_thread(thread["id"], branch=result["branch"])
            return {**result, "shared": not thread["isolated"]}
        if method == "stop":
            asyncio.get_running_loop().call_later(0.05, self.stopping.set)
            return {"stopping": True}
        raise RequestError("unknown_method", f"Unknown method {method!r}. Run zeus-code --help for supported commands.")

    @staticmethod
    def _settings(value: Any) -> dict:
        if not isinstance(value, dict) or len(json.dumps(value)) > 4096:
            raise RequestError("invalid_request", "settings must be a JSON object smaller than 4 KiB.")
        return value

    async def _check_providers(self) -> dict:
        if self._provider_cache and time.monotonic() - self._provider_cache[0] < 30:
            return self._provider_cache[1]

        async def check(factory: Callable) -> dict:
            try:
                return await asyncio.wait_for(factory().check(), timeout=20)
            except asyncio.TimeoutError:
                return {"available": False, "detail": "Provider discovery timed out. Check the provider CLI and its authentication on this machine."}
            except Exception as exc:
                return {"available": False, "detail": f"Provider setup check failed: {str(exc)[:500]}"}

        factories = self._provider_factories()
        results = await asyncio.gather(*(check(factory) for factory in factories.values()))
        result = dict(zip(factories, results))
        self._provider_cache = (time.monotonic(), result)
        return result

    async def _execute(self, thread: dict, run: dict, prompt: str) -> None:
        assert self.store is not None
        s = self.store
        run_id = run["id"]

        async def emit(kind: str, data: dict) -> None:
            if kind not in {"provider_session", "message_delta", "message", "tool", "status"} or not isinstance(data, dict):
                raise ProviderError("Provider returned an invalid event.")
            if kind == "provider_session":
                session_id = data.get("session_id")
                if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
                    raise ProviderError("Provider returned an invalid session identifier.")
                s.update_thread(thread["id"], session_id=session_id)
            # Bound each wire page's building blocks; preserve all text via chunks.
            text = data.get("text")
            if isinstance(text, str) and len(text) > 8192:
                for offset in range(0, len(text), 8192):
                    self._check_storage_budget()
                    chunk = {**data, "text": text[offset:offset + 8192]}
                    if offset:
                        chunk["continuation"] = True
                    s.append_event(thread["id"], run_id, kind, chunk)
            else:
                self._check_storage_budget()
                s.append_event(thread["id"], run_id, kind, data)
            await asyncio.sleep(0)

        async def approve(payload: dict) -> str:
            # Capture machine and cwd from the execution context, never from the client.
            payload = {**payload, "hostname": socket.gethostname(), "cwd": payload.get("cwd") or thread["cwd"]}
            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 32 * 1024:
                raise ProviderError("Provider approval exceeds the 32 KiB request limit. Review this action in the provider CLI; Zeus did not grant approval.")
            self._check_storage_budget()
            approval = s.create_approval(run_id, payload)
            if approval["state"] == "resolved":
                return approval["decision"]
            existing = self.approval_waiters.get(approval["id"])
            if existing is not None:
                return await asyncio.shield(existing)
            waiter = asyncio.get_running_loop().create_future()
            self.approval_waiters[approval["id"]] = waiter
            try:
                return await waiter
            finally:
                self.approval_waiters.pop(approval["id"], None)

        try:
            adapter = self._provider_factories()[thread["provider"]]()
            context = RunContext(thread["id"], run_id, thread["cwd"], thread.get("session_id"),
                                 thread.get("model"), thread.get("settings") or {}, emit, approve)
            await adapter.run(context, prompt)
            s.finish_run(run_id, "completed")
        except asyncio.CancelledError:
            s.finish_run(run_id, "cancelled")
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            s.finish_run(run_id, "failed", error=message[:4096])
        finally:
            self.tasks.pop(run_id, None)
            for approval in s.approvals():
                if approval["run_id"] == run_id:
                    waiter = self.approval_waiters.pop(approval["id"], None)
                    if waiter is not None and not waiter.done():
                        waiter.cancel()
            with contextlib.suppress(Exception):
                repo = await inspect_repository(thread["cwd"])
                s.update_thread(thread["id"], branch=repo["branch"])

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
            except asyncio.TimeoutError:
                pass
        for writer in tuple(self.clients):
            writer.close()
        client_tasks = list(self.client_tasks)
        for task in client_tasks:
            task.cancel()
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        if self.store is not None:
            self.store.close()
            self.store = None
        if self._lock_fd is not None:
            sock = socket_path(self.data_dir)
            with contextlib.suppress(FileNotFoundError):
                if sock.is_socket():
                    sock.unlink()
            with contextlib.suppress(FileNotFoundError):
                (self.data_dir / "server.json").unlink()
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None


async def serve(data_dir: Path | None = None) -> int:
    daemon = Daemon(data_dir or default_data_dir())
    if not await daemon.start():
        print(f"Zeus Code is already serving from {daemon.data_dir}. Use zeus-code status.", flush=True)
        return 0
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon.stopping.set)
    print(f"Zeus Code {__version__} ready on {socket.gethostname()}\nState: {daemon.data_dir}\n"
          "Connect locally: zeus-code\nConnect remotely: zeus-code connect <ssh-host-alias>", flush=True)
    try:
        await daemon.stopping.wait()
    finally:
        await daemon.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.remove_signal_handler(sig)
    return 0
