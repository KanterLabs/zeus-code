"""Download and atomically install the latest stable Zeus Code zipapp."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO, Iterator
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import PROTOCOL_VERSION, __version__
from .paths import default_data_dir
from .storage import SCHEMA_VERSION


LATEST_RELEASE_URL = (
    "https://api.github.com/repos/KanterLabs/zeus-code/releases/latest"
)
REQUIRED_ASSETS = ("zeus-code.pyz", "SHA256SUMS", "release.json")
NETWORK_TIMEOUT = 20.0
DOWNLOAD_DEADLINE = 60.0
RELEASE_LIMIT = 2 * 1024 * 1024
CHECKSUM_LIMIT = 256 * 1024
METADATA_LIMIT = 64 * 1024
ARTIFACT_LIMIT = 128 * 1024 * 1024
DATABASE_BACKUP_DEADLINE = 30.0
_VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_HASH_RE = re.compile(r"([0-9a-fA-F]{64})[ \t]+(?:\*)?(.+?)\s*\Z")


@dataclass(frozen=True)
class _Release:
    version: str
    assets: dict[str, str]


@dataclass(frozen=True)
class _Target:
    path: Path
    version: str | None
    schema_version: int | None
    protocol_version: int | None
    kind: str
    runtime: Path | None = None


@dataclass(frozen=True)
class _ZipIdentity:
    version: str
    schema_version: int
    protocol_version: int


@dataclass(frozen=True)
class _DaemonState:
    running: bool
    pid: int | None = None
    version: str | None = None
    protocol_version: int | None = None
    target_in_use: bool | None = None


_LAUNCHER_MARKER = "# Zeus Code managed launcher: "
_RUNTIME_DIRECTORY = ".zeus-code-runtimes"
_LAUNCHER_LIMIT = 64 * 1024


def _open_url(url: str) -> BinaryIO:
    """Open a production release URL; tests replace this narrow seam."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": (
                "application/vnd.github+json"
                if url == LATEST_RELEASE_URL else "application/octet-stream"
            ),
            "User-Agent": f"zeus-code/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT)


@contextmanager
def _response(url: str, label: str) -> Iterator[BinaryIO]:
    try:
        response = _open_url(url)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        detail = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
        raise RuntimeError(f"Could not download {label}: {detail}") from exc
    try:
        yield response
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        detail = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
        raise RuntimeError(f"Could not download {label}: {detail}") from exc
    finally:
        try:
            response.close()
        except OSError:
            pass


