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
from collections.abc import Callable, Mapping, Sequence
import zipfile

from . import PROTOCOL_VERSION, __version__
from .client import _validate_host


MAX_APPLICATION_BYTES = 64 * 1024 * 1024
MAX_SSH_OUTPUT_BYTES = 64 * 1024
SSH_CONNECT_TIMEOUT = 8
SSH_CLEANUP_TIMEOUT = 2.0
CHECK_TIMEOUT = 15.0
INSTALL_TIMEOUT = 60.0
START_TIMEOUT = 20.0
VERIFY_TIMEOUT = 20.0

_MAIN = b"from zeus_code.cli import main\nraise SystemExit(main())\n"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_ZIP_MODE = (stat.S_IFREG | 0o644) << 16


class RemoteSetupError(RuntimeError):
    """An actionable, machine-readable remote onboarding failure."""

    def __init__(
        self,
        summary: str,
        *,
        stage: str,
        code: str,
        host: str,
        action: str,
        detail: str = "",
    ) -> None:
        message = summary.rstrip(". ")
        if detail:
            message += ": " + detail.rstrip(". ")
        if action:
            message += ". " + action.rstrip(". ")
        super().__init__(message + ".")
        self.stage = stage
        self.code = code
        # ``category`` is a friendlier name for callers displaying grouped
        # remedies, while ``code`` remains convenient for programmatic checks.
        self.category = code
        self.host = host
        self.action = action
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {
            "message": str(self),
            "stage": self.stage,
            "code": self.code,
            "category": self.category,
            "host": self.host,
            "action": self.action,
            "detail": self.detail,
        }

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


def _stage_for_label(label: str) -> str:
    if label == "Remote compatibility check":
        return "requirements"
    if label == "Zeus Code installation":
        return "install"
    if label == "Zeus Code server startup":
        return "start"
    if label == "Remote connection verification":
        return "verify"
    return "ssh"


def _ssh_failure(host: str, detail: str) -> RemoteSetupError:
    """Classify OpenSSH diagnostics without changing SSH configuration."""

    lowered = detail.casefold()
    fixtures = (
        (
            "ssh_authentication",
            ("permission denied", "too many authentication failures",
             "no supported authentication methods", "sign_and_send_pubkey"),
            "SSH authentication failed for {host}",
            "Run `ssh {host}` and configure an SSH key or agent that works without a password prompt",
        ),
        (
            "ssh_host_key",
            ("host key verification failed", "remote host identification has changed", "offending key"),
            "SSH host-key verification failed for {host}",
            "Run `ssh {host}`, verify the fingerprint, and repair its known_hosts entry before retrying",
        ),
        (
            "ssh_hostname",
            ("could not resolve hostname", "name or service not known",
             "nodename nor servname provided", "temporary failure in name resolution"),
            "SSH could not resolve {host}",
            "Check the destination spelling, DNS, and its Host entry in ~/.ssh/config",
        ),
        (
            "ssh_refused",
            ("connection refused",),
            "SSH connection to {host} was refused",
            "Confirm the SSH server is running and its port and firewall allow access",
        ),
        (
            "ssh_network",
            ("connection timed out", "operation timed out", "no route to host", "network is unreachable"),
            "SSH could not reach {host}",
            "Check the network or VPN, then confirm SSH is allowed through the firewall",
        ),
        (
            "ssh_proxy",
            ("proxyjump", "proxycommand", "stdio forwarding failed"),
            "SSH proxy setup failed for {host}",
            "Run `ssh -v {host}` and correct ProxyJump or ProxyCommand in ~/.ssh/config",
        ),
    )
    for code, phrases, summary, action in fixtures:
        if any(phrase in lowered for phrase in phrases):
            return RemoteSetupError(
                summary.format(host=host),
                stage="ssh",
                code=code,
                host=host,
                detail=detail,
                action=action.format(host=host),
            )
    return RemoteSetupError(
        f"Could not connect to {host} with SSH",
        stage="ssh",
        code="ssh_connection",
        host=host,
        detail=detail,
        action=(
            f"Run `ssh {host}` and make sure the alias and non-interactive authentication work "
            "before retrying"
        ),
    )


