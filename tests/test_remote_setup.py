from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from zeus_code import remote


WORK = Path(__file__).resolve().parents[1] / ".work"


class RemoteProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def test_provision_transfers_checksum_and_quotes_runtime_path(self) -> None:
        application = b"#!/usr/bin/env python3\ntest zip application"
        digest = hashlib.sha256(application).hexdigest()
        remote_command = (
            f"/home/dev's workspace/.local/share/zeus-code/runtimes/{digest}/zeus-code.pyz"
        )
        calls: list[tuple[str, str, bytes, float, str]] = []

        async def run_ssh(
            host: str,
            command: str,
            *,
            payload: bytes = b"",
            timeout: float,
            label: str,
        ) -> bytes:
            calls.append((host, command, payload, timeout, label))
            if len(calls) == 1:
                return b'{"ok":true}\n'
            if len(calls) == 2:
                return json.dumps({"remote_command": remote_command}).encode()
            return b"Zeus Code is running\n"

        progress: list[str] = []
        with mock.patch.object(remote, "_application_bytes", return_value=application), mock.patch.object(
            remote, "_run_ssh", side_effect=run_ssh
        ):
            result = await remote.provision_remote("dev-vm", on_progress=progress.append)

        self.assertEqual({"remote_command": remote_command}, result)
        self.assertEqual(
            ["Checking SSH", "Installing Zeus Code", "Starting server"], progress
        )
        self.assertEqual(["dev-vm"] * 3, [call[0] for call in calls])
        self.assertEqual([b"", application, b""], [call[2] for call in calls])
        check_argv = shlex.split(calls[0][1])
        self.assertEqual(["python3", "-c", remote._CHECK_SCRIPT], check_argv)
        install_argv = shlex.split(calls[1][1])
        self.assertEqual(
            ["python3", "-c", remote._INSTALL_SCRIPT, digest, str(len(application))],
            install_argv,
        )
        self.assertEqual(
            [remote_command, "serve", "--background"], shlex.split(calls[2][1])
        )

    async def test_rejects_unsafe_host_before_reporting_progress(self) -> None:
        progress: list[str] = []
        with self.assertRaisesRegex(ValueError, "Unsafe SSH host"):
            await remote.provision_remote("dev host;bad", on_progress=progress.append)
        self.assertEqual([], progress)

    def test_ssh_arguments_are_noninteractive_without_host_key_bypass(self) -> None:
        argv = remote._ssh_argv("dev-vm", "python3 -c 'print(1)'")
        self.assertEqual("ssh", argv[0])
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("ConnectTimeout=8", argv)
        self.assertEqual(("--", "dev-vm", "python3 -c 'print(1)'"), argv[-3:])
        self.assertFalse(any("StrictHostKeyChecking" in argument for argument in argv))
        self.assertFalse(any("UserKnownHostsFile" in argument for argument in argv))

    async def test_cancellation_terminates_the_owned_ssh_process(self) -> None:
        class FakeWriter:
            def __init__(self) -> None:
                self.closed = False

            def write(self, data: bytes) -> None:
                pass

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

            def is_closing(self) -> bool:
                return self.closed

            async def wait_closed(self) -> None:
                pass

        class HangingProcess:
            def __init__(self) -> None:
                self.stdin = FakeWriter()
                self.stdout = asyncio.StreamReader()
                self.stderr = asyncio.StreamReader()
                self.returncode: int | None = None
                self.terminated = False
                self.finished = asyncio.Event()

            async def wait(self) -> int:
                await self.finished.wait()
                assert self.returncode is not None
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.finished.set()

            def kill(self) -> None:
                self.returncode = -9
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.finished.set()

        process = HangingProcess()
        launched = asyncio.Event()

        async def create_process(*args: object, **kwargs: object) -> HangingProcess:
            launched.set()
            return process

        with mock.patch.object(remote.asyncio, "create_subprocess_exec", side_effect=create_process):
            task = asyncio.create_task(
                remote._run_ssh(
                    "dev-vm", "remote command", timeout=30, label="Remote setup"
                )
            )
            await launched.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(process.terminated)
        self.assertTrue(process.stdin.closed)


class RemoteInstallerFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        WORK.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)

    def install(self, application: bytes, digest: str) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        return subprocess.run(
            [sys.executable, "-c", remote._INSTALL_SCRIPT, digest, str(len(application))],
            input=application,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=5,
            check=False,
        )

    def test_installer_validates_checksum_and_reuses_immutable_runtime(self) -> None:
        application = b"#!/usr/bin/env python3\napplication bytes"
        digest = hashlib.sha256(application).hexdigest()

        first = self.install(application, digest)

        self.assertEqual(b"", first.stderr)
        self.assertEqual(0, first.returncode)
        target = Path(json.loads(first.stdout)["remote_command"])
        self.assertTrue(target.is_absolute())
        self.assertEqual(application, target.read_bytes())
        self.assertEqual(0o755, stat.S_IMODE(target.stat().st_mode))
        self.assertEqual(
            self.home / ".local/share/zeus-code/runtimes" / digest / "zeus-code.pyz",
            target,
        )
        self.assertEqual(0, self.install(application, digest).returncode)

        target.write_bytes(b"tampered")
        corrupt = self.install(application, digest)

        self.assertNotEqual(0, corrupt.returncode)
        self.assertIn(b"checksum mismatch", corrupt.stderr)
        self.assertEqual(b"tampered", target.read_bytes())
        self.assertEqual([], list(target.parent.glob(".zeus-code-*")))

    def test_installer_removes_temporary_file_after_bad_checksum(self) -> None:
        application = b"application bytes"
        incorrect = "0" * 64

        result = self.install(application, incorrect)

        runtime_dir = self.home / ".local/share/zeus-code/runtimes" / incorrect
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"checksum mismatch", result.stderr)
        self.assertFalse((runtime_dir / "zeus-code.pyz").exists())
        self.assertEqual([], list(runtime_dir.glob(".zeus-code-*")))


if __name__ == "__main__":
    unittest.main()
