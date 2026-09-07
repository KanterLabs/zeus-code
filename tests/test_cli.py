from __future__ import annotations

import json
import io
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from zeus_code import cli


WORK = Path(__file__).resolve().parents[1] / ".work"


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, data_dir, host=None, remote_command="zeus-code") -> None:
        self.data_dir = data_dir
        self.host = host
        self.hello = {"protocol_version": 1, "server_id": "server", "pid": 123}
        self.calls: list[tuple[str, object]] = []
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def call(self, method, params=None):
        self.calls.append((method, params))
        return {"method": method, "params": params}


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.instances.clear()
        WORK.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)

    def run_rpc(self, arguments: list[str]) -> tuple[int, FakeClient]:
        with mock.patch.object(cli, "RPCClient", FakeClient), mock.patch("builtins.print"):
            result = cli.main([*arguments, "--data-dir", str(self.data_dir)])
        return result, FakeClient.instances[-1]

    def test_default_and_connect_open_tui(self) -> None:
        calls = []
        module = types.ModuleType("zeus_code.tui")
        module.run_tui = lambda **kwargs: calls.append(kwargs) or 0
        with mock.patch.dict("sys.modules", {"zeus_code.tui": module}):
            self.assertEqual(cli.main(["--data-dir", str(self.data_dir)]), 0)
            self.assertEqual(cli.main(["connect", "homelab", "--data-dir", str(self.data_dir)]), 0)
        self.assertEqual(calls[0], {"data_dir": self.data_dir.resolve(), "initial_host": None})
        self.assertEqual(calls[1], {"data_dir": self.data_dir.resolve(), "initial_host": "homelab"})

    def test_tui_runtime_error_is_concise(self) -> None:
        module = types.ModuleType("zeus_code.tui")
        module.run_tui = mock.Mock(side_effect=RuntimeError("An interactive terminal is required."))
        stderr = io.StringIO()
        with mock.patch.dict("sys.modules", {"zeus_code.tui": module}), mock.patch("sys.stderr", stderr):
            self.assertEqual(cli.main(["--data-dir", str(self.data_dir)]), 1)
        self.assertEqual(stderr.getvalue(), "zeus-code: An interactive terminal is required.\n")

    def test_project_add_supports_global_options_after_nested_command(self) -> None:
        result, client = self.run_rpc(["project", "add", "~/repo", "--name", "Zeus", "--host", "homelab"])
        self.assertEqual(result, 0)
        self.assertEqual(client.host, "homelab")
        self.assertEqual(client.calls[0][0], "add_project")
        self.assertEqual(client.calls[0][1]["name"], "Zeus")
        self.assertEqual(client.calls[0][1]["path"], "~/repo")

    def test_thread_create_maps_options(self) -> None:
        result, client = self.run_rpc(
            ["thread", "create", "project", "Fix bug", "--provider", "codex", "--model", "gpt", "--worktree"]
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            client.calls[0],
            (
                "create_thread",
                {
                    "project_id": "project",
                    "title": "Fix bug",
                    "provider": "codex",
                    "worktree": True,
                    "model": "gpt",
                },
            ),
        )

    def test_send_generates_stable_request_id_for_call(self) -> None:
        result, client = self.run_rpc(["send", "thread", "hello"])
        self.assertEqual(result, 0)
        method, params = client.calls[0]
        self.assertEqual(method, "send")
        self.assertEqual(params["thread_id"], "thread")
        self.assertEqual(params["prompt"], "hello")
        self.assertRegex(params["request_id"], r"^[0-9a-f-]{36}$")

    def test_send_reports_request_id_if_transport_result_is_uncertain(self) -> None:
        class FailingClient(FakeClient):
            async def call(self, method, params=None):
                self.calls.append((method, params))
                raise ConnectionError("connection lost")

        stderr = io.StringIO()
        with mock.patch.object(cli, "RPCClient", FailingClient), mock.patch("sys.stderr", stderr):
            self.assertEqual(cli.main(["send", "thread", "hello", "--data-dir", str(self.data_dir)]), 1)
        request_id = FailingClient.instances[-1].calls[0][1]["request_id"]
        self.assertIn(f"--request-id {request_id}", stderr.getvalue())

    def test_double_dash_keeps_option_shaped_prompt_literal(self) -> None:
        with mock.patch.object(cli, "RPCClient", FakeClient), mock.patch("builtins.print"):
            result = cli.main(
                ["--data-dir", str(self.data_dir), "send", "thread", "--", "--host"]
            )
        client = FakeClient.instances[-1]
        self.assertEqual(result, 0)
        self.assertEqual(client.calls[0][1]["prompt"], "--host")

    def test_status_uses_snapshot(self) -> None:
        result, client = self.run_rpc(["status"])
        self.assertEqual(result, 0)
        self.assertEqual(client.calls, [("snapshot", None)])

    def test_result_output_is_json(self) -> None:
        with mock.patch.object(cli, "RPCClient", FakeClient), mock.patch("builtins.print") as printed:
            self.assertEqual(cli.main(["cancel", "thread", "--data-dir", str(self.data_dir)]), 0)
        rendered = printed.call_args.args[0]
        self.assertEqual(json.loads(rendered)["method"], "cancel")

    def test_source_launcher_detaches_without_pythonpath(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "zeus-code"
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        base = [str(launcher), "--data-dir", str(self.data_dir)]
        started = subprocess.run(
            [*base, "serve", "--background"],
            cwd=self.data_dir,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        try:
            self.assertEqual(started.returncode, 0, started.stderr)
            status = subprocess.run(
                [*base, "status"], env=env, text=True, capture_output=True, timeout=15
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(json.loads(status.stdout)["protocol_version"], 1)
        finally:
            subprocess.run([*base, "stop"], env=env, capture_output=True, timeout=15)
