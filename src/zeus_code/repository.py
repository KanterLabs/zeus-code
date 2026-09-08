"""Safe, bounded Git repository inspection for the daemon review API."""

from __future__ import annotations

import asyncio
from collections import deque
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass


DIFF_LIMIT = 256 * 1024
_METADATA_LIMIT = 8 * 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_GIT_TIMEOUT = 20.0
_UNTRACKED_STATS_LIMIT = 32 * 1024 * 1024
_UNTRACKED_STATS_TIMEOUT = 1.0

DISCOVERY_MAX_DEPTH = 4
DISCOVERY_MAX_DIRECTORIES = 2000
DISCOVERY_MAX_PROJECTS = 200
_DISCOVERY_SKIPPED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
    "venv",
}


class RepositoryError(RuntimeError):
    """A repository operation failed in a way that is safe to show to a user."""


def _discovery_root(path: str) -> Path:
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise ValueError("discovery root must be a non-empty path")
    try:
        expanded = Path(path).expanduser()
    except (OSError, RuntimeError) as exc:
        raise RepositoryError(f"Could not expand project discovery root {path!r}: {exc}") from exc
    try:
        root = expanded.resolve(strict=True)
    except FileNotFoundError:
        raise RepositoryError(f"Project discovery root does not exist: {expanded}") from None
    except (OSError, RuntimeError) as exc:
        raise RepositoryError(f"Cannot access project discovery root {expanded}: {exc}") from exc
    try:
        mode = root.stat().st_mode
    except OSError as exc:
        raise RepositoryError(f"Cannot access project discovery root {root}: {exc}") from exc
    if not stat.S_ISDIR(mode):
        raise RepositoryError(f"Project discovery root is not a directory: {root}")
    return root


def _discover_projects(path: str) -> dict[str, object]:
    root = _discovery_root(path)
    pending: deque[tuple[Path, int]] = deque([(root, 0)])
    projects: list[dict[str, str]] = []
    visited = 0
    truncated = False

    while pending:
        if visited >= DISCOVERY_MAX_DIRECTORIES:
            truncated = True
            break
        directory, depth = pending.popleft()
        visited += 1
        try:
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            if directory == root:
                raise RepositoryError(f"Cannot read project discovery root {root}: {exc}") from exc
            truncated = True
            continue

        is_repository = False
        for entry in children:
            if entry.name != ".git":
                continue
            try:
                is_repository = entry.is_dir(follow_symlinks=False) or entry.is_file(
                    follow_symlinks=False
                )
            except OSError:
                truncated = True
            break

        if is_repository:
            if len(projects) >= DISCOVERY_MAX_PROJECTS:
                truncated = True
                break
            projects.append({"path": os.fspath(directory), "name": directory.name})
            continue

        descendants: list[Path] = []
        for entry in children:
            if entry.name.startswith(".") or entry.name in _DISCOVERY_SKIPPED_DIRECTORIES:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    descendants.append(Path(entry.path))
            except OSError:
                truncated = True
        if depth >= DISCOVERY_MAX_DEPTH:
            if descendants:
                truncated = True
            continue
        pending.extend((child, depth + 1) for child in descendants)

    projects.sort(key=lambda project: project["path"])
    return {"root": os.fspath(root), "projects": projects, "truncated": truncated}


async def discover_projects(path: str) -> dict[str, object]:
    """Find nearby Git working trees without blocking the daemon event loop."""
    return await asyncio.to_thread(_discover_projects, path)


@dataclass(frozen=True)
class _CommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int
    truncated: bool


@dataclass
class _StatsBudget:
    remaining: int = _UNTRACKED_STATS_LIMIT
    deadline: float = 0.0

    @classmethod
    def start(cls) -> _StatsBudget:
        return cls(deadline=time.monotonic() + _UNTRACKED_STATS_TIMEOUT)


