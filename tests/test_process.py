from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from pathlib import Path

from zeus_code import process as process_module
from zeus_code.process import spawn_supervised


WORK = Path.cwd() / ".work" / "tmp"

WRITER = r"""
import os
import signal
import sys
import time

marker = sys.argv[1]
if len(sys.argv) > 2 and sys.argv[2] == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(marker, "a", buffering=1) as output:
    while True:
        output.write(f"{time.monotonic()}\n")
        output.flush()
        os.fsync(output.fileno())
        time.sleep(0.02)
"""


def descendant_provider(*, linger: bool, ignore_term: bool = False) -> str:
    return textwrap.dedent(
        f"""
        import os
        import subprocess
        import sys
        import time

        child = subprocess.Popen(
            [sys.executable, "-c", {WRITER!r}, sys.argv[1],
             {"ignore-term" if ignore_term else "handle-term"!r}]
        )
        deadline = time.monotonic() + 3.0
        while True:
            try:
                ready = os.path.getsize(sys.argv[1]) > 0
            except FileNotFoundError:
                ready = False
            if ready:
                break
            if time.monotonic() >= deadline:
                child.kill()
                try:
                    child.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
                raise RuntimeError("descendant did not complete its first write")
            time.sleep(0.01)
        print(child.pid, flush=True)
        {"time.sleep(60)" if linger else "raise SystemExit(7)"}
        """
    )


async def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.02)


class SupervisedProcessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        WORK.mkdir(parents=True, exist_ok=True)

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.directory = Path(self.temp.name)
        self.processes: list[asyncio.subprocess.Process] = []

    async def asyncTearDown(self) -> None:
        for process in self.processes:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 2.0)
                except TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
        self.temp.cleanup()

    async def spawn(self, *argv, **kwargs) -> asyncio.subprocess.Process:
        process = await spawn_supervised(*argv, **kwargs)
        self.processes.append(process)
        return process

    async def test_stdio_environment_cwd_and_exit_status_are_transparent(self) -> None:
        environment = os.environ.copy()
        environment["ZEUS_PROCESS_TEST"] = "present"
        code = r"""
import json
import os
import sys

request = json.loads(sys.stdin.readline())
print(json.dumps({"request": request, "cwd": os.getcwd(),
                  "env": os.environ["ZEUS_PROCESS_TEST"]}), flush=True)
print("provider diagnostic", file=sys.stderr, flush=True)
raise SystemExit(23)
"""
        process = await self.spawn(
            sys.executable,
            "-c",
            code,
            cwd=self.directory,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = await process.communicate(b'{"value": 4}\n')

        self.assertEqual(process.returncode, 23)
        self.assertEqual(
            json.loads(stdout),
            {
                "request": {"value": 4},
                "cwd": str(self.directory),
                "env": "present",
            },
        )
        self.assertEqual(stderr, b"provider diagnostic\n")

    async def test_missing_executable_raises_original_os_error(self) -> None:
        missing = self.directory / "does-not-exist"
        with self.assertRaises(FileNotFoundError) as caught:
            await spawn_supervised(
                missing,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        self.assertEqual(Path(caught.exception.filename), missing)

    async def test_rejects_disabling_owned_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "start_new_session=True"):
            await spawn_supervised(sys.executable, start_new_session=False)

    async def test_stalled_startup_is_bounded_and_reaps_guardian(self) -> None:
        guardian_pid_file = self.directory / "stalled-guardian"
        stalled_supervisor = r"""
import os
import sys
import time

with open(sys.argv[5], "w") as output:
    output.write(str(os.getpid()))
time.sleep(60)
"""
        with (
            mock.patch.object(process_module, "_SUPERVISOR_CODE", stalled_supervisor),
            mock.patch.object(process_module, "_START_TIMEOUT", 0.2),
            self.assertRaisesRegex(OSError, "timed out"),
        ):
            await spawn_supervised(sys.executable, guardian_pid_file)

        guardian_pid = int(guardian_pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(guardian_pid, 0)

    async def test_malformed_startup_status_reaps_guardian(self) -> None:
        guardian_pid_file = self.directory / "malformed-guardian"
        malformed_supervisor = r"""
import os
import sys
import time

with open(sys.argv[5], "w") as output:
    output.write(str(os.getpid()))
os.write(int(sys.argv[2]), b"not-json\n")
os.close(int(sys.argv[2]))
time.sleep(60)
"""
        with (
            mock.patch.object(process_module, "_SUPERVISOR_CODE", malformed_supervisor),
            self.assertRaisesRegex(OSError, "before launching"),
        ):
            await spawn_supervised(sys.executable, guardian_pid_file)

        guardian_pid = int(guardian_pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(guardian_pid, 0)

    async def test_normal_provider_exit_cleans_lingering_descendant(self) -> None:
        marker = self.directory / "normal-exit-writes"
        process = await self.spawn(
            sys.executable,
            "-c",
            descendant_provider(linger=False, ignore_term=True),
            str(marker),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        descendant_pid = int(await asyncio.wait_for(process.stdout.readline(), 5.0))
        await asyncio.wait_for(process.wait(), 3.0)

        self.assertEqual(process.returncode, 7)
        await wait_until(lambda: marker.exists() and marker.stat().st_size > 0)
        size = marker.stat().st_size
        await asyncio.sleep(0.15)
        self.assertEqual(marker.stat().st_size, size)
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant_pid, 0)

    async def test_terminate_cleans_provider_group_including_stubborn_child(self) -> None:
        marker = self.directory / "terminated-writes"
        process = await self.spawn(
            sys.executable,
            "-c",
            descendant_provider(linger=True, ignore_term=True),
            str(marker),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        descendant_pid = int(await asyncio.wait_for(process.stdout.readline(), 5.0))
        await wait_until(lambda: marker.exists() and marker.stat().st_size > 0)

        process.terminate()
        await asyncio.wait_for(process.wait(), 3.0)

        self.assertEqual(process.returncode, -signal.SIGTERM)
        size = marker.stat().st_size
        await asyncio.sleep(0.15)
        self.assertEqual(marker.stat().st_size, size)
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant_pid, 0)

    async def test_parent_sigkill_stops_provider_descendant_writes(self) -> None:
        marker = self.directory / "orphan-writes"
        provider_code = descendant_provider(linger=True)
        parent_code = r"""
import asyncio
import json
import sys

from zeus_code.process import spawn_supervised

async def main():
    process = await spawn_supervised(
        sys.executable, "-c", sys.argv[2], sys.argv[1],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    descendant_pid = int(await process.stdout.readline())
    print(json.dumps({"supervisor": process.pid, "descendant": descendant_pid}),
          flush=True)
    await process.wait()

asyncio.run(main())
"""
        environment = os.environ.copy()
        source_path = str(Path.cwd() / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            part
            for part in (source_path, environment.get("PYTHONPATH", ""))
            if part
        )
        parent = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            parent_code,
            str(marker),
            provider_code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        self.processes.append(parent)
        assert parent.stdout is not None
        ready = json.loads(await asyncio.wait_for(parent.stdout.readline(), 5.0))
        await wait_until(lambda: marker.exists() and marker.stat().st_size > 0)

        killed_at = time.monotonic()
        parent.kill()
        await parent.wait()
        await wait_until(
            lambda: marker.exists()
            and float(marker.read_text().splitlines()[-1]) <= time.monotonic() - 0.1,
            timeout=3.0,
        )
        last_write = float(marker.read_text().splitlines()[-1])
        size = marker.stat().st_size
        await asyncio.sleep(0.2)

        self.assertLess(last_write - killed_at, 0.75)
        self.assertEqual(marker.stat().st_size, size)
        for pid in (ready["supervisor"], ready["descendant"]):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
