"""Download and atomically install the latest stable Zeus Code zipapp."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
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
import sys
import tempfile
import time
from typing import BinaryIO, Iterator
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import __version__
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
    kind: str


@dataclass(frozen=True)
class _ZipIdentity:
    version: str
    schema_version: int


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
    if schema != SCHEMA_VERSION:
        raise RuntimeError(
            f"Release schema version {schema} differs from this installation's schema "
            f"version {SCHEMA_VERSION}. Use the manual upgrade procedure and retain the "
            "current executable and database backup for rollback compatibility."
        )
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
            storage_info = archive.getinfo("zeus_code/storage.py")
            if storage_info.file_size > 2 * 1024 * 1024:
                raise RuntimeError(f"{label} has an unexpectedly large storage module")
            schema_version = _schema_from_storage(
                archive.read(storage_info), f"{label}:zeus_code/storage.py"
            )
            return _ZipIdentity(version, schema_version)
    except RuntimeError:
        raise
    except (OSError, KeyError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise RuntimeError(f"{label} is not a valid Zeus Code zipapp: {exc}") from exc


def _zip_version(source: Path | bytes, *, label: str) -> str:
    return _zip_identity(source, label=label).version


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
        return candidate.resolve()
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
    if running is not None and running.is_file():
        try:
            _zip_version(running, label=f"Running executable {running}")
        except RuntimeError:
            pass
        else:
            return running
    return (Path.home() / ".local/bin/zeus-code").resolve()


def _inspect_target(path: Path) -> _Target:
    if not path.exists() and not path.is_symlink():
        return _Target(path, None, None, "absent")
    if path.is_symlink():
        raise RuntimeError(f"Refusing to replace symbolic-link update target: {path}")
    try:
        mode = path.stat().st_mode
    except OSError:
        raise
    if not stat.S_ISREG(mode):
        raise RuntimeError(f"Refusing to replace non-file update target: {path}")
    try:
        identity = _zip_identity(path, label=f"Existing executable {path}")
    except RuntimeError as zip_error:
        if _is_legacy_zeus_launcher(path):
            return _Target(path, None, None, "legacy")
        raise RuntimeError(
            f"Refusing to overwrite unknown non-Zeus executable at {path}: {zip_error}"
        ) from zip_error
    if identity.schema_version != SCHEMA_VERSION:
        raise RuntimeError(
            f"Existing executable {path} uses schema version {identity.schema_version}, "
            f"but this updater uses {SCHEMA_VERSION}. Use the manual upgrade procedure "
            "so the retained executable and state backup remain rollback-compatible."
        )
    return _Target(path, identity.version, identity.schema_version, "zipapp")


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


def _lock_stop_command(data_dir: Path) -> str:
    resolved = data_dir.expanduser().resolve()
    if resolved == default_data_dir().expanduser().resolve():
        return "zeus-code stop"
    return f"zeus-code --data-dir {shlex.quote(str(resolved))} stop"


@contextmanager
def _daemon_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        raise
    lock_path = data_dir / "server.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            command = _lock_stop_command(data_dir)
            raise RuntimeError(
                "Zeus Code daemon is running. Stop the daemon first with "
                f"'{command}', then run the update again. Active provider work was left running."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
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
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
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
                f"{SCHEMA_VERSION}. Use the manual upgrade procedure and retain the current "
                "executable for rollback compatibility."
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
    if comparison == 0:
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
    _metadata(metadata_raw, release.version)

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
        if artifact.schema_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Downloaded package schema version {artifact.schema_version} does not "
                f"match release/current schema version {SCHEMA_VERSION}. Use the manual "
                "upgrade procedure and retain the current executable and database backup "
                "for rollback compatibility."
            )

        database_backup: Path | None = None
        executable_backup: Path | None = None
        first_install = False
        with _daemon_lock(data_dir):
            _assert_not_source_launcher(target.path)
            locked_target = _inspect_target(target.path)
            locked_comparison = _status(locked_target, release.version)
            if locked_comparison == 0:
                print(f"Zeus Code {release.version} is already installed at {target.path}.")
                return 0
            if locked_comparison > 0:
                print(
                    f"Installed Zeus Code {locked_target.version} at {target.path} is newer "
                    f"than {release.version}; refusing to downgrade."
                )
                return 0
            database_backup = _backup_database(data_dir, release.version)
            executable_backup = _unique_executable_backup(locked_target)
            first_install = locked_target.kind == "absent"
            os.replace(stage, target.path)

        print(f"Installed Zeus Code {release.version} at {target.path}.")
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
