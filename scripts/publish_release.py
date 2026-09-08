#!/usr/bin/env python3
"""Publish a verified Zeus Code bundle as an atomic GitHub Release."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Protocol, Sequence
from urllib import error, parse, request


ROOT = Path(__file__).resolve().parents[1]
ASSET_NAMES = ("zeus-code.pyz", "SHA256SUMS", "release.json")
CHECKSUMMED_NAMES = ("zeus-code.pyz", "release.json")
PYTHON_REQUIRES = "3.11"
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
CHECKSUM_PATTERN = re.compile(r"([0-9a-fA-F]{64})  ([A-Za-z0-9][A-Za-z0-9._-]*)")
SHA256_PATTERN = re.compile(r"sha256:([0-9a-fA-F]{64})")
STABLE_VERSION_PATTERN = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z"
)


class ReleaseError(RuntimeError):
    """Raised when a release cannot be published without weakening validation."""


class GitHubAPIError(ReleaseError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Asset:
    path: Path
    sha256: str
    size: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class Bundle:
    tag: str
    version: str
    assets: tuple[Asset, ...]


class ReleaseClient(Protocol):
    def find_release(self, tag: str) -> Mapping[str, Any] | None: ...

    def create_draft(self, tag: str, version: str) -> Mapping[str, Any]: ...

    def list_assets(self, release_id: int) -> Sequence[Mapping[str, Any]]: ...

    def asset_sha256(self, asset: Mapping[str, Any]) -> str: ...

    def upload_asset(self, upload_url: str, asset: Asset) -> Mapping[str, Any]: ...

    def publish_release(self, release_id: int, version: str) -> Mapping[str, Any]: ...


def _literal_assignment(path: Path, name: str, expected_type: type[Any]) -> Any:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ReleaseError(f"cannot read {name} from {path}: {exc}") from exc
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
        raise ReleaseError(f"expected exactly one assignment to {name} in {path}")
    try:
        value = ast.literal_eval(matches[0])
    except (ValueError, TypeError, SyntaxError) as exc:
        raise ReleaseError(f"{name} in {path} must be a literal") from exc
    if type(value) is not expected_type:
        raise ReleaseError(f"{name} in {path} must be {expected_type.__name__}")
    return value


def source_metadata(root: Path = ROOT) -> tuple[str, int, int]:
    version = _literal_assignment(root / "src/zeus_code/__init__.py", "__version__", str)
    schema_version = _literal_assignment(
        root / "src/zeus_code/storage.py", "SCHEMA_VERSION", int
    )
    return version, schema_version, _literal_assignment(root / "src/zeus_code/__init__.py", "PROTOCOL_VERSION", int)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_metadata(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{path.name} must contain a JSON object")
    return value


def _read_checksums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseError(f"invalid {path.name}: {exc}") from exc
    checksums: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        match = CHECKSUM_PATTERN.fullmatch(line)
        if match is None:
            raise ReleaseError(f"invalid {path.name} line {line_number}")
        digest, name = match.groups()
        if name in checksums:
            raise ReleaseError(f"duplicate checksum entry for {name}")
        checksums[name] = digest.lower()
    if set(checksums) != set(CHECKSUMMED_NAMES):
        expected = ", ".join(CHECKSUMMED_NAMES)
        raise ReleaseError(f"{path.name} must cover exactly: {expected}")
    return checksums


def validate_bundle(
    dist: Path,
    tag: str,
    *,
    source_version: str,
    source_schema_version: int,
    source_protocol_version: int = 1,
) -> Bundle:
    if STABLE_VERSION_PATTERN.fullmatch(source_version) is None:
        raise ReleaseError(
            f"package version must be a stable X.Y.Z version, got {source_version!r}"
        )
    expected_tag = f"v{source_version}"
    if tag != expected_tag:
        raise ReleaseError(
            f"tag {tag!r} does not match package version {source_version!r}; "
            f"expected {expected_tag!r}"
        )
    paths = {name: dist / name for name in ASSET_NAMES}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"release asset is missing or not a regular file: {name}")

    metadata = _read_metadata(paths["release.json"])
    expected_metadata = {
        "python_requires": PYTHON_REQUIRES,
        "protocol_version": source_protocol_version,
        "schema_version": source_schema_version,
        "version": source_version,
    }
    if type(metadata.get("schema_version")) is not int or type(metadata.get("protocol_version")) is not int or metadata != expected_metadata:
        raise ReleaseError(
            "release.json does not match source version, schema version, and Python requirement"
        )

    checksums = _read_checksums(paths["SHA256SUMS"])
    for name, expected_digest in checksums.items():
        actual_digest = sha256(paths[name])
        if actual_digest != expected_digest:
            raise ReleaseError(f"checksum mismatch for local asset {name}")

    assets = tuple(
        Asset(path=paths[name], sha256=sha256(paths[name]), size=paths[name].stat().st_size)
        for name in ASSET_NAMES
    )
    return Bundle(tag=tag, version=source_version, assets=assets)


def release_body() -> str:
    return """## Terminal UX improvements

