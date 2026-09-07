"""XDG locations and private state directories."""
import os
from pathlib import Path


def default_data_dir() -> Path:
    override = os.environ.get("ZEUS_CODE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "zeus-code"


def client_state_path() -> Path:
    override = os.environ.get("ZEUS_CODE_CLIENT_STATE")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "zeus-code/client.json"


def secure_directory(path: Path) -> Path:
    path = path.expanduser().resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def socket_path(data_dir: Path) -> Path:
    path = data_dir / "server.sock"
    if len(os.fsencode(path)) > 103:
        raise ValueError("State directory is too long for a Unix socket. Use --data-dir with a shorter path.")
    return path
