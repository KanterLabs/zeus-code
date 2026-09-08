from __future__ import annotations

from contextlib import redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

from zeus_code import updater
from zeus_code.storage import SCHEMA_VERSION


ARTIFACT_URL = "https://downloads.example/zeus-code.pyz"
SUMS_URL = "https://downloads.example/SHA256SUMS"
METADATA_URL = "https://downloads.example/release.json"


def zipapp(version: str, *, schema_version: int = SCHEMA_VERSION, include_cli: bool = True) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "__main__.py",
            "from zeus_code.cli import main\nraise SystemExit(main())\n",
        )
        archive.writestr(
            "zeus_code/__init__.py",
            f'__version__ = "{version}"\nPROTOCOL_VERSION = 1\n',
        )
        if include_cli:
            archive.writestr("zeus_code/cli.py", "def main():\n    return 0\n")
        archive.writestr(
            "zeus_code/storage.py",
            f"SCHEMA_VERSION = {schema_version}\n",
        )
    return output.getvalue()


class FakeRelease:
    def __init__(
        self,
        version: str,
        *,
        artifact_version: str | None = None,
        metadata_version: str | None = None,
        metadata_schema: int = SCHEMA_VERSION,
        artifact_schema: int = SCHEMA_VERSION,
        artifact: bytes | None = None,
    ) -> None:
        artifact = artifact or zipapp(
            artifact_version or version,
            schema_version=artifact_schema,
        )
        metadata = json.dumps(
            {
                "version": metadata_version or version,
                "schema_version": metadata_schema,
                "python_requires": "3.11",
            },
            sort_keys=True,
        ).encode()
        sums = (
            f"{hashlib.sha256(artifact).hexdigest()}  zeus-code.pyz\n"
            f"{hashlib.sha256(metadata).hexdigest()}  release.json\n"
        ).encode()
        release = json.dumps(
            {
                "tag_name": f"v{version}",
                "draft": False,
                "prerelease": False,
                "assets": [
                    {"name": "zeus-code.pyz", "browser_download_url": ARTIFACT_URL},
                    {"name": "SHA256SUMS", "browser_download_url": SUMS_URL},
                    {"name": "release.json", "browser_download_url": METADATA_URL},
                ],
            }
        ).encode()
        self.values: dict[str, bytes | BaseException] = {
            updater.LATEST_RELEASE_URL: release,
            ARTIFACT_URL: artifact,
            SUMS_URL: sums,
            METADATA_URL: metadata,
        }
        self.calls: list[str] = []

    def open(self, url: str) -> io.BytesIO:
        self.calls.append(url)
        value = self.values[url]
        if isinstance(value, BaseException):
            raise value
        return io.BytesIO(value)


class UpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="zeus-updater-")
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "state"
        self.install_dir = self.root / "install dir with spaces"
        self.install_dir.mkdir()
        self.target = self.install_dir / "zeus-code"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_update(
        self,
        release: FakeRelease,
        *,
        check_only: bool = False,
        install_dir: Path | None = None,
    ) -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(updater, "_open_url", side_effect=release.open):
            with redirect_stdout(output):
                result = updater.update(
                    self.data_dir,
                    check_only=check_only,
                    install_dir=self.install_dir if install_dir is None else install_dir,
                )
        return result, output.getvalue()

    def test_installs_latest_release_to_path_with_spaces(self) -> None:
        release = FakeRelease("1.1.0")

        result, output = self.run_update(release)

        self.assertEqual(0, result)
        self.assertEqual(release.values[ARTIFACT_URL], self.target.read_bytes())
        self.assertTrue(self.target.stat().st_mode & 0o111)
        self.assertIn("Installed Zeus Code 1.1.0", output)
        self.assertIn(f"Add {self.install_dir} to PATH", output)
        self.assertFalse((self.data_dir / "state.sqlite3").exists())
        self.assertEqual(
            [updater.LATEST_RELEASE_URL, SUMS_URL, METADATA_URL, ARTIFACT_URL],
            release.calls,
        )

    def test_check_only_changes_no_files_or_state_and_fetches_only_api_metadata(self) -> None:
        old = zipapp("1.0.0")
        self.target.write_bytes(old)
        before = {item.name: item.read_bytes() for item in self.install_dir.iterdir()}
        release = FakeRelease("1.1.0")

        result, output = self.run_update(release, check_only=True)

        self.assertEqual(0, result)
        self.assertIn("1.0.0 -> 1.1.0", output)
        self.assertEqual(before, {item.name: item.read_bytes() for item in self.install_dir.iterdir()})
        self.assertFalse(self.data_dir.exists())
        self.assertEqual([updater.LATEST_RELEASE_URL], release.calls)

    def test_up_to_date_and_newer_installs_do_not_download_assets(self) -> None:
        for installed, latest, phrase in (
            ("1.1.0", "1.1.0", "up to date"),
            ("2.0.0", "1.1.0", "refusing to downgrade"),
        ):
            with self.subTest(installed=installed, latest=latest):
                self.target.write_bytes(zipapp(installed))
                original = self.target.read_bytes()
                release = FakeRelease(latest)
                result, output = self.run_update(release)
                self.assertEqual(0, result)
                self.assertIn(phrase, output)
                self.assertEqual(original, self.target.read_bytes())
                self.assertEqual([updater.LATEST_RELEASE_URL], release.calls)

    def test_corrupt_or_incomplete_downloads_preserve_existing_executable(self) -> None:
        cases: list[tuple[str, callable]] = [
            (
                "bad artifact hash",
                lambda release: release.values.__setitem__(ARTIFACT_URL, b"corrupt"),
            ),
            (
                "missing release hash",
                lambda release: release.values.__setitem__(
                    SUMS_URL,
                    str(release.values[SUMS_URL], "utf-8").splitlines()[0].encode() + b"\n",
                ),
            ),
            (
                "network failure",
                lambda release: release.values.__setitem__(
                    ARTIFACT_URL, urllib.error.URLError("offline")
                ),
            ),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                old = zipapp("1.0.0")
                self.target.write_bytes(old)
                release = FakeRelease("1.1.0")
                mutate(release)
                with self.assertRaises(RuntimeError):
                    self.run_update(release)
                self.assertEqual(old, self.target.read_bytes())
                self.assertEqual([], list(self.install_dir.glob(".zeus-code-update-*")))
                self.assertEqual([], list(self.install_dir.glob("zeus-code.rollback-*")))

    def test_source_launcher_routes_to_user_bin_even_at_same_version(self) -> None:
        checkout_launcher = Path(__file__).resolve().parents[1] / "zeus-code"
        original = checkout_launcher.read_bytes()
        home = self.root / "home with spaces"
        release = FakeRelease(updater.__version__)
        output = io.StringIO()
        with mock.patch.object(updater, "_open_url", side_effect=release.open), mock.patch.object(
            updater.sys, "argv", [str(checkout_launcher), "update"]
        ), mock.patch.dict(os.environ, {"HOME": str(home), "PATH": "/usr/bin"}, clear=False):
            with redirect_stdout(output):
                result = updater.update(self.data_dir)

        installed = home / ".local/bin/zeus-code"
        self.assertEqual(0, result)
        self.assertEqual(release.values[ARTIFACT_URL], installed.read_bytes())
        self.assertEqual(original, checkout_launcher.read_bytes())
        self.assertIn("Add", output.getvalue())
        self.assertIn("to PATH", output.getvalue())

    def test_running_renamed_zipapp_updates_in_place(self) -> None:
        running = self.root / "renamed zeus executable"
        old = zipapp("1.0.0")
        running.write_bytes(old)
        release = FakeRelease("1.1.0")
        output = io.StringIO()
        with mock.patch.object(updater, "_open_url", side_effect=release.open), mock.patch.object(
            updater.sys, "argv", [str(running), "update"]
        ):
            with redirect_stdout(output):
                result = updater.update(self.data_dir)

        self.assertEqual(0, result)
        self.assertEqual(release.values[ARTIFACT_URL], running.read_bytes())
        backups = list(self.root.glob("renamed zeus executable.rollback-1.0.0*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(old, backups[0].read_bytes())

    def test_refuses_unknown_target_without_using_network(self) -> None:
        self.target.write_text("#!/bin/sh\necho unrelated\n")
        release = FakeRelease("1.1.0")

        with self.assertRaisesRegex(RuntimeError, "unknown non-Zeus"):
            self.run_update(release)

        self.assertEqual([], release.calls)
        self.assertEqual("#!/bin/sh\necho unrelated\n", self.target.read_text())

    def test_refuses_launcher_in_a_different_source_checkout(self) -> None:
        checkout = self.root / "different checkout"
        (checkout / "src/zeus_code").mkdir(parents=True)
        (checkout / "pyproject.toml").write_text("[project]\nname='zeus-code'\n")
        launcher = checkout / "zeus-code"
        launcher.write_text("from zeus_code.cli import main\n")
        release = FakeRelease("1.1.0")

        with self.assertRaisesRegex(RuntimeError, "source-checkout launcher"):
            self.run_update(release, install_dir=checkout)

        self.assertEqual("from zeus_code.cli import main\n", launcher.read_text())
        self.assertEqual([], release.calls)

    def test_active_daemon_lock_refuses_update_with_data_dir_command(self) -> None:
        old = zipapp("1.0.0")
        self.target.write_bytes(old)
        self.data_dir.mkdir()
        lock_path = self.data_dir / "server.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = FakeRelease("1.1.0")
        try:
            with self.assertRaises(RuntimeError) as raised:
                self.run_update(release)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        message = str(raised.exception)
        self.assertIn("Stop the daemon", message)
        self.assertIn("--data-dir", message)
        self.assertIn(str(self.data_dir), message)
        self.assertIn("Active provider work was left running", message)
        self.assertEqual(old, self.target.read_bytes())
        self.assertEqual([], list(self.install_dir.glob("zeus-code.rollback-*")))

    def test_populated_database_and_old_executable_get_unique_verified_backups(self) -> None:
        old = zipapp("1.0.0")
        self.target.write_bytes(old)
        occupied = self.install_dir / "zeus-code.rollback-1.0.0"
        occupied.write_bytes(b"older rollback")
        self.data_dir.mkdir()
        database = sqlite3.connect(self.data_dir / "state.sqlite3")
        database.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        database.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        database.execute("INSERT INTO records(value) VALUES ('preserve me')")
        database.commit()
        database.close()
        release = FakeRelease("1.1.0")

        result, output = self.run_update(release)

        self.assertEqual(0, result)
        self.assertEqual(release.values[ARTIFACT_URL], self.target.read_bytes())
        self.assertEqual(b"older rollback", occupied.read_bytes())
        rollback = self.install_dir / "zeus-code.rollback-1.0.0.1"
        self.assertEqual(old, rollback.read_bytes())
        backups = list((self.data_dir / "backups").glob("state-before-1.1.0-*.sqlite3"))
        self.assertEqual(1, len(backups))
        self.assertEqual(0o600, backups[0].stat().st_mode & 0o777)
        backup = sqlite3.connect(backups[0])
        self.assertEqual("ok", backup.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual("preserve me", backup.execute("SELECT value FROM records").fetchone()[0])
        backup.close()
        original = sqlite3.connect(self.data_dir / "state.sqlite3")
        self.assertEqual("preserve me", original.execute("SELECT value FROM records").fetchone()[0])
        original.close()
        self.assertIn("Verified state database backup", output)

    def test_wrong_manifest_or_embedded_schema_is_rejected_before_replacement(self) -> None:
        for name, release in (
            ("manifest", FakeRelease("1.1.0", metadata_schema=SCHEMA_VERSION + 1)),
            ("artifact", FakeRelease("1.1.0", artifact_schema=SCHEMA_VERSION + 1)),
        ):
            with self.subTest(name=name):
                old = zipapp("1.0.0")
                self.target.write_bytes(old)
                with self.assertRaisesRegex(RuntimeError, "schema version") as raised:
                    self.run_update(release)
                self.assertIn("manual upgrade", str(raised.exception))
                self.assertIn("rollback compatibility", str(raised.exception))
                self.assertEqual(old, self.target.read_bytes())

    def test_tag_metadata_and_artifact_versions_must_match(self) -> None:
        for release in (
            FakeRelease("1.1.0", metadata_version="1.2.0"),
            FakeRelease("1.1.0", artifact_version="1.2.0"),
        ):
            with self.subTest(calls=release.calls):
                old = zipapp("1.0.0")
                self.target.write_bytes(old)
                with self.assertRaisesRegex(RuntimeError, "version"):
                    self.run_update(release)
                self.assertEqual(old, self.target.read_bytes())

    def test_incompatible_existing_bundle_schema_is_not_retained_as_rollback(self) -> None:
        old = zipapp("1.0.0", schema_version=SCHEMA_VERSION + 1)
        self.target.write_bytes(old)
        release = FakeRelease("1.1.0")

        with self.assertRaisesRegex(RuntimeError, "rollback-compatible"):
            self.run_update(release)

        self.assertEqual(old, self.target.read_bytes())
        self.assertEqual([], release.calls)

    def test_incompatible_database_schema_preserves_database_and_executable(self) -> None:
        old = zipapp("1.0.0")
        self.target.write_bytes(old)
        self.data_dir.mkdir()
        database_path = self.data_dir / "state.sqlite3"
        database = sqlite3.connect(database_path)
        database.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        database.execute("CREATE TABLE records(value TEXT)")
        database.execute("INSERT INTO records VALUES ('untouched')")
        database.commit()
        database.close()
        release = FakeRelease("1.1.0")

        with self.assertRaisesRegex(RuntimeError, "manual upgrade"):
            self.run_update(release)

        self.assertEqual(old, self.target.read_bytes())
        preserved = sqlite3.connect(database_path)
        self.assertEqual("untouched", preserved.execute("SELECT value FROM records").fetchone()[0])
        self.assertEqual(SCHEMA_VERSION + 1, preserved.execute("PRAGMA user_version").fetchone()[0])
        preserved.close()
        self.assertEqual([], list((self.data_dir / "backups").glob("*.sqlite3")))

    def test_dangling_database_symlink_is_rejected_without_replacement(self) -> None:
        old = zipapp("1.0.0")
        self.target.write_bytes(old)
        self.data_dir.mkdir()
        (self.data_dir / "state.sqlite3").symlink_to(self.root / "missing.sqlite3")
        release = FakeRelease("1.1.0")

        with self.assertRaisesRegex(RuntimeError, "symbolic link"):
            self.run_update(release)

        self.assertEqual(old, self.target.read_bytes())


if __name__ == "__main__":
    unittest.main()
