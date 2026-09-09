"""Command-line entry point for Zeus Code."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .client import RPCClient, RPCError, bridge
from .paths import default_data_dir, secure_directory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zeus-code", description="Persistent local and remote coding conversations")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(), metavar="PATH")
    parser.add_argument("--host", metavar="SSH_ALIAS", help="run the RPC command through this SSH host alias")
    commands = parser.add_subparsers(dest="command")

    serve_parser = commands.add_parser("serve", help="run the local daemon")
    serve_parser.add_argument("--background", action="store_true", help="detach and write output to the state log")
    connect_parser = commands.add_parser("connect", help="open the TUI, initially connected to an SSH host")
    connect_parser.add_argument("ssh_alias", nargs="?")
    connect_parser.add_argument("--projects", metavar="REMOTE_FOLDER", help="set up the SSH server and import this remote projects folder before opening")
    remote = commands.add_parser("remote", help="connect a dev server").add_subparsers(dest="remote_command", required=True)
    remote_add = remote.add_parser("add", help="install/start Zeus over SSH and import remote repositories")
    remote_add.add_argument("ssh_alias")
    remote_add.add_argument("--projects", default="~/projects", metavar="REMOTE_FOLDER")
    remote_add.add_argument("--name", default="", help="display name for the server")
    commands.add_parser("status", help="show daemon status")
    commands.add_parser("stop", help="gracefully stop the daemon")
    commands.add_parser("bridge", help="relay SSH transport (internal)")
    commands.add_parser("doctor", help="check daemon and provider availability")
    update_parser = commands.add_parser("update", help="install the latest stable GitHub release")
    update_parser.add_argument("--check", action="store_true", help="check for a release without installing")
    update_parser.add_argument("--install-dir", type=Path, metavar="DIR",
                               help="install as DIR/zeus-code instead of updating the current bundle")

    project = commands.add_parser("project", help="manage projects").add_subparsers(dest="project_command", required=True)
    project_add = project.add_parser("add", help="register a repository")
    project_add.add_argument("path", type=Path)
    project_add.add_argument("--name")

    thread = commands.add_parser("thread", help="manage threads").add_subparsers(dest="thread_command", required=True)
    thread_create = thread.add_parser("create", help="create a provider thread")
    thread_create.add_argument("project_id")
    thread_create.add_argument("title")
    thread_create.add_argument("--provider", required=True, choices=("codex", "opencode"))
    thread_create.add_argument("--model")
    thread_create.add_argument("--worktree", action="store_true")

    send = commands.add_parser("send", help="send a prompt to a thread")
    send.add_argument("thread_id")
    send.add_argument("prompt")
    send.add_argument("--request-id", type=_uuid, default=None, metavar="UUID")
    cancel = commands.add_parser("cancel", help="cancel the current run in one thread")
    cancel.add_argument("thread_id")
    events = commands.add_parser("events", help="read persisted events")
    events.add_argument("--after", type=_nonnegative_int, default=0, metavar="N")
    events.add_argument("--thread", dest="thread_id", metavar="ID")
    diff = commands.add_parser("diff", help="show checkout changes for a thread")
    diff.add_argument("thread_id")
    diff.add_argument("--path")
    approve = commands.add_parser("approve", help="answer one approval request")
    approve.add_argument("request_id")
    approve.add_argument("decision", choices=("allow", "reject"))
    backup = commands.add_parser("backup", help="create a consistent database backup")
    backup.add_argument("target", type=Path)
    return parser


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _normalize_global_options(argv: Sequence[str]) -> list[str]:
    """Permit --data-dir/--host before or after nested subcommands."""
    globals_: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            rest.extend(argv[i:])
            break
        if arg in {"--data-dir", "--host"}:
            if i + 1 >= len(argv):
                rest.append(arg)
            else:
                globals_.extend((arg, argv[i + 1]))
                i += 1
        elif arg.startswith("--data-dir=") or arg.startswith("--host="):
            globals_.append(arg)
        else:
            rest.append(arg)
        i += 1
    return globals_ + rest


def _print_result(result: Any) -> None:
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False, default=str))


def _configured_client(args: argparse.Namespace) -> RPCClient:
    if args.host:
        from .workspace import CacheStore

        machine = next((m for m in CacheStore().load()["machines"].values() if m.get("host") == args.host or m.get("alias") == args.host), None)
        if machine and machine.get("remote_command"):
            return RPCClient(args.data_dir, host=machine["host"], remote_command=machine["remote_command"])
    return RPCClient(args.data_dir, host=args.host)


async def _rpc(args: argparse.Namespace, method: str, params: dict[str, Any] | None = None) -> Any:
    async with _configured_client(args) as client:
        return await client.call(method, params)


async def _background_serve(data_dir: Path) -> int:
    try:
        async with RPCClient(data_dir) as client:
            hello = client.hello or {}
            print(f"Zeus Code is already running (pid {hello.get('pid', 'unknown')}).")
            return 0
    except OSError:
        pass
    except (ConnectionError, asyncio.TimeoutError) as exc:
        print(f"zeus-code: an existing server did not complete protocol negotiation: {exc}", file=sys.stderr)
        return 1

    data_dir = secure_directory(data_dir)
    log_path = data_dir / "server.log"
    if Path(sys.argv[0]).suffix in {".pyz", ".pyzw"}:
        launcher = [sys.executable, str(Path(sys.argv[0]).resolve())]
        child_env = None
    else:
        launcher = [sys.executable, "-m", "zeus_code"]
        # A source-checkout launcher modifies only its own sys.path. Propagate
        # the package root so its detached child can import zeus_code too.
        child_env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[1])
        current_pythonpath = child_env.get("PYTHONPATH")
        child_env["PYTHONPATH"] = (
            source_root if not current_pythonpath else source_root + os.pathsep + current_pythonpath
        )
    command = [*launcher, "--data-dir", str(data_dir), "serve"]
    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=os.getcwd(),
            env=child_env,
        )

    deadline = time.monotonic() + 10.0
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            async with RPCClient(data_dir) as client:
                hello = client.hello or {}
                print(f"Zeus Code is running in the background (pid {hello.get('pid', process.pid)}).")
                print(f"Log: {log_path}")
                return 0
        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            last_error = exc
            if process.poll() is not None:
                break
            await asyncio.sleep(0.1)
    detail = f": {last_error}" if last_error else ""
    print(f"zeus-code: background server failed to become ready{detail}. See {log_path}", file=sys.stderr)
    return 1


async def _run_async(args: argparse.Namespace) -> int:
    command = args.command
    if command == "remote":
        if args.host:
            raise ValueError("Use remote add SSH_ALIAS, without --host")
        from .workspace import Workspace

        workspace = Workspace(data_dir=args.data_dir)
        try:
            result = await workspace.connect_remote(args.ssh_alias, args.projects, alias=args.name,
                on_progress=lambda message: print(message, file=sys.stderr, flush=True))
            print(json.dumps({"server": result["machine"]["alias"], "projects": result["projects"],
                              "imported": result["imported"], "warnings": result["warnings"],
                              "truncated": result["truncated"]}, indent=2))
        finally:
            await workspace.close()
        return 0
    if command == "update":
        if args.host:
            raise ValueError("update is local-only; run it on the machine you want to update")
        from .updater import update

        return update(args.data_dir, check_only=args.check, install_dir=args.install_dir)
    if command == "serve":
        if args.host:
            raise ValueError("--host cannot be used with serve")
        if args.background:
            return await _background_serve(args.data_dir)
        from .daemon import serve

        result = await serve(args.data_dir)
        return int(result or 0)
    if command == "bridge":
        if args.host:
            raise ValueError("--host cannot be used with bridge")
        await bridge(args.data_dir)
        return 0
    if command == "status":
        result = await _rpc(args, "snapshot")
    elif command == "stop":
        result = await _rpc(args, "stop")
    elif command == "doctor":
        async with _configured_client(args) as client:
            result = {"client_version": __version__, "server": client.hello,
                      "providers": await client.call("providers")}
    elif command == "project" and args.project_command == "add":
        project_path = str(args.path) if args.host is not None else str(args.path.expanduser().resolve())
        params: dict[str, Any] = {"path": project_path}
        if args.name is not None:
            params["name"] = args.name
        result = await _rpc(args, "add_project", params)
    elif command == "thread" and args.thread_command == "create":
        params = {
            "project_id": args.project_id,
            "title": args.title,
            "provider": args.provider,
            "worktree": args.worktree,
        }
        if args.model is not None:
            params["model"] = args.model
        result = await _rpc(args, "create_thread", params)
    elif command == "send":
        if args.request_id is None:
            args.request_id = str(uuid.uuid4())
        result = await _rpc(
            args,
            "send",
            {
                "thread_id": args.thread_id,
                "prompt": args.prompt,
                "request_id": args.request_id,
            },
        )
    elif command == "cancel":
        result = await _rpc(args, "cancel", {"thread_id": args.thread_id})
    elif command == "events":
        params = {"after": args.after}
        if args.thread_id is not None:
            params["thread_id"] = args.thread_id
        result = await _rpc(args, "events", params)
    elif command == "diff":
        params = {"thread_id": args.thread_id}
        if args.path is not None:
            params["path"] = args.path
        result = await _rpc(args, "diff", params)
    elif command == "approve":
        result = await _rpc(args, "approve", {"request_id": args.request_id, "decision": args.decision})
    elif command == "backup":
        if args.host:
            raise ValueError("backup is local-only; omit --host")
        from .storage import Store

        store = Store(args.data_dir)
        try:
            result = store.backup(args.target.expanduser().resolve())
        finally:
            store.close()
        if result is None:
            result = {"backup": str(args.target.expanduser().resolve())}
    else:
        raise RuntimeError(f"unhandled command: {command}")
    _print_result(result)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(_normalize_global_options(list(sys.argv[1:] if argv is None else argv)))
    args.data_dir = args.data_dir.expanduser().resolve()
    try:
        if args.command is None or args.command == "connect":
            initial_host = args.host
            if args.command == "connect" and args.ssh_alias is not None:
                if initial_host is not None and initial_host != args.ssh_alias:
                    parser.error("connect SSH_ALIAS conflicts with --host")
                initial_host = args.ssh_alias
            from .tui import run_tui

            if args.command == "connect" and args.projects is not None:
                if not initial_host:
                    parser.error("connect --projects requires an SSH destination")
                from .workspace import Workspace

                async def setup_remote() -> None:
                    workspace = Workspace(data_dir=args.data_dir)
                    try:
                        await workspace.connect_remote(initial_host, args.projects,
                            on_progress=lambda message: print(message, file=sys.stderr, flush=True))
                    finally:
                        await workspace.close()

                asyncio.run(setup_remote())

            result = run_tui(data_dir=args.data_dir, initial_host=initial_host)
            return int(result or 0)
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        return 130
    except RPCError as exc:
        print(f"zeus-code: {exc.message} (RPC {exc.code})", file=sys.stderr)
        return 1
    except (OSError, ConnectionError, RuntimeError, ValueError, asyncio.TimeoutError) as exc:
        detail = str(exc) or type(exc).__name__
        if isinstance(exc, OSError) and args.host is None and args.command in {"status", "stop", "doctor", "project", "thread", "send", "cancel", "events", "diff", "approve"}:
            detail += ". Start a local server with 'zeus-code serve --background'."
        if args.command == "send" and args.request_id is not None:
            detail += (
                f" Retry with --request-id {args.request_id}; the daemon may already have accepted this prompt."
            )
        print(f"zeus-code: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