def _remote_command_failure(
    host: str, label: str, returncode: int, detail: str
) -> RemoteSetupError:
    if returncode == 255:
        return _ssh_failure(host, detail)

    lowered = detail.casefold()
    stage = _stage_for_label(label)
    if label == "Remote compatibility check":
        if returncode == 127 or any(
            phrase in lowered
            for phrase in ("python3: command not found", "python3: not found", "no such file or directory: 'python3'")
        ):
            return RemoteSetupError(
                f"Python 3 is not available on {host}",
                stage=stage,
                code="remote_python_missing",
                host=host,
                detail=detail,
                action="Install Python 3.11 or newer with the curses module on the remote machine",
            )
        if returncode == 3 or "python 3.11 or newer" in lowered:
            return RemoteSetupError(
                f"The Python version on {host} is too old for Zeus Code",
                stage=stage,
                code="remote_python_version",
                host=host,
                detail=detail,
                action="Install Python 3.11 or newer on the remote machine and ensure `python3` selects it",
            )
        if returncode == 4 or "curses module" in lowered:
            return RemoteSetupError(
                f"Python on {host} does not include the curses module",
                stage=stage,
                code="remote_python_curses",
                host=host,
                detail=detail,
                action="Install the operating system package that provides Python curses, then retry",
            )
        action = f"Run `ssh {host} python3 --version` and fix the reported remote Python error"
        code = "remote_requirements"
    elif label == "Zeus Code installation":
        if "no space left on device" in lowered:
            action = "Free disk space under ~/.local/share on the remote machine, then retry"
            code = "remote_disk_full"
        elif "permission denied" in lowered or "read-only file system" in lowered:
            action = "Make ~/.local/share/zeus-code writable by the remote user, then retry"
            code = "remote_install_permission"
        else:
            action = "Check remote disk space and permissions for ~/.local/share/zeus-code, then retry"
            code = "remote_install"
    elif label == "Zeus Code server startup":
        action = (
            "Inspect ~/.local/share/zeus-code/server.log on the remote machine and correct the "
            "reported startup error"
        )
        code = "remote_start"
    elif label == "Remote connection verification":
        if "version_mismatch" in lowered or (
            "protocol" in lowered
            and any(phrase in lowered for phrase in ("incompatible", "mismatch", "required"))
        ):
            return _protocol_mismatch_error(host, detail=detail)
        action = (
            "Inspect ~/.local/share/zeus-code/server.log on the remote machine, then retry; "
            "Zeus Code did not stop the daemon"
        )
        code = "remote_unhealthy"
    else:
        action = f"Run `ssh {host}` and retry the failed command after correcting the reported error"
        code = "remote_command"

    summary = f"{label} failed on {host}"
    if not detail:
        detail = f"ssh exited with status {returncode}"
    return RemoteSetupError(
        summary,
        stage=stage,
        code=code,
        host=host,
        detail=detail,
        action=action,
    )


def _timeout_failure(host: str, label: str) -> RemoteSetupError:
    stage = _stage_for_label(label)
    if stage in {"ssh", "requirements"}:
        summary = f"SSH connection to {host} timed out"
        action = "Check the network or VPN and confirm that the SSH host is reachable"
        code = "ssh_timeout"
        stage = "ssh"
    elif stage == "install":
        summary = f"Installing Zeus Code on {host} timed out"
        action = "Check the SSH connection and free space under ~/.local/share, then retry"
        code = "remote_install_timeout"
    elif stage == "start":
        summary = f"Starting Zeus Code on {host} timed out"
        action = "Inspect ~/.local/share/zeus-code/server.log on the remote machine, then retry"
        code = "remote_start_timeout"
    else:
        summary = f"Verifying Zeus Code on {host} timed out"
        action = (
            "Check the SSH connection and remote server log, then retry; Zeus Code did not stop the daemon"
        )
        code = "remote_verify_timeout"
    return RemoteSetupError(
        summary,
        stage=stage,
        code=code,
        host=host,
        action=action,
    )


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
            raise _timeout_failure(host, label) from exc
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
        raise RemoteSetupError(
            "The local ssh command is not installed or is not on PATH",
            stage="ssh",
            code="ssh_unavailable",
            host=host,
            action="Install an OpenSSH client and ensure its `ssh` command is on PATH",
        ) from exc

    assert process is not None
    if process.returncode:
        detail = _diagnostic(stderr)
        raise _remote_command_failure(host, label, process.returncode, detail)
    return stdout


def _json_result(output: bytes, label: str) -> dict[str, object]:
    try:
        result = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} returned an invalid response") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{label} returned an invalid response")
    return result