def _read_url(url: str, *, limit: int, label: str) -> bytes:
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    with _response(url, label) as response:
        while True:
            if time.monotonic() > deadline:
                raise RuntimeError(f"Timed out while downloading {label}")
            chunk = response.read(min(64 * 1024, limit + 1 - size))
            if time.monotonic() > deadline:
                raise RuntimeError(f"Timed out while downloading {label}")
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise RuntimeError(f"{label} exceeds the {limit:,}-byte download limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _stable_version(value: object, *, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or _VERSION_RE.fullmatch(value) is None:
        raise RuntimeError(f"{label} must be a stable X.Y.Z version, got {value!r}")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _release() -> _Release:
    raw = _read_url(
        LATEST_RELEASE_URL,
        limit=RELEASE_LIMIT,
        label="latest release metadata",
    )
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Latest release metadata is not valid JSON") from exc
    if not isinstance(document, dict):
        raise RuntimeError("Latest release metadata must be a JSON object")
    if document.get("draft", False) is not False or document.get("prerelease", False) is not False:
        raise RuntimeError("GitHub's latest release is not a stable published release")
    tag = document.get("tag_name")
    if not isinstance(tag, str):
        raise RuntimeError("Latest release is missing its tag version")
    version = tag[1:] if tag.startswith("v") else tag
    _stable_version(version, label="Latest release tag")
    if tag not in {version, f"v{version}"}:
        raise RuntimeError(f"Latest release tag {tag!r} is not a stable version tag")

    assets_raw = document.get("assets")
    if not isinstance(assets_raw, list):
        raise RuntimeError("Latest release has no downloadable assets")
    assets: dict[str, str] = {}
    for item in assets_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if name not in REQUIRED_ASSETS:
            continue
        if name in assets:
            raise RuntimeError(f"Latest release contains duplicate {name} assets")
        url = item.get("browser_download_url")
        if not isinstance(url, str) or not url:
            raise RuntimeError(f"Latest release asset {name} has no download URL")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError(f"Latest release asset {name} does not use HTTPS")
        assets[name] = url
    missing = [name for name in REQUIRED_ASSETS if name not in assets]
    if missing:
        raise RuntimeError(f"Latest release is missing required asset(s): {', '.join(missing)}")
    return _Release(version, assets)


def _checksums(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("SHA256SUMS is not valid UTF-8") from exc
    result: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = _HASH_RE.fullmatch(line)
        if match is None:
            raise RuntimeError(f"SHA256SUMS has an invalid line {number}")
        digest, name = match.groups()
        if name in result:
            raise RuntimeError(f"SHA256SUMS lists {name} more than once")
        result[name] = digest.lower()
    missing = [name for name in ("zeus-code.pyz", "release.json") if name not in result]
    if missing:
        raise RuntimeError(f"SHA256SUMS does not cover: {', '.join(missing)}")
    return result


def _verify_digest(data: bytes, expected: str, label: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"SHA-256 verification failed for {label}: expected {expected}, got {actual}"
        )


def _metadata(raw: bytes, release_version: str) -> dict[str, object]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("release.json is not valid JSON") from exc
    if not isinstance(document, dict):
        raise RuntimeError("release.json must contain a JSON object")
    version = document.get("version")
    _stable_version(version, label="release.json version")
    if version != release_version:
        raise RuntimeError(
            f"Release tag version {release_version} does not match release.json version {version!r}"
        )
    schema = document.get("schema_version")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise RuntimeError("release.json schema_version must be an integer")
    protocol = document.get("protocol_version")
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise RuntimeError("release.json protocol_version must be an integer")
    python_requires = document.get("python_requires")
    if python_requires != "3.11":
        raise RuntimeError(
            "release.json python_requires must be exactly '3.11', "
            f"got {python_requires!r}"
        )
    return document


def _version_from_init(source: bytes, label: str) -> str:
    if len(source) > 256 * 1024:
        raise RuntimeError(f"{label} is unexpectedly large")
    try:
        tree = ast.parse(source.decode("utf-8"), filename=label)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise RuntimeError(f"{label} does not contain valid Python") from exc
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            _stable_version(value.value, label="Zipapp package version")
            return value.value
    raise RuntimeError(f"{label} does not define a stable __version__")


def _schema_from_storage(source: bytes, label: str) -> int:
    if len(source) > 2 * 1024 * 1024:
        raise RuntimeError(f"{label} is unexpectedly large")
    try:
        tree = ast.parse(source.decode("utf-8"), filename=label)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise RuntimeError(f"{label} does not contain valid Python") from exc
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION"
            for target in targets
        ):
            continue
        value = node.value
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, int)
            and not isinstance(value.value, bool)
        ):
            return value.value
    raise RuntimeError(f"{label} does not define an integer SCHEMA_VERSION")


def _protocol_from_init(source: bytes, label: str) -> int:
    if len(source) > 256 * 1024:
        raise RuntimeError(f"{label} is unexpectedly large")
    try:
        tree = ast.parse(source.decode("utf-8"), filename=label)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise RuntimeError(f"{label} does not contain valid Python") from exc
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == "PROTOCOL_VERSION"
            for target in targets
        ):
            continue
        value = node.value
        if (
            isinstance(value, ast.Constant)
            and isinstance(value.value, int)
            and not isinstance(value.value, bool)
        ):
            return value.value
    raise RuntimeError(f"{label} does not define an integer PROTOCOL_VERSION")


def _zip_identity(source: Path | bytes, *, label: str) -> _ZipIdentity:
    try:
        archive_source: Path | io.BytesIO
        archive_source = source if isinstance(source, Path) else io.BytesIO(source)
        with zipfile.ZipFile(archive_source) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise RuntimeError(f"{label} contains duplicate ZIP entries")
            required = {
                "__main__.py",
                "zeus_code/__init__.py",
                "zeus_code/cli.py",
                "zeus_code/storage.py",
            }
            missing = sorted(required.difference(names))
            if missing:
                raise RuntimeError(
                    f"{label} is not a Zeus Code zipapp; missing {', '.join(missing)}"
                )
            main_info = archive.getinfo("__main__.py")
            if main_info.file_size > 64 * 1024:
                raise RuntimeError(f"{label} has an unexpectedly large __main__.py")
            main = archive.read(main_info)
            if b"zeus_code.cli" not in main:
                raise RuntimeError(f"{label} does not start the Zeus Code CLI")
            init_info = archive.getinfo("zeus_code/__init__.py")
            if init_info.file_size > 256 * 1024:
                raise RuntimeError(f"{label} has an unexpectedly large package initializer")
            version = _version_from_init(
                archive.read(init_info), f"{label}:zeus_code/__init__.py"
            )
            protocol_version = _protocol_from_init(
                archive.read(init_info), f"{label}:zeus_code/__init__.py"
            )
            storage_info = archive.getinfo("zeus_code/storage.py")
            if storage_info.file_size > 2 * 1024 * 1024:
                raise RuntimeError(f"{label} has an unexpectedly large storage module")
            schema_version = _schema_from_storage(
                archive.read(storage_info), f"{label}:zeus_code/storage.py"
            )
            return _ZipIdentity(version, schema_version, protocol_version)
    except RuntimeError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise RuntimeError(f"{label} is not a valid Zeus Code zipapp: {exc}") from exc


