"""Durable SQLite storage for the Zeus Code daemon.

The store deliberately exposes small synchronous operations.  Every state change
that must be observed together with an event is committed in one transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


SCHEMA_VERSION = 1
ACTIVE_STATES = frozenset({"running", "awaiting_approval"})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
THREAD_STATES = frozenset({"idle", *ACTIVE_STATES, *TERMINAL_STATES})
INTERRUPTED_ERROR = "Run interrupted by daemon restart; send again to resume."
WIRE_PAGE_BYTES = 512 * 1024


class StoreError(RuntimeError):
    """Raised when durable state cannot safely satisfy an operation."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _id() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class Store:
    """A thread-safe, short-operation SQLite state store."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.path = self.data_dir / "state.sqlite3"
        if self.path.is_symlink():
            raise StoreError(f"refusing symbolic-link database path: {self.path}")
        try:
            descriptor = os.open(
                self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            if not self.path.is_file():
                raise StoreError(f"database path is not a regular file: {self.path}")
            os.chmod(self.path, 0o600)
        else:
            os.close(descriptor)
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
            timeout=10.0,
        )
        self._db.row_factory = sqlite3.Row
        try:
            version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise StoreError(
                    f"database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if version < SCHEMA_VERSION:
                self._migrate(version)
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA synchronous = FULL")
            self._db.execute("PRAGMA busy_timeout = 10000")
            os.chmod(self.path, 0o600)
            self._secure_sidecars()
            with self._transaction():
                row = self._db.execute(
                    "SELECT value FROM metadata WHERE key = 'server_id'"
                ).fetchone()
                if row is None:
                    self._server_id = _id()
                    self._db.execute(
                        "INSERT INTO metadata(key, value) VALUES ('server_id', ?)",
                        (self._server_id,),
                    )
                else:
                    self._server_id = str(row["value"])
        except Exception:
            self._db.close()
            self._closed = True
            raise

    @property
    def server_id(self) -> str:
        return self._server_id

    @property
    def last_seq(self) -> int:
        with self._lock:
            self._ensure_open()
            row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
            return int(row[0])

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._db.close()
            self._closed = True
            self._secure_sidecars()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise StoreError("store is closed")

    def _secure_sidecars(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def _migrate(self, version: int) -> None:
        if version != 0:
            raise StoreError(f"no migration path from schema version {version}")
        # executescript is used so schema creation and the version marker share
        # one SQLite transaction. CREATE IF NOT EXISTS preserves version-zero
        # databases populated by development builds of this schema.
        self._db.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                branch TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL,
                provider TEXT NOT NULL,
                cwd TEXT NOT NULL,
                branch TEXT,
                isolated INTEGER NOT NULL CHECK (isolated IN (0, 1)),
                session_id TEXT,
                state TEXT NOT NULL,
                archived INTEGER NOT NULL CHECK (archived IN (0, 1)),
                draft TEXT NOT NULL,
                scroll INTEGER NOT NULL,
                model TEXT,
                settings TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS threads_project_idx
                ON threads(project_id, created_at);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL REFERENCES threads(id),
                prompt TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runs_thread_idx
                ON runs(thread_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_thread
                ON runs(thread_id) WHERE state IN ('running', 'awaiting_approval');
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL REFERENCES threads(id),
                run_id TEXT REFERENCES runs(id),
                kind TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_thread_seq_idx
                ON events(thread_id, seq);
            CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id),
                thread_id TEXT NOT NULL REFERENCES threads(id),
                provider_request_id TEXT,
                state TEXT NOT NULL,
                decision TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS approvals_pending_idx
                ON approvals(state, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS approvals_provider_request_idx
                ON approvals(run_id, provider_request_id)
                WHERE provider_request_id IS NOT NULL;
            PRAGMA user_version = 1;
            COMMIT;
            """
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._ensure_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")
                self._secure_sidecars()

    @staticmethod
    def _project(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _thread(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["isolated"] = bool(result["isolated"])
        result["archived"] = bool(result["archived"])
        result["settings"] = _decode(result["settings"])
        return result

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["data"] = _decode(result["data"])
        return result

    @staticmethod
    def _approval(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = _decode(result["payload"])
        return result

    def projects(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                "SELECT * FROM projects ORDER BY created_at, id"
            ).fetchall()
            return [self._project(row) for row in rows]

    def project(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                raise KeyError(project_id)
            return self._project(row)

    def add_project(
        self, path: str | Path, name: str | None = None, branch: str | None = None
    ) -> dict[str, Any]:
        project_path = str(Path(path).expanduser().resolve())
        project_name = name or Path(project_path).name or project_path
        project_id = _id()
        now = _now()
        with self._transaction():
            try:
                self._db.execute(
                    """INSERT INTO projects
                       (id, name, path, branch, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (project_id, project_name, project_path, branch, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"project path is already registered: {project_path}") from exc
        return self.project(project_id)

    def threads(self, include_archived: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            where = "" if include_archived else "WHERE archived = 0"
            rows = self._db.execute(
                f"SELECT * FROM threads {where} ORDER BY updated_at DESC, id"
            ).fetchall()
            return [self._thread(row) for row in rows]

    def thread(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                raise KeyError(thread_id)
            return self._thread(row)

    def create_thread(
        self,
        project_id: str,
        title: str,
        provider: str,
        cwd: str | Path,
        branch: str | None,
        isolated: bool,
        model: str | None = None,
        settings: Any = None,
    ) -> dict[str, Any]:
        thread_id = _id()
        now = _now()
        with self._transaction():
            if self._db.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            ).fetchone() is None:
                raise KeyError(project_id)
            self._db.execute(
                """INSERT INTO threads
                   (id, project_id, title, provider, cwd, branch, isolated,
                    session_id, state, archived, draft, scroll, model, settings,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'idle', 0, '', 0, ?, ?, ?, ?)""",
                (
                    thread_id,
                    project_id,
                    title,
                    provider,
                    str(cwd),
                    branch,
                    int(isolated),
                    model,
                    _json(settings) if settings is not None else None,
                    now,
                    now,
                ),
            )
        return self.thread(thread_id)

    def update_thread(self, thread_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "title",
            "archived",
            "draft",
            "scroll",
            "model",
            "settings",
            "session_id",
            "branch",
            "state",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"thread fields cannot be updated: {', '.join(sorted(unknown))}")
        if not changes:
            return self.thread(thread_id)
        if "state" in changes and changes["state"] not in THREAD_STATES:
            raise ValueError(f"invalid thread state: {changes['state']}")
        values: list[Any] = []
        assignments: list[str] = []
        for key, value in changes.items():
            if key in {"archived"}:
                value = int(bool(value))
            elif key == "settings":
                value = _json(value) if value is not None else None
            assignments.append(f"{key} = ?")
            values.append(value)
        assignments.append("updated_at = ?")
        values.extend((_now(), thread_id))
        with self._transaction():
            cursor = self._db.execute(
                f"UPDATE threads SET {', '.join(assignments)} WHERE id = ?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(thread_id)
        return self.thread(thread_id)

    def _insert_event(
        self, thread_id: str, run_id: str | None, kind: str, data: Any
    ) -> dict[str, Any]:
        now = _now()
        cursor = self._db.execute(
            """INSERT INTO events(thread_id, run_id, kind, data, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (thread_id, run_id, kind, _json(data), now),
        )
        return {
            "seq": int(cursor.lastrowid),
            "thread_id": thread_id,
            "run_id": run_id,
            "kind": kind,
            "data": data,
            "created_at": now,
        }

    def create_run(
        self, thread_id: str, prompt: str, request_id: str
    ) -> tuple[dict[str, Any], bool]:
        with self._transaction():
            existing = self._db.execute(
                "SELECT * FROM runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                if existing["thread_id"] != thread_id or existing["prompt"] != prompt:
                    raise ValueError("request_id was already used for a different send")
                return self._run(existing), False
            if self._db.execute(
                "SELECT 1 FROM threads WHERE id = ?", (thread_id,)
            ).fetchone() is None:
                raise KeyError(thread_id)
            if self._db.execute(
                """SELECT 1 FROM runs
                   WHERE thread_id = ? AND state IN ('running', 'awaiting_approval')""",
                (thread_id,),
            ).fetchone() is not None:
                raise ValueError("thread already has an active run")
            run_id = _id()
            now = _now()
            self._db.execute(
                """INSERT INTO runs
                   (id, thread_id, prompt, request_id, state, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'running', NULL, ?, ?)""",
                (run_id, thread_id, prompt, request_id, now, now),
            )
            self._db.execute(
                "UPDATE threads SET state = 'running', updated_at = ? WHERE id = ?",
                (now, thread_id),
            )
            self._insert_event(
                thread_id, run_id, "message", {"role": "user", "text": prompt}
            )
            self._insert_event(thread_id, run_id, "run_state", {"state": "running"})
            row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            assert row is not None
            return self._run(row), True

    def run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return self._run(row)

    def find_run_by_request(self, request_id: str) -> dict[str, Any] | None:
        """Look up an accepted send for retry handling without changing state."""

        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT * FROM runs WHERE request_id = ?", (request_id,)
            ).fetchone()
            return None if row is None else self._run(row)

    def active_run(self, thread_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_open()
            if self._db.execute(
                "SELECT 1 FROM threads WHERE id = ?", (thread_id,)
            ).fetchone() is None:
                raise KeyError(thread_id)
            row = self._db.execute(
                """SELECT * FROM runs WHERE thread_id = ?
                   AND state IN ('running', 'awaiting_approval')
                   ORDER BY created_at DESC LIMIT 1""",
                (thread_id,),
            ).fetchone()
            return None if row is None else self._run(row)

    def finish_run(
        self, run_id: str, state: str, error: str | None = None
    ) -> dict[str, Any]:
        if state not in TERMINAL_STATES:
            raise ValueError(f"run finish state must be terminal: {state}")
        with self._transaction():
            row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["state"] in TERMINAL_STATES:
                if row["state"] != state or row["error"] != error:
                    raise ValueError("run is already finished with a different result")
                return self._run(row)
            thread_id = str(row["thread_id"])
            now = _now()
            self._db.execute(
                "UPDATE runs SET state = ?, error = ?, updated_at = ? WHERE id = ?",
                (state, error, now, run_id),
            )
            self._db.execute(
                "UPDATE threads SET state = ?, updated_at = ? WHERE id = ?",
                (state, now, thread_id),
            )
            pending = self._db.execute(
                "SELECT id FROM approvals WHERE run_id = ? AND state = 'pending'",
                (run_id,),
            ).fetchall()
            for approval in pending:
                approval_id = str(approval["id"])
                self._db.execute(
                    """UPDATE approvals SET state = 'invalidated', updated_at = ?
                       WHERE id = ?""",
                    (now, approval_id),
                )
                self._insert_event(
                    thread_id,
                    run_id,
                    "approval",
                    {
                        "id": approval_id,
                        "approval_state": "invalidated",
                        "decision": None,
                    },
                )
            state_data: dict[str, Any] = {"state": state}
            if error is not None:
                state_data["error"] = error
            self._insert_event(thread_id, run_id, "run_state", state_data)
            finished = self._db.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert finished is not None
            return self._run(finished)

    def set_thread_state(self, thread_id: str, state: str) -> dict[str, Any]:
        if state not in THREAD_STATES:
            raise ValueError(f"invalid thread state: {state}")
        return self.update_thread(thread_id, state=state)

    def append_event(
        self, thread_id: str, run_id: str | None, kind: str, data: Any
    ) -> dict[str, Any]:
        with self._transaction():
            if self._db.execute(
                "SELECT 1 FROM threads WHERE id = ?", (thread_id,)
            ).fetchone() is None:
                raise KeyError(thread_id)
            if run_id is not None:
                run_row = self._db.execute(
                    "SELECT thread_id FROM runs WHERE id = ?", (run_id,)
                ).fetchone()
                if run_row is None:
                    raise KeyError(run_id)
                if run_row["thread_id"] != thread_id:
                    raise ValueError("run does not belong to thread")
            return self._insert_event(thread_id, run_id, kind, data)

    def events(
        self,
        after: int = 0,
        limit: int = 200,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        if after < 0:
            raise ValueError("after must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            self._ensure_open()
            if thread_id is None:
                rows = self._db.execute(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                    (after, limit),
                ).fetchall()
            else:
                rows = self._db.execute(
                    """SELECT * FROM events WHERE seq > ? AND thread_id = ?
                       ORDER BY seq LIMIT ?""",
                    (after, thread_id, limit),
                ).fetchall()
            items = self._bounded_events(rows)
            # A page cursor only advances through rows actually returned. Using
            # MAX(seq) here would silently skip unseen rows after a full page.
            return {"events": items, "last_seq": items[-1]["seq"] if items else after}

    def history(
        self, thread_id: str, before: int | None = None, limit: int = 200
    ) -> dict[str, Any]:
        if before is not None and before < 0:
            raise ValueError("before must be non-negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._lock:
            self._ensure_open()
            if self._db.execute(
                "SELECT 1 FROM threads WHERE id = ?", (thread_id,)
            ).fetchone() is None:
                raise KeyError(thread_id)
            if before is None:
                rows = self._db.execute(
                    """SELECT * FROM events WHERE thread_id = ?
                       ORDER BY seq DESC LIMIT ?""",
                    (thread_id, limit + 1),
                ).fetchall()
            else:
                rows = self._db.execute(
                    """SELECT * FROM events WHERE thread_id = ? AND seq < ?
                       ORDER BY seq DESC LIMIT ?""",
                    (thread_id, before, limit + 1),
                ).fetchall()
            selected = self._bounded_events(rows[:limit])
            has_more = len(rows) > len(selected)
            selected.reverse()
            return {
                "events": selected,
                "has_more": has_more,
            }

    @classmethod
    def _bounded_events(cls, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        """Return a wire-safe prefix while always advancing by at least one row."""

        items: list[dict[str, Any]] = []
        used = 0
        for row in rows:
            item = cls._event(row)
            size = len(
                json.dumps(item, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            ) + 1
            if items and used + size > WIRE_PAGE_BYTES:
                break
            items.append(item)
            used += size
        return items

    def create_approval(
        self, run_id: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        payload_dict = dict(payload)
        provider_request_id = payload_dict.get("provider_request_id")
        if provider_request_id is not None and (
            not isinstance(provider_request_id, str) or not provider_request_id
        ):
            raise ValueError("provider_request_id must be nonempty text")
        encoded_payload = _json(payload_dict)
        with self._transaction():
            existing = None
            if provider_request_id is not None:
                existing = self._db.execute(
                    """SELECT * FROM approvals
                       WHERE run_id = ? AND provider_request_id = ?""",
                    (run_id, provider_request_id),
                ).fetchone()
            if existing is not None:
                if existing["payload"] != encoded_payload:
                    raise ValueError(
                        "provider_request_id was reused with a different approval payload"
                    )
                return self._approval(existing)
            run_row = self._db.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            if run_row["state"] not in ACTIVE_STATES:
                raise ValueError("cannot request approval for a finished run")
            thread_id = str(run_row["thread_id"])
            approval_id = _id()
            now = _now()
            self._db.execute(
                """INSERT INTO approvals
                   (id, run_id, thread_id, provider_request_id, state, decision,
                    payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?, ?)""",
                (
                    approval_id,
                    run_id,
                    thread_id,
                    provider_request_id,
                    encoded_payload,
                    now,
                    now,
                ),
            )
            self._db.execute(
                "UPDATE runs SET state = 'awaiting_approval', updated_at = ? WHERE id = ?",
                (now, run_id),
            )
            self._db.execute(
                """UPDATE threads SET state = 'awaiting_approval', updated_at = ?
                   WHERE id = ?""",
                (now, thread_id),
            )
            self._insert_event(
                thread_id,
                run_id,
                "approval",
                {
                    "id": approval_id,
                    "approval_state": "pending",
                    "decision": None,
                    "payload": payload_dict,
                },
            )
            self._insert_event(
                thread_id, run_id, "run_state", {"state": "awaiting_approval"}
            )
            row = self._db.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            assert row is not None
            return self._approval(row)

    def approval(self, approval_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise KeyError(approval_id)
            return self._approval(row)

    def approvals(self) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                """SELECT * FROM approvals WHERE state = 'pending'
                   ORDER BY created_at, id"""
            ).fetchall()
            return [self._approval(row) for row in rows]

    def resolve_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in {"allow", "reject"}:
            raise ValueError("approval decision must be 'allow' or 'reject'")
        with self._transaction():
            row = self._db.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise KeyError(approval_id)
            if row["state"] == "resolved":
                if row["decision"] != decision:
                    raise ValueError("approval was already resolved differently")
                return self._approval(row)
            if row["state"] != "pending":
                raise ValueError("approval is stale")
            run_id = str(row["run_id"])
            thread_id = str(row["thread_id"])
            run_row = self._db.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run_row is None or run_row["state"] not in ACTIVE_STATES:
                raise ValueError("approval is stale")
            now = _now()
            self._db.execute(
                """UPDATE approvals SET state = 'resolved', decision = ?, updated_at = ?
                   WHERE id = ?""",
                (decision, now, approval_id),
            )
            self._insert_event(
                thread_id,
                run_id,
                "approval",
                {
                    "id": approval_id,
                    "approval_state": "resolved",
                    "decision": decision,
                },
            )
            pending = self._db.execute(
                """SELECT 1 FROM approvals
                   WHERE run_id = ? AND state = 'pending' LIMIT 1""",
                (run_id,),
            ).fetchone()
            if pending is None:
                self._db.execute(
                    "UPDATE runs SET state = 'running', updated_at = ? WHERE id = ?",
                    (now, run_id),
                )
                self._db.execute(
                    "UPDATE threads SET state = 'running', updated_at = ? WHERE id = ?",
                    (now, thread_id),
                )
                self._insert_event(
                    thread_id, run_id, "run_state", {"state": "running"}
                )
            resolved = self._db.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            assert resolved is not None
            return self._approval(resolved)

    def recover_interrupted(self) -> int:
        """Fail runs left active by a previous process; prompts are never replayed."""

        with self._transaction():
            rows = self._db.execute(
                """SELECT id, thread_id FROM runs
                   WHERE state IN ('running', 'awaiting_approval')"""
            ).fetchall()
            now = _now()
            for row in rows:
                run_id = str(row["id"])
                thread_id = str(row["thread_id"])
                self._db.execute(
                    """UPDATE runs SET state = 'failed', error = ?, updated_at = ?
                       WHERE id = ?""",
                    (INTERRUPTED_ERROR, now, run_id),
                )
                approvals = self._db.execute(
                    """SELECT id FROM approvals
                       WHERE run_id = ? AND state = 'pending'""",
                    (run_id,),
                ).fetchall()
                for approval in approvals:
                    approval_id = str(approval["id"])
                    self._db.execute(
                        """UPDATE approvals SET state = 'invalidated', updated_at = ?
                           WHERE id = ?""",
                        (now, approval_id),
                    )
                    self._insert_event(
                        thread_id,
                        run_id,
                        "approval",
                        {
                            "id": approval_id,
                            "approval_state": "invalidated",
                            "decision": None,
                        },
                    )
                self._db.execute(
                    "UPDATE threads SET state = 'failed', updated_at = ? WHERE id = ?",
                    (now, thread_id),
                )
                self._insert_event(
                    thread_id,
                    run_id,
                    "run_state",
                    {"state": "failed", "error": INTERRUPTED_ERROR},
                )
            # These are only possible after an older development build or
            # manual repair, but recovery still leaves no stale attention.
            orphan_approvals = self._db.execute(
                """SELECT id, run_id, thread_id FROM approvals
                   WHERE state = 'pending'"""
            ).fetchall()
            for approval in orphan_approvals:
                approval_id = str(approval["id"])
                self._db.execute(
                    """UPDATE approvals SET state = 'invalidated', updated_at = ?
                       WHERE id = ?""",
                    (now, approval_id),
                )
                self._insert_event(
                    str(approval["thread_id"]),
                    str(approval["run_id"]),
                    "approval",
                    {
                        "id": approval_id,
                        "approval_state": "invalidated",
                        "decision": None,
                    },
                )
            orphan_threads = self._db.execute(
                """SELECT id FROM threads
                   WHERE state IN ('running', 'awaiting_approval')
                   AND NOT EXISTS (
                       SELECT 1 FROM runs
                       WHERE runs.thread_id = threads.id
                       AND runs.state IN ('running', 'awaiting_approval')
                   )"""
            ).fetchall()
            for thread in orphan_threads:
                thread_id = str(thread["id"])
                self._db.execute(
                    "UPDATE threads SET state = 'failed', updated_at = ? WHERE id = ?",
                    (now, thread_id),
                )
                self._insert_event(
                    thread_id,
                    None,
                    "run_state",
                    {"state": "failed", "error": INTERRUPTED_ERROR},
                )
            return len(rows)

    def backup(self, target: str | Path) -> Path:
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._ensure_open()
            try:
                descriptor = os.open(
                    destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                raise FileExistsError(destination) from None
            else:
                os.close(descriptor)
            backup_db = sqlite3.connect(destination)
            try:
                self._db.backup(backup_db)
                check = backup_db.execute("PRAGMA integrity_check").fetchone()
                if check is None or check[0] != "ok":
                    raise StoreError(f"backup integrity check failed: {check!r}")
            except BaseException:
                backup_db.close()
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                raise
            else:
                backup_db.close()
        os.chmod(destination, 0o600)
        return destination