def _protocol_mismatch_error(
    host: str,
    *,
    server_protocol: object | None = None,
    detail: str = "",
) -> RemoteSetupError:
    observed = detail
    if not observed:
        observed = (
            f"running daemon protocol {server_protocol!r}; installed runtime protocol "
            f"{PROTOCOL_VERSION}"
        )
    return RemoteSetupError(
        f"The running Zeus Code daemon on {host} uses an incompatible protocol",
        stage="verify",
        code="protocol_mismatch",
        host=host,
        detail=observed,
        action=(
            "Finish its active work, stop it using its original Zeus Code installation, and retry; "
            "this setup left the active daemon running"
        ),
    )


def _provider_setup_status(
    host: str, providers: Mapping[str, object]
) -> dict[str, dict[str, object]]:
    """Add setup guidance using only evidence returned by provider discovery."""

    result: dict[str, dict[str, object]] = {}
    login_commands = {"codex": "codex login", "opencode": "opencode auth login"}
    display_names = {"codex": "Codex", "opencode": "OpenCode"}
    for name, raw in providers.items():
        entry = dict(raw) if isinstance(raw, Mapping) else {
            "available": False,
            "status": "unavailable",
            "detail": "Provider discovery returned an invalid status.",
        }
        available = entry.get("available") is True
        detail = entry.get("detail")
        if not isinstance(detail, str):
            detail = "Provider is ready." if available else "Provider setup is unavailable."
            entry["detail"] = detail
        lowered = detail.casefold()

        if available or (isinstance(entry.get("version"), str) and entry["version"]):
            installation = "installed"
        elif any(
            phrase in lowered
            for phrase in ("not installed", "was not found", "is not on path", "not on path")
        ):
            installation = "missing"
        else:
            installation = "unknown"

        if any(
            phrase in lowered
            for phrase in ("not authenticated", "no provider is authenticated", "auth login", "codex login")
        ):
            authentication = "required"
        elif "does not require" in lowered and "auth" in lowered:
            authentication = "not_required"
        elif available and any(
            phrase in lowered for phrase in ("authenticated with", "connected providers")
        ):
            authentication = "authenticated"
        else:
            authentication = "unknown"

        entry["installation_status"] = installation
        entry["authentication_status"] = authentication
        status = entry.get("status")
        if status == "checking":
            entry["setup_action"] = "Provider discovery is still in progress; refresh server health shortly."
        elif installation == "missing":
            display = display_names.get(name.casefold(), name)
            entry["setup_action"] = (
                f"Install the {display} CLI on {host} and ensure it is available to non-interactive SSH sessions."
            )
        elif authentication == "required":
            command = login_commands.get(name.casefold())
            if command is not None:
                entry["setup_action"] = f"Run `ssh {host}`, then run `{command}` on the remote machine."
            else:
                entry["setup_action"] = f"Authenticate {name} on {host}, then refresh provider health."
        elif not available:
            entry["setup_action"] = f"Check the {name} provider setup on {host}, then refresh provider health."
        result[str(name)] = entry
    return result