def _zip_version(source: Path | bytes, *, label: str) -> str:
    return _zip_identity(source, label=label).version


def _digest_file(path: Path, *, label: str) -> str:
    if path.is_symlink():
        raise RuntimeError(f"{label} is a symbolic link: {path}")
    try:
        info = path.stat()
    except OSError as exc:
        raise RuntimeError(f"Could not inspect {label.lower()} {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"{label} is not a regular file: {path}")
    if info.st_size > ARTIFACT_LIMIT:
        raise RuntimeError(f"{label} exceeds the {ARTIFACT_LIMIT:,}-byte limit")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_path(directory: Path, digest: str) -> Path:
    return directory / _RUNTIME_DIRECTORY / digest / "zeus-code.pyz"


def _launcher_bytes(identity: _ZipIdentity, digest: str) -> bytes:
    metadata = json.dumps(
        {
            "digest": digest,
            "protocol_version": identity.protocol_version,
            "schema_version": identity.schema_version,
            "version": identity.version,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    source = f'''#!/usr/bin/env python3
{_LAUNCHER_MARKER}{metadata}
import hashlib
import os
from pathlib import Path
import stat
import sys

_RUNTIME_DIGEST = {digest!r}


def _runtime_path():
    base = Path(__file__).resolve().parent
    root = base / {_RUNTIME_DIRECTORY!r}
    version_dir = root / _RUNTIME_DIGEST
    runtime = version_dir / "zeus-code.pyz"
    if root.is_symlink() or version_dir.is_symlink() or runtime.is_symlink():
        raise RuntimeError("managed runtime path contains a symbolic link")
    descriptor = os.open(runtime, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("managed runtime is not a regular file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != _RUNTIME_DIGEST:
            raise RuntimeError("managed runtime checksum does not match its address")
    finally:
        os.close(descriptor)
    os.environ["ZEUS_CODE_INSTALL_TARGET"] = str(Path(__file__).resolve())
    return runtime


try:
    _runtime = _runtime_path()
except (OSError, RuntimeError) as exc:
    print(f"zeus-code: {{exc}}", file=sys.stderr)
    raise SystemExit(1)
os.execv(sys.executable, [sys.executable, str(_runtime), *sys.argv[1:]])
'''
    return source.encode("utf-8")


def _managed_target(path: Path) -> _Target | None:
    try:
        size = path.stat().st_size
        if size > _LAUNCHER_LIMIT:
            return None
        source = path.read_bytes()
    except OSError:
        return None
    try:
        first, marker, *_ = source.decode("utf-8").splitlines()
    except (UnicodeDecodeError, ValueError):
        return None
    if marker.startswith(_LAUNCHER_MARKER) is False:
        return None
    if first != "#!/usr/bin/env python3":
        raise RuntimeError(f"Managed Zeus Code launcher has an invalid header: {path}")
    try:
        document = json.loads(marker.removeprefix(_LAUNCHER_MARKER))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Managed Zeus Code launcher metadata is invalid: {path}") from exc
    if not isinstance(document, dict) or set(document) != {
        "digest",
        "protocol_version",
        "schema_version",
        "version",
    }:
        raise RuntimeError(f"Managed Zeus Code launcher metadata is invalid: {path}")
    digest = document.get("digest")
    version = document.get("version")
    schema_version = document.get("schema_version")
    protocol_version = document.get("protocol_version")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise RuntimeError(f"Managed Zeus Code launcher digest is invalid: {path}")
    _stable_version(version, label="Managed launcher version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise RuntimeError(f"Managed Zeus Code launcher schema version is invalid: {path}")
    if isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
        raise RuntimeError(f"Managed Zeus Code launcher protocol version is invalid: {path}")
    identity = _ZipIdentity(version, schema_version, protocol_version)
    if source != _launcher_bytes(identity, digest):
        raise RuntimeError(f"Managed Zeus Code launcher contents are invalid: {path}")

    root = path.parent / _RUNTIME_DIRECTORY
    version_directory = root / digest
    for directory in (root, version_directory):
        if directory.is_symlink():
            raise RuntimeError(
                f"Managed Zeus Code runtime directory is a symbolic link: {directory}"
            )
        if not directory.is_dir():
            raise RuntimeError(
                f"Managed Zeus Code runtime directory is missing: {directory}"
            )
    runtime = _runtime_path(path.parent, digest)
    actual_digest = _digest_file(runtime, label="Managed Zeus Code runtime")
    if actual_digest != digest:
        raise RuntimeError(
            f"Managed Zeus Code runtime checksum mismatch at {runtime}: "
            f"expected {digest}, got {actual_digest}"
        )
    runtime_identity = _zip_identity(runtime, label=f"Managed Zeus Code runtime {runtime}")
    if runtime_identity != identity:
        raise RuntimeError(
            f"Managed Zeus Code launcher metadata does not match runtime {runtime}"
        )
    return _Target(
        path,
        identity.version,
        identity.schema_version,
        identity.protocol_version,
        "managed",
        runtime,
    )


def _is_legacy_zeus_launcher(path: Path) -> bool:
    try:
        if path.stat().st_size > 256 * 1024:
            return False
        source = path.read_bytes()
    except OSError:
        return False
    if b"\x00" in source:
        return False
    return b"zeus_code.cli" in source and b"main" in source


def _running_path() -> Path | None:
    if not sys.argv or not sys.argv[0]:
        return None
    raw = sys.argv[0]
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() and candidate.parent == Path("."):
        found = shutil.which(raw)
        if found is not None:
            candidate = Path(found)
    try:
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.parent.resolve() / candidate.name
    except OSError:
        return None


def _source_launcher() -> Path | None:
    """Return the checkout-owned launcher when this module runs from source."""
    try:
        root = Path(__file__).resolve().parents[2]
    except IndexError:
        return None
    if not (root / "pyproject.toml").is_file() or not (root / "src/zeus_code").is_dir():
        return None
    launcher = root / "zeus-code"
    return launcher.resolve() if launcher.exists() else None


def _target_path(install_dir: Path | None) -> Path:
    if install_dir is not None:
        return (Path(install_dir).expanduser().resolve() / "zeus-code")
    running = _running_path()
    installed_target = os.environ.get("ZEUS_CODE_INSTALL_TARGET")
    if installed_target:
        candidate = Path(installed_target).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.parent.resolve() / candidate.name
        if candidate.is_symlink():
            return candidate
        managed = _managed_target(candidate)
        if (
            managed is not None
            and running is not None
            and managed.runtime == running.resolve()
        ):
            return candidate
    if running is not None and running.is_file():
        if (
            len(running.parents) >= 3
            and running.name == "zeus-code.pyz"
            and running.parent.parent.name == _RUNTIME_DIRECTORY
        ):
            launcher = running.parent.parent.parent / "zeus-code"
            try:
                managed = _managed_target(launcher)
            except RuntimeError:
                managed = None
            if managed is not None and managed.runtime == running:
                return launcher
        if running.suffix.lower() in {".pyz", ".pyzw"}:
            return (Path.home() / ".local/bin/zeus-code").resolve()
        try:
            _zip_version(running, label=f"Running executable {running}")
        except RuntimeError:
            pass
        else:
            return running
    return (Path.home() / ".local/bin/zeus-code").resolve()


def _inspect_target(path: Path) -> _Target:
    if not path.exists() and not path.is_symlink():
        return _Target(path, None, None, None, "absent")
    if path.is_symlink():
        raise RuntimeError(f"Refusing to replace symbolic-link update target: {path}")
    try:
        mode = path.stat().st_mode
    except OSError:
        raise
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"Refusing to replace non-file update target: {path}")
    managed = _managed_target(path)
    if managed is not None:
        if managed.schema_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Existing executable {path} uses schema version "
                f"{managed.schema_version}, but this updater uses {SCHEMA_VERSION}. "
                "The server update is deferred; use the manual upgrade procedure so "
                "the retained executable and state backup remain rollback-compatible."
            )
        return managed
    try:
        identity = _zip_identity(path, label=f"Existing executable {path}")
    except RuntimeError as zip_error:
        if _is_legacy_zeus_launcher(path):
            return _Target(path, None, None, None, "legacy")
        raise RuntimeError(
            f"Refusing to overwrite unknown non-Zeus executable at {path}: {zip_error}"
        ) from zip_error
    if identity.schema_version != SCHEMA_VERSION:
        raise RuntimeError(
            f"Existing executable {path} uses schema version {identity.schema_version}, "
            f"but this updater uses {SCHEMA_VERSION}. The server update is deferred; "
            "use the manual upgrade procedure so the retained executable and state "
            "backup remain rollback-compatible."
        )
    return _Target(
        path,
        identity.version,
        identity.schema_version,
        identity.protocol_version,
        "zipapp",
    )


def _assert_not_source_launcher(path: Path) -> None:
    launcher = _source_launcher()
    resolved = path.resolve()
    adjacent_checkout = (
        path.name == "zeus-code"
        and (path.parent / "pyproject.toml").is_file()
        and (path.parent / "src/zeus_code").is_dir()
    )
    if adjacent_checkout or (launcher is not None and resolved == launcher):
        raise RuntimeError(
            f"Refusing to overwrite the source-checkout launcher at {path}; "
            "omit --install-dir to install in ~/.local/bin instead."
        )


def _download_artifact(url: str, directory: Path, expected: str) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=".zeus-code-update-", dir=directory)
    path = Path(temporary)
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    try:
        with os.fdopen(descriptor, "wb") as output:
            with _response(url, "zeus-code.pyz") as response:
                while True:
                    if time.monotonic() > deadline:
                        raise RuntimeError("Timed out while downloading zeus-code.pyz")
                    chunk = response.read(min(64 * 1024, ARTIFACT_LIMIT + 1 - size))
                    if time.monotonic() > deadline:
                        raise RuntimeError("Timed out while downloading zeus-code.pyz")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > ARTIFACT_LIMIT:
                        raise RuntimeError(
                            f"zeus-code.pyz exceeds the {ARTIFACT_LIMIT:,}-byte download limit"
                        )
                    output.write(chunk)
                    digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise RuntimeError(
                "SHA-256 verification failed for zeus-code.pyz: "
                f"expected {expected}, got {digest.hexdigest()}"
            )
        os.chmod(path, 0o755)
        return path
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def _runtime_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing symbolic-link runtime directory: {path}")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Runtime path is not a regular directory: {path}")
    os.chmod(path, 0o700)


