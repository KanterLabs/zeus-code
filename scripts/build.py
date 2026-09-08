#!/usr/bin/env python3
"""Create a standalone, dependency-free Python zip application."""
import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import TypeVar
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PYTHON_REQUIRES = "3.11"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_MODE = (stat.S_IFREG | 0o644) << 16
MAIN = b"from zeus_code.cli import main\nraise SystemExit(main())\n"

T = TypeVar("T", str, int)


def read_literal_assignment(path: Path, name: str, expected_type: type[T]) -> T:
    """Read one top-level literal assignment without importing application code."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                matches.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            matches.append(node.value)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one assignment to {name} in {path}")
    try:
        value = ast.literal_eval(matches[0])
    except (ValueError, TypeError, SyntaxError) as exc:
        raise ValueError(f"{name} in {path} must be a literal") from exc
    if type(value) is not expected_type:
        raise ValueError(f"{name} in {path} must be {expected_type.__name__}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = ZIP_MODE
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _build_archive(source: Path, artifact: Path) -> None:
    entries: list[tuple[str, bytes]] = [("__main__.py", MAIN)]
    for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"refusing symbolic link in application source: {path}")
        if path.is_file():
            entries.append((f"zeus_code/{relative.as_posix()}", path.read_bytes()))

    artifact.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{artifact.name}.", suffix=".tmp", dir=artifact.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(stream, "w") as archive:
                for name, data in entries:
                    _zip_entry(archive, name, data)
        os.chmod(temporary, 0o755)
        os.replace(temporary, artifact)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def build_release(root: Path, output: Path) -> tuple[Path, Path, Path]:
    """Build the release bundle and return its three publishable paths."""
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / "zeus-code.pyz"
    _build_archive(root / "src/zeus_code", artifact)
    version = read_literal_assignment(
        root / "src/zeus_code/__init__.py", "__version__", str
    )
    schema_version = read_literal_assignment(
        root / "src/zeus_code/storage.py", "SCHEMA_VERSION", int
    )
    metadata = output / "release.json"
    metadata.write_text(
        json.dumps(
            {
                "python_requires": PYTHON_REQUIRES,
                "protocol_version": read_literal_assignment(root / "src/zeus_code/__init__.py", "PROTOCOL_VERSION", int),
                "schema_version": schema_version,
                "version": version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    checksums = {path.name: sha256(path) for path in (artifact, metadata)}
    checksum_file = output / "SHA256SUMS"
    checksum_file.write_text(
        "".join(f"{checksums[name]}  {name}\n" for name in (artifact.name, metadata.name)),
        encoding="utf-8",
    )
    return artifact, checksum_file, metadata


def main() -> None:
    output = ROOT / "dist"
    artifact, _, metadata = build_release(ROOT, output)
    version = read_literal_assignment(
        ROOT / "src/zeus_code/__init__.py", "__version__", str
    )
    schema_version = read_literal_assignment(
        ROOT / "src/zeus_code/storage.py", "SCHEMA_VERSION", int
    )
    checksums = {path.name: sha256(path) for path in (artifact, metadata)}
    print(f"Built {artifact.relative_to(ROOT)} ({artifact.stat().st_size:,} bytes)")
    print(f"Release {version} (schema {schema_version}, Python {PYTHON_REQUIRES}+)")
    print(f"SHA256 {checksums[artifact.name]}")


if __name__ == "__main__":
    main()