- F4: searchable model and supported reasoning/variant settings, with a visible model indicator.
- F9: automatic Codex child-agent activity and readable details, including short terminals.
- F5: unread results and approvals across machines, with preserved drafts and history position.
- Ctrl+K: searchable commands with clear targets, plus dark/light/terminal/monochrome themes.
- Compatible client updates retain immutable runtimes while daemon work continues.
- OpenCode and Codex are discovered in common user install locations even in minimal SSH environments.

## Update

Check the available update without changing the installed executable:

```sh
zeus-code update --check
```

Install the release:

```sh
zeus-code update
```

Compatible client updates retain immutable runtimes so existing daemon work continues. If a legacy daemon uses the exact archive being replaced, Zeus installs the new runtime and explains the one-time deferred launcher switch; it never stops active work.

## First install from release assets

Download `zeus-code.pyz`, `release.json`, and `SHA256SUMS` from this release. Verify both files named in the checksum manifest with `sha256sum -c SHA256SUMS` on Linux or `shasum -a 256 -c SHA256SUMS` on macOS. Python 3.11 or newer is required.

Then install the executable in a user-writable directory on `PATH`:

```sh
python3 zeus-code.pyz update --install-dir ~/.local/bin
export PATH="$HOME/.local/bin:$PATH"
zeus-code --version
```
"""


class GitHubClient:
    def __init__(self, repository: str, token: str, *, timeout: float = 30.0) -> None:
        if REPOSITORY_PATTERN.fullmatch(repository) is None:
            raise ReleaseError("repository must have the form owner/name")
        if not token or "\n" in token or "\r" in token:
            raise ReleaseError("GITHUB_TOKEN is required")
        owner, name = repository.split("/", 1)
        self.base_url = (
            "https://api.github.com/repos/"
            f"{parse.quote(owner, safe='')}/{parse.quote(name, safe='')}"
        )
        self.token = token
        self.timeout = timeout

    def _make_request(
        self,
        url: str,
        *,
        method: str,
        data: bytes | None = None,
        accept: str = "application/vnd.github+json",
        content_type: str | None = None,
    ) -> request.Request:
        outgoing = request.Request(url, data=data, method=method)
        outgoing.add_header("Accept", accept)
        outgoing.add_header("User-Agent", "zeus-code-release-publisher")
        outgoing.add_header("X-GitHub-Api-Version", "2022-11-28")
        if content_type is not None:
            outgoing.add_header("Content-Type", content_type)
        # urllib copies normal headers to redirects. Keep the bearer token on the
        # initial GitHub request so signed asset redirects never receive it.
        outgoing.add_unredirected_header("Authorization", f"Bearer {self.token}")
        return outgoing

    @staticmethod
    def _http_error(method: str, exc: error.HTTPError) -> GitHubAPIError:
        message = "request rejected"
        try:
            payload = json.loads(exc.read(64 * 1024).decode("utf-8", "replace"))
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                message = " ".join(payload["message"].split())
        except (OSError, json.JSONDecodeError):
            pass
        return GitHubAPIError(exc.code, f"GitHub {method} failed with HTTP {exc.code}: {message}")

    def _json(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None = None,
        *,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        outgoing = self._make_request(
            url, method=method, data=data, content_type=content_type
        )
        try:
            with request.urlopen(outgoing, timeout=self.timeout) as response:
                body = response.read()
        except error.HTTPError as exc:
            raise self._http_error(method, exc) from exc
        except error.URLError as exc:
            raise ReleaseError(f"GitHub {method} failed: {exc.reason}") from exc
        try:
            return json.loads(body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError(f"GitHub {method} returned invalid JSON") from exc

    def find_release(self, tag: str) -> Mapping[str, Any] | None:
        for page in range(1, 1001):
            url = f"{self.base_url}/releases?per_page=100&page={page}"
            releases = self._json("GET", url)
            if not isinstance(releases, list):
                raise ReleaseError("GitHub releases response was not a list")
            matches = [item for item in releases if isinstance(item, dict) and item.get("tag_name") == tag]
            if len(matches) > 1:
                raise ReleaseError(f"multiple GitHub releases exist for tag {tag}")
            if matches:
                return matches[0]
            if len(releases) < 100:
                return None
        raise ReleaseError("GitHub release lookup exceeded 1000 pages")

    def create_draft(self, tag: str, version: str) -> Mapping[str, Any]:
        try:
            created = self._json(
                "POST",
                f"{self.base_url}/releases",
                {
                    "body": release_body(),
                    "draft": True,
                    "generate_release_notes": True,
                    "name": f"Zeus Code {version}",
                    "prerelease": False,
                    "tag_name": tag,
                },
            )
        except GitHubAPIError as exc:
            if exc.status != 422:
                raise
            existing = self.find_release(tag)
            if existing is None:
                raise
            return existing
        if not isinstance(created, dict):
            raise ReleaseError("GitHub create-release response was not an object")
        return created

    def list_assets(self, release_id: int) -> Sequence[Mapping[str, Any]]:
        found: list[Mapping[str, Any]] = []
        for page in range(1, 1001):
            url = f"{self.base_url}/releases/{release_id}/assets?per_page=100&page={page}"
            assets = self._json("GET", url)
            if not isinstance(assets, list) or not all(isinstance(item, dict) for item in assets):
                raise ReleaseError("GitHub release-assets response was not a list of objects")
            found.extend(assets)
            if len(assets) < 100:
                return found
        raise ReleaseError("GitHub release asset lookup exceeded 1000 pages")

    @staticmethod
    def _trusted_url(url: Any, host: str) -> str:
        if not isinstance(url, str):
            raise ReleaseError("GitHub response omitted a required URL")
        parsed = parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != host:
            raise ReleaseError(f"GitHub response contained an untrusted {host} URL")
        return url

    def asset_sha256(self, asset: Mapping[str, Any]) -> str:
        supplied = asset.get("digest")
        if isinstance(supplied, str):
            match = SHA256_PATTERN.fullmatch(supplied)
            if match is not None:
                return match.group(1).lower()
        url = self._trusted_url(asset.get("url"), "api.github.com")
        outgoing = self._make_request(
            url, method="GET", accept="application/octet-stream"
        )
        digest = hashlib.sha256()
        try:
            with request.urlopen(outgoing, timeout=self.timeout) as response:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
        except error.HTTPError as exc:
            raise self._http_error("asset download", exc) from exc
        except error.URLError as exc:
            raise ReleaseError(f"GitHub asset download failed: {exc.reason}") from exc
        return digest.hexdigest()

    def upload_asset(self, upload_url: str, asset: Asset) -> Mapping[str, Any]:
        base = self._trusted_url(upload_url.split("{", 1)[0], "uploads.github.com")
        separator = "&" if "?" in base else "?"
        url = f"{base}{separator}{parse.urlencode({'name': asset.name})}"
        content_types = {
            "zeus-code.pyz": "application/zip",
            "SHA256SUMS": "text/plain",
            "release.json": "application/json",
        }
        try:
            data = asset.path.read_bytes()
        except OSError as exc:
            raise ReleaseError(f"cannot read release asset {asset.name}: {exc}") from exc
        uploaded = self._json(
            "POST", url, data=data, content_type=content_types[asset.name]
        )
        if not isinstance(uploaded, dict):
            raise ReleaseError("GitHub upload response was not an object")
        return uploaded

    def publish_release(self, release_id: int, version: str) -> Mapping[str, Any]:
        published = self._json(
            "PATCH",
            f"{self.base_url}/releases/{release_id}",
            {
                "body": release_body(),
                "draft": False,
                "make_latest": "true",
                "name": f"Zeus Code {version}",
                "prerelease": False,
            },
        )
        if not isinstance(published, dict):
            raise ReleaseError("GitHub publish-release response was not an object")
        return published


def _release_id(release: Mapping[str, Any], tag: str) -> int:
    if release.get("tag_name") != tag:
        raise ReleaseError("GitHub release response has the wrong tag")
    release_id = release.get("id")
    if type(release_id) is not int or release_id <= 0:
        raise ReleaseError("GitHub release response has an invalid id")
    return release_id


def _index_assets(
    remote_assets: Sequence[Mapping[str, Any]], expected_names: set[str]
) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for remote in remote_assets:
        name = remote.get("name")
        if not isinstance(name, str):
            raise ReleaseError("GitHub release asset has no valid name")
        if name in indexed:
            raise ReleaseError(f"GitHub release contains duplicate asset {name}")
        indexed[name] = remote
    unexpected = sorted(set(indexed) - expected_names)
    if unexpected:
        raise ReleaseError(
            "GitHub release contains unexpected assets: " + ", ".join(unexpected)
        )
    return indexed


def _verify_remote_asset(client: ReleaseClient, local: Asset, remote: Mapping[str, Any]) -> None:
    if remote.get("state") != "uploaded":
        raise ReleaseError(f"GitHub asset {local.name} is not fully uploaded")
    size = remote.get("size")
    if type(size) is not int or size != local.size:
        raise ReleaseError(f"GitHub asset {local.name} differs from the local file")
    if client.asset_sha256(remote) != local.sha256:
        raise ReleaseError(f"GitHub asset {local.name} differs from the local file")


def publish_bundle(bundle: Bundle, client: ReleaseClient) -> bool:
    """Publish the bundle, returning True when a draft was made public."""
    release = client.find_release(bundle.tag)
    if release is None:
        release = client.create_draft(bundle.tag, bundle.version)
    release_id = _release_id(release, bundle.tag)
    draft = release.get("draft")
    if type(draft) is not bool:
        raise ReleaseError("GitHub release response has no valid draft state")
    if release.get("prerelease") is not False:
        raise ReleaseError("refusing to reuse a prerelease for a stable release")
    upload_url = release.get("upload_url")
    expected = {asset.name: asset for asset in bundle.assets}
    remote = _index_assets(client.list_assets(release_id), set(expected))

    if not draft:
        if set(remote) != set(expected):
            raise ReleaseError("existing public release is missing required assets")
        for name, local in expected.items():
            _verify_remote_asset(client, local, remote[name])
        return False

    if not isinstance(upload_url, str):
        raise ReleaseError("GitHub draft response omitted upload_url")
    for name, local in expected.items():
        existing = remote.get(name)
        if existing is None:
            client.upload_asset(upload_url, local)
        else:
            _verify_remote_asset(client, local, existing)

    # Re-fetch after uploads. A release is never made visible based only on an
    # upload response or a pre-upload snapshot.
    remote = _index_assets(client.list_assets(release_id), set(expected))
    if set(remote) != set(expected):
        missing = ", ".join(sorted(set(expected) - set(remote)))
        raise ReleaseError(f"draft release is missing required assets: {missing}")
    for name, local in expected.items():
        _verify_remote_asset(client, local, remote[name])
    client.publish_release(release_id, bundle.version)
    return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if not args.repository:
            raise ReleaseError("GITHUB_REPOSITORY or --repository is required")
        if not args.tag:
            raise ReleaseError("GITHUB_REF_NAME or --tag is required")
        source_version, schema_version, protocol_version = source_metadata()
        bundle = validate_bundle(
            args.dist,
            args.tag,
            source_version=source_version,
            source_schema_version=schema_version,
            source_protocol_version=protocol_version,
        )
        client = GitHubClient(args.repository, os.environ.get("GITHUB_TOKEN", ""))
        published = publish_bundle(bundle, client)
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    if published:
        print(f"Published {bundle.tag} with {len(bundle.assets)} verified assets")
    else:
        print(f"Release {bundle.tag} already contains the verified assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
