from __future__ import annotations

import hashlib
import json
import shlex
import unittest
from unittest import mock

from zeus_code import PROTOCOL_VERSION, __version__, remote


class DailyRemoteProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def test_guided_setup_returns_verified_health_and_provider_readiness(self) -> None:
        application = b"#!/usr/bin/env python3\nfixture"
        digest = hashlib.sha256(application).hexdigest()
        remote_command = f"/home/dev/.local/share/zeus-code/runtimes/{digest}/zeus-code.pyz"
        doctor = {
            "client_version": __version__,
            "server": {
                "version": __version__,
                "protocol_version": PROTOCOL_VERSION,
                "server_id": "server-123",
                "hostname": "buildbox",
                "pid": 4321,
            },
            "providers": {
                "codex": {
                    "available": False,
                    "status": "checking",
                    "detail": "Provider discovery is in progress.",
                },
                "opencode": {
                    "available": False,
                    "status": "unavailable",
                    "version": "1.2.3",
                    "detail": (
                        "OpenCode 1.2.3 is installed, but no provider is authenticated. "
                        "Run `opencode auth login`."
                    ),
                },
                "missing": {
                    "available": False,
                    "status": "unavailable",
                    "detail": "Provider CLI is not installed or is not on PATH.",
                },
                "ready": {
                    "available": True,
                    "status": "ready",
                    "version": "9.0",
                    "detail": "Provider is ready.",
                },
            },
        }
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
            if label == "Remote compatibility check":
                return b'{"ok":true}\n'
            if label == "Zeus Code installation":
                return json.dumps({"remote_command": remote_command}).encode()
            if label == "Zeus Code server startup":
                return b"Zeus Code is running\n"
            if label == "Remote connection verification":
                return json.dumps(doctor).encode()
            raise AssertionError(label)

        progress: list[str] = []
        with mock.patch.object(remote, "_application_bytes", return_value=application), mock.patch.object(
            remote, "_run_ssh", side_effect=run_ssh
        ):
            result = await remote.provision_remote(
                "dev", on_progress=progress.append, include_health=True
            )

        self.assertEqual(
            [
                "Checking SSH",
                "Preparing Zeus Code",
                "Installing Zeus Code",
                "Starting server",
                "Verifying connection",
                "Ready",
            ],
            progress,
        )
        self.assertEqual(remote_command, result["remote_command"])
        self.assertEqual(
            {
                "status": "ready",
                "host": "dev",
                "version": __version__,
                "server_version": __version__,
                "client_version": __version__,
                "protocol_version": PROTOCOL_VERSION,
                "server_id": "server-123",
                "version_match": True,
                "upgrade_status": "current",
                "hostname": "buildbox",
                "pid": 4321,
            },
            result["health"],
        )
        providers = result["providers"]
        self.assertEqual("unknown", providers["codex"]["installation_status"])
        self.assertEqual("unknown", providers["codex"]["authentication_status"])
        self.assertIn("still in progress", providers["codex"]["setup_action"])
        self.assertEqual("installed", providers["opencode"]["installation_status"])
        self.assertEqual("required", providers["opencode"]["authentication_status"])
        self.assertIn("opencode auth login", providers["opencode"]["setup_action"])
        self.assertEqual("missing", providers["missing"]["installation_status"])
        self.assertEqual("unknown", providers["missing"]["authentication_status"])
        self.assertEqual("installed", providers["ready"]["installation_status"])
        self.assertEqual("unknown", providers["ready"]["authentication_status"])

        self.assertEqual(4, len(calls))
        self.assertEqual([remote_command, "serve", "--background"], shlex.split(calls[2][1]))
        self.assertEqual([remote_command, "doctor"], shlex.split(calls[3][1]))
        self.assertFalse(any(" stop" in call[1] or " login" in call[1] for call in calls))

    async def test_compatible_older_daemon_is_ready_and_upgrade_is_deferred(self) -> None:
        application = b"fixture"
        digest = hashlib.sha256(application).hexdigest()
        remote_command = f"/home/dev/.local/share/zeus-code/runtimes/{digest}/zeus-code.pyz"
        calls: list[str] = []

        async def run_ssh(
            host: str,
            command: str,
            *,
            payload: bytes = b"",
            timeout: float,
            label: str,
        ) -> bytes:
            calls.append(command)
            if label == "Remote compatibility check":
                return b'{"ok":true}'
            if label == "Zeus Code installation":
                return json.dumps({"remote_command": remote_command}).encode()
            if label == "Zeus Code server startup":
                return b"already running"
            return json.dumps(
                {
                    "client_version": __version__,
                    "server": {
                        "version": "1.1.1",
                        "protocol_version": PROTOCOL_VERSION,
                        "server_id": "old-server",
                    },
                    "providers": {},
                }
            ).encode()

        progress: list[str] = []
        with mock.patch.object(remote, "_application_bytes", return_value=application), mock.patch.object(
            remote, "_run_ssh", side_effect=run_ssh
        ):
            result = await remote.provision_remote(
                "dev", on_progress=progress.append, include_health=True
            )

        health = result["health"]
        self.assertEqual("ready", health["status"])
        self.assertEqual("1.1.1", health["server_version"])
        self.assertEqual(__version__, health["client_version"])
        self.assertFalse(health["version_match"])
        self.assertEqual("deferred", health["upgrade_status"])
        self.assertIn("remains active", health["note"])
        self.assertEqual("Ready", progress[-1])
        self.assertFalse(any(" stop" in command for command in calls))