def _install_runtime(
    source: Path,
    directory: Path,
    digest: str,
    identity: _ZipIdentity,
) -> Path:
    root = directory / _RUNTIME_DIRECTORY
    _runtime_directory(root)
    version_directory = root / digest
    _runtime_directory(version_directory)
    runtime = version_directory / "zeus-code.pyz"
    if runtime.is_symlink():
        raise RuntimeError(f"Refusing symbolic-link managed runtime: {runtime}")
    if not runtime.exists():
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".zeus-code-runtime-",
            dir=version_directory,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o555)
            try:
                os.link(temporary, runtime, follow_symlinks=False)
            except FileExistsError:
                pass
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        finally:
            temporary.unlink(missing_ok=True)

    actual = _digest_file(runtime, label="Managed Zeus Code runtime")
    if actual != digest:
        raise RuntimeError(
            f"Existing content-addressed runtime checksum mismatch at {runtime}: "
            f"expected {digest}, got {actual}"
        )
    actual_identity = _zip_identity(runtime, label=f"Managed Zeus Code runtime {runtime}")
    if actual_identity != identity:
        raise RuntimeError(f"Managed Zeus Code runtime identity mismatch at {runtime}")
    os.chmod(runtime, 0o555)
    _fsync_directory(version_directory)
    _fsync_directory(root)
    return runtime


