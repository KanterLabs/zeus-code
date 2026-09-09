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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import RPCError
from .paths import client_state_path


CACHE_VERSION = 1
MAX_EVENTS_PER_THREAD = 500
EVENT_PAGE_SIZE = 200
RUN_STATES = frozenset({"running", "awaiting_approval", "completed", "failed", "cancelled"})
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})


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
                "runs": {},
                "seen_threads": {},
                "cursor": 0,
                "server_id": None,
                "last_error": None,
                "last_connected_at": None,
            }
        },
        "selected_machine": "local",
        "selected_project": None,
        "selected_thread": None,
        "thread_views": {},
        "draft_revisions": {},
        "project_preferences": {},
        "thread_preferences": {},
        "uncertain_sends": {},
        "send_recovery_evidence": {},
        "settings": {"enter_sends": True},
    }


def _view_key(machine_id: str, thread_id: str) -> str:
    return f"{machine_id}:{thread_id}"


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _cached_server_id(machine: Mapping[str, Any]) -> str | None:
    value = machine.get("server_id")
    snapshot = machine.get("snapshot")
    if value is None and isinstance(snapshot, dict):
        value = snapshot.get("server_id")
    return value if isinstance(value, str) and value else None


def _is_attention_event(event: Mapping[str, Any]) -> bool:
    """Return whether a durable event represents user-visible new work."""
    kind = event.get("kind")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if kind in {"message", "message_delta"}:
        return data.get("role", "assistant") in {"assistant", "agent"}
    if kind == "run_state":
        return data.get("state") in TERMINAL_RUN_STATES
    if kind == "approval":
        return data.get("approval_state", data.get("state", "pending")) == "pending"
    return False


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
            machine.setdefault("runs", {})
            if not isinstance(machine.get("seen_threads"), dict):
                machine["seen_threads"] = {}
            machine.setdefault("snapshot", {"projects": [], "threads": [], "approvals": []})
            machine.setdefault("cursor", 0)
            machine.setdefault("server_id", machine.get("snapshot", {}).get("server_id"))
            machine.setdefault("last_connected_at", None)
            Workspace._derive_run_cache(machine)
        if state.get("selected_machine") not in state["machines"]:
            state["selected_machine"] = "local"
            state["selected_project"] = None
            state["selected_thread"] = None
        state.setdefault("thread_views", {})
        state.setdefault("uncertain_sends", {})
        for field in (
            "thread_views",
            "draft_revisions",
            "project_preferences",
            "thread_preferences",
            "uncertain_sends",
            "send_recovery_evidence",
        ):
            if not isinstance(state.get(field), dict):
                state[field] = {}
        if not isinstance(state.get("settings"), dict):
            state["settings"] = {"enter_sends": True}
        else:
            state["settings"].setdefault("enter_sends", True)
        # Version-one uncertain-send records predate explicit server scoping.
        # Bind them to the durable cached identity before any reconnect can
        # discover that the host now points at a replacement daemon.
        for field in ("uncertain_sends", "send_recovery_evidence"):
            for record in state[field].values():
                if not isinstance(record, dict) or record.get("server_id"):
                    continue
                machine = state["machines"].get(record.get("machine_id"))
                if isinstance(machine, dict):
                    server_id = _cached_server_id(machine)
                    if server_id is not None:
                        record["server_id"] = server_id
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
        self._repair_selection()
        self.poll_interval = poll_interval
        self.rpc_factory = rpc_factory or self._default_rpc_factory
        self.clients: dict[str, Any] = {}
        self._observed_server_ids: dict[str, str] = {}
        self._client_server_ids: dict[int, str] = {}
        self.connect_locks: dict[str, asyncio.Lock] = {}
        self.poll_tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self._wake = asyncio.Event()

    @staticmethod
    def _default_rpc_factory(*, data_dir: Path | None, host: str | None, remote_command: str = "zeus-code") -> Any:
        from .client import RPCClient

        return RPCClient(data_dir=data_dir, host=host, remote_command=remote_command)

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

    def _repair_selection(self, *, machine_id: str | None = None, authoritative: bool = False) -> bool:
        """Repair selection only when cached or fresh records prove it is stale.

        An offline cache can legitimately contain a remembered identifier before
        it has a snapshot. Empty legacy collections therefore do not erase the
        last selection during startup. A successful snapshot is authoritative,
        including when its project or thread collection is empty.
        """
        selected_machine = self.state.get("selected_machine")
        if selected_machine not in self.machines:
            selected_machine = "local"
            self.state["selected_machine"] = selected_machine
            self.state["selected_project"] = None
            self.state["selected_thread"] = None
        if machine_id is not None and selected_machine != machine_id:
            return False

        machine = self.machines[selected_machine]
        snapshot = machine.get("snapshot") if isinstance(machine.get("snapshot"), dict) else {}
        projects_value = snapshot.get("projects")
        threads_value = snapshot.get("threads")
        projects = [item for item in projects_value or [] if isinstance(item, dict)] if isinstance(projects_value, list) else []
        threads = [item for item in threads_value or [] if isinstance(item, dict)] if isinstance(threads_value, list) else []
        projects_known = authoritative or bool(projects)
        threads_known = authoritative or bool(threads)
        project_by_id = {item.get("id"): item for item in projects}
        thread_by_id = {item.get("id"): item for item in threads}

        previous = (self.state.get("selected_project"), self.state.get("selected_thread"))
        project_id, thread_id = previous
        selected_thread = thread_by_id.get(thread_id)
        if thread_id is not None and selected_thread is None and threads_known:
            thread_id = None
        elif selected_thread is not None and selected_thread.get("archived"):
            thread_id = None
        elif selected_thread is not None:
            parent_id = selected_thread.get("project_id")
            if parent_id is not None:
                if projects_known and parent_id not in project_by_id:
                    thread_id = None
                else:
                    project_id = parent_id
        if project_id is not None and project_id not in project_by_id and projects_known:
            project_id = None
            if selected_thread is not None:
                thread_id = None

        self.state["selected_project"] = project_id
        self.state["selected_thread"] = thread_id
        return previous != (project_id, thread_id)

    def _server_id(self, machine_id: str) -> str | None:
        return _cached_server_id(self.machines[machine_id])

    def _preference_record(
        self,
        field: str,
        machine_id: str,
        item_id: str,
        *,
        create: bool = False,
    ) -> dict[str, Any] | None:
        records = self.state[field]
        key = _view_key(machine_id, item_id)
        record = records.get(key)
        server_id = self._server_id(machine_id)
        if isinstance(record, dict):
            recorded_server = record.get("server_id")
            if not (
                isinstance(recorded_server, str)
                and server_id is not None
                and recorded_server != server_id
            ):
                return record
            if not create:
                return None
            # Retain extension fields while dropping preferences that belonged
            # to a different daemon previously reached through this machine.
            known = {"server_id", "pinned", "last_opened_at", "collapsed"}
            record = {name: value for name, value in record.items() if name not in known}
        elif not create:
            return None
        else:
            record = {}
        if server_id is not None:
            record["server_id"] = server_id
        records[key] = record
        return record

    @staticmethod
    def _record_time(record: Mapping[str, Any], field: str) -> float:
        value = record.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        parsed = _timestamp(value)
        return parsed if parsed is not None else float("-inf")

    def project_pinned(self, machine_id: str, project_id: str) -> bool:
        preference = self._preference_record("project_preferences", machine_id, project_id)
        return bool(preference and preference.get("pinned") is True)

    def thread_pinned(self, machine_id: str, thread_id: str) -> bool:
        preference = self._preference_record("thread_preferences", machine_id, thread_id)
        return bool(preference and preference.get("pinned") is True)

    def set_project_pinned(self, machine_id: str, project_id: str, pinned: bool = True) -> bool:
        if not any(item.get("id") == project_id for item in self.projects(machine_id)):
            raise KeyError(project_id)
        preference = self._preference_record("project_preferences", machine_id, project_id, create=True)
        assert preference is not None
        preference["pinned"] = bool(pinned)
        self._changed(force=True)
        return bool(pinned)

    def set_thread_pinned(self, machine_id: str, thread_id: str, pinned: bool = True) -> bool:
        if not any(item.get("id") == thread_id for item in self.threads(machine_id, include_archived=True)):
            raise KeyError(thread_id)
        preference = self._preference_record("thread_preferences", machine_id, thread_id, create=True)
        assert preference is not None
        preference["pinned"] = bool(pinned)
        self._changed(force=True)
        return bool(pinned)

    def project_collapse_preference(self, machine_id: str, project_id: str) -> bool | None:
        preference = self._preference_record("project_preferences", machine_id, project_id)
        if preference is None or not isinstance(preference.get("collapsed"), bool):
            return None
        return preference["collapsed"]

    def project_collapsed(self, machine_id: str, project_id: str, *, default: bool = False) -> bool:
        if self.selected_machine_id == machine_id and self.state.get("selected_project") == project_id:
            return False
        preference = self.project_collapse_preference(machine_id, project_id)
        return bool(default) if preference is None else preference

    def set_project_collapsed(self, machine_id: str, project_id: str, collapsed: bool = True) -> bool:
        if not any(item.get("id") == project_id for item in self.projects(machine_id)):
            raise KeyError(project_id)
        preference = self._preference_record("project_preferences", machine_id, project_id, create=True)
        assert preference is not None
        preference["collapsed"] = bool(collapsed)
        self._changed(force=True)
        return bool(collapsed)

    def toggle_project_collapsed(self, machine_id: str, project_id: str, *, default: bool = False) -> bool:
        current = self.project_collapse_preference(machine_id, project_id)
        collapsed = not (bool(default) if current is None else current)
        return self.set_project_collapsed(machine_id, project_id, collapsed)

    def ordered_projects(self, machine_id: str | None = None) -> list[dict[str, Any]]:
        machine_id = machine_id or self.selected_machine_id

        def sort_key(project: Mapping[str, Any]) -> tuple[Any, ...]:
            project_id = str(project.get("id") or "")
            preference = self._preference_record("project_preferences", machine_id, project_id) or {}
            return (
                not self.project_pinned(machine_id, project_id),
                -self._record_time(preference, "last_opened_at"),
                -self._record_time(project, "updated_at"),
                str(project.get("name") or "").casefold(),
                project_id,
            )

        return sorted(self.projects(machine_id), key=sort_key)

    def ordered_threads(
        self,
        machine_id: str | None = None,
        project_id: str | None = None,
        *,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        machine_id = machine_id or self.selected_machine_id
        records = self.threads(machine_id, include_archived=include_archived)
        if project_id is not None:
            records = [item for item in records if item.get("project_id") == project_id]

        def sort_key(thread: Mapping[str, Any]) -> tuple[Any, ...]:
            thread_id = str(thread.get("id") or "")
            preference = self._preference_record("thread_preferences", machine_id, thread_id) or {}
            return (
                not self.thread_pinned(machine_id, thread_id),
                -self._record_time(preference, "last_opened_at"),
                -self._record_time(thread, "updated_at"),
                str(thread.get("title") or "").casefold(),
                thread_id,
            )

        return sorted(records, key=sort_key)

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
            "runs": {},
            "seen_threads": {},
            "cursor": 0,
            "server_id": None,
            "last_error": None,
            "last_connected_at": None,
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

    async def connect_remote(self, host: str, projects_root: str = "~/projects", *,
                             alias: str = "", on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Explicit onboarding: provision a server, import repositories and save it."""
        from .client import _validate_host
        from .remote import provision_remote

        host = _validate_host(host.strip())
        alias = alias.strip() or host
        existing = next((m for m in self.machines.values() if m.get("host") == host), None)
        if alias == "local" or any(m.get("alias") == alias and m is not existing for m in self.machines.values()):
            raise ValueError(f"Machine name already exists: {alias}")
        if not projects_root.strip():
            raise ValueError("Enter a projects folder on the remote server, such as ~/projects")
        progress = on_progress or (lambda _: None)
        installed = await provision_remote(host, on_progress=progress, include_health=True)
        client = self.rpc_factory(data_dir=None, host=host, remote_command=installed["remote_command"])
        if inspect.isawaitable(client):
            client = await client
        warnings: list[str] = []
        imported = 0
        try:
            await client.connect()
            progress("Finding repositories on the server…")
            snapshot = await client.call("snapshot")
            try:
                found = await client.call("discover_projects", {"root": projects_root.strip()})
            except RPCError as exc:
                if exc.code in {-32601, "unknown_method"}:
                    raise RuntimeError("The running server is older. Finish its active work, stop it using its original installation on the server, then connect again.") from exc
                raise
            known = {p["path"] for p in snapshot.get("projects", [])}
            for index, project in enumerate(found["projects"]):
                progress(f"Importing repositories {index + 1}/{len(found['projects'])}…")
                if project["path"] in known:
                    continue
                try:
                    await client.call("add_project", {"path": project["path"], "name": project["name"]})
                    imported += 1
                except RPCError as exc:
                    warnings.append(f"{project['name']}: {exc.message}")
        finally:
            await client.close()
        machine = existing or self.add_machine(alias, host)
        machine_id = machine["id"]
        polling = self.poll_tasks.pop(machine_id, None)
        if polling is not None:
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
        await self._drop_client(machine_id)
        machine.update(
            alias=alias,
            remote_command=installed["remote_command"],
            projects_root=found["root"],
            remote_health=deepcopy(installed.get("health")),
        )
        if isinstance(installed.get("providers"), dict):
            machine["providers"] = deepcopy(installed["providers"])
        self._changed(force=True)
        progress("Loading remote workspace…")
        await self.sync_machine(machine_id)
        if machine.get("connection") != "connected":
            raise ConnectionError(machine.get("last_error") or "Server did not connect")
        self.switch(machine_id)
        self._changed(force=True)
        return {
            "machine": machine,
            "imported": imported,
            "projects": len(self.projects(machine_id)),
            "warnings": warnings,
            "truncated": found.get("truncated", False),
            "health": deepcopy(machine.get("remote_health")),
            "providers": deepcopy(machine.get("providers", {})),
        }

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

    def unread_count(self, machine_id: str, thread_id: str) -> int:
        """Return distinct unread results newer than the server-scoped marker."""
        machine = self.machines[machine_id]
        server_id = machine.get("server_id")
        marker = machine.get("seen_threads", {}).get(thread_id)
        seen_seq = 0
        if isinstance(marker, dict) and marker.get("server_id") == server_id:
            try:
                seen_seq = max(0, int(marker.get("seq", 0)))
            except (TypeError, ValueError):
                seen_seq = 0
        results: set[tuple[str, str | int]] = set()
        for event in machine.get("events", {}).get(thread_id, []):
            try:
                seq = int(event.get("seq", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if seq <= seen_seq or not _is_attention_event(event):
                continue
            run_id = event.get("run_id")
            if isinstance(run_id, str) and run_id:
                results.add(("run", run_id))
            else:
                results.add(("seq", seq))
        return len(results)

    def mark_thread_seen(self, machine_id: str, thread_id: str, *, visible: bool) -> bool:
        """Persist the live tail after the selected conversation was drawn.

        Selection changes and synchronization deliberately never call this.
        The caller supplies whether the conversation is actually visible (for
        example, no overlay covers it), while the workspace independently
        verifies the current selection and live-tail scroll position.
        """
        if not visible:
            return False
        if self.selected_machine_id != machine_id or self.state.get("selected_thread") != thread_id:
            return False
        if int(self.thread_view(thread_id, machine_id).get("scroll", 0)) != 0:
            return False
        machine = self.machines[machine_id]
        server_id = machine.get("server_id")
        if not isinstance(server_id, str) or not server_id:
            return False
        tail = 0
        for event in machine.get("events", {}).get(thread_id, []):
            try:
                tail = max(tail, int(event.get("seq", 0)))
            except (AttributeError, TypeError, ValueError):
                continue
        if tail <= 0:
            return False
        markers = machine.setdefault("seen_threads", {})
        current = markers.get(thread_id)
        try:
            current_seq = int(current.get("seq", 0)) if isinstance(current, dict) else 0
        except (TypeError, ValueError):
            current_seq = 0
        if (
            isinstance(current, dict)
            and current.get("server_id") == server_id
            and current_seq >= tail
        ):
            return False
        markers[thread_id] = {"server_id": server_id, "seq": tail}
        # Advancing happens only after a successful draw and is infrequent
        # because identical frames are rejected above, so make it crash-durable.
        self._changed(force=True)
        return True

    def attention_items(self) -> list[dict[str, Any]]:
        """Return unread threads and unresolved approvals across all machines."""
        items: list[dict[str, Any]] = []
        for machine_id, machine in self.machines.items():
            projects = {project.get("id"): project for project in self.projects(machine_id)}
            approval_threads = {
                approval.get("thread_id")
                for approval in self.approvals(machine_id)
                if approval.get("thread_id")
            }
            for thread in self.threads(machine_id, include_archived=True):
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                unread = self.unread_count(machine_id, thread_id)
                needs_approval = thread_id in approval_threads
                if unread == 0 and not needs_approval:
                    continue
                project = projects.get(thread.get("project_id"), {})
                items.append(
                    {
                        "machine_id": machine_id,
                        "thread_id": thread_id,
                        "title": str(thread.get("title") or thread_id),
                        "project_name": str(project.get("name") or ""),
                        "machine_name": str(machine.get("alias") or machine_id),
                        "state": str(thread.get("state") or "idle"),
                        "unread_count": unread,
                        "needs_approval": needs_approval,
                        "stale": bool(machine.get("stale")),
                    }
                )
        state_priority = {"failed": 0, "completed": 1, "cancelled": 2}
        return sorted(
            items,
            key=lambda item: (
                not item["needs_approval"],
                state_priority.get(item["state"], 3),
                -item["unread_count"],
                item["machine_name"].casefold(),
                item["project_name"].casefold(),
                item["title"].casefold(),
            ),
        )

    def thread_events(self, thread_id: str | None = None, machine_id: str | None = None) -> list[dict[str, Any]]:
        machine = self.machines[machine_id or self.selected_machine_id]
        thread_id = thread_id or self.state.get("selected_thread")
        return list(machine.get("events", {}).get(thread_id, [])) if thread_id else []

    def thread_run(self, thread_id: str | None = None, machine_id: str | None = None) -> dict[str, Any]:
        """Return cached metadata for the current or latest run of a thread.

        An unknown run is represented by an empty dictionary.  Known records
        expose ``id``, ``state``, ``created_at``, ``updated_at``,
        ``last_event_at`` and ``ended_at``; timestamps remain ``None`` until a
        daemon acknowledgement or durable event supplies them.
        """
        machine = self.machines[machine_id or self.selected_machine_id]
        thread_id = thread_id or self.state.get("selected_thread")
        if not thread_id:
            return {}
        cached = machine.get("runs", {}).get(thread_id)
        if not isinstance(cached, dict):
            return {}
        public = {
            key: cached.get(key)
            for key in ("id", "state", "created_at", "updated_at", "last_event_at", "ended_at")
        }
        if cached.get("error") is not None:
            public["error"] = cached["error"]
        return deepcopy(public)

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
            self._set_cached_draft(
                self.selected_machine_id, str(self.selected_thread["id"]), draft
            )
            self._changed()

    def _draft_revision(self, machine_id: str, thread_id: str) -> int:
        value = self.state["draft_revisions"].get(_view_key(machine_id, thread_id), 0)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _set_cached_draft(self, machine_id: str, thread_id: str, draft: str) -> None:
        self.thread_view(thread_id, machine_id)["draft"] = draft
        key = _view_key(machine_id, thread_id)
        self.state["draft_revisions"][key] = self._draft_revision(machine_id, thread_id) + 1

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
        machine_projects = self.projects(machine_id)
        machine_threads = self.threads(machine_id, include_archived=True)
        selected_thread = next((item for item in machine_threads if item.get("id") == thread_id), None)
        if thread_id is not None and selected_thread is None and machine_threads:
            raise KeyError(thread_id)
        if selected_thread is not None:
            parent_id = selected_thread.get("project_id")
            if project_id is None:
                project_id = parent_id
            elif parent_id is not None and project_id != parent_id:
                raise ValueError(f"Thread {thread_id} does not belong to project {project_id}")
        if project_id is not None and machine_projects and not any(
            item.get("id") == project_id for item in machine_projects
        ):
            raise KeyError(project_id)
        # The current draft and scroll already live in their keyed view.  Flush
        # before changing selection so a crash cannot lose a just-typed draft.
        self.cache.mark_dirty()
        self.cache.save_if_due(self.state, force=True)
        self.state["selected_machine"] = machine_id
        self.state["selected_project"] = project_id
        self.state["selected_thread"] = thread_id
        opened_at = time.time()
        if project_id is not None:
            preference = self._preference_record(
                "project_preferences", machine_id, str(project_id), create=True
            )
            assert preference is not None
            preference["last_opened_at"] = opened_at
        if thread_id is not None:
            preference = self._preference_record(
                "thread_preferences", machine_id, str(thread_id), create=True
            )
            assert preference is not None
            preference["last_opened_at"] = opened_at
        self._changed(force=True)

    def search_threads(self, query: str = "", *, include_archived: bool = False) -> list[dict[str, Any]]:
        words = query.casefold().split()
        results: list[dict[str, Any]] = []
        for machine_id, machine in self.machines.items():
            projects = {p.get("id"): p for p in self.projects(machine_id)}
            for thread in self.threads(machine_id, include_archived=include_archived):
                project = projects.get(thread.get("project_id"), {})
                haystack = " ".join(
                    str(value) for value in (
                        thread.get("title", ""), project.get("name", ""), machine.get("alias", ""),
                        thread.get("provider", ""), thread.get("state", "")
                    )
                ).casefold()
                if all(word in haystack for word in words):
                    results.append({"machine_id": machine_id, "machine": machine, "project": project, "thread": thread})

        def sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
            machine_id = str(item["machine_id"])
            thread = item["thread"]
            thread_id = str(thread.get("id") or "")
            preference = self._preference_record("thread_preferences", machine_id, thread_id) or {}
            return (
                not self.thread_pinned(machine_id, thread_id),
                -self._record_time(preference, "last_opened_at"),
                -self._record_time(thread, "updated_at"),
                str(thread.get("title") or "").casefold(),
                thread_id,
            )

        return sorted(results, key=sort_key)

    def _changed(self, *, force: bool = False) -> None:
        self.cache.mark_dirty()
        self.cache.save_if_due(self.state, force=force)

    def _selection_context(self) -> tuple[str, Any, Any]:
        return (
            self.selected_machine_id,
            self.state.get("selected_project"),
            self.state.get("selected_thread"),
        )

    @staticmethod
    def _upsert_snapshot_record(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
        record_id = record["id"]
        for index, existing in enumerate(records):
            if existing.get("id") == record_id:
                records[index] = deepcopy(record)
                return
        records.append(deepcopy(record))

    async def _make_client(self, machine_id: str) -> Any:
        machine = self.machines[machine_id]
        kwargs = {"data_dir": self.data_dir if machine.get("host") is None else None, "host": machine.get("host")}
        if machine.get("remote_command"):
            kwargs["remote_command"] = machine["remote_command"]
        client = self.rpc_factory(**kwargs)
        if inspect.isawaitable(client):
            client = await client
        try:
            hello = await client.connect()
        except BaseException:
            try:
                await client.close()
            except Exception:
                pass
            raise
        if not isinstance(hello, Mapping):
            hello = getattr(client, "hello", None)
        if isinstance(hello, Mapping):
            observed_server_id = hello.get("server_id")
            if isinstance(observed_server_id, str) and observed_server_id:
                self._observed_server_ids[machine_id] = observed_server_id
                self._client_server_ids[id(client)] = observed_server_id
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
        previous_selection = self._selection_context()
        baseline_runs = deepcopy(machine.get("runs", {}))
        baseline_ids = {
            field: {
                record.get("id")
                for record in machine.get("snapshot", {}).get(field, [])
            }
            for field in ("projects", "threads")
        }
        if machine_id not in self.clients:
            cached = machine.get("snapshot", {})
            has_cache = bool(machine.get("server_id") or cached.get("projects") or cached.get("threads"))
            machine["connection"] = "reconnecting" if has_cache else "disconnected"
        try:
            client = await self._client(machine_id)
            snapshot = await client.call("snapshot")
            providers = await client.call("providers")
            server_id = snapshot.get("server_id")
            hello = getattr(client, "hello", None)
            hello_server_id = hello.get("server_id") if isinstance(hello, Mapping) else None
            if (
                isinstance(server_id, str)
                and server_id
                and isinstance(hello_server_id, str)
                and hello_server_id
                and server_id != hello_server_id
            ):
                raise ConnectionError("Daemon identity changed on the active connection")
            if isinstance(server_id, str) and server_id:
                # Test doubles and older compatible clients may not expose the
                # hello payload. A snapshot from this exact transport still
                # establishes its identity for later recovery checks.
                self._observed_server_ids[machine_id] = server_id
                self._client_server_ids[id(client)] = server_id
            if server_id is not None and server_id != machine.get("server_id") and (
                machine.get("server_id") is not None or int(machine.get("cursor", 0)) > 0
            ):
                # This host now represents a new durable event sequence. Keep
                # user-owned views/drafts, but never carry a cursor across it.
                machine["cursor"] = 0
                machine["events"] = {}
                machine["runs"] = {}
                for key, view in self.state.get("thread_views", {}).items():
                    if key.startswith(machine_id + ":"):
                        view["scroll"] = 0
                        view["anchor_seq"] = None
                        view["history_events"] = None
            machine["server_id"] = server_id
            try:
                snapshot_seq = max(0, int(snapshot.get("last_seq", 0)))
            except (TypeError, ValueError):
                snapshot_seq = 0
            # Catch up through the snapshot's finite high-water. A page can be
            # short due to its byte budget, so keep requesting until that
            # sequence is reached. Processing the whole final page also retains
            # events that arrived just after the snapshot without waiting for a
            # continuously busy daemon to return an empty page.
            await self._drain_events(machine_id, client, through_seq=snapshot_seq)
            # A successful mutation can finish after this snapshot was taken.
            # Preserve records added to the cache while catch-up was in flight;
            # the next poll will reconcile them with an authoritative snapshot.
            current_snapshot = machine.get("snapshot", {})
            for field, known_ids in baseline_ids.items():
                target = snapshot.setdefault(field, [])
                target_ids = {record.get("id") for record in target}
                for record in current_snapshot.get(field, []):
                    record_id = record.get("id")
                    if record_id not in known_ids and record_id not in target_ids:
                        target.append(deepcopy(record))
                        target_ids.add(record_id)
            machine["snapshot"] = snapshot
            machine["providers"] = providers
            # Events committed after the snapshot may already have arrived.
            # Apply only those later state transitions over the snapshot.
            for bucket in machine.get("events", {}).values():
                for event in bucket:
                    if int(event.get("seq", 0)) > snapshot_seq:
                        self._apply_thread_state(machine, event)
            # A send can be accepted while this older snapshot is in flight.
            # Its acknowledgement has no event sequence, so retain that newer
            # local fact until the next snapshot observes it.
            for thread_id, run in machine.get("runs", {}).items():
                if not isinstance(run, dict) or run == baseline_runs.get(thread_id):
                    continue
                if not run.get("_acknowledged") or run.get("state") not in RUN_STATES:
                    continue
                for thread in snapshot.get("threads", []):
                    if thread.get("id") == thread_id:
                        thread["state"] = run["state"]
                        break
            machine["connection"] = "connected"
            machine["stale"] = False
            machine["last_error"] = None
            machine["last_connected_at"] = time.time()
            self._repair_selection(machine_id=machine_id, authoritative=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            machine["connection"] = "disconnected"
            machine["stale"] = True
            machine["last_error"] = str(exc)
            await self._drop_client(machine_id)
        current_for_persistence = {key: value for key, value in machine.items() if key != "last_connected_at"}
        previous_for_persistence = {key: value for key, value in previous.items() if key != "last_connected_at"}
        if current_for_persistence != previous_for_persistence or self._selection_context() != previous_selection:
            self._changed()

    async def _drain_events(
        self, machine_id: str, client: Any, *, through_seq: int | None = None
    ) -> None:
        machine = self.machines[machine_id]
        cursor = int(machine.get("cursor", 0))
        if through_seq is not None and cursor >= through_seq:
            return
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
            if through_seq is not None and cursor >= through_seq:
                break
            # The daemon may return a short page to respect its byte budget, so
            # only an empty page proves catch-up is complete.
            if not events:
                if through_seq is not None and cursor < through_seq:
                    raise RuntimeError(
                        f"Event stream stopped at sequence {cursor} before snapshot {through_seq}"
                    )
                break
            # A malformed/non-advancing page must not spin forever.
            if cursor == previous_cursor:
                if through_seq is not None and cursor < through_seq:
                    raise RuntimeError(
                        f"Event stream did not advance past sequence {cursor} toward snapshot {through_seq}"
                    )
                break

    @staticmethod
    def _derive_run_cache(machine: dict[str, Any]) -> None:
        """Backfill run metadata when loading caches written before ``runs``."""
        runs = machine.setdefault("runs", {})
        for run in runs.values():
            if isinstance(run, dict):
                run.setdefault("_prompt", None)
                run.setdefault("_request_id", None)
        events = [
            event
            for bucket in machine.get("events", {}).values()
            if isinstance(bucket, list)
            for event in bucket
            if isinstance(event, dict)
        ]
        for event in sorted(events, key=lambda item: int(item.get("seq", 0))):
            thread_id = event.get("thread_id")
            run_id = event.get("run_id")
            current = runs.get(thread_id) if thread_id else None
            if run_id and (not isinstance(current, dict) or current.get("id") == run_id):
                Workspace._apply_run_event(machine, event)
            elif run_id and isinstance(current, dict):
                current_seq = int(current.get("_last_seq", 0))
                if int(event.get("seq", 0)) > current_seq and current.get("state") in TERMINAL_RUN_STATES:
                    Workspace._apply_run_event(machine, event)

    @staticmethod
    def _new_run_record(run_id: str) -> dict[str, Any]:
        return {
            "id": run_id,
            "state": None,
            "created_at": None,
            "updated_at": None,
            "last_event_at": None,
            "ended_at": None,
            "_last_seq": 0,
            "_acknowledged": False,
            "_prompt": None,
            "_request_id": None,
        }

    @staticmethod
    def _apply_run_prompt(current: dict[str, Any], event: Mapping[str, Any]) -> None:
        if event.get("kind") != "message":
            return
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        prompt = data.get("text")
        if data.get("role") == "user" and isinstance(prompt, str) and prompt:
            current["_prompt"] = prompt

    @staticmethod
    def _apply_run_event(machine: dict[str, Any], event: dict[str, Any]) -> None:
        thread_id = event.get("thread_id")
        run_id = event.get("run_id")
        if not thread_id or not run_id:
            return
        runs = machine.setdefault("runs", {})
        current = runs.get(thread_id)
        event_seq = int(event.get("seq", 0))
        timestamp = event.get("created_at") if isinstance(event.get("created_at"), str) else None

        if not isinstance(current, dict) or current.get("id") != run_id:
            if isinstance(current, dict):
                # An accepted active run is newer than any delayed cache page
                # for the preceding run. A different run can follow only after
                # the active run reaches a terminal state.
                if current.get("_acknowledged") and current.get("state") not in TERMINAL_RUN_STATES:
                    return
                if event_seq <= int(current.get("_last_seq", 0)):
                    return
            current = Workspace._new_run_record(str(run_id))
            runs[thread_id] = current
        elif event_seq <= int(current.get("_last_seq", 0)):
            # Cache loading replays retained events to backfill legacy records.
            # Metadata already reconciled through this sequence is newer.
            Workspace._apply_run_prompt(current, event)
            return

        previous_seq = int(current.get("_last_seq", 0))
        current["_last_seq"] = max(int(current.get("_last_seq", 0)), event_seq)
        Workspace._apply_run_prompt(current, event)
        if timestamp:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            is_initial_event = (
                event.get("kind") == "message" and data.get("role") == "user"
            ) or (
                event.get("kind") == "run_state" and data.get("state") == "running"
            )
            if current.get("created_at") is None and previous_seq == 0 and is_initial_event:
                current["created_at"] = timestamp
            if _timestamp(timestamp) is not None and (
                _timestamp(current.get("last_event_at")) is None
                or _timestamp(timestamp) >= _timestamp(current.get("last_event_at"))
            ):
                current["last_event_at"] = timestamp

        if event.get("kind") != "run_state":
            return
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        state = data.get("state")
        if state not in RUN_STATES:
            return
        current_state = current.get("state")
        if current_state in TERMINAL_RUN_STATES and state not in TERMINAL_RUN_STATES:
            return
        event_time = _timestamp(timestamp)
        updated_time = _timestamp(current.get("updated_at"))
        if event_time is not None and updated_time is not None and event_time < updated_time:
            return
        current["state"] = state
        if timestamp:
            current["updated_at"] = timestamp
            if state in TERMINAL_RUN_STATES:
                current["ended_at"] = timestamp
            elif current.get("ended_at") is not None:
                current["ended_at"] = None
        if data.get("error") is not None:
            current["error"] = data["error"]

    @staticmethod
    def _apply_run_ack(machine: dict[str, Any], thread_id: str, result: Mapping[str, Any]) -> None:
        run_id = result.get("id")
        if not run_id:
            return
        runs = machine.setdefault("runs", {})
        current = runs.get(thread_id)
        same_run = isinstance(current, dict) and current.get("id") == run_id
        if not same_run:
            if isinstance(current, dict):
                incoming_created = _timestamp(result.get("created_at"))
                current_created = _timestamp(current.get("created_at"))
                if (
                    incoming_created is not None
                    and current_created is not None
                    and incoming_created <= current_created
                ):
                    return
                if incoming_created is None and current.get("_acknowledged") and current.get("state") not in TERMINAL_RUN_STATES:
                    return
            current = Workspace._new_run_record(str(run_id))
            runs[thread_id] = current

        assert isinstance(current, dict)
        if isinstance(result.get("prompt"), str) and result.get("prompt"):
            current["_prompt"] = result["prompt"]
        if isinstance(result.get("request_id"), str) and result.get("request_id"):
            current["_request_id"] = result["request_id"]
        acknowledged_state = result.get("state")
        current_state = current.get("state")
        acknowledged_time = _timestamp(result.get("updated_at"))
        current_time = _timestamp(current.get("updated_at"))
        accept_state = False
        if acknowledged_state in RUN_STATES:
            if current_state not in RUN_STATES:
                accept_state = True
            elif current_state in TERMINAL_RUN_STATES:
                # A run never resumes after a terminal transition.
                accept_state = (
                    acknowledged_state in TERMINAL_RUN_STATES
                    and acknowledged_time is not None
                    and (current_time is None or acknowledged_time >= current_time)
                )
            elif acknowledged_time is not None and current_time is not None:
                accept_state = acknowledged_time >= current_time
            elif acknowledged_state in TERMINAL_RUN_STATES:
                # Terminality remains monotonic when one side lacks a usable
                # timestamp, as can happen with an older client cache.
                accept_state = True
            elif acknowledged_time is not None and current_time is None:
                accept_state = True
        if result.get("created_at") is not None:
            current["created_at"] = result["created_at"]
        if accept_state:
            current["state"] = acknowledged_state
            if result.get("updated_at") is not None:
                current["updated_at"] = result["updated_at"]
            if result.get("error") is not None:
                current["error"] = result["error"]
        current["_acknowledged"] = True
        if current.get("state") in TERMINAL_RUN_STATES and current.get("ended_at") is None:
            updated_at = current.get("updated_at")
            if isinstance(updated_at, str):
                current["ended_at"] = updated_at

        # The send acknowledgement is authoritative immediately. It carries no
        # transcript event, so only update the existing thread record.
        state = current.get("state")
        if state in RUN_STATES:
            for thread in machine.get("snapshot", {}).get("threads", []):
                if thread.get("id") == thread_id:
                    thread["state"] = state
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
        Workspace._apply_run_event(machine, event)
        Workspace._apply_thread_state(machine, event)

    @staticmethod
    def _apply_thread_state(machine: dict[str, Any], event: dict[str, Any]) -> None:
        if event.get("kind") != "run_state":
            return
        thread_id = event.get("thread_id")
        run_id = event.get("run_id")
        current = machine.get("runs", {}).get(thread_id)
        if run_id and isinstance(current, dict) and current.get("id") != run_id:
            return
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        state = data.get("state")
        if state in RUN_STATES:
            for thread in machine.get("snapshot", {}).get("threads", []):
                if thread.get("id") == thread_id:
                    thread["state"] = state
                    break

    async def poll_forever(self, machine_id: str) -> None:
        # Python 3.11 wait_for can return a just-completed result while its
        # caller is being cancelled. Honor the pending cancellation before
        # starting another poll, including when replacing a saved connection.
        task = asyncio.current_task()
        while not self._stopping and machine_id in self.machines and not task.cancelling():
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
            self._client_server_ids.pop(id(client), None)
        self._observed_server_ids.pop(machine_id, None)

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
        selection = self._selection_context()
        result = await self.rpc("add_project", {"path": path, "name": name}, machine_id=machine_id)
        projects = self.machines[machine_id].setdefault("snapshot", {}).setdefault("projects", [])
        self._upsert_snapshot_record(projects, result)
        if self._selection_context() == selection and selection[0] == machine_id:
            self.switch(machine_id, result["id"])
        else:
            self._changed(force=True)
        self._wake.set()
        return result

    async def create_thread(
        self, project_id: str, title: str, provider: str, *, model: str | None = None,
        settings: dict[str, Any] | None = None, worktree: bool = False, machine_id: str | None = None,
    ) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        selection = self._selection_context()
        params: dict[str, Any] = {
            "project_id": project_id, "title": title, "provider": provider, "worktree": worktree,
        }
        if model:
            params["model"] = model
        if settings:
            params["settings"] = settings
        result = await self.rpc("create_thread", params, machine_id=machine_id)
        threads = self.machines[machine_id].setdefault("snapshot", {}).setdefault("threads", [])
        self._upsert_snapshot_record(threads, result)
        if (
            self._selection_context() == selection
            and selection[0] == machine_id
            and selection[1] == project_id
        ):
            self.switch(machine_id, project_id, result["id"])
        else:
            self._changed(force=True)
        self._wake.set()
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

    @staticmethod
    def _recovery_record(record: Mapping[str, Any], *, status: str = "uncertain") -> dict[str, Any]:
        value = deepcopy(dict(record))
        value.setdefault("acceptance", "unknown")
        value.setdefault("status", status)
        return value

    def _current_server_id(self, machine_id: str) -> str | None:
        return self._observed_server_ids.get(machine_id) or self._server_id(machine_id)

    def _transport_server_id(self, machine_id: str, client: Any) -> str | None:
        hello = getattr(client, "hello", None)
        if isinstance(hello, Mapping):
            server_id = hello.get("server_id")
            if isinstance(server_id, str) and server_id:
                return server_id
        return self._client_server_ids.get(id(client))

    def _recovery_scope(self, machine_id: str, record: Mapping[str, Any]) -> str:
        recorded = record.get("server_id")
        current = self._current_server_id(machine_id)
        if isinstance(recorded, str) and recorded and isinstance(current, str) and current:
            return "same_server" if recorded == current else "server_changed"
        return "unknown"

    def _require_recovery_scope(
        self, machine_id: str, record: dict[str, Any], transport_server_id: str | None
    ) -> None:
        recorded_server_id = record.get("server_id")
        if (
            isinstance(recorded_server_id, str)
            and recorded_server_id
            and isinstance(transport_server_id, str)
            and transport_server_id
        ):
            scope = "same_server" if recorded_server_id == transport_server_id else "server_changed"
        else:
            scope = "unknown"
        if scope == "same_server":
            return
        request_id = record.get("request_id")
        record.update(status=scope, acceptance="unknown", updated_at=time.time())
        if isinstance(request_id, str) and request_id:
            self.state["send_recovery_evidence"][request_id] = deepcopy(record)
        key = _view_key(machine_id, str(record.get("thread_id") or ""))
        active = self.state["uncertain_sends"].get(key)
        if isinstance(active, dict) and active.get("request_id") == request_id:
            active.update(status=scope, acceptance="unknown", updated_at=record["updated_at"])
        self._changed(force=True)
        if scope == "server_changed":
            raise ValueError(
                "This send belongs to a previous server instance; its acceptance is still unknown and it cannot be retried here"
            )
        raise ValueError(
            "This send has no verified server identity; its acceptance is unknown and it cannot be retried safely"
        )

    def uncertain_send(
        self, thread_id: str | None = None, *, machine_id: str | None = None
    ) -> dict[str, Any] | None:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or self.state.get("selected_thread")
        if thread_id is None:
            return None
        record = self.state["uncertain_sends"].get(_view_key(machine_id, str(thread_id)))
        if not isinstance(record, dict):
            return None
        recovery = self._recovery_record(record)
        scope = self._recovery_scope(machine_id, recovery)
        recovery["server_scope"] = scope
        recovery["retryable"] = scope == "same_server"
        return recovery

    def _recovery_evidence(
        self,
        machine_id: str,
        thread_id: str,
        *,
        acceptance: str | None = None,
        prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for value in self.state["send_recovery_evidence"].values():
            if not isinstance(value, dict):
                continue
            record = self._recovery_record(value, status=str(value.get("status") or "dismissed"))
            if record.get("machine_id") != machine_id or record.get("thread_id") != thread_id:
                continue
            if acceptance is not None and record.get("acceptance") != acceptance:
                continue
            if prompt is not None and record.get("prompt") != prompt:
                continue
            records.append(record)

        def recorded_at(item: Mapping[str, Any]) -> float:
            value = item.get("updated_at") or item.get("created_at")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            parsed = _timestamp(value)
            return parsed if parsed is not None else 0.0

        return sorted(records, key=recorded_at, reverse=True)

    def dismiss_uncertain(
        self, thread_id: str | None = None, *, machine_id: str | None = None
    ) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or self.state.get("selected_thread")
        if thread_id is None:
            raise ValueError("There is no uncertain send to dismiss")
        key = _view_key(machine_id, str(thread_id))
        raw = self.state["uncertain_sends"].pop(key, None)
        if not isinstance(raw, dict):
            raise ValueError("There is no uncertain send to dismiss")
        record = self._recovery_record(raw)
        record.update(status="dismissed", acceptance="unknown", updated_at=time.time())
        request_id = record.get("request_id")
        if isinstance(request_id, str) and request_id:
            self.state["send_recovery_evidence"][request_id] = deepcopy(record)
        view = self.thread_view(str(thread_id), machine_id)
        if view.get("draft") == record.get("prompt"):
            self._set_cached_draft(machine_id, str(thread_id), "")
        self._changed(force=True)
        return deepcopy(record)

    def failed_prompt(
        self, thread_id: str | None = None, *, machine_id: str | None = None
    ) -> dict[str, Any] | None:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or self.state.get("selected_thread")
        if thread_id is None:
            return None
        run = self.machines[machine_id].get("runs", {}).get(str(thread_id))
        if not isinstance(run, dict) or run.get("state") != "failed":
            return None
        prompt = run.get("_prompt")
        if not isinstance(prompt, str) or not prompt:
            return None
        return {
            "machine_id": machine_id,
            "thread_id": str(thread_id),
            "run_id": run.get("id"),
            "request_id": run.get("_request_id"),
            "prompt": prompt,
            "acceptance": "accepted",
            "status": "failed",
            "error": run.get("error"),
        }

    def recover_failed_prompt(
        self, thread_id: str | None = None, *, machine_id: str | None = None
    ) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        recovery = self.failed_prompt(thread_id, machine_id=machine_id)
        if recovery is None:
            raise ValueError("There is no failed prompt to recover")
        thread_id = str(recovery["thread_id"])
        view = self.thread_view(thread_id, machine_id)
        draft = view.get("draft")
        if isinstance(draft, str) and draft and draft != recovery["prompt"]:
            raise ValueError("This thread already has a newer draft; clear it before recovering the failed prompt")
        self._set_cached_draft(machine_id, thread_id, str(recovery["prompt"]))
        self._changed(force=True)
        return deepcopy(recovery)

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
        draft_revision = self._draft_revision(machine_id, str(thread_id))
        key = _view_key(machine_id, thread["id"])
        if request_id is None:
            if self.uncertain_send(str(thread_id), machine_id=machine_id) is not None:
                raise ValueError("This thread has a send whose acceptance is unknown; retry or dismiss it before sending again")
            if self._recovery_evidence(
                machine_id, str(thread_id), acceptance="unknown", prompt=prompt
            ):
                raise ValueError("This prompt's acceptance is still unknown; retry it with its original request ID or edit it")
        machine = self.machines[machine_id]
        readiness = machine.get("providers", {}).get(thread.get("provider"), {})
        if readiness.get("available") is False:
            # A setup failure is definitive, not an accepted provider run.
            # Keep the draft and avoid persisting a misleading failed turn.
            detail = readiness.get("detail") or "Finish provider setup on this server."
            raise ValueError(f"{thread.get('provider', 'Provider')} is unavailable on {machine.get('alias', machine_id)}: {detail}")
        request_id = request_id or uuid.uuid4().hex
        # Keep one connected transport and its hello identity across validation
        # and send. A concurrent reconnect must not turn an old idempotency key
        # into a first-time request on a replacement daemon.
        client = await self._client(machine_id)
        transport_server_id = self._transport_server_id(machine_id, client)
        previous_evidence = self.state["send_recovery_evidence"].get(request_id)
        active_evidence = self.state["uncertain_sends"].get(key)
        if (
            not isinstance(previous_evidence, dict)
            and isinstance(active_evidence, dict)
            and active_evidence.get("request_id") == request_id
        ):
            previous_evidence = active_evidence
        record = self._recovery_record(previous_evidence) if isinstance(previous_evidence, dict) else {
            "machine_id": machine_id,
            "thread_id": thread["id"],
            "prompt": prompt,
            "request_id": request_id,
            "server_id": transport_server_id,
            "acceptance": "unknown",
            "status": "uncertain",
            "created_at": time.time(),
        }
        if isinstance(previous_evidence, dict):
            self._require_recovery_scope(machine_id, record, transport_server_id)
        try:
            result = await client.call(
                "send", {"thread_id": thread["id"], "prompt": prompt, "request_id": request_id}
            )
        except asyncio.CancelledError:
            # Cancellation can arrive after the request bytes were written.
            # Treat the missing acknowledgement exactly like a lost transport
            # reply, and leave any retry to an explicit user action.
            record.update(
                acceptance="unknown",
                status="uncertain",
                updated_at=time.time(),
                last_error="Send was interrupted before its acknowledgement arrived",
            )
            self.state["uncertain_sends"][key] = record
            self.state["send_recovery_evidence"][request_id] = deepcopy(record)
            self._changed(force=True)
            raise
        except RPCError as exc:
            # A structured daemon response is definitive; it is safe to edit
            # the prompt and create a new request rather than offering retry.
            if isinstance(previous_evidence, dict) or (
                isinstance(self.state["uncertain_sends"].get(key), dict)
                and self.state["uncertain_sends"][key].get("request_id") == request_id
            ):
                record.update(
                    acceptance="not_accepted",
                    status="failed",
                    updated_at=time.time(),
                    error={"code": exc.code, "message": exc.message},
                )
                self.state["send_recovery_evidence"][request_id] = deepcopy(record)
                self.state["uncertain_sends"].pop(key, None)
                self._changed(force=True)
            raise
        except Exception as exc:
            # Transport exceptions are uncertain; retry is always explicit and
            # reuses this id so the daemon can resolve it idempotently.
            record.update(
                acceptance="unknown",
                status="uncertain",
                updated_at=time.time(),
                last_error=str(exc),
            )
            self.state["uncertain_sends"][key] = record
            self.state["send_recovery_evidence"][request_id] = deepcopy(record)
            self._changed(force=True)
            raise
        active = self.state["uncertain_sends"].get(key)
        if isinstance(previous_evidence, dict) or (
            isinstance(active, dict) and active.get("request_id") == request_id
        ):
            record.update(
                acceptance="accepted",
                status="resolved",
                updated_at=time.time(),
                run_id=result.get("id") if isinstance(result, Mapping) else None,
            )
            self.state["send_recovery_evidence"][request_id] = deepcopy(record)
        if isinstance(active, dict) and active.get("request_id") == request_id:
            self.state["uncertain_sends"].pop(key, None)
        if isinstance(result, Mapping):
            acknowledged = dict(result)
            acknowledged.setdefault("prompt", prompt)
            acknowledged.setdefault("request_id", request_id)
            self._apply_run_ack(self.machines[machine_id], thread["id"], acknowledged)
        # Do not erase a follow-up typed while this RPC was in flight.
        if (
            view.get("draft") == prompt
            and self._draft_revision(machine_id, str(thread_id)) == draft_revision
        ):
            self._set_cached_draft(machine_id, str(thread_id), "")
        self._changed(force=True)
        self._wake.set()
        return result

    async def retry_uncertain(self, thread_id: str | None = None, *, machine_id: str | None = None) -> Any:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or self.state.get("selected_thread")
        key = _view_key(machine_id, thread_id) if thread_id else ""
        record = self.state["uncertain_sends"].get(key)
        if not isinstance(record, dict) and thread_id is not None:
            evidence = self._recovery_evidence(machine_id, str(thread_id), acceptance="unknown")
            record = evidence[0] if evidence else None
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
        self,
        path: str | None = None,
        *,
        machine_id: str | None = None,
        thread_id: str | None = None,
        independent: bool = False,
    ) -> dict[str, Any]:
        machine_id = machine_id or self.selected_machine_id
        thread_id = thread_id or (self.selected_thread or {}).get("id")
        if thread_id is None:
            raise ValueError("Select a thread before reviewing changes")
        params: dict[str, Any] = {"thread_id": thread_id}
        if path:
            params["path"] = path
        if not independent:
            return await self.rpc("diff", params, machine_id=machine_id)

        machine = self.machines[machine_id]
        kwargs = {
            "data_dir": self.data_dir if machine.get("host") is None else None,
            "host": machine.get("host"),
        }
        if machine.get("remote_command"):
            kwargs["remote_command"] = machine["remote_command"]
        client = self.rpc_factory(**kwargs)
        if inspect.isawaitable(client):
            client = await client
        try:
            await client.connect()
            return await client.call("diff", params)
        finally:
            await client.close()

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