def _connection_health(
    host: str, output: bytes
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    try:
        doctor = _json_result(output, "Remote connection verification")
    except RuntimeError as exc:
        raise RemoteSetupError(
            f"Zeus Code on {host} returned an invalid health response",
            stage="verify",
            code="remote_health_response",
            host=host,
            action=(
                "Inspect ~/.local/share/zeus-code/server.log on the remote machine, then retry; "
                "Zeus Code did not stop the daemon"
            ),
        ) from exc

    server = doctor.get("server")
    providers = doctor.get("providers")
    if not isinstance(server, Mapping) or not isinstance(providers, Mapping):
        raise RemoteSetupError(
            f"Zeus Code on {host} returned incomplete health metadata",
            stage="verify",
            code="remote_health_response",
            host=host,
            action="Check the installed Zeus Code version and remote server log, then retry",
        )

    server_version = server.get("version")
    server_protocol = server.get("protocol_version")
    if not isinstance(server_version, str) or not server_version.strip():
        raise RemoteSetupError(
            f"Zeus Code on {host} returned health metadata without a version",
            stage="verify",
            code="remote_health_response",
            host=host,
            action="Check the installed Zeus Code version and remote server log, then retry",
        )
    if (
        isinstance(server_protocol, bool)
        or not isinstance(server_protocol, int)
        or server_protocol != PROTOCOL_VERSION
    ):
        raise _protocol_mismatch_error(
            host,
            server_protocol=server_protocol,
        )
    server_id = server.get("server_id")
    if not isinstance(server_id, str) or not server_id.strip():
        raise RemoteSetupError(
            f"Zeus Code on {host} returned health metadata without a server identity",
            stage="verify",
            code="remote_health_response",
            host=host,
            action="Inspect the remote server log and retry setup",
        )

    reported_client_version = doctor.get("client_version")
    client_version = (
        reported_client_version
        if isinstance(reported_client_version, str) and reported_client_version.strip()
        else __version__
    )
    version_match = server_version == client_version
    health: dict[str, object] = {
        "status": "ready",
        "host": host,
        "version": server_version,
        "server_version": server_version,
        "client_version": client_version,
        "protocol_version": server_protocol,
        "server_id": server_id,
        "version_match": version_match,
        "upgrade_status": "current" if version_match else "deferred",
    }
    if not version_match:
        health["note"] = (
            f"The compatible {server_version} daemon remains active; the installed runtime "
            f"is {client_version}. A server runtime change is deferred until its active work "
            "is finished and you explicitly restart it."
        )
    hostname = server.get("hostname")
    if isinstance(hostname, str) and hostname:
        health["hostname"] = hostname
    pid = server.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        health["pid"] = pid
    return health, _provider_setup_status(host, providers)


async def provision_remote(
    host: str,
    *,
    on_progress: Callable[[str], None] | None = None,
    include_health: bool = False,
) -> dict[str, object]:
    """Install this Zeus Code build on ``host`` and start its daemon.

    ``include_health`` adds a read-only daemon/provider readiness check and
    metadata for onboarding UIs. It is opt-in so callers that only need the
    immutable runtime path retain the original three-step contract.
    """

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
    try:
        check = _json_result(check_output, "Remote compatibility check")
    except RuntimeError as exc:
        raise RemoteSetupError(
            f"The remote requirements check on {host} returned an invalid response",
            stage="requirements",
            code="remote_requirements_response",
            host=host,
            action="Run `python3 --version` on the remote machine and ensure it is Python 3.11 or newer",
        ) from exc
    if check.get("ok") is not True:
        raise RemoteSetupError(
            f"The remote requirements check on {host} did not report success",
            stage="requirements",
            code="remote_requirements_response",
            host=host,
            action="Run `python3 --version` on the remote machine and ensure Python includes curses",
        )

    if include_health:
        progress("Preparing Zeus Code")
    application = await asyncio.to_thread(_application_bytes)
    if len(application) > MAX_APPLICATION_BYTES:
        raise RuntimeError(
            f"Zeus Code application exceeds the {MAX_APPLICATION_BYTES}-byte transfer limit"
        )
    progress("Installing Zeus Code")
    digest = hashlib.sha256(application).hexdigest()
    install_output = await _run_ssh(
        host,
        _shell_command(("python3", "-c", _INSTALL_SCRIPT, digest, str(len(application)))),
        payload=application,
        timeout=INSTALL_TIMEOUT,
        label="Zeus Code installation",
    )
    try:
        install = _json_result(install_output, "Zeus Code installation")
    except RuntimeError as exc:
        raise RemoteSetupError(
            f"Zeus Code installation on {host} returned an invalid response",
            stage="install",
            code="remote_install_response",
            host=host,
            action="Check remote disk space and permissions for ~/.local/share/zeus-code, then retry",
        ) from exc
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
        raise RemoteSetupError(
            f"Zeus Code installation on {host} returned an invalid runtime path",
            stage="install",
            code="remote_install_response",
            host=host,
            action="Check ~/.local/share/zeus-code on the remote machine, then retry",
        )

    progress("Starting server")
    await _run_ssh(
        host,
        _shell_command((remote_command, "serve", "--background")),
        timeout=START_TIMEOUT,
        label="Zeus Code server startup",
    )
    result: dict[str, object] = {"remote_command": remote_command}
    if include_health:
        progress("Verifying connection")
        health_output = await _run_ssh(
            host,
            _shell_command((remote_command, "doctor")),
            timeout=VERIFY_TIMEOUT,
            label="Remote connection verification",
        )
        health, providers = _connection_health(host, health_output)
        result.update(health=health, providers=providers)
        progress("Ready")
    return result