def _atomic_install_launcher(path: Path, identity: _ZipIdentity, digest: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".zeus-code-launcher-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_launcher_bytes(identity, digest))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _read_server_metadata(data_dir: Path) -> tuple[int | None, str | None, int | None]:
    path = data_dir / "server.json"
    if path.is_symlink():
        raise RuntimeError(f"Daemon metadata is a symbolic link: {path}")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
    except FileNotFoundError:
        return None, None, None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"Daemon metadata is not a regular file: {path}")
        if info.st_size > METADATA_LIMIT:
            raise RuntimeError(
                f"Daemon metadata exceeds the {METADATA_LIMIT:,}-byte limit"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(METADATA_LIMIT + 1)
    except OSError as exc:
        raise RuntimeError(f"Could not read daemon metadata at {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Daemon metadata is invalid at {path}") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"Daemon metadata is invalid at {path}")
    pid = document.get("pid")
    version = document.get("version")
    protocol = document.get("protocol_version")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        pid = None
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        version = None
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        protocol = None
    return pid, version, protocol


def _process_uses_path(pid: int | None, target: Path) -> bool | None:
    if pid is None:
        return None
    proc = Path("/proc") / str(pid)
    command_path = proc / "cmdline"
    if command_path.exists():
        try:
            raw = command_path.read_bytes()
            if len(raw) > 1024 * 1024:
                return None
            arguments = [
                os.fsdecode(argument)
                for argument in raw.split(b"\0")
                if argument
            ]
            cwd = Path(os.readlink(proc / "cwd"))
            expected = target.resolve()
            for argument in arguments[1:]:
                if not argument or argument.startswith("-") or "\x00" in argument:
                    continue
                candidate = Path(argument).expanduser()
                if not candidate.is_absolute():
                    candidate = cwd / candidate
                try:
                    if candidate.resolve() == expected and candidate.is_file():
                        return True
                except OSError:
                    continue
            return False
        except (OSError, UnicodeError):
            return None

    # macOS does not expose procfs. Its wide command column retains the script
    # path, which is sufficient to distinguish a directly executed zipapp from
    # a daemon running out of another immutable runtime.
    try:
        result = subprocess.run(
            ("ps", "-ww", "-p", str(pid), "-o", "command="),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        command = os.fsdecode(result.stdout).strip()
    except UnicodeError:
        return None
    if not command:
        return None
    return str(target.resolve()) in command


@contextmanager
def _daemon_guard(data_dir: Path, target: Path) -> Iterator[_DaemonState]:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    lock_path = data_dir / "server.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    locked = False
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pid, version, protocol = _read_server_metadata(data_dir)
            state = _DaemonState(
                True,
                pid=pid,
                version=version,
                protocol_version=protocol,
                target_in_use=_process_uses_path(pid, target),
            )
        else:
            locked = True
            state = _DaemonState(False)
        yield state
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _update_lock(target: Path) -> Iterator[None]:
    path = target.with_name(f".{target.name}.update.lock")
    if path.is_symlink():
        raise RuntimeError(f"Update lock is a symbolic link: {path}")
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise RuntimeError(f"Could not open update lock {path}: {exc}") from exc
    locked = False
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Another Zeus Code update is already installing at {target}; try again "
                "after it finishes."
            ) from exc
        locked = True
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _backup_database(data_dir: Path, release_version: str) -> Path | None:
    source_path = data_dir / "state.sqlite3"
    if source_path.is_symlink():
        raise RuntimeError(f"State database is a symbolic link: {source_path}")
    if not source_path.exists():
        return None
    if not source_path.is_file():
        raise RuntimeError(f"State database is not a regular file: {source_path}")
    if source_path.stat().st_size == 0:
        return None

    backups = data_dir / "backups"
    if backups.is_symlink():
        raise RuntimeError(f"State backup directory is a symbolic link: {backups}")
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    if backups.is_symlink() or not backups.is_dir():
        raise RuntimeError(f"State backup path is not a directory: {backups}")
    os.chmod(backups, 0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"state-before-{release_version}-",
        suffix=".sqlite3",
        dir=backups,
    )
    os.close(descriptor)
    destination = Path(temporary)
    os.chmod(destination, 0o600)
    source: sqlite3.Connection | None = None
    backup: sqlite3.Connection | None = None
    try:
        source_uri = source_path.resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=5.0)
        source.execute("PRAGMA query_only = ON")
        row = source.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else -1
        if version != SCHEMA_VERSION:
            raise RuntimeError(
                f"State database schema version {version} is not compatible with version "
                f"{SCHEMA_VERSION}. The server update is deferred; use the manual upgrade "
                "procedure and retain the current executable for rollback compatibility. "
                "The updater will not stop the daemon or restore this database automatically."
            )
        backup = sqlite3.connect(destination)
        deadline = time.monotonic() + DATABASE_BACKUP_DEADLINE

        def progress(status: int, remaining: int, total: int) -> None:
            del status, remaining, total
            if time.monotonic() > deadline:
                raise RuntimeError("Timed out while backing up the state database")

        source.backup(backup, pages=256, progress=progress, sleep=0.05)
        check = backup.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise RuntimeError(f"Database backup integrity check failed: {check!r}")
        copied_version = int(backup.execute("PRAGMA user_version").fetchone()[0])
        if copied_version != version:
            raise RuntimeError(
                f"Database backup schema changed from {version} to {copied_version}"
            )
        foreign_key_error = backup.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise RuntimeError(
                f"Database backup foreign-key check failed: {tuple(foreign_key_error)!r}"
            )
        backup.close()
        backup = None
        source.close()
        source = None
        with destination.open("rb") as stream:
            os.fsync(stream.fileno())
        os.chmod(destination, 0o600)
        return destination
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not create a verified state database backup: {exc}") from exc
    except BaseException:
        raise
    finally:
        if backup is not None:
            backup.close()
        if source is not None:
            source.close()
        if sys.exc_info()[0] is not None:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass


