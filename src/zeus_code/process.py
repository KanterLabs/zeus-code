"""Portable supervision for provider subprocess trees.

The daemon launches a small Python supervisor in its own session.  The actual
provider gets a second, dedicated session so the supervisor can signal and
reap the provider's complete process group without signalling itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Mapping
from typing import Any


_START_TIMEOUT = 5.0
_CLEANUP_TIMEOUT = 2.0

_SUPERVISOR_CODE = r"""
import json
import os
import signal
import subprocess
import sys
import time

expected_parent = int(sys.argv[1])
status_fd = int(sys.argv[2])
passed_fds = tuple(int(fd) for fd in sys.argv[3].split(",") if fd)
argv = sys.argv[4:]
stopping_signal = 0


def report(message):
    try:
        os.write(status_fd, json.dumps(message).encode("utf-8") + b"\n")
    except OSError:
        pass
    finally:
        try:
            os.close(status_fd)
        except OSError:
            pass


def request_stop(signum, frame):
    global stopping_signal
    if not stopping_signal:
        stopping_signal = signum


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def stop_group(child):
    pgid = child.pid
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        child.poll()
        if not group_exists(pgid):
            break
        time.sleep(0.02)
    if group_exists(pgid):
        signal_group(pgid, signal.SIGKILL)
    try:
        child.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def exit_like_child(returncode):
    if returncode >= 0:
        raise SystemExit(returncode)
    signum = -returncode
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    os._exit(128 + signum)


for handled_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(handled_signal, request_stop)

if os.getppid() != expected_parent:
    report({"ok": False, "strerror": "parent exited during supervisor startup"})
    raise SystemExit(1)

try:
    child = subprocess.Popen(
        argv,
        close_fds=True,
        pass_fds=passed_fds,
        start_new_session=True,
    )
except OSError as error:
    report(
        {
            "ok": False,
            "errno": error.errno,
            "strerror": error.strerror or str(error),
            "filename": error.filename,
        }
    )
    raise SystemExit(127)

report({"ok": True})

while True:
    returncode = child.poll()
    if returncode is not None:
        stop_group(child)
        exit_like_child(returncode)
    if stopping_signal or os.getppid() != expected_parent:
        stop_group(child)
        if stopping_signal:
            signal.signal(stopping_signal, signal.SIG_DFL)
            os.kill(os.getpid(), stopping_signal)
        raise SystemExit(1)
    time.sleep(0.05)
"""


async def _read_status(fd: int) -> bytes:
    """Read one short launch-status line without blocking the event loop."""

    loop = asyncio.get_running_loop()
    future: asyncio.Future[bytes] = loop.create_future()
    chunks = bytearray()

    def readable() -> None:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            if not future.done():
                future.set_exception(exc)
            return
        if chunk:
            chunks.extend(chunk)
            if b"\n" not in chunks:
                return
        if not future.done():
            future.set_result(bytes(chunks).split(b"\n", 1)[0])

    os.set_blocking(fd, False)
    loop.add_reader(fd, readable)
    try:
        return await future
    finally:
        loop.remove_reader(fd)
        os.close(fd)


async def _terminate_supervisor(process: asyncio.subprocess.Process) -> None:
    """Let the supervisor clean its provider group after an aborted spawn."""

    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), _CLEANUP_TIMEOUT)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def spawn_supervised(
    *argv: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Spawn ``argv`` under a parent-death and process-group supervisor.

    Keyword arguments follow :func:`asyncio.create_subprocess_exec`.  The
    wrapper and provider each start a new session; explicitly passing
    ``start_new_session=False`` is therefore rejected.  ``preexec_fn``,
    ``process_group``, and ``executable`` cannot safely preserve their usual
    meaning through the wrapper and are rejected.
    """

    if os.name != "posix":
        raise NotImplementedError("supervised subprocesses require POSIX process groups")
    if not argv:
        raise ValueError("spawn_supervised requires an executable")

    start_new_session = kwargs.pop("start_new_session", True)
    if start_new_session is not True:
        raise ValueError("supervised subprocesses require start_new_session=True")
    for unsupported in ("preexec_fn", "process_group", "executable"):
        value = kwargs.pop(unsupported, None)
        if value is not None:
            raise ValueError(f"{unsupported} is not supported for supervised subprocesses")

    inherited_fds = tuple(kwargs.pop("pass_fds", ()))
    status_read, status_write = os.pipe()
    process: asyncio.subprocess.Process | None = None
    launch: asyncio.Task[asyncio.subprocess.Process] | None = None
    try:
        os.set_inheritable(status_write, True)
        wrapper_pass_fds = (*inherited_fds, status_write)
        inherited_arg = ",".join(str(fd) for fd in inherited_fds)
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _SUPERVISOR_CODE,
                str(os.getpid()),
                str(status_write),
                inherited_arg,
                *argv,
                start_new_session=True,
                pass_fds=wrapper_pass_fds,
                **kwargs,
            )
        )
        process = await asyncio.shield(launch)
        os.close(status_write)
        status_write = -1
        status_fd = status_read
        status_read = -1
        try:
            raw_status = await asyncio.wait_for(
                _read_status(status_fd), _START_TIMEOUT
            )
        except TimeoutError as exc:
            raise OSError("timed out waiting for the subprocess supervisor") from exc
        try:
            status: Mapping[str, Any] = json.loads(raw_status)
        except (TypeError, ValueError) as exc:
            raise OSError("subprocess supervisor exited before launching the provider") from exc
        if not status.get("ok"):
            await asyncio.wait_for(process.wait(), _CLEANUP_TIMEOUT)
            error_number = status.get("errno")
            if not isinstance(error_number, int):
                raise OSError(str(status.get("strerror") or "provider launch failed"))
            raise OSError(
                error_number,
                str(status.get("strerror") or "provider launch failed"),
                status.get("filename"),
            )
        return process
    except asyncio.CancelledError:
        if process is None and launch is not None:
            try:
                process = await asyncio.shield(launch)
            except BaseException:
                process = None
        if process is not None:
            await asyncio.shield(_terminate_supervisor(process))
        raise
    except BaseException:
        if process is not None:
            await asyncio.shield(_terminate_supervisor(process))
        raise
    finally:
        if status_read >= 0:
            os.close(status_read)
        if status_write >= 0:
            os.close(status_write)
