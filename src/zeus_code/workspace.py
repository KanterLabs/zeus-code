"""Client-side workspace state and durable cache for the Zeus Code TUI.

The daemon remains authoritative for projects, threads, runs and approvals.  This
module deliberately keeps enough last-known state locally to make navigation
instant and useful while a machine is unavailable.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from .client import RPCError
from .paths import client_state_path


CACHE_VERSION = 1
MAX_EVENTS_PER_THREAD = 500
EVENT_PAGE_SIZE = 200


def _initial_state() -> dict[str, Any]:
    return {
        "version": CACHE_VERSION,
        "machines": {
            "local": {
                "id": "local",
                "alias": "local",
                "host": None,
                "connection": "disconnected",
                "stale": True,
                "snapshot": {"projects": [], "threads": [], "approvals": [], "last_seq": 0},
                "providers": {},
                "events": {},
                "cursor": 0,
                "server_id": None,
                "last_error": None,
            }
        },
        "selected_machine": "local",
        "selected_project": None,
        "selected_thread": None,
        "thread_views": {},
        "uncertain_sends": {},
        "settings": {"enter_sends": True},
    }


def _view_key(machine_id: str, thread_id: str) -> str:
    return f"{machine_id}:{thread_id}"


class CacheStore:
    """Small private JSON store written atomically and with a debounce."""

    def __init__(self, path: Path | None = None, debounce: float = 0.4) -> None:
        self.path = (path or client_state_path()).expanduser()
        self.debounce = debounce
        self._dirty = False
        self._last_save = 0.0

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _initial_state()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Client cache {self.path} is not valid JSON and was preserved. "
                "Move it aside to start with a fresh cache."
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Cannot read client cache {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"Client cache {self.path} has an invalid top-level value and was preserved.")
        version = raw.get("version")
        if version != CACHE_VERSION:
            raise RuntimeError(
                f"Client cache {self.path} uses schema version {version!r}; this Zeus Code supports {CACHE_VERSION}. "
                "Upgrade Zeus Code or move the cache aside."
            )
        state = _initial_state()
        state.update(raw)
        if not isinstance(state.get("machines"), dict):
            state["machines"] = {}
        # Local is an invariant, including after hand-edited or older cache files.
        local = _initial_state()["machines"]["local"]
        state["machines"].setdefault("local", local)
        for machine in state["machines"].values():
            machine["connection"] = "disconnected"
            machine["stale"] = True
            machine.setdefault("events", {})
            machine.setdefault("snapshot", {"projects": [], "threads": [], "approvals": []})
            machine.setdefault("cursor", 0)
            machine.setdefault("server_id", machine.get("snapshot", {}).get("server_id"))
        if state.get("selected_machine") not in state["machines"]:
            state["selected_machine"] = "local"
        state.setdefault("thread_views", {})
        state.setdefault("uncertain_sends", {})
        state.setdefault("settings", {"enter_sends": True})
        return state

    def mark_dirty(self) -> None:
        self._dirty = True

    def save_if_due(self, state: Mapping[str, Any], *, force: bool = False) -> bool:
        if not self._dirty:
            return False
        now = time.monotonic()
        if not force and now - self._last_save < self.debounce:
            return False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".client-", suffix=".json", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        self._dirty = False
        self._last_save = now
        return True


class Workspace:
    """Cached multi-machine model with independent asynchronous polling."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        cache_path: Path | None = None,
        rpc_factory: Callable[..., Any] | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.data_dir = data_dir
        self.cache = CacheStore(cache_path)
        self.state = self.cache.load()
        self.poll_interval = poll_interval
        self.rpc_factory = rpc_factory or self._default_rpc_factory
        self.clients: dict[str, Any] = {}
        self.connect_locks: dict[str, asyncio.Lock] = {}
        self.poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self._wake = asyncio.Event()

    @staticmethod
    def _default_rpc_factory(*, data_dir: Path | None, host: str | None) -> Any:
        from .client import RPCClient

        return RPCClient(data_dir=data_dir, host=host, remote_command="zeus-code")

    @property
    def machines(self) -> dict[str, dict[str, Any]]:
        return self.state["machines"]

    @property
    def selected_machine_id(self) -> str:
        return self.state["selected_machine"]

    @property
    def selected_machine(self) -> dict[str, Any]:
        return self.machines[self.selected_machine_id]

    @property
    def selected_project(self) -> dict[str, Any] | None:
        project_id = self.state.get("selected_project")
        return next((p for p in self.projects() if p.get("id") == project_id), None)

    @property
    def selected_thread(self) -> dict[str, Any] | None:
        thread_id = self.state.get("selected_thread")
        return next((t for t in self.threads() if t.get("id") == thread_id), None)

    def add_machine(self, alias: str, host: str) -> dict[str, Any]:
        alias, host = alias.strip(), host.strip()
        if not alias or not host:
            raise ValueError("Machine alias and SSH host are required")
        if alias == "local" or any(m.get("alias") == alias for m in self.machines.values()):
            raise ValueError(f"Machine alias already exists: {alias}")
        machine_id = uuid.uuid4().hex
        machine = {
            "id": machine_id,
            "alias": alias,
            "host": host,
            "connection": "disconnected",
            "stale": True,
            "snapshot": {"projects": [], "threads": [], "approvals": [], "last_seq": 0},
            "providers": {},
            "events": {},
            "cursor": 0,
            "server_id": None,
            "last_error": None,
        }
        self.machines[machine_id] = machine
        self._changed(force=True)
        self._wake.set()
        return machine

    def ensure_host(self, host: str) -> str:
        for machine_id, machine in self.machines.items():
            if machine.get("host") == host:
                return machine_id
        return self.add_machine(host, host)["id"]

    def projects(self, machine_id: str | None = None) -> list[dict[str, Any]]:
        machine = self.machines[machine_id or self.selected_machine_id]
        return list(machine.get("snapshot", {}).get("projects", []))

    def threads(self, machine_id: str | None = None, *, include_archived: bool = False) -> list[dict[str, Any]]:
        machine = self.machines[machine_id or self.selected_machine_id]
        threads = list(machine.get("snapshot", {}).get("threads", []))
        return threads if include_archived else [t for t in threads if not t.get("archived")]

    def approvals(self, machine_id: str | None = None) -> list[dict[str, Any]]:
        machine = self.machines[machine_id or self.selected_machine_id]
        return [
            a for a in machine.get("snapshot", {}).get("approvals", [])
            if a.get("state", "pending") == "pending" and not a.get("decision")
        ]

    def pending_approval_count(self) -> int:
        return sum(len(self.approvals(machine_id)) for machine_id in self.machines)

    def thread_events(self, thread_id: str | None = None, machine_id: str | None = None) -> list[dict[str, Any]]:
        machine = self.machines[machine_id or self.selected_machine_id]
        thread_id = thread_id or self.state.get("selected_thread")
        return list(machine.get("events", {}).get(thread_id, [])) if thread_id else []

    def thread_view(self, thread_id: str | None = None, machine_id: str | None = None) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or self.state.get("selected_thread")
        if not thread_id:
            return {"draft": "", "scroll": 0}
        view = self.state["thread_views"].setdefault(
            _view_key(machine_id, thread_id), {"draft": "", "scroll": 0, "anchor_seq": None}
        )
        view.setdefault("draft", "")
        view.setdefault("scroll", 0)
        view.setdefault("anchor_seq", None)
        view.setdefault("history_events", None)
        return view

    def set_draft(self, draft: str) -> None:
        if self.selected_thread:
            self.thread_view()["draft"] = draft
            self._changed()

    def set_scroll(self, scroll: int) -> None:
        if self.selected_thread:
            view = self.thread_view()
            view["scroll"] = max(0, int(scroll))
            if view["scroll"] == 0:
                view["anchor_seq"] = None
                view["history_events"] = None
            self._changed()

    def anchor_scroll(self) -> None:
        """Pin the current event high-water while the reader scrolls upward."""
        view = self.thread_view()
        if view.get("anchor_seq") is None:
            events = self.thread_events()
            view["anchor_seq"] = int(events[-1].get("seq", 0)) if events else 0
            view["history_events"] = deepcopy(events)
            self._changed()

    def view_events(self, thread_id: str | None = None, machine_id: str | None = None) -> list[dict[str, Any]]:
        """Return the frozen reader window while scrolled, otherwise the live tail."""
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or self.state.get("selected_thread")
        if not thread_id:
            return []
        view = self.thread_view(thread_id, machine_id)
        frozen = view.get("history_events")
        if int(view.get("scroll", 0)) > 0 and isinstance(frozen, list):
            return list(frozen)
        return self.thread_events(thread_id, machine_id)

    def switch(self, machine_id: str, project_id: str | None = None, thread_id: str | None = None) -> None:
        if machine_id not in self.machines:
            raise KeyError(machine_id)
        # The current draft and scroll already live in their keyed view.  Flush
        # before changing selection so a crash cannot lose a just-typed draft.
        self.cache.mark_dirty()
        self.cache.save_if_due(self.state, force=True)
        self.state["selected_machine"] = machine_id
        self.state["selected_project"] = project_id
        self.state["selected_thread"] = thread_id
        self._changed(force=True)

    def search_threads(self, query: str = "") -> list[dict[str, Any]]:
        words = query.casefold().split()
        results: list[dict[str, Any]] = []
        for machine_id, machine in self.machines.items():
            projects = {p.get("id"): p for p in self.projects(machine_id)}
            for thread in self.threads(machine_id):
                project = projects.get(thread.get("project_id"), {})
                haystack = " ".join(
                    str(value) for value in (
                        thread.get("title", ""), project.get("name", ""), machine.get("alias", ""),
                        thread.get("provider", ""), thread.get("state", "")
                    )
                ).casefold()
                if all(word in haystack for word in words):
                    results.append({"machine_id": machine_id, "machine": machine, "project": project, "thread": thread})
        return sorted(results, key=lambda item: str(item["thread"].get("updated_at", "")), reverse=True)

    def _changed(self, *, force: bool = False) -> None:
        self.cache.mark_dirty()
        self.cache.save_if_due(self.state, force=force)

    async def _make_client(self, machine_id: str) -> Any:
        machine = self.machines[machine_id]
        kwargs = {"data_dir": self.data_dir if machine.get("host") is None else None, "host": machine.get("host")}
        client = self.rpc_factory(**kwargs)
        if inspect.isawaitable(client):
            client = await client
        try:
            await client.connect()
        except BaseException:
            try:
                await client.close()
            except Exception:
                pass
            raise
        self.clients[machine_id] = client
        return client

    async def _client(self, machine_id: str) -> Any:
        existing = self.clients.get(machine_id)
        if existing is not None:
            return existing
        lock = self.connect_locks.setdefault(machine_id, asyncio.Lock())
        async with lock:
            existing = self.clients.get(machine_id)
            if existing is not None:
                return existing
            return await self._make_client(machine_id)

    async def sync_machine(self, machine_id: str) -> None:
        """Refresh snapshot and drain all event pages without skipping sequences."""
        machine = self.machines[machine_id]
        previous = deepcopy(machine)
        if machine_id not in self.clients:
            cached = machine.get("snapshot", {})
            has_cache = bool(machine.get("server_id") or cached.get("projects") or cached.get("threads"))
            machine["connection"] = "reconnecting" if has_cache else "disconnected"
        try:
            client = await self._client(machine_id)
            snapshot = await client.call("snapshot")
            providers = await client.call("providers")
            server_id = snapshot.get("server_id")
            if server_id is not None and server_id != machine.get("server_id") and (
                machine.get("server_id") is not None or int(machine.get("cursor", 0)) > 0
            ):
                # This host now represents a new durable event sequence. Keep
                # user-owned views/drafts, but never carry a cursor across it.
                machine["cursor"] = 0
                machine["events"] = {}
                for key, view in self.state.get("thread_views", {}).items():
                    if key.startswith(machine_id + ":"):
                        view["scroll"] = 0
                        view["anchor_seq"] = None
                        view["history_events"] = None
            machine["server_id"] = server_id
            # snapshot.last_seq is informational.  The events result reports
            # the last item in that page, and a page can be short due to its byte
            # budget. Keep displaying the old coherent snapshot until catch-up.
            await self._drain_events(machine_id, client)
            machine["snapshot"] = snapshot
            machine["providers"] = providers
            # Events committed after the snapshot may already have arrived.
            # Apply only those later state transitions over the snapshot.
            snapshot_seq = int(snapshot.get("last_seq", 0))
            for bucket in machine.get("events", {}).values():
                for event in bucket:
                    if int(event.get("seq", 0)) > snapshot_seq:
                        self._apply_thread_state(machine, event)
            machine["connection"] = "connected"
            machine["stale"] = False
            machine["last_error"] = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            machine["connection"] = "disconnected"
            machine["stale"] = True
            machine["last_error"] = str(exc)
            await self._drop_client(machine_id)
        if machine != previous:
            self._changed()

    async def _drain_events(self, machine_id: str, client: Any) -> None:
        machine = self.machines[machine_id]
        cursor = int(machine.get("cursor", 0))
        while True:
            previous_cursor = cursor
            page = await client.call("events", {"after": cursor, "limit": EVENT_PAGE_SIZE})
            events = list(page.get("events", []))
            advanced = cursor
            for event in events:
                seq = int(event.get("seq", 0))
                if seq <= cursor:
                    continue
                self._apply_event(machine, event)
                advanced = max(advanced, seq)
            cursor = advanced
            machine["cursor"] = cursor
            # The daemon may return a short page to respect its byte budget, so
            # only an empty page proves catch-up is complete.
            if not events:
                break
            # A malformed/non-advancing page must not spin forever.
            if cursor == previous_cursor:
                break

    @staticmethod
    def _apply_event(machine: dict[str, Any], event: dict[str, Any]) -> None:
        thread_id = event.get("thread_id")
        if not thread_id:
            return
        bucket = machine.setdefault("events", {}).setdefault(thread_id, [])
        seq = int(event.get("seq", 0))
        if any(int(existing.get("seq", -1)) == seq for existing in bucket):
            return
        bucket.append(deepcopy(event))
        bucket.sort(key=lambda item: int(item.get("seq", 0)))
        if len(bucket) > MAX_EVENTS_PER_THREAD:
            del bucket[:-MAX_EVENTS_PER_THREAD]
        Workspace._apply_thread_state(machine, event)

    @staticmethod
    def _apply_thread_state(machine: dict[str, Any], event: dict[str, Any]) -> None:
        thread_id = event.get("thread_id")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        state = data.get("state")
        if state:
            for thread in machine.get("snapshot", {}).get("threads", []):
                if thread.get("id") == thread_id:
                    thread["state"] = state
                    break

    async def poll_forever(self, machine_id: str) -> None:
        while not self._stopping and machine_id in self.machines:
            await self.sync_machine(machine_id)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                self._wake.clear()
            except TimeoutError:
                pass

    def start_polling(self) -> None:
        self._stopping = False
        for machine_id in self.machines:
            task = self.poll_tasks.get(machine_id)
            if task is None or task.done():
                self.poll_tasks[machine_id] = asyncio.create_task(self.poll_forever(machine_id))

    async def _drop_client(self, machine_id: str) -> None:
        client = self.clients.pop(machine_id, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    async def close(self) -> None:
        self._stopping = True
        for task in self.poll_tasks.values():
            task.cancel()
        if self.poll_tasks:
            await asyncio.gather(*self.poll_tasks.values(), return_exceptions=True)
        await asyncio.gather(*(self._drop_client(machine_id) for machine_id in list(self.clients)), return_exceptions=True)
        self.cache.mark_dirty()
        self.cache.save_if_due(self.state, force=True)

    async def rpc(self, method: str, params: dict[str, Any] | None = None, *, machine_id: str | None = None) -> Any:
        machine_id = machine_id or self.selected_machine_id
        client = await self._client(machine_id)
        return await client.call(method, params)

    async def add_project(self, path: str, name: str | None = None, *, machine_id: str | None = None) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        result = await self.rpc("add_project", {"path": path, "name": name}, machine_id=machine_id)
        await self.sync_machine(machine_id)
        return result

    async def create_thread(
        self, project_id: str, title: str, provider: str, *, model: str | None = None,
        settings: dict[str, Any] | None = None, worktree: bool = False, machine_id: str | None = None,
    ) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        params: dict[str, Any] = {
            "project_id": project_id, "title": title, "provider": provider, "worktree": worktree,
        }
        if model:
            params["model"] = model
        if settings:
            params["settings"] = settings
        result = await self.rpc("create_thread", params, machine_id=machine_id)
        await self.sync_machine(machine_id)
        if self.selected_machine_id == machine_id and self.state.get("selected_project") == project_id:
            self.switch(machine_id, project_id, result["id"])
        return result

    async def update_thread(self, thread_id: str, *, machine_id: str | None = None, **changes: Any) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        allowed = {"title", "archived", "draft", "scroll", "model", "settings"}
        params = {key: value for key, value in changes.items() if key in allowed}
        params["thread_id"] = thread_id
        result = await self.rpc("update_thread", params, machine_id=machine_id)
        await self.sync_machine(machine_id)
        return result

    async def push_selected_view(self) -> None:
        thread = self.selected_thread
        if thread is None:
            return
        view = self.thread_view()
        try:
            await self.update_thread(thread["id"], draft=view["draft"], scroll=view["scroll"])
        except Exception:
            # The local cache is deliberately sufficient while disconnected.
            pass

    async def send_prompt(
        self, prompt: str | None = None, *, request_id: str | None = None,
        machine_id: str | None = None, thread_id: str | None = None,
    ) -> Any:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or (self.selected_thread or {}).get("id")
        thread = next((item for item in self.threads(machine_id, include_archived=True) if item.get("id") == thread_id), None)
        if thread is None:
            raise ValueError("Select a thread before sending")
        view = self.thread_view(str(thread_id), machine_id)
        prompt = view["draft"] if prompt is None else prompt
        if not prompt.strip():
            raise ValueError("Prompt is empty")
        request_id = request_id or uuid.uuid4().hex
        key = _view_key(machine_id, thread["id"])
        record = {"machine_id": machine_id, "thread_id": thread["id"], "prompt": prompt, "request_id": request_id}
        try:
            result = await self.rpc(
                "send", {"thread_id": thread["id"], "prompt": prompt, "request_id": request_id},
                machine_id=machine_id,
            )
        except RPCError:
            # A structured daemon response is definitive; it is safe to edit
            # the prompt and create a new request rather than offering retry.
            raise
        except Exception:
            # Transport exceptions are uncertain; retry is always explicit and
            # reuses this id so the daemon can resolve it idempotently.
            self.state["uncertain_sends"][key] = record
            self._changed(force=True)
            raise
        self.state["uncertain_sends"].pop(key, None)
        # Do not erase a follow-up typed while this RPC was in flight.
        if view.get("draft") == prompt:
            view["draft"] = ""
        self._changed(force=True)
        self._wake.set()
        return result

    async def retry_uncertain(self, thread_id: str | None = None, *, machine_id: str | None = None) -> Any:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or (self.selected_thread or {}).get("id")
        key = _view_key(machine_id, thread_id) if thread_id else ""
        record = self.state["uncertain_sends"].get(key)
        if not record:
            raise ValueError("There is no uncertain send to retry")
        return await self.send_prompt(
            record["prompt"], request_id=record["request_id"],
            machine_id=record["machine_id"], thread_id=record["thread_id"],
        )

    async def cancel_thread(self, thread_id: str, *, machine_id: str | None = None) -> Any:
        machine_id = machine_id or self.selected_machine_id
        if not any(item.get("id") == thread_id for item in self.threads(machine_id, include_archived=True)):
            raise ValueError("Select a thread before cancelling")
        result = await self.rpc("cancel", {"thread_id": thread_id}, machine_id=machine_id)
        self._wake.set()
        return result

    async def cancel_selected(self) -> Any:
        thread = self.selected_thread
        if thread is None:
            raise ValueError("Select a thread before cancelling")
        return await self.cancel_thread(str(thread["id"]), machine_id=self.selected_machine_id)

    async def decide_approval(self, request_id: str, decision: str, *, machine_id: str | None = None) -> Any:
        if decision not in {"allow", "reject"}:
            raise ValueError("decision must be 'allow' or 'reject'")
        result = await self.rpc("approve", {"request_id": request_id, "decision": decision}, machine_id=machine_id)
        self._wake.set()
        return result

    async def get_diff(
        self, path: str | None = None, *, machine_id: str | None = None, thread_id: str | None = None,
    ) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or (self.selected_thread or {}).get("id")
        if thread_id is None:
            raise ValueError("Select a thread before reviewing changes")
        params: dict[str, Any] = {"thread_id": thread_id}
        if path:
            params["path"] = path
        return await self.rpc("diff", params, machine_id=machine_id)

    async def load_older(
        self, before: int | None = None, limit: int = 100, *,
        machine_id: str | None = None, thread_id: str | None = None,
    ) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or (self.selected_thread or {}).get("id")
        if thread_id is None:
            raise ValueError("Select a thread before loading history")
        params: dict[str, Any] = {"thread_id": thread_id, "limit": limit}
        if before is not None:
            params["before"] = before
        result = await self.rpc("history", params, machine_id=machine_id)
        machine = self.machines[machine_id]
        view = self.thread_view(thread_id, machine_id)
        if not isinstance(view.get("history_events"), list):
            view["history_events"] = deepcopy(machine.setdefault("events", {}).setdefault(thread_id, []))
        bucket = view["history_events"]
        merged = {int(event.get("seq", 0)): event for event in result.get("events", [])}
        merged.update({int(event.get("seq", 0)): event for event in bucket})
        bucket[:] = [merged[seq] for seq in sorted(merged)]
        if len(bucket) > MAX_EVENTS_PER_THREAD:
            # An explicit older-page request means the reader is looking back;
            # retain that window. Fresh events remain durable on the daemon.
            del bucket[MAX_EVENTS_PER_THREAD:]
        self._changed()
        return result


__all__ = ["CACHE_VERSION", "CacheStore", "MAX_EVENTS_PER_THREAD", "Workspace"]
