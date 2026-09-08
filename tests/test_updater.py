from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

from zeus_code import PROTOCOL_VERSION, updater
from zeus_code.storage import SCHEMA_VERSION


ARTIFACT_URL = "https://downloads.example/zeus-code.pyz"
SUMS_URL = "https://downloads.example/SHA256SUMS"
METADATA_URL = "https://downloads.example/release.json"


def zipapp(
    version: str,
    *,
    schema_version: int = SCHEMA_VERSION,
    protocol_version: int = PROTOCOL_VERSION,
    include_cli: bool = True,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "__main__.py",
            "from zeus_code.cli import main\nraise SystemExit(main())\n",
        )
        archive.writestr(
            "zeus_code/__init__.py",
            f'__version__ = "{version}"\nPROTOCOL_VERSION = {protocol_version}\n',
        )
        if include_cli:
            archive.writestr("zeus_code/cli.py", "def main():\n    return 0\n")
        archive.writestr(
            "zeus_code/storage.py",
            f"SCHEMA_VERSION = {schema_version}\n",
        )
    return output.getvalue()


def daemon_zipapp(
    version: str,
    *,
    schema_version: int = SCHEMA_VERSION,
    protocol_version: int = PROTOCOL_VERSION,
) -> bytes:
    output = io.BytesIO()
    main = f'''# zeus_code.cli fixture entrypoint
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys

data_dir = Path(sys.argv[1])
data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
lock = os.open(data_dir / "server.lock", os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
database = sqlite3.connect(data_dir / "state.sqlite3")
database.execute("PRAGMA user_version = {schema_version}")
database.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, value TEXT)")
database.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, state TEXT)")
database.execute("INSERT OR REPLACE INTO records VALUES ('record-1', 'preserve me')")
database.execute("INSERT OR REPLACE INTO runs VALUES ('run-1', 'running')")
database.commit()
(data_dir / "server.json").write_text(json.dumps({{
    "pid": os.getpid(),
    "protocol_version": {protocol_version},
    "server_id": "fixture-server",
    "version": {version!r},
}}) + "\\n")
print(json.dumps({{"pid": os.getpid()}}), flush=True)
for command in sys.stdin:
    if command.strip() == "probe":
        import lazy_probe
        print(lazy_probe.VALUE, flush=True)
    elif command.strip() == "stop":
        break
database.close()
fcntl.flock(lock, fcntl.LOCK_UN)
os.close(lock)
'''
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", main)
        archive.writestr(
            "zeus_code/__init__.py",
            f'__version__ = "{version}"\nPROTOCOL_VERSION = {protocol_version}\n',
        )
        archive.writestr("zeus_code/cli.py", "def main():\n    return 0\n")
        archive.writestr("zeus_code/storage.py", f"SCHEMA_VERSION = {schema_version}\n")
        archive.writestr("lazy_probe.py", f"VALUE = 'lazy runtime {version}'\n")
    return output.getvalue()