def _unique_executable_backup(target: _Target) -> Path | None:
    if target.kind == "absent":
        return None
    suffix = target.version or "legacy"
    for counter in range(10_000):
        extra = "" if counter == 0 else f".{counter}"
        candidate = target.path.with_name(
            f"{target.path.name}.rollback-{suffix}{extra}"
        )
        try:
            os.link(target.path, candidate, follow_symlinks=False)
        except FileExistsError:
            continue
        except OSError as exc:
            # Some otherwise valid local filesystems do not permit hard links.
            try:
                descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as output, target.path.open("rb") as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(candidate, target.path.stat().st_mode & 0o777)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
                raise
            return candidate
        else:
            return candidate
    raise RuntimeError(f"Could not choose a unique rollback path next to {target.path}")


def _status(target: _Target, release_version: str) -> int:
    if target.version is None:
        return -1
    installed = _stable_version(target.version, label="Installed bundle version")
    latest = _stable_version(release_version, label="Latest release version")
    return (installed > latest) - (installed < latest)


def _directory_on_path(directory: Path) -> bool:
    expected = os.path.normcase(os.path.abspath(directory))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = entry or os.curdir
        candidate = os.path.expanduser(candidate)
        if os.path.normcase(os.path.abspath(candidate)) == expected:
            return True
    return False