def _git(
    args: list[str],
    *,
    cwd: str | os.PathLike[str],
    stdout_limit: int = _METADATA_LIMIT,
    allow_truncated: bool = False,
    ok_returncodes: tuple[int, ...] = (0,),
) -> _CommandResult:
    """Run Git without a shell and drain both pipes into bounded buffers."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    try:
        process = subprocess.Popen(
            ["git", *args],
            cwd=os.fspath(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise RepositoryError(f"could not start git: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = bytearray()
    errors = bytearray()
    truncated = False
    deadline = time.monotonic() + _GIT_TIMEOUT
    killed = False

    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate(process)
                killed = True
                raise RepositoryError(f"git {' '.join(args[:2])} timed out")
            events = selector.select(min(remaining, 0.25))
            if not events and process.poll() is not None:
                # A final nonblocking pass drains bytes written just before exit.
                events = selector.select(0)
                if not events:
                    break
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = output if key.data == "stdout" else errors
                limit = stdout_limit if key.data == "stdout" else _STDERR_LIMIT
                available = max(0, limit - len(target))
                target.extend(chunk[:available])
                if len(chunk) > available:
                    if key.data == "stdout" and allow_truncated:
                        truncated = True
                    else:
                        _terminate(process)
                        killed = True
                        kind = "output" if key.data == "stdout" else "error output"
                        raise RepositoryError(f"git {kind} exceeded its safety limit")
                    _terminate(process)
                    killed = True
            if truncated:
                break
    finally:
        selector.close()
        if killed or process.poll() is None:
            _terminate(process)
        try:
            returncode = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            returncode = process.wait()
        process.stdout.close()
        process.stderr.close()

    result = _CommandResult(bytes(output), bytes(errors), returncode, truncated)
    if not truncated and returncode not in ok_returncodes:
        detail = errors.decode("utf-8", "replace").strip()
        if not detail:
            detail = f"git exited with status {returncode}"
        raise RepositoryError(detail)
    return result


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _root(path: str) -> Path:
    if not isinstance(path, str) or not path:
        raise ValueError("repository path must be a non-empty string")
    candidate = Path(path).expanduser()
    if not candidate.is_dir():
        raise RepositoryError(f"repository path is not a directory: {path}")
    result = _git(["rev-parse", "--show-toplevel"], cwd=candidate, stdout_limit=64 * 1024)
    value = os.fsdecode(result.stdout).rstrip("\r\n")
    if not value:
        raise RepositoryError(f"not a Git working tree: {path}")
    return Path(value).resolve()


def _branch(root: Path) -> str:
    symbolic = _git(
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root,
        stdout_limit=64 * 1024,
        ok_returncodes=(0, 1),
    )
    if symbolic.returncode == 0:
        return os.fsdecode(symbolic.stdout).rstrip("\r\n")
    commit = _git(
        ["rev-parse", "--verify", "--short=12", "HEAD^{commit}"],
        cwd=root,
        stdout_limit=64 * 1024,
    )
    return f"detached@{os.fsdecode(commit.stdout).strip()}"


def _inspect_repository(path: str) -> dict[str, str]:
    root = _root(path)
    return {"path": os.fspath(root), "branch": _branch(root)}


async def inspect_repository(path: str) -> dict[str, str]:
    """Return the canonical working-tree root and current branch context."""
    return await asyncio.to_thread(_inspect_repository, path)


def _create_worktree(repo_path: str, destination: str, branch: str) -> dict[str, str]:
    root = _root(repo_path)
    if not isinstance(destination, str) or not destination:
        raise ValueError("worktree destination must be a non-empty string")
    if not isinstance(branch, str) or not branch or "\0" in branch:
        raise ValueError("branch must be a non-empty Git branch name")

    destination_path = Path(destination).expanduser().absolute()
    if os.path.lexists(destination_path):
        raise RepositoryError(f"worktree destination already exists: {destination_path}")
    _git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=root, stdout_limit=64 * 1024)
    _git(["check-ref-format", f"refs/heads/{branch}"], cwd=root, stdout_limit=4096)
    existing = _git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        stdout_limit=4096,
        ok_returncodes=(0, 1),
    )
    if existing.returncode == 0:
        raise RepositoryError(f"branch already exists: {branch}")

    _git(
        ["worktree", "add", "-b", branch, "--", os.fspath(destination_path), "HEAD"],
        cwd=root,
        stdout_limit=256 * 1024,
    )
    return {"cwd": os.fspath(destination_path.resolve()), "branch": branch}


async def create_worktree(repo_path: str, destination: str, branch: str) -> dict[str, str]:
    """Create a new branch in a new linked worktree without replacing any path."""
    return await asyncio.to_thread(_create_worktree, repo_path, destination, branch)


def _pathspec(path: str | None) -> str | None:
    if path is None:
        return None
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("diff path must be a non-empty relative path")
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("diff path must stay within the checkout")
    return os.fspath(candidate)


def _status_records(data: bytes) -> list[tuple[bytes, str]]:
    fields = data.split(b"\0")
    records: list[tuple[bytes, str]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise RepositoryError("git returned malformed status data")
        code = record[:2]
        records.append((record[3:], code.decode("ascii", "replace").strip()))
        if b"R" in code or b"C" in code:
            if index >= len(fields):
                raise RepositoryError("git returned an incomplete rename record")
            index += 1  # porcelain v1 -z places the original name second
    return records


def _numstat(data: bytes) -> dict[bytes, tuple[int, int]]:
    fields = data.split(b"\0")
    stats: dict[bytes, tuple[int, int]] = {}
    index = 0
    while index < len(fields):
        header = fields[index]
        index += 1
        if not header:
            continue
        pieces = header.split(b"\t", 2)
        if len(pieces) != 3:
            raise RepositoryError("git returned malformed diff statistics")
        additions = 0 if pieces[0] == b"-" else int(pieces[0])
        deletions = 0 if pieces[1] == b"-" else int(pieces[1])
        current = pieces[2]
        if not current:  # A rename/copy record carries old and new paths next.
            if index + 1 >= len(fields):
                raise RepositoryError("git returned incomplete rename statistics")
            index += 1
            current = fields[index]
            index += 1
        stats[current] = (additions, deletions)
    return stats


def _safe_regular_file(root: Path, raw_path: bytes) -> Path | None:
    relative = Path(os.fsdecode(raw_path))
    candidate = root / relative
    try:
        mode = candidate.lstat().st_mode
    except OSError:
        return None
    if not stat.S_ISREG(mode):
        return None
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate


def _untracked_stats(
    root: Path, raw_path: bytes, budget: _StatsBudget
) -> tuple[int | None, int | None, str | None]:
    if time.monotonic() > budget.deadline:
        return (None, None, "safety_limit")
    candidate = _safe_regular_file(root, raw_path)
    if candidate is None:
        return (0, 0, None)
    try:
        size = candidate.stat().st_size
    except OSError:
        return (None, None, "unreadable")
    if time.monotonic() > budget.deadline or size > budget.remaining:
        return (None, None, "safety_limit")
    lines = 0
    last = b""
    first = True
    try:
        with candidate.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                if time.monotonic() > budget.deadline or len(chunk) > budget.remaining:
                    return (None, None, "safety_limit")
                budget.remaining -= len(chunk)
                if first:
                    first = False
                    if b"\0" in chunk[:8000]:
                        return (0, 0, None)
                lines += chunk.count(b"\n")
                last = chunk[-1:]
    except OSError:
        return (None, None, "unreadable")
    if last and last != b"\n":
        lines += 1
    return (lines, 0, None)


def _append_diff(
    root: Path,
    args: list[str],
    output: bytearray,
    *,
    ok_returncodes: tuple[int, ...] = (0,),
) -> bool:
    remaining = DIFF_LIMIT - len(output)
    if remaining <= 0:
        return True
    result = _git(
        args,
        cwd=root,
        stdout_limit=remaining,
        allow_truncated=True,
        ok_returncodes=ok_returncodes,
    )
    output.extend(result.stdout)
    return result.truncated


def _get_diff(cwd: str, path: str | None) -> dict[str, object]:
    root = _root(cwd)
    selected = _pathspec(path)
    path_args = [] if selected is None else [selected]
    status_result = _git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *path_args],
        cwd=root,
    )
    records = _status_records(status_result.stdout)
    untracked = [raw for raw, code in records if code == "??"]

    has_head = _git(
        ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        cwd=root,
        stdout_limit=64 * 1024,
        ok_returncodes=(0, 1),
    ).returncode == 0

    if has_head:
        stat_result = _git(
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--numstat",
                "-z",
                "HEAD",
                "--",
                *path_args,
            ],
            cwd=root,
        )
        stats = _numstat(stat_result.stdout)
    else:
        stats = {}

    stats_budget = _StatsBudget.start()
    unavailable: dict[bytes, str] = {}
    for raw_path in untracked:
        additions, deletions, reason = _untracked_stats(root, raw_path, stats_budget)
        stats[raw_path] = (additions, deletions)
        if reason is not None:
            unavailable[raw_path] = reason
    if not has_head:
        for raw_path, code in records:
            if code != "??":
                additions, deletions, reason = _untracked_stats(root, raw_path, stats_budget)
                stats[raw_path] = (additions, deletions)
                if reason is not None:
                    unavailable[raw_path] = reason

    files = []
    for raw_path, code in records:
        entry: dict[str, object] = {
            "path": os.fsdecode(raw_path),
            "status": code,
            "additions": stats.get(raw_path, (0, 0))[0],
            "deletions": stats.get(raw_path, (0, 0))[1],
        }
        if raw_path in unavailable:
            entry["stats_unavailable"] = unavailable[raw_path]
            if selected is None:
                entry["diff_omitted"] = True
        files.append(entry)

    patch = bytearray()
    truncated = False
    if has_head:
        truncated = _append_diff(
            root,
            [
                "-c",
                "core.quotePath=true",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                "--",
                *path_args,
            ],
            patch,
        )
        extra_paths = untracked
    else:
        extra_paths = [raw for raw, _ in records]

    if not truncated:
        for raw_path in extra_paths:
            if selected is None and raw_path in unavailable:
                continue
            if _safe_regular_file(root, raw_path) is None:
                continue
            if _append_diff(
                root,
                [
                    "-c",
                    "core.quotePath=true",
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-index",
                    "--",
                    "/dev/null",
                    os.fsdecode(raw_path),
                ],
                patch,
                ok_returncodes=(0, 1),
            ):
                truncated = True
                break

    return {
        "branch": _branch(root),
        "files": files,
        "diff": bytes(patch).decode("utf-8", "replace"),
        "truncated": truncated,
    }


async def get_diff(cwd: str, path: str | None = None) -> dict[str, object]:
    """Return bounded staged, unstaged, and untracked changes for a checkout."""
    return await asyncio.to_thread(_get_diff, cwd, path)