class DailyRemoteFailureTests(unittest.TestCase):
    def test_ssh_failures_have_stable_categories_and_specific_actions(self) -> None:
        fixtures = {
            "ssh_authentication": "Permission denied (publickey).",
            "ssh_host_key": "Host key verification failed.",
            "ssh_hostname": "Could not resolve hostname dev: Name or service not known",
            "ssh_refused": "connect to host dev port 22: Connection refused",
            "ssh_network": "connect to host dev port 22: No route to host",
            "ssh_proxy": "stdio forwarding failed through ProxyJump",
            "ssh_connection": "Connection closed by UNKNOWN port 65535",
        }
        for category, detail in fixtures.items():
            with self.subTest(category=category):
                error = remote._ssh_failure("dev", detail)
                self.assertEqual("ssh", error.stage)
                self.assertEqual(category, error.category)
                self.assertEqual("dev", error.host)
                self.assertEqual(detail, error.detail)
                self.assertTrue(error.action)
                self.assertIn(detail, str(error))
                self.assertEqual(category, error.as_dict()["category"])

    def test_remote_requirement_failures_tell_user_what_to_install(self) -> None:
        fixtures = (
            (127, "sh: python3: not found", "remote_python_missing", "Python 3.11"),
            (3, "Zeus Code requires Python 3.11 or newer", "remote_python_version", "Python 3.11"),
            (4, "Remote Python must include the curses module", "remote_python_curses", "curses"),
        )
        for status, detail, category, action_fragment in fixtures:
            with self.subTest(category=category):
                error = remote._remote_command_failure(
                    "dev", "Remote compatibility check", status, detail
                )
                self.assertEqual("requirements", error.stage)
                self.assertEqual(category, error.category)
                self.assertIn(action_fragment, error.action)

    def test_protocol_failure_is_a_version_mismatch_without_a_stop_command(self) -> None:
        error = remote._remote_command_failure(
            "dev",
            "Remote connection verification",
            1,
            "Protocol 2 is required (RPC version_mismatch)",
        )
        self.assertEqual("protocol_mismatch", error.code)
        self.assertEqual("verify", error.stage)
        self.assertIn("original Zeus Code installation", error.action)
        self.assertNotIn("`stop", error.action)


if __name__ == "__main__":
    unittest.main()