def _compatibility_error(identity: _ZipIdentity) -> RuntimeError:
    differences: list[str] = []
    if identity.schema_version != SCHEMA_VERSION:
        differences.append(
            f"schema version {identity.schema_version} differs from {SCHEMA_VERSION}"
        )
    if identity.protocol_version != PROTOCOL_VERSION:
        differences.append(
            f"protocol version {identity.protocol_version} differs from {PROTOCOL_VERSION}"
        )
    detail = " and ".join(differences)
    return RuntimeError(
        f"Release {identity.version} uses incompatible {detail}. The client/server "
        "switch is deferred so active work and populated state remain untouched. "
        "Finish active daemon work, then use the manual upgrade procedure. Zeus Code "
        "will never stop the daemon or restore a database automatically during update; "
        "retain the current executable and backup for rollback compatibility."
    )


def _resume_update_command(data_dir: Path, runtime: Path, target: Path) -> str:
    arguments = [str(runtime)]
    if data_dir != default_data_dir().expanduser().resolve():
        arguments.extend(("--data-dir", str(data_dir)))
    arguments.extend(("update", "--install-dir", str(target.parent)))
    return shlex.join(arguments)


def _defer_running_switch(
    *,
    data_dir: Path,
    daemon: _DaemonState,
    new_version: str,
    runtime: Path,
    target: _Target,
    database_backup: Path | None,
    retained_runtime: Path | None,
    reason: str,
    runtime_usable: bool,
) -> None:
    pid = f" (pid {daemon.pid})" if daemon.pid is not None else ""
    print(f"Downloaded Zeus Code {new_version} to {runtime}.")
    print(
        f"The running daemon{pid} {reason}. The executable switch is deferred; "
        "active local and remote provider work was left running."
    )
    if retained_runtime is not None:
        print(f"Current runtime retained at {retained_runtime}.")
    if database_backup is not None:
        print(f"Verified state database backup: {database_backup}.")
    command = _resume_update_command(data_dir, runtime, target.path)
    if runtime_usable:
        print(
            f"The verified new runtime is usable now at {runtime}. After daemon work "
            f"finishes, run: {command}"
        )
    else:
        print(
            f"The verified new runtime is retained at {runtime}, but it cannot connect "
            "to the current daemon protocol. After active work finishes, complete the "
            f"compatible server upgrade before using it. Then run: {command}"
        )