class FakeRelease:
    def __init__(
        self,
        version: str,
        *,
        artifact_version: str | None = None,
        metadata_version: str | None = None,
        metadata_schema: int = SCHEMA_VERSION,
        metadata_protocol: int = PROTOCOL_VERSION,
        artifact_schema: int = SCHEMA_VERSION,
        artifact_protocol: int = PROTOCOL_VERSION,
        artifact: bytes | None = None,
    ) -> None:
        artifact = artifact or zipapp(
            artifact_version or version,
            schema_version=artifact_schema,
            protocol_version=artifact_protocol,
        )
        metadata = json.dumps(
            {
                "version": metadata_version or version,
                "schema_version": metadata_schema,
                "protocol_version": metadata_protocol,
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

    def managed_runtime(self, target: Path | None = None) -> Path:
        inspected = updater._inspect_target(target or self.target)
        self.assertEqual("managed", inspected.kind)
        self.assertIsNotNone(inspected.runtime)
        assert inspected.runtime is not None
        return inspected.runtime

    @contextmanager
    def running_daemon(self, runtime: Path):
        process = subprocess.Popen(
            [sys.executable, str(runtime), str(self.data_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 5.0)
        if not ready:
            process.terminate()
            process.wait(timeout=5)
            self.fail("fixture daemon did not become ready")
        line = process.stdout.readline()
        if process.poll() is not None:
            self.fail(f"fixture daemon exited during startup: {line}")
        metadata = json.loads(line)
        try:
            yield process, metadata
        finally:
            if process.poll() is None:
                assert process.stdin is not None
                try:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                    process.wait(timeout=5)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=5)
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()

    def probe_daemon(self, process: subprocess.Popen[str]) -> str:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write("probe\n")
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 5.0)
        self.assertTrue(ready, "fixture daemon did not answer lazy-load probe")
        return process.stdout.readline().strip()

    def test_installs_latest_release_to_path_with_spaces(self) -> None:
        release = FakeRelease("1.1.0")

        result, output = self.run_update(release)

        self.assertEqual(0, result)
        runtime = self.managed_runtime()
        self.assertEqual(release.values[ARTIFACT_URL], runtime.read_bytes())
        self.assertNotEqual(release.values[ARTIFACT_URL], self.target.read_bytes())
        self.assertTrue(self.target.stat().st_mode & 0o111)
        launched = subprocess.run(
            [str(self.target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
        self.assertEqual(b"", launched.stderr)
        self.assertEqual(0, launched.returncode)
        self.assertIn("Installed Zeus Code 1.1.0", output)
        self.assertIn(f"Immutable runtime: {runtime}", output)
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

    def test_managed_up_to_date_and_newer_bundle_do_not_download_assets(self) -> None:
        installed = FakeRelease("1.1.0")
        self.run_update(installed)
        current_launcher = self.target.read_bytes()

        same = FakeRelease("1.1.0")
        result, output = self.run_update(same)

        self.assertEqual(0, result)
        self.assertIn("up to date", output)
        self.assertEqual(current_launcher, self.target.read_bytes())
        self.assertEqual([updater.LATEST_RELEASE_URL], same.calls)

        newer = zipapp("2.0.0")
        self.target.unlink()
        self.target.write_bytes(newer)
        older_release = FakeRelease("1.1.0")
        result, output = self.run_update(older_release)
        self.assertEqual(0, result)
        self.assertIn("refusing to downgrade", output)
        self.assertEqual(newer, self.target.read_bytes())
        self.assertEqual([updater.LATEST_RELEASE_URL], older_release.calls)

    def test_current_legacy_zipapp_migrates_to_managed_launcher(self) -> None:
        old = zipapp("1.1.0")
        self.target.write_bytes(old)
        release = FakeRelease("1.1.0", artifact=old)

        result, output = self.run_update(release)

        self.assertEqual(0, result)
        self.assertEqual(old, self.managed_runtime().read_bytes())
        self.assertIn("Installed Zeus Code 1.1.0", output)
        self.assertEqual(
            [updater.LATEST_RELEASE_URL, SUMS_URL, METADATA_URL, ARTIFACT_URL],
            release.calls,
        )

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
        self.assertEqual(
            release.values[ARTIFACT_URL],
            self.managed_runtime(installed).read_bytes(),
        )
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
        new_runtime = self.managed_runtime(running)
        self.assertEqual(release.values[ARTIFACT_URL], new_runtime.read_bytes())
        backups = list(self.root.glob("renamed zeus executable.rollback-1.0.0*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(old, backups[0].read_bytes())
        old_digest = hashlib.sha256(old).hexdigest()
        retained = self.root / updater._RUNTIME_DIRECTORY / old_digest / "zeus-code.pyz"
        self.assertEqual(old, retained.read_bytes())

    def test_custom_command_name_remains_update_target_through_launcher(self) -> None:
        custom = self.root / "zeus-custom"
        custom.write_bytes(zipapp("1.0.0"))
        first = FakeRelease("1.1.0")
        output = io.StringIO()
        with mock.patch.object(updater, "_open_url", side_effect=first.open), mock.patch.object(
            updater.sys, "argv", [str(custom), "update"]
        ):
            with redirect_stdout(output):
                updater.update(self.data_dir)
        first_launcher = custom.read_bytes()
        first_target = updater._inspect_target(custom)
        self.assertEqual("managed", first_target.kind)
        assert first_target.runtime is not None

        second = FakeRelease("1.2.0")
        with mock.patch.object(updater, "_open_url", side_effect=second.open), mock.patch.dict(
            os.environ,
            {"ZEUS_CODE_INSTALL_TARGET": str(custom)},
            clear=False,
        ), mock.patch.object(updater.sys, "argv", [str(first_target.runtime), "update"]):
            with redirect_stdout(output):
                updater.update(self.data_dir)

        self.assertNotEqual(first_launcher, custom.read_bytes())
        self.assertEqual("1.2.0", updater._inspect_target(custom).version)
        self.assertFalse(self.target.exists())

    def test_direct_content_addressed_pyz_installs_standalone_command(self) -> None:
        home = self.root / "home"
        runtime = (
            home
            / ".local/share/zeus-code/runtimes"
            / ("a" * 64)
            / "zeus-code.pyz"
        )
        runtime.parent.mkdir(parents=True)
        old = zipapp("1.0.0")
        runtime.write_bytes(old)
        release = FakeRelease("1.1.0")
        output = io.StringIO()
        with mock.patch.object(updater, "_open_url", side_effect=release.open), mock.patch.object(
            updater.sys, "argv", [str(runtime), "update"]
        ), mock.patch.dict(
            os.environ,
            {"HOME": str(home), "PATH": "/usr/bin"},
            clear=False,
        ):
            with redirect_stdout(output):
                result = updater.update(self.data_dir)

        installed = home / ".local/bin/zeus-code"
        self.assertEqual(0, result)
        self.assertEqual(old, runtime.read_bytes())
        self.assertEqual("managed", updater._inspect_target(installed).kind)
        self.assertIn(f"Installed Zeus Code 1.1.0 at {installed}", output.getvalue())

    def test_refuses_unknown_target_without_using_network(self) -> None:
        self.target.write_text("#!/bin/sh\necho unrelated\n")
        release = FakeRelease("1.1.0")

        with self.assertRaisesRegex(RuntimeError, "unknown non-Zeus"):
            self.run_update(release)

        self.assertEqual([], release.calls)
        self.assertEqual("#!/bin/sh\necho unrelated\n", self.target.read_text())

    def test_refuses_symbolic_link_target_without_using_network(self) -> None:
        destination = self.root / "real-zeus-code"
        original = zipapp("1.0.0")
        destination.write_bytes(original)
        self.target.symlink_to(destination)
        release = FakeRelease("1.1.0")

        with self.assertRaisesRegex(RuntimeError, "symbolic-link update target"):
            self.run_update(release)

        self.assertTrue(self.target.is_symlink())
        self.assertEqual(original, destination.read_bytes())
        self.assertEqual([], release.calls)

    def test_refuses_symbolic_link_from_launcher_environment(self) -> None:
        destination = self.root / "managed-destination"
        destination.write_bytes(zipapp("1.0.0"))
        link = self.root / "public-command"
        link.symlink_to(destination)
        release = FakeRelease("1.1.0")

        with mock.patch.dict(
            os.environ,
            {"ZEUS_CODE_INSTALL_TARGET": str(link)},
            clear=False,
        ), mock.patch.object(updater, "_open_url", side_effect=release.open):
            with self.assertRaisesRegex(RuntimeError, "symbolic-link update target"):
                updater.update(self.data_dir)

        self.assertTrue(link.is_symlink())
        self.assertEqual([], release.calls)

    def test_refuses_symbolic_link_runtime_root(self) -> None:
        destination = self.root / "external-runtimes"
        destination.mkdir()
        (self.install_dir / updater._RUNTIME_DIRECTORY).symlink_to(destination)
        release = FakeRelease("1.1.0")

        with self.assertRaisesRegex(RuntimeError, "symbolic link|symbolic-link"):
            self.run_update(release)

        self.assertFalse(self.target.exists())
        self.assertEqual([], list(destination.iterdir()))

    def test_concurrent_update_lock_serializes_target_switch(self) -> None:
        old = zipapp("1.0.0")
        self.target.write_bytes(old)
        lock_path = self.install_dir / ".zeus-code.update.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(RuntimeError, "Another Zeus Code update"):
                self.run_update(FakeRelease("1.1.0"))
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertEqual(old, self.target.read_bytes())

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

    def test_active_daemon_from_other_runtime_survives_update_with_state(self) -> None:
        old = zipapp("1.0.0")
        self.target.write_bytes(old)
        daemon_runtime = self.root / "daemon-runtime.pyz"
        daemon_runtime.write_bytes(daemon_zipapp("1.0.0"))
        release = FakeRelease("1.1.0")

        with self.running_daemon(daemon_runtime) as (process, ready):
            result, output = self.run_update(release)

            self.assertEqual(0, result)
            self.assertEqual(ready["pid"], process.pid)
            self.assertIsNone(process.poll())
            self.assertEqual("lazy runtime 1.0.0", self.probe_daemon(process))
            server = json.loads((self.data_dir / "server.json").read_text())
            self.assertEqual(process.pid, server["pid"])
            database = sqlite3.connect(self.data_dir / "state.sqlite3")
            self.assertEqual(
                ("preserve me",),
                database.execute(
                    "SELECT value FROM records WHERE id = 'record-1'"
                ).fetchone(),
            )
            self.assertEqual(
                ("running",),
                database.execute("SELECT state FROM runs WHERE id = 'run-1'").fetchone(),
            )
            database.close()
            self.assertEqual(release.values[ARTIFACT_URL], self.managed_runtime().read_bytes())
            self.assertIn("Installed Zeus Code 1.1.0", output)

        backups = list((self.data_dir / "backups").glob("state-before-1.1.0-*.sqlite3"))
        self.assertEqual(1, len(backups))
        backup = sqlite3.connect(backups[0])
        self.assertEqual(
            ("running",),
            backup.execute("SELECT state FROM runs WHERE id = 'run-1'").fetchone(),
        )
        backup.close()

    def test_active_daemon_from_managed_runtime_survives_launcher_switch(self) -> None:
        old_artifact = daemon_zipapp("1.0.0")
        self.run_update(FakeRelease("1.0.0", artifact=old_artifact))
        old_runtime = self.managed_runtime()
        old_launcher = self.target.read_bytes()
        release = FakeRelease("1.1.0")

        with self.running_daemon(self.target) as (process, ready):
            result, output = self.run_update(release)

            self.assertEqual(0, result)
            self.assertEqual(ready["pid"], process.pid)
            self.assertIsNone(process.poll())
            self.assertEqual("lazy runtime 1.0.0", self.probe_daemon(process))
            self.assertEqual(old_artifact, old_runtime.read_bytes())
            self.assertEqual(release.values[ARTIFACT_URL], self.managed_runtime().read_bytes())
            self.assertIn("Installed Zeus Code 1.1.0", output)

        rollback = list(self.install_dir.glob("zeus-code.rollback-1.0.0*"))
        self.assertEqual(1, len(rollback))
        self.assertEqual(old_launcher, rollback[0].read_bytes())

    def test_fresh_install_dir_does_not_disturb_active_daemon(self) -> None:
        daemon_runtime = self.root / "daemon-runtime.pyz"
        daemon_runtime.write_bytes(daemon_zipapp("1.0.0"))
        release = FakeRelease("1.1.0")

        with self.running_daemon(daemon_runtime) as (process, _):
            result, output = self.run_update(release)

            self.assertEqual(0, result)
            self.assertIsNone(process.poll())
            self.assertEqual("lazy runtime 1.0.0", self.probe_daemon(process))
            self.assertEqual(release.values[ARTIFACT_URL], self.managed_runtime().read_bytes())
            self.assertIn("Installed Zeus Code 1.1.0", output)

    def test_incompatible_active_daemon_defers_client_switch(self) -> None:
        daemon_runtime = self.root / "daemon-runtime.pyz"
        daemon_runtime.write_bytes(
            daemon_zipapp("0.9.0", protocol_version=PROTOCOL_VERSION + 1)
        )
        release = FakeRelease("1.1.0")

        with self.running_daemon(daemon_runtime) as (process, _):
            result, output = self.run_update(release)

            self.assertEqual(0, result)
            self.assertIsNone(process.poll())
            self.assertFalse(self.target.exists())
            self.assertIn("executable switch is deferred", output)
            self.assertIn("cannot connect to the current daemon protocol", output)
            self.assertNotIn("verified new runtime is usable now", output)
            self.assertEqual("lazy runtime 0.9.0", self.probe_daemon(process))

    def test_exact_live_legacy_zipapp_is_retained_and_switch_is_deferred(self) -> None:
        old = daemon_zipapp("1.0.0")
        self.target.write_bytes(old)
        release = FakeRelease("1.1.0")

        with self.running_daemon(self.target) as (process, ready):
            result, output = self.run_update(release)

            self.assertEqual(0, result)
            self.assertEqual(old, self.target.read_bytes())
            self.assertEqual(ready["pid"], process.pid)
            self.assertIsNone(process.poll())
            self.assertEqual("lazy runtime 1.0.0", self.probe_daemon(process))
            database = sqlite3.connect(self.data_dir / "state.sqlite3")
            self.assertEqual(
                ("running",),
                database.execute("SELECT state FROM runs WHERE id = 'run-1'").fetchone(),
            )
            database.close()
            self.assertIn("executable switch is deferred", output)
            self.assertIn("active local and remote provider work was left running", output)
            self.assertIn("verified new runtime is usable now", output)
            new_digest = hashlib.sha256(release.values[ARTIFACT_URL]).hexdigest()
            new_runtime = (
                self.install_dir
                / updater._RUNTIME_DIRECTORY
                / new_digest
                / "zeus-code.pyz"
            )
            self.assertEqual(release.values[ARTIFACT_URL], new_runtime.read_bytes())
            old_digest = hashlib.sha256(old).hexdigest()
            retained = (
                self.install_dir
                / updater._RUNTIME_DIRECTORY
                / old_digest
                / "zeus-code.pyz"
            )
            self.assertEqual(old, retained.read_bytes())

        backups = list((self.data_dir / "backups").glob("state-before-1.1.0-*.sqlite3"))
        self.assertEqual(1, len(backups))
        self.assertEqual([], list(self.install_dir.glob("zeus-code.rollback-*")))

        resumed = FakeRelease("1.1.0")
        result, output = self.run_update(resumed)
        self.assertEqual(0, result)
        self.assertEqual(resumed.values[ARTIFACT_URL], self.managed_runtime().read_bytes())
        self.assertIn("Installed Zeus Code 1.1.0", output)
        rollback = list(self.install_dir.glob("zeus-code.rollback-1.0.0*"))
        self.assertEqual(1, len(rollback))
        self.assertEqual(old, rollback[0].read_bytes())

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
        self.assertEqual(release.values[ARTIFACT_URL], self.managed_runtime().read_bytes())
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

    def test_wrong_manifest_or_embedded_compatibility_is_rejected(self) -> None:
        for name, release, phrase in (
            (
                "manifest schema",
                FakeRelease("1.1.0", metadata_schema=SCHEMA_VERSION + 1),
                "schema version",
            ),
            (
                "manifest protocol",
                FakeRelease("1.1.0", metadata_protocol=PROTOCOL_VERSION + 1),
                "protocol version",
            ),
            (
                "artifact schema",
                FakeRelease("1.1.0", artifact_schema=SCHEMA_VERSION + 1),
                "schema version",
            ),
            (
                "artifact protocol",
                FakeRelease("1.1.0", artifact_protocol=PROTOCOL_VERSION + 1),
                "protocol version",
            ),
        ):
            with self.subTest(name=name):
                old = zipapp("1.0.0")
                self.target.write_bytes(old)
                with self.assertRaisesRegex(RuntimeError, phrase) as raised:
                    self.run_update(release)
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

    def test_symbolic_link_backup_directory_is_rejected(self) -> None:
        old = zipapp("1.0.0")
        self.target.write_bytes(old)
        self.data_dir.mkdir()
        database = sqlite3.connect(self.data_dir / "state.sqlite3")
        database.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        database.execute("CREATE TABLE records(value TEXT)")
        database.execute("INSERT INTO records VALUES ('untouched')")
        database.commit()
        database.close()
        external = self.root / "external-backups"
        external.mkdir()
        (self.data_dir / "backups").symlink_to(external)

        with self.assertRaisesRegex(RuntimeError, "backup directory is a symbolic link"):
            self.run_update(FakeRelease("1.1.0"))

        self.assertEqual(old, self.target.read_bytes())
        self.assertEqual([], list(external.iterdir()))


if __name__ == "__main__":
    unittest.main()
