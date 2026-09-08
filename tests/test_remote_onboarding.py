"""Remote onboarding UI/cache integration over a real daemon, with SSH provision stubbed."""
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from zeus_code.client import RPCClient, RPCError
from zeus_code.daemon import Daemon
from zeus_code.tui import TUIApplication
from zeus_code.workspace import Workspace


class TestProvider:
    async def check(self):
        return {'available': True, 'detail': 'Fixture provider; no model calls'}


class RemoteOnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        work = Path(__file__).resolve().parents[1] / '.work'
        work.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=work)
        self.root = Path(self.temporary.name)
        self.projects = self.root / 'remote projects'
        self.projects.mkdir()
        for name in ('one', 'two'):
            subprocess.run(['git', 'init', '-q', str(self.projects / name)], check=True)
        self.state = self.root / 'server'
        self.daemon = Daemon(self.state, providers={'codex': TestProvider})
        await self.daemon.start()
        self.calls = []

        def factory(**kwargs):
            self.calls.append(kwargs)
            return RPCClient(self.state)

        self.factory = factory
        self.workspace = Workspace(cache_path=self.root / 'client.json', rpc_factory=factory)
        self.app = TUIApplication(self.workspace)
        self.provision = AsyncMock(return_value={'remote_command': '/home/test/Zeus Runtime/zeus-code.pyz'})
        self.patch = patch('zeus_code.remote.provision_remote', self.provision)
        self.patch.start()

    async def asyncTearDown(self):
        for task in list(self.app.tasks):
            task.cancel()
        await asyncio.gather(*self.app.tasks, return_exceptions=True)
        await self.workspace.close()
        await self.daemon.close()
        self.patch.stop()
        self.temporary.cleanup()

    async def settle(self):
        await asyncio.wait_for(asyncio.gather(*self.app.tasks, return_exceptions=True), 10)
        await asyncio.sleep(0)

    async def test_connect_import_select_and_reopen_preserve_saved_runtime(self):
        self.app._remote_server_form('dev')
        self.app.form.values[1] = str(self.projects)
        self.app.form.key(19)
        self.app.form.key(19)  # Duplicate clicks never provision or import twice.
        await self.settle()
        self.assertIsNone(self.app.form)
        self.assertEqual(self.provision.await_count, 1)
        machine_id = self.workspace.selected_machine_id
        self.assertNotEqual(machine_id, 'local')
        self.assertEqual(self.workspace.selected_machine['host'], 'dev')
        self.assertEqual(len(self.workspace.projects()), 2)
        self.assertIn('2 projects', self.app.status)
        self.assertTrue(any(c.get('remote_command') == '/home/test/Zeus Runtime/zeus-code.pyz' for c in self.calls))
        project = self.workspace.projects()[0]
        self.workspace.switch(machine_id, project['id'])
        self.app._new_thread_form()
        self.assertEqual(self.app.form.values[0], project['path'])
        self.assertEqual(self.app._form_machine_id, machine_id)
        await self.workspace.close()
        reopened = Workspace(cache_path=self.root / 'client.json', rpc_factory=self.factory)
        try:
            self.assertEqual(reopened.selected_machine_id, machine_id)
            self.assertEqual(len(reopened.projects()), 2)
            await reopened.sync_machine(machine_id)
            self.assertEqual(reopened.selected_machine['connection'], 'connected')
        finally:
            await reopened.close()

    async def test_failed_root_keeps_form_and_retry_imports_once(self):
        self.app._remote_server_form('dev')
        form = self.app.form
        form.values[1] = str(self.root / 'missing')
        form.key(19)
        await self.settle()
        self.assertIs(self.app.form, form)
        self.assertTrue(form.error)
        self.assertEqual(self.workspace.selected_machine_id, 'local')
        form.values[1] = str(self.projects)
        form.key(19)
        await self.settle()
        self.assertIsNone(self.app.form)
        result = await self.workspace.connect_remote('dev', str(self.projects))
        self.assertEqual(result['imported'], 0)
        self.assertEqual(len(self.workspace.machines), 2)
        self.assertEqual(result['projects'], 2)

    async def test_unsafe_host_and_duplicate_name_fail_before_provisioning(self):
        with self.assertRaises(ValueError):
            await self.workspace.connect_remote('-oProxyCommand=oops', str(self.projects))
        with self.assertRaises(ValueError):
            await self.workspace.connect_remote('dev', str(self.projects), alias='local')
        self.provision.assert_not_awaited()

    async def test_legacy_server_reports_restart_without_stopping_it(self):
        client = AsyncMock()
        client.call.side_effect = [{"projects": []}, RPCError("unknown_method", "Unknown method")]
        self.workspace.rpc_factory = lambda **kwargs: client
        with self.assertRaisesRegex(RuntimeError, "Finish its active work"):
            await self.workspace.connect_remote('dev', str(self.projects))
        self.assertEqual([call.args[0] for call in client.call.await_args_list],
                         ['snapshot', 'discover_projects'])
        client.close.assert_awaited_once()
        self.assertEqual(self.workspace.selected_machine_id, 'local')
