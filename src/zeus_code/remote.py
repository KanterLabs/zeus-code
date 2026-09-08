"""Install and start the matching Zeus Code runtime over SSH."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import sys
from collections.abc import Callable, Sequence
import zipfile

from .client import _validate_host


MAX_APPLICATION_BYTES = 64 * 1024 * 1024
MAX_SSH_OUTPUT_BYTES = 64 * 1024
SSH_CONNECT_TIMEOUT = 8
SSH_CLEANUP_TIMEOUT = 2.0
CHECK_TIMEOUT = 15.0
INSTALL_TIMEOUT = 60.0
START_TIMEOUT = 20.0

_MAIN = b"from zeus_code.cli import main\nraise SystemExit(main())\n"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_ZIP_MODE = (stat.S_IFREG | 0o644) << 16

_CHECK_SCRIPT = r'''import json, sys
if sys.version_info < (3, 11):
    print("Zeus Code requires Python 3.11 or newer on the remote machine.", file=sys.stderr)
    raise SystemExit(3)
try:
    import curses
except ImportError:
    print("Remote Python must include the curses module.", file=sys.stderr)
    raise SystemExit(4)
print(json.dumps({"ok": True}, separators=(",", ":")))
'''

_INSTALL_SCRIPT = r'''import hashlib, json, os, stat, sys, tempfile
from pathlib import Path

expected = sys.argv[1]
expected_size = int(sys.argv[2])
runtime_dir = Path.home() / ".local" / "share" / "zeus-code" / "runtimes" / expected
target = runtime_dir / "zeus-code.pyz"
temporary = None

def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                return None
            digest.update(chunk)
    return digest.hexdigest() if size == expected_size else None

try:
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        mode = target.lstat().st_mode
        if not stat.S_ISREG(mode) or digest_file(target) != expected:
            raise RuntimeError("existing content-addressed runtime has a checksum mismatch")
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".zeus-code-", dir=runtime_dir)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        received = 0
        with os.fdopen(descriptor, "wb") as stream:
            while True:
                chunk = sys.stdin.buffer.read(min(65536, expected_size + 1 - received))
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size:
                    raise RuntimeError("received application exceeds its declared size")
                digest.update(chunk)
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if received != expected_size:
            raise RuntimeError("application transfer ended before its declared size")
        if digest.hexdigest() != expected:
            raise RuntimeError("application checksum mismatch after transfer")
        os.chmod(temporary, 0o755)
        try:
            os.link(temporary, target)
        except FileExistsError:
            mode = target.lstat().st_mode
            if not stat.S_ISREG(mode) or digest_file(target) != expected:
                raise RuntimeError("existing content-addressed runtime has a checksum mismatch")
    os.chmod(target, 0o755)
    print(json.dumps({"remote_command": str(target.absolute())}, separators=(",", ":")))
except Exception as exc:
    print("Zeus Code installation failed: " + str(exc), file=sys.stderr)
    raise SystemExit(1)
finally:
    if temporary is not None:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
'''


def _shell_command(arguments: Sequence[str]) -> str:
    """Quote a fixed argv vector for the remote login shell."""

    return " ".join(shlex.quote(argument) for argument in arguments)


def _ssh_argv(host: str, command: str) -> tuple[str, ...]:
    return (
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "--",
        host,
        command,
    )


def _zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = _ZIP_MODE
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _source_zipapp() -> bytes:
    source = Path(__file__).resolve().parent
    entries: list[tuple[str, bytes]] = [("__main__.py", _MAIN)]
    source_bytes = len(_MAIN)
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise RuntimeError(f"Cannot provision a symbolic link in the application source: {path}")
        if not path.is_file():
            continue
        data = path.read_bytes()
        source_bytes += len(data)
        if source_bytes > MAX_APPLICATION_BYTES:
            raise RuntimeError(
                f"Zeus Code application sources exceed the {MAX_APPLICATION_BYTES}-byte transfer limit"
            )
        entries.append((f"zeus_code/{relative.as_posix()}", data))

    output = io.BytesIO()
    output.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in entries:
            _zip_entry(archive, name, data)
    application = output.getvalue()
    if len(application) > MAX_APPLICATION_BYTES:
        raise RuntimeError(
            f"Zeus Code application exceeds the {MAX_APPLICATION_BYTES}-byte transfer limit"
        )
    return application


def _application_bytes() -> bytes:
    """Read the running zipapp, or build an equivalent one from package sources."""

    candidate = Path(sys.argv[0]).expanduser()
    with contextlib.suppress(OSError):
        if candidate.is_file() and candidate.stat().st_size <= MAX_APPLICATION_BYTES:
            data = candidate.read_bytes()
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    names = set(archive.namelist())
                if {"__main__.py", "zeus_code/cli.py"}.issubset(names):
                    return data
            except zipfile.BadZipFile:
                pass
    return _source_zipapp()


async def _read_bounded(
    stream: asyncio.StreamReader, *, label: str
) -> bytes:
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = await stream.read(min(65536, MAX_SSH_OUTPUT_BYTES + 1 - received))
        if not chunk:
            return b"".join(chunks)
        received += len(chunk)
        if received > MAX_SSH_OUTPUT_BYTES:
            raise RuntimeError(
                f"{label} produced more than {MAX_SSH_OUTPUT_BYTES} bytes of output"
            )
        chunks.append(chunk)


async def _feed_stdin(writer: asyncio.StreamWriter, payload: bytes) -> None:
    try:
        for offset in range(0, len(payload), 65536):
            writer.write(payload[offset : offset + 65536])
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await writer.wait_closed()


async def _communicate_bounded(
    process: asyncio.subprocess.Process, payload: bytes, label: str
) -> tuple[bytes, bytes]:
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    input_task = asyncio.create_task(_feed_stdin(process.stdin, payload))
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, label=label))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, label=label))
    wait_task = asyncio.create_task(process.wait())
    tasks = (input_task, stdout_task, stderr_task, wait_task)
    try:
        _, stdout, stderr, _ = await asyncio.gather(*tasks)
        return stdout, stderr
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None and not process.stdin.is_closing():
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), SSH_CLEANUP_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def _diagnostic(stderr: bytes) -> str:
    detail = " ".join(stderr.decode("utf-8", errors="replace").split())
    if len(detail) > 1000:
        return detail[:997] + "..."
    return detail


async def _run_ssh(
    host: str,
    command: str,
    *,
    payload: bytes = b"",
    timeout: float,
    label: str,
) -> bytes:
    process: asyncio.subprocess.Process | None = None
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *_ssh_argv(host, command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_SSH_OUTPUT_BYTES + 1,
        )
    )
    try:
        process = await asyncio.shield(launch)
        try:
            stdout, stderr = await asyncio.wait_for(
                _communicate_bounded(process, payload, label), timeout
            )
        except asyncio.TimeoutError as exc:
            await asyncio.shield(_terminate(process))
            raise TimeoutError(f"{label} timed out on {host}") from exc
        except BaseException:
            await asyncio.shield(_terminate(process))
            raise
    except asyncio.CancelledError:
        if process is None:
            with contextlib.suppress(BaseException):
                process = await asyncio.shield(launch)
        if process is not None:
            await asyncio.shield(_terminate(process))
        raise
    except FileNotFoundError as exc:
        raise RuntimeError("The local ssh command is not installed or is not on PATH") from exc

    assert process is not None
    if process.returncode:
        detail = _diagnostic(stderr)
        if process.returncode == 255:
            message = f"Could not connect to {host} with SSH"
            if detail:
                message += f": {detail}"
            message += ". Verify the SSH alias and non-interactive SSH authentication."
            raise RuntimeError(message)
        message = f"{label} failed on {host}"
        if detail:
            message += f": {detail}"
        else:
            message += f" (ssh exited with status {process.returncode})"
        raise RuntimeError(message)
    return stdout


def _json_result(output: bytes, label: str) -> dict[str, object]:
    try:
        result = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} returned an invalid response") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{label} returned an invalid response")
    return result


async def provision_remote(
    host: str, *, on_progress: Callable[[str], None] | None = None
) -> dict[str, str]:
    """Install this Zeus Code build on ``host`` and start its daemon."""

    host = _validate_host(host)

    def progress(stage: str) -> None:
        if on_progress is not None:
            on_progress(stage)

    progress("Checking SSH")
    check_output = await _run_ssh(
        host,
        _shell_command(("python3", "-c", _CHECK_SCRIPT)),
        timeout=CHECK_TIMEOUT,
        label="Remote compatibility check",
    )
    check = _json_result(check_output, "Remote compatibility check")
    if check.get("ok") is not True:
        raise RuntimeError("Remote compatibility check returned an invalid response")

    progress("Installing Zeus Code")
    application = await asyncio.to_thread(_application_bytes)
    if len(application) > MAX_APPLICATION_BYTES:
        raise RuntimeError(
            f"Zeus Code application exceeds the {MAX_APPLICATION_BYTES}-byte transfer limit"
        )
    digest = hashlib.sha256(application).hexdigest()
    install_output = await _run_ssh(
        host,
        _shell_command(("python3", "-c", _INSTALL_SCRIPT, digest, str(len(application)))),
        payload=application,
        timeout=INSTALL_TIMEOUT,
        label="Zeus Code installation",
    )
    install = _json_result(install_output, "Zeus Code installation")
    remote_command = install.get("remote_command")
    expected_suffix = ("runtimes", digest, "zeus-code.pyz")
    if (
        not isinstance(remote_command, str)
        or not remote_command
        or "\x00" in remote_command
        or "\n" in remote_command
        or "\r" in remote_command
        or not PurePosixPath(remote_command).is_absolute()
        or PurePosixPath(remote_command).parts[-3:] != expected_suffix
    ):
        raise RuntimeError("Zeus Code installation returned an invalid runtime path")

    progress("Starting server")
    await _run_ssh(
        host,
        _shell_command((remote_command, "serve", "--background")),
        timeout=START_TIMEOUT,
        label="Zeus Code server startup",
    )
    return {"remote_command": remote_command}
