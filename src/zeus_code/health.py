"""Explicit client release checks and updates, safe to call from curses."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

from . import __version__


async def check_latest_release() -> dict[str, object]:
    # Reuse the updater's stable-release and asset validation. No installation
    # or daemon operation occurs during a check.
    from .updater import _release, _stable_version

    release = await asyncio.to_thread(_release)
    return {
        "current_version": __version__,
        "latest_version": release.version,
        "update_available": _stable_version(release.version, label="Latest release")
        > _stable_version(__version__, label="Current client"),
    }


def _update_command(data_dir: Path) -> tuple[list[str], dict[str, str]]:
    executable = Path(sys.argv[0]).resolve()
    environment = os.environ.copy()
    if executable.suffix in {".pyz", ".pyzw"}:
        command = [sys.executable, str(executable)]
        install_arguments: list[str] = []
    else:
        command = [sys.executable, "-m", "zeus_code"]
        root = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [root, environment.get("PYTHONPATH")]))
        # A checkout remains a checkout; give its user the standard installed
        # launcher rather than asking the updater to overwrite source files.
        install_arguments = ["--install-dir", str(Path.home() / ".local" / "bin")]
    return [*command, "--data-dir", str(data_dir), "update", *install_arguments], environment


async def update_client(data_dir: Path) -> str:
    """Run the normal safe updater without printing over the active terminal.

    This only changes the installed client entry point. The current client and
    connected daemon continue using their retained runtimes.
    """
    command, environment = _update_command(data_dir)
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=environment,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=180)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        raise
    message = output.decode("utf-8", errors="replace").strip()
    if process.returncode:
        raise RuntimeError(message or "Client update failed. Run zeus-code update for details.")
    return message