def update(
    data_dir: Path,
    *,
    check_only: bool = False,
    install_dir: Path | None = None,
) -> int:
    """Check for or safely install the latest stable Zeus Code release."""
    data_dir = Path(data_dir).expanduser().resolve()
    target_path = _target_path(install_dir)
    _assert_not_source_launcher(target_path)
    target = _inspect_target(target_path)

    print("Checking the latest stable Zeus Code release...")
    release = _release()
    comparison = _status(target, release.version)
    if comparison == 0 and (check_only or target.kind == "managed"):
        print(f"Zeus Code {release.version} is up to date at {target.path}.")
        return 0
    if comparison > 0:
        print(
            f"Installed Zeus Code {target.version} at {target.path} is newer than "
            f"the latest stable release {release.version}; refusing to downgrade."
        )
        return 0
    if check_only:
        if target.version is None:
            print(f"Zeus Code {release.version} is available for installation at {target.path}.")
        else:
            print(
                f"Zeus Code update available: {target.version} -> {release.version} "
                f"at {target.path}."
            )
        return 0

    sums_raw = _read_url(
        release.assets["SHA256SUMS"],
        limit=CHECKSUM_LIMIT,
        label="SHA256SUMS",
    )
    sums = _checksums(sums_raw)
    metadata_raw = _read_url(
        release.assets["release.json"],
        limit=METADATA_LIMIT,
        label="release.json",
    )
    _verify_digest(metadata_raw, sums["release.json"], "release.json")
    metadata = _metadata(metadata_raw, release.version)
    release_identity = _ZipIdentity(
        release.version,
        int(metadata["schema_version"]),
        int(metadata["protocol_version"]),
    )
    if (
        release_identity.schema_version != SCHEMA_VERSION
        or release_identity.protocol_version != PROTOCOL_VERSION
    ):
        raise _compatibility_error(release_identity)

    target.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    stage = _download_artifact(
        release.assets["zeus-code.pyz"],
        target.path.parent,
        sums["zeus-code.pyz"],
    )
    try:
        artifact = _zip_identity(stage, label="Downloaded zeus-code.pyz")
        if artifact.version != release.version:
            raise RuntimeError(
                f"Downloaded package version {artifact.version} does not match release "
                f"version {release.version}"
            )
        if artifact.schema_version != release_identity.schema_version:
            raise RuntimeError(
                f"Downloaded package schema version {artifact.schema_version} does not "
                f"match release.json schema version {release_identity.schema_version}; "
                "the executable and state remain untouched for rollback compatibility."
            )
        if artifact.protocol_version != release_identity.protocol_version:
            raise RuntimeError(
                f"Downloaded package protocol version {artifact.protocol_version} does not "
                f"match release.json protocol version {release_identity.protocol_version}; "
                "the executable and state remain untouched for rollback compatibility."
            )

        database_backup: Path | None = None
        executable_backup: Path | None = None
        retained_runtime: Path | None = None
        first_install = False
        digest = sums["zeus-code.pyz"]
        runtime = _install_runtime(stage, target.path.parent, digest, artifact)
        with _update_lock(target.path):
            with _daemon_guard(data_dir, target.path) as daemon:
                _assert_not_source_launcher(target.path)
                locked_target = _inspect_target(target.path)
                locked_comparison = _status(locked_target, release.version)
                if locked_comparison == 0 and locked_target.kind == "managed":
                    print(
                        f"Zeus Code {release.version} is already installed at "
                        f"{target.path}."
                    )
                    return 0
                if locked_comparison > 0:
                    print(
                        f"Installed Zeus Code {locked_target.version} at {target.path} is "
                        f"newer than {release.version}; refusing to downgrade."
                    )
                    return 0
                database_backup = _backup_database(data_dir, release.version)

                if locked_target.kind == "zipapp":
                    retained_identity = _zip_identity(
                        locked_target.path,
                        label=f"Existing executable {locked_target.path}",
                    )
                    retained_digest = _digest_file(
                        locked_target.path,
                        label="Existing Zeus Code executable",
                    )
                    retained_runtime = _install_runtime(
                        locked_target.path,
                        locked_target.path.parent,
                        retained_digest,
                        retained_identity,
                    )

                if daemon.running and daemon.protocol_version not in {
                    None,
                    artifact.protocol_version,
                }:
                    _defer_running_switch(
                        data_dir=data_dir,
                        daemon=daemon,
                        new_version=release.version,
                        runtime=runtime,
                        target=locked_target,
                        database_backup=database_backup,
                        retained_runtime=retained_runtime,
                        reason=(
                            f"uses protocol {daemon.protocol_version}, while the new "
                            f"client uses protocol {artifact.protocol_version}"
                        ),
                        runtime_usable=False,
                    )
                    return 0

                if (
                    daemon.running
                    and locked_target.kind == "zipapp"
                    and daemon.target_in_use is not False
                ):
                    reason = (
                        "is still loading code from the legacy zipapp "
                        f"{locked_target.path}"
                        if daemon.target_in_use
                        else (
                            "may still be loading code from the legacy zipapp because "
                            "its runtime path could not be confirmed"
                        )
                    )
                    _defer_running_switch(
                        data_dir=data_dir,
                        daemon=daemon,
                        new_version=release.version,
                        runtime=runtime,
                        target=locked_target,
                        database_backup=database_backup,
                        retained_runtime=retained_runtime,
                        reason=reason,
                        runtime_usable=True,
                    )
                    return 0

                executable_backup = _unique_executable_backup(locked_target)
                first_install = locked_target.kind == "absent"
                _atomic_install_launcher(target.path, artifact, digest)

        print(f"Installed Zeus Code {release.version} at {target.path}.")
        print(f"Immutable runtime: {runtime}.")
        if executable_backup is not None:
            print(f"Previous executable retained at {executable_backup}.")
        if database_backup is not None:
            print(f"Verified state database backup: {database_backup}.")
        if first_install and not _directory_on_path(target.path.parent):
            print(
                f"Add {target.path.parent} to PATH, then run 'zeus-code --version' "
                "from any directory."
            )
        return 0
    finally:
        try:
            stage.unlink()
        except FileNotFoundError:
            pass


__all__ = ["update"]
