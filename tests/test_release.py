from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any, Mapping, Sequence

from scripts import build
from scripts import publish_release as release


WORK = Path(__file__).resolve().parents[1] / ".work"


class FakeGitHubClient:
    def __init__(
        self,
        published_release: Mapping[str, Any] | None = None,
        assets: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.release = dict(published_release) if published_release is not None else None
        self.assets = [dict(asset) for asset in assets]
        self.calls: list[tuple[str, Any]] = []

    def find_release(self, tag: str) -> Mapping[str, Any] | None:
        self.calls.append(("find", tag))
        return self.release

    def create_draft(self, tag: str, version: str) -> Mapping[str, Any]:
        self.calls.append(("create", tag))
        self.release = {
            "draft": True,
            "id": 41,
            "prerelease": False,
            "tag_name": tag,
            "upload_url": "https://uploads.github.com/releases/41/assets{?name,label}",
        }
        return self.release

    def list_assets(self, release_id: int) -> Sequence[Mapping[str, Any]]:
        self.calls.append(("list", release_id))
        return list(self.assets)

    def asset_sha256(self, asset: Mapping[str, Any]) -> str:
        self.calls.append(("digest", asset["name"]))
        return str(asset["sha256"])

    def upload_asset(self, upload_url: str, asset: release.Asset) -> Mapping[str, Any]:
        self.calls.append(("upload", asset.name))
        uploaded = {
            "name": asset.name,
            "sha256": asset.sha256,
            "size": asset.size,
            "state": "uploaded",
        }
        self.assets.append(uploaded)
        return uploaded

    def publish_release(self, release_id: int, version: str) -> Mapping[str, Any]:
        self.calls.append(("publish", release_id))
        assert self.release is not None
        self.release["draft"] = False
        return self.release


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def make_source(self) -> Path:
        root = self.base / "source"
        package = root / "src/zeus_code"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('__version__ = "1.2.3"\nPROTOCOL_VERSION = 1\n', encoding="utf-8")
        (package / "storage.py").write_text("SCHEMA_VERSION = 7\n", encoding="utf-8")
        (package / "cli.py").write_text(
            "from . import __version__\n"
            "def main():\n"
            "    print(__version__)\n"
            "    return 0\n",
            encoding="utf-8",
        )
        return root

    def make_bundle(self) -> release.Bundle:
        dist = self.base / "bundle"
        dist.mkdir()
        application = dist / "zeus-code.pyz"
        application.write_bytes(b"standalone application")
        metadata = dist / "release.json"
        metadata.write_text(
            json.dumps(
                {
                    "python_requires": "3.11",
                    "protocol_version": 1,
                    "schema_version": 7,
                    "version": "1.2.3",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        (dist / "SHA256SUMS").write_text(
            f"{release.sha256(application)}  zeus-code.pyz\n"
            f"{release.sha256(metadata)}  release.json\n",
            encoding="ascii",
        )
        return release.validate_bundle(
            dist,
            "v1.2.3",
            source_version="1.2.3",
            source_schema_version=7,
        )

    @staticmethod
    def remote_asset(asset: release.Asset, *, digest: str | None = None) -> dict[str, Any]:
        return {
            "name": asset.name,
            "sha256": digest or asset.sha256,
            "size": asset.size,
            "state": "uploaded",
        }

    @staticmethod
    def draft(tag: str = "v1.2.3") -> dict[str, Any]:
        return {
            "draft": True,
            "id": 41,
            "prerelease": False,
            "tag_name": tag,
            "upload_url": "https://uploads.github.com/releases/41/assets{?name,label}",
        }

    def test_build_is_reproducible_after_source_mtimes_change(self) -> None:
        root = self.make_source()
        first = build.build_release(root, self.base / "dist-one")
        first_bytes = {path.name: path.read_bytes() for path in first}

        for index, path in enumerate(sorted((root / "src").rglob("*.py"))):
            timestamp = 2_000_000_000 + index
            os.utime(path, (timestamp, timestamp))
        second = build.build_release(root, self.base / "dist-two")

        self.assertEqual(first_bytes, {path.name: path.read_bytes() for path in second})
        self.assertEqual(
            {
                "python_requires": "3.11",
                    "protocol_version": 1,
                "schema_version": 7,
                "version": "1.2.3",
            },
            json.loads(second[2].read_text(encoding="utf-8")),
        )

    def test_bundle_rejects_tag_that_does_not_match_source_version(self) -> None:
        bundle = self.make_bundle()
        with self.assertRaisesRegex(release.ReleaseError, "does not match package version"):
            release.validate_bundle(
                bundle.assets[0].path.parent,
                "v1.2.2",
                source_version="1.2.3",
                source_schema_version=7,
            )

    def test_bundle_rejects_metadata_that_does_not_match_source(self) -> None:
        bundle = self.make_bundle()
        dist = bundle.assets[0].path.parent
        metadata = dist / "release.json"
        metadata.write_text(
            json.dumps(
                {
                    "python_requires": "3.11",
                    "protocol_version": 1,
                    "schema_version": True,
                    "version": "1.2.3",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        application = dist / "zeus-code.pyz"
        (dist / "SHA256SUMS").write_text(
            f"{release.sha256(application)}  zeus-code.pyz\n"
            f"{release.sha256(metadata)}  release.json\n",
            encoding="ascii",
        )

        with self.assertRaisesRegex(release.ReleaseError, "release.json does not match"):
            release.validate_bundle(
                dist,
                "v1.2.3",
                source_version="1.2.3",
                source_schema_version=1,
            )

    def test_bundle_rejects_nonstable_source_version(self) -> None:
        bundle = self.make_bundle()
        with self.assertRaisesRegex(release.ReleaseError, "stable X.Y.Z"):
            release.validate_bundle(
                bundle.assets[0].path.parent,
                "v1.2.3-rc1",
                source_version="1.2.3-rc1",
                source_schema_version=7,
            )

    def test_retry_resumes_draft_without_duplicate_uploads(self) -> None:
        bundle = self.make_bundle()
        existing = self.remote_asset(bundle.assets[0])
        client = FakeGitHubClient(self.draft(), [existing])

        self.assertTrue(release.publish_bundle(bundle, client))

        uploads = [value for call, value in client.calls if call == "upload"]
        self.assertEqual(["SHA256SUMS", "release.json"], uploads)
        self.assertEqual("publish", client.calls[-1][0])
        self.assertEqual(set(release.ASSET_NAMES), {asset["name"] for asset in client.assets})

    def test_conflicting_draft_asset_is_never_replaced_or_published(self) -> None:
        bundle = self.make_bundle()
        conflicting = self.remote_asset(bundle.assets[0], digest="0" * 64)
        client = FakeGitHubClient(self.draft(), [conflicting])

        with self.assertRaisesRegex(release.ReleaseError, "differs from the local file"):
            release.publish_bundle(bundle, client)

        self.assertNotIn("upload", [call for call, _ in client.calls])
        self.assertNotIn("publish", [call for call, _ in client.calls])

    def test_existing_complete_public_release_is_idempotent(self) -> None:
        bundle = self.make_bundle()
        published = {**self.draft(), "draft": False}
        client = FakeGitHubClient(
            published, [self.remote_asset(asset) for asset in bundle.assets]
        )

        self.assertFalse(release.publish_bundle(bundle, client))
        self.assertNotIn("upload", [call for call, _ in client.calls])
        self.assertNotIn("publish", [call for call, _ in client.calls])

    def test_release_body_documents_update_and_manual_bootstrap(self) -> None:
        body = release.release_body()
        self.assertIn("zeus-code update --check", body)
        self.assertNotIn("zeus-code stop", body)
        self.assertIn("zeus-code update", body)
        self.assertIn("retain immutable runtimes", body)
        self.assertIn("sha256sum -c SHA256SUMS", body)


if __name__ == "__main__":
    unittest.main()
