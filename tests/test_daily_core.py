import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from zeus_code.activity import summarize_activity, timestamp
from zeus_code.health import _update_command, check_latest_release, update_client
from zeus_code.storage import Store
from zeus_code.updater import _Release


class AutomaticTitleTests(unittest.TestCase):
    def test_first_accepted_prompt_titles_only_untouched_conversations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            project = store.add_project('/repo', 'Repo', 'main')
            thread = store.create_thread(project['id'], 'New conversation', 'codex', '/repo', 'main', False)
            run, created = store.create_run(thread['id'], '  Fix\n the login bug  ', 'request-1')
            self.assertTrue(created)
            self.assertEqual(store.thread(thread['id'])['title'], 'Fix the login bug')
            store.update_thread(thread['id'], title='My custom title')
            store.create_run(thread['id'], '  Fix\n the login bug  ', 'request-1')
            self.assertEqual(store.thread(thread['id'])['title'], 'My custom title')
            store.finish_run(run['id'], 'completed')
            store.create_run(thread['id'], 'Another request', 'request-2')
            self.assertEqual(store.thread(thread['id'])['title'], 'My custom title')
            custom = store.create_thread(project['id'], 'Named by me', 'codex', '/repo', 'main', False)
            store.create_run(custom['id'], 'Do something else', 'request-3')
            self.assertEqual(store.thread(custom['id'])['title'], 'Named by me')
            store.close()
            reopened = Store(Path(directory))
            self.assertEqual(reopened.thread(thread['id'])['title'], 'My custom title')
            reopened.close()

    def test_quiet_provider_is_observed_silence_not_invented_failure(self):
        start = '2026-09-08T12:00:00Z'
        run = {'id': 'r', 'state': 'running', 'created_at': start, 'last_event_at': start}
        activity = summarize_activity({'provider': 'codex'}, [], run, now=timestamp(start) + 150)
        self.assertEqual(activity.state, 'running')
        self.assertEqual(activity.label, 'No recent activity')
        self.assertIn('2m 30s', activity.last_activity)
        self.assertIn('check connection or wait', activity.detail)
        offline = summarize_activity({}, [], run, now=timestamp(start) + 150, stale=True)
        self.assertIn('Reconnecting', offline.detail)
        self.assertFalse(offline.active)


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_captures_output_and_surfaces_failure(self):
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b'Installed; old runtime retained.\n', None))
        with patch('zeus_code.health.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as launch:
            self.assertEqual(await update_client(Path('/state')), 'Installed; old runtime retained.')
        self.assertNotIn('stop', launch.call_args.args)
        process.returncode = 1
        process.communicate.return_value = (b'Checksum mismatch', None)
        with patch('zeus_code.health.asyncio.create_subprocess_exec', AsyncMock(return_value=process)):
            with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                await update_client(Path('/state'))

    async def test_release_check_is_read_only_and_rejects_downgrade(self):
        with patch('zeus_code.updater._release', return_value=_Release('0.0.1', {})):
            result = await check_latest_release()
        self.assertFalse(result['update_available'])
        self.assertEqual(result['latest_version'], '0.0.1')

    def test_update_uses_exact_runtime_and_keeps_managed_target(self):
        with patch('sys.argv', ['/test/runtime.pyz']), patch.dict('os.environ', {'ZEUS_CODE_INSTALL_TARGET': '/test/bin/zeus-code'}):
            command, env = _update_command(Path('/test/state'))
        self.assertEqual(command[1:], ['/test/runtime.pyz', '--data-dir', '/test/state', 'update'])
        self.assertEqual(env['ZEUS_CODE_INSTALL_TARGET'], '/test/bin/zeus-code')
        self.assertNotIn('stop', command)
