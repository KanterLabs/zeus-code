#!/usr/bin/env python3
"""Exercise the real terminal UX against an isolated fixture daemon; no model calls."""
import argparse
import asyncio
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from zeus_code.daemon import Daemon
from zeus_code.workspace import Workspace


class FixtureProvider:
    starts = []
    gates = {}
    ticks = {}

    async def check(self):
        return {'available': True, 'detail': 'Isolated UX fixture', 'models': [
            {'id': 'fixture-balanced', 'name': 'Balanced', 'reasoning_efforts': ['low', 'high']},
            {'id': 'fixture-fast', 'name': 'Fast', 'reasoning_efforts': ['low']},
        ]}

    async def run(self, context, prompt):
        """Execute free local fixture turns, with gates for live-switch checks."""
        type(self).starts.append((context.thread_id, context.run_id, prompt))
        await context.emit('provider_session', {
            'session_id': context.session_id or 'fixture-session-' + context.thread_id,
        })
        if prompt.startswith('Hold live '):
            gate = type(self).gates.setdefault(context.thread_id, asyncio.Event())
            tick = 0
            while not gate.is_set():
                tick += 1
                type(self).ticks[context.thread_id] = tick
                await context.emit('status', {'text': f'{prompt} heartbeat {tick}'})
                try:
                    await asyncio.wait_for(gate.wait(), timeout=.12)
                except asyncio.TimeoutError:
                    pass
            await context.emit('message', {
                'role': 'assistant', 'item_id': 'fixture-answer',
                'text': f'{prompt} completed',
            })
            return
        await context.emit('message', {
            'role': 'assistant', 'item_id': 'fixture-answer',
            'text': 'Fixture retry completed without an external model call.',
        })


def counter_value(path):
    try:
        return int(path.read_text())
    except (FileNotFoundError, ValueError):
        return 0


def health_fixture_environment(base):
    """Install child-process seams that count checks and forbid real updates."""
    shim = base / 'health-shim'
    shim.mkdir()
    check_counter = base / 'health-checks'
    update_counter = base / 'update-attempts'
    module_origin = base / 'health-module-origin'
    check_counter.write_text('0')
    update_counter.write_text('0')
    (shim / 'sitecustomize.py').write_text('''\
import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import sys

_checks = Path(os.environ["ZEUS_CODE_E2E_HEALTH_COUNTER"])
_updates = Path(os.environ["ZEUS_CODE_E2E_UPDATE_COUNTER"])
_origin = Path(os.environ["ZEUS_CODE_E2E_HEALTH_MODULE"])
_patched_check = None
_patched_update = None

def _increment(path):
    try:
        current = int(path.read_text())
    except (FileNotFoundError, ValueError):
        current = 0
    path.write_text(str(current + 1))

def _patch_health(module):
    global _patched_check, _patched_update
    _origin.write_text(str(getattr(module, "__file__", "")))

    async def check_latest_release():
        _increment(_checks)
        return {
            "current_version": module.__version__,
            "latest_version": "9.9.9",
            "update_available": True,
        }

    async def update_client(*args, **kwargs):
        _increment(_updates)
        raise AssertionError("the UX fixture forbids client updates")

    _patched_check = module.check_latest_release = check_latest_release
    _patched_update = module.update_client = update_client

def _patch_tui(module):
    if _patched_check is not None:
        module.check_latest_release = _patched_check
    if _patched_update is not None:
        module.update_client = _patched_update

class _PatchLoader(importlib.abc.Loader):
    def __init__(self, fullname, loader):
        self.fullname = fullname
        self.loader = loader

    def create_module(self, spec):
        create = getattr(self.loader, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module):
        self.loader.exec_module(module)
        if self.fullname == "zeus_code.health":
            _patch_health(module)
        else:
            _patch_tui(module)

class _PatchFinder(importlib.abc.MetaPathFinder):
    _targets = {"zeus_code.health", "zeus_code.tui"}

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self._targets:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = _PatchLoader(fullname, spec.loader)
        return spec

sys.meta_path.insert(0, _PatchFinder())
''')
    return {
        # The executable supplies its own package path. Adding checkout src
        # here would make archive validation accidentally exercise source.
        'PYTHONPATH': str(shim),
        'ZEUS_CODE_E2E_HEALTH_COUNTER': str(check_counter),
        'ZEUS_CODE_E2E_UPDATE_COUNTER': str(update_counter),
        'ZEUS_CODE_E2E_HEALTH_MODULE': str(module_origin),
    }, check_counter, update_counter, module_origin


def assert_health_module_origin(executable, module_origin):
    loaded = module_origin.read_text().strip()
    assert loaded, 'health fixture did not record its loaded module'
    executable = executable.resolve()
    if executable.suffix in {'.pyz', '.pyzw'}:
        assert str(executable) in loaded, f'health fixture loaded {loaded}, not {executable}'
    elif executable != (ROOT / 'zeus-code').resolve():
        assert str((ROOT / 'src').resolve()) not in loaded, f'packaged E2E loaded checkout source: {loaded}'


async def terminal(
    executable, state, cache, size, scene, captures, baseline=False, *,
    env_overrides=None, health_counter=None, update_counter=None,
):
    pid, fd = pty.fork()
    if pid == 0:
        try:
            env = os.environ.copy()
            env.update(TERM='xterm-256color', ZEUS_CODE_CLIENT_STATE=str(cache))
            env.update(env_overrides or {})
            os.execve(sys.executable, [sys.executable, str(executable), '--data-dir', str(state)], env)
        except BaseException:
            os._exit(127)
    exited = False
    output = bytearray()
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', size[1], size[0], 0, 0))

    async def read_for(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b''
                output.extend(chunk)
            await asyncio.sleep(.02)

    async def expect(text, start=0):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await read_for(.1)
            if text.encode() in output[start:]:
                return
        raise AssertionError(f'{size} {scene}: missing {text!r}: {bytes(output[-1500:])!r}')

    async def send(keys, delay=.15):
        os.write(fd, keys)
        await read_for(delay)

    def capture(name):
        captures.mkdir(parents=True, exist_ok=True)
        (captures / f'{size[0]}x{size[1]}-{name}.ansi').write_bytes(output)

    try:
        await read_for(1)
        assert b'Traceback' not in output, f'{size} {scene}: client crashed: {bytes(output[-2000:])!r}'
        assert b'Error in sitecustomize' not in output, f'{size} {scene}: health fixture failed to load'
        if not baseline:
            assert b'Draft stays here' in output, f'{size} {scene}: conversation not visible'
        assert baseline or b'too small' not in output.lower(), f'{size}: unsupported terminal size'
        capture(scene)
        if scene == 'completed' and not baseline:
            before = len(output)
            os.write(fd, b'\x0b')  # Ctrl+K
            await expect('Command palette', before)
            os.write(fd, b'change model\r')
            await expect('Model for')
            os.write(fd, b'fixture-fast')
            await expect('fixture-fast')
            os.write(fd, b'\r')
            await expect('Reasoning for Fast')
            os.write(fd, b'\x1bOB\r')  # Choose low, then persist the model settings.
            await read_for(.4)  # The persisted state is asserted after the client exits.
            await read_for(.15)
            os.write(fd, b'\x0bopen attention\r')
            await expect('Attention')
            os.write(fd, b'\x1b')
            await read_for(.15)
            before = len(output)
            os.write(fd, b'\x0btoggle agent\r')
            await read_for(.3)
            await expect('Review tests', before)
            assert b'Traceback' not in output
            (captures / f'{size[0]}x{size[1]}-controls.ansi').write_bytes(output)
            if size[1] <= 18:
                os.write(fd, b'\x1b')
                await read_for(.15)
            os.write(fd, b'\x0bchoose theme\r')
            await expect('Theme')
            os.write(fd, b'\x1bOC\x13')  # Dark -> light, then Ctrl+S saves.
            await read_for(.3)
            capture('light')
        elif scene == 'wide-agents':
            # No F9: the task panel must be useful without knowing a shortcut.
            await expect('AGENTS')
            await expect('Review tests')
            await send(b'\t', .2)
            await send(b'\t', .2)
            before = len(output)
            await send(b'\r', .25)  # composer -> sidebar -> agents; open the visible running task.
            await expect('Agent details', before)
            await expect('Review tests', before)
            capture('wide-agent-details')
            await send(b'\x1b')
        elif scene == 'switch-running':
            await expect('Live A')
            await expect('Draft stays here in running alpha')
            before = len(output)
            await send(b'\x10betatwo')
            await expect('Thread switcher', before)
            before = len(output)
            await send(b'\r', .3)
            await expect('Second thread keeps its own beta draft', before)
            before = len(output)
            await send(b'\x10alphaone')
            await expect('Thread switcher', before)
            await send(b'\r', .3)
            # Curses may restore this screen using only cursor moves and the
            # changed cells, so the cache assertion after exit is the durable
            # proof that the second Enter returned to A with its draft intact.
            capture('running-thread-switch')
        elif scene == 'daily':
            assert health_counter is not None and update_counter is not None
            assert counter_value(health_counter) == 0, 'release check ran before Health was opened'
            assert counter_value(update_counter) == 0, 'client update ran before Health was opened'

            before = len(output)
            await send(b'\x0bpin thread')
            await expect('Pin thread', before)
            await send(b'\r', .35)
            before = len(output)
            await send(b'\x0bunpin thread')
            await expect('Unpin thread', before)
            capture('daily-pin')
            await send(b'\x1b')

            # Freeze a non-live history position, then prove another thread can
            # be found without disturbing either conversation's draft.
            await send(b'\x1b[5~', .25)
            before = len(output)
            await send(b'\x10UX error')
            await expect('Thread switcher', before)
            await expect('UX error', before)
            before = len(output)
            await send(b'\r', .35)
            await expect('Run failed', before)

            before = len(output)
            await send(b'\x05')  # Ctrl+E explains recovery without sending.
            await expect('Prompt recovery', before)
            capture('daily-failed-recovery-options')
            await send(b'\x1b')
            before = len(output)
            await send(b'\x0bretry failed prompt')
            await expect('Retry failed prompt', before)
            before = len(output)
            await send(b'\r', .25)
            await expect('Retry started as a new run', before)
            await read_for(1.2)  # Let the normal poll observe fixture completion before archiving.
            capture('daily-failed-retry')
            await send(b'Draft stays here in search target', .25)

            before = len(output)
            await send(b'\x0barchive thread')
            await expect('Archive thread', before)
            before = len(output)
            await send(b'\r', .2)
            await expect('Archived UX error', before)
            before = len(output)
            await send(b'\x0barchived threads')
            await expect('Archived threads', before)
            before = len(output)
            await send(b'\r', .2)
            await expect('Archived threads', before)
            await send(b'UX error', .2)
            before = len(output)
            await send(b'\r', .2)
            await expect('Restored UX error', before)
            await expect('Draft stays here in search target', before)
            capture('daily-archive-restore')

            before = len(output)
            await send(b'\x10UX completed')
            await expect('Thread switcher', before)
            before = len(output)
            await send(b'\r', .3)
            await expect('Draft stays here for daily reopen', before)

            before = len(output)
            await send(b'\x04')
            await expect('Changed files', before)
            await expect('daily-first.txt', before)
            await expect('daily-second.txt', before)
            before = len(output)
            await send(b'\x1bOB\r', .25)
            await expect('Patch: daily-second.txt', before)
            capture('daily-changed-file')
            await send(b'\x1b')

            before = len(output)
            await send(b'\x0bhealth')
            await expect('Health', before)
            before = len(output)
            await send(b'\r', .25)
            await expect('Health', before)
            await expect('Check now', before)
            assert counter_value(health_counter) == 0, 'opening Health performed a release check'
            assert counter_value(update_counter) == 0, 'opening Health performed an update'
            capture('daily-health-cached')
            before = len(output)
            await send(b'c', .25)
            await expect('9.9.9', before)
            assert counter_value(health_counter) == 1, 'explicit Check now did not perform exactly one check'
            assert counter_value(update_counter) == 0, 'release check unexpectedly ran the updater'
            capture('daily-health-checked')
            await send(b'\x1b')
        elif scene == 'remembered':
            assert health_counter is not None and update_counter is not None
            await expect('UX completed')
            await expect('Draft stays here for daily reopen')
            before = len(output)
            await send(b'\x0bunpin thread')
            await expect('Unpin thread', before)
            capture('daily-remembered-selection')
            await send(b'\x1b')
            before = len(output)
            await send(b'\x0bchange model\r')
            await expect('Model for', before)
            await expect('fixture-fast', before)
            capture('daily-remembered-model')
            await send(b'\x1b')
            assert counter_value(health_counter) == 1, 'reopening performed another release check'
            assert counter_value(update_counter) == 0, 'reopening ran the updater'
        os.write(fd, b'\x11')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            await read_for(.05)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                exited = True
                assert os.waitstatus_to_exitcode(status) == 0
                return
        raise AssertionError('Ctrl+Q did not exit the client')
    finally:
        if not exited:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(fd)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, default=ROOT / 'zeus-code')
    parser.add_argument('--baseline', action='store_true', help='Capture the old UI without new-control assertions')
    parser.add_argument('--captures', type=Path, default=ROOT / '.work/ux-captures')
    args = parser.parse_args()
    work = ROOT / '.work'
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ux-', dir=work) as folder:
        base = Path(folder)
        state, repo, cache = base / 's', base / 'r', base / 'client.json'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        FixtureProvider.starts = []
        FixtureProvider.gates = {}
        FixtureProvider.ticks = {}
        daemon = Daemon(state, providers={'codex': FixtureProvider})
        await daemon.start()
        try:
            store = daemon.store
            project = store.add_project(str(repo), 'UX fixture', 'main')
            scenes = {}
            scene_runs = {}
            for scene in ('empty', 'running', 'approval', 'error', 'completed'):
                thread = store.create_thread(project['id'], 'UX ' + scene, 'codex', repo, 'main', False,
                                             model='fixture-balanced', settings={'reasoning_effort': 'high'})
                scenes[scene] = thread
                if scene == 'empty':
                    continue
                run, _ = store.create_run(thread['id'], 'Check this fixture', str(uuid.uuid4()))
                scene_runs[scene] = run
                store.append_event(thread['id'], run['id'], 'tool', {
                    'item_id': 'collab', 'tool_type': 'collabAgentToolCall', 'title': 'Review agents',
                    'status': 'completed', 'agents': [] if scene == 'running' else [
                        {
                            'id': 'agent-one', 'parent_id': thread['id'], 'label': 'Review tests',
                            'state': 'running' if scene == 'running' else 'completed',
                            'result': None if scene == 'running' else 'Tests checked',
                        },
                        {'id': 'agent-two', 'parent_id': thread['id'], 'label': 'Review docs', 'state': 'completed', 'result': 'Docs checked'},
                    ],
                })
                if scene == 'running':
                    # Real pre-1.2.1 daemons stored these public Codex items
                    # without an agents array. The client must still show them.
                    for agent_id, label, kind in (
                        ('agent-one', 'Review tests', 'started'),
                        ('agent-two', 'Review docs', 'completed'),
                    ):
                        store.append_event(thread['id'], run['id'], 'tool', {
                            'item_id': 'legacy-' + agent_id,
                            'tool_type': 'subAgentActivity', 'title': 'subAgentActivity',
                            'status': 'completed',
                            'text': json.dumps({'agentThreadId': agent_id, 'agentPath': label, 'kind': kind}),
                        })
                if scene == 'completed':
                    store.append_event(thread['id'], run['id'], 'message', {
                        'role': 'assistant', 'item_id': 'answer', 'text': 'Checked the change.\n- Tests pass.\n```python\n    ready = True\n```'})
                    store.finish_run(run['id'], 'completed')
                elif scene == 'error':
                    store.finish_run(run['id'], 'failed', 'Fixture failure: check the chosen server')
                elif scene == 'approval':
                    store.create_approval(run['id'], {'command': 'fixture command'})
            for size in ((48, 16), (80, 24), (120, 35)):
                for scene, thread in scenes.items():
                    workspace = Workspace(data_dir=state, cache_path=cache)
                    await workspace.sync_machine('local')
                    workspace.switch('local', project['id'], thread['id'])
                    workspace.set_draft('Draft stays here')
                    workspace.state['settings']['theme'] = 'dark'
                    await workspace.close()
                    await terminal(args.executable.resolve(), state, cache, size, scene, args.captures, args.baseline)
                    if scene == 'completed' and not args.baseline:
                        saved = store.thread(thread['id'])
                        assert saved['model'] == 'fixture-fast', 'Model selection was not persisted'
                        assert saved['settings'].get('reasoning_effort') == 'low', 'Reasoning choice was not persisted'
                    reopened = Workspace(data_dir=state, cache_path=cache)
                    assert reopened.thread_view(thread['id'], 'local')['draft'] == 'Draft stays here'
                    if scene == 'completed' and not args.baseline:
                        assert reopened.state['settings'].get('theme') == 'light', 'Theme selection was not persisted'
                    await reopened.close()

            if not args.baseline:
                # One additional wide run proves the agent panel appears by
                # itself and that opening child details does not touch the
                # selected parent conversation state.
                wide_parent = scenes['running']
                workspace = Workspace(data_dir=state, cache_path=cache)
                await workspace.sync_machine('local')
                workspace.switch('local', project['id'], wide_parent['id'])
                workspace.set_draft('Draft stays here in wide agent parent')
                workspace.set_scroll(7)
                await workspace.close()
                await terminal(
                    args.executable.resolve(), state, cache, (160, 40), 'wide-agents',
                    args.captures,
                )
                reopened = Workspace(data_dir=state, cache_path=cache)
                wide_view = reopened.thread_view(wide_parent['id'], 'local')
                assert wide_view['draft'] == 'Draft stays here in wide agent parent'
                assert wide_view['scroll'] == 7, 'agent detail selection changed parent history position'
                await reopened.close()

                # Start two actual daemon fixture tasks and switch between
                # them through curses. Both must continue emitting while they
                # are off screen; switching is never a cancellation signal.
                live = {}
                for suffix in ('A', 'B'):
                    search_token = 'alphaone' if suffix == 'A' else 'betatwo'
                    thread = store.create_thread(
                        project['id'], f'Live {suffix} {search_token}', 'codex', repo, 'main', False,
                        model='fixture-balanced', settings={'reasoning_effort': 'low'},
                    )
                    live[suffix] = thread
                    await daemon.dispatch('send', {
                        'thread_id': thread['id'], 'prompt': 'Hold live ' + suffix,
                        'request_id': 'live-' + suffix.lower(),
                    })
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and any(
                    FixtureProvider.ticks.get(live[suffix]['id'], 0) < 2 for suffix in ('A', 'B')
                ):
                    await asyncio.sleep(.05)
                assert all(FixtureProvider.ticks.get(live[suffix]['id'], 0) >= 2 for suffix in ('A', 'B'))
                workspace = Workspace(data_dir=state, cache_path=cache)
                await workspace.sync_machine('local')
                workspace.switch('local', project['id'], live['A']['id'])
                workspace.set_draft('Draft stays here in running alpha')
                workspace.set_scroll(6)
                workspace.switch('local', project['id'], live['B']['id'])
                workspace.set_draft('Second thread keeps its own beta draft')
                workspace.set_scroll(4)
                workspace.switch('local', project['id'], live['A']['id'])
                await workspace.close()
                ticks_before_switch = dict(FixtureProvider.ticks)
                await terminal(
                    args.executable.resolve(), state, cache, (80, 24), 'switch-running',
                    args.captures,
                )
                reopened = Workspace(data_dir=state, cache_path=cache)
                assert reopened.state['selected_thread'] == live['A']['id'], 'PTY switch did not return to Live A'
                await reopened.close()
                for suffix, expected_scroll in (('A', 6), ('B', 4)):
                    thread_id = live[suffix]['id']
                    assert store.thread(thread_id)['state'] == 'running', f'Live {suffix} stopped while off screen'
                    assert FixtureProvider.ticks[thread_id] > ticks_before_switch[thread_id], f'Live {suffix} stopped emitting while off screen'
                    reopened = Workspace(data_dir=state, cache_path=cache)
                    view = reopened.thread_view(thread_id, 'local')
                    expected_draft = (
                        'Draft stays here in running alpha'
                        if suffix == 'A'
                        else 'Second thread keeps its own beta draft'
                    )
                    assert view['draft'] == expected_draft
                    assert view['scroll'] == expected_scroll
                    await reopened.close()

                # Create real changed-file data and enough transcript history
                # for a non-live reader position to survive two switches and a
                # full client restart.
                (repo / 'daily-first.txt').write_text('first fixture line\n')
                (repo / 'daily-second.txt').write_text('second fixture line\n')
                completed = scenes['completed']
                failed = scenes['error']
                for index in range(32):
                    store.append_event(completed['id'], scene_runs['completed']['id'], 'message', {
                        'role': 'assistant', 'item_id': f'daily-history-{index}',
                        'text': f'Daily history line {index:02d}',
                    })
                workspace = Workspace(data_dir=state, cache_path=cache)
                await workspace.sync_machine('local')
                workspace.switch('local', project['id'], failed['id'])
                workspace.set_draft('')
                workspace.set_scroll(0)
                workspace.switch('local', project['id'], completed['id'])
                workspace.set_draft('Draft stays here for daily reopen')
                workspace.set_scroll(0)
                workspace.state['settings']['theme'] = 'dark'
                await workspace.close()
                fixture_env, health_counter, update_counter, module_origin = health_fixture_environment(base)
                retry_starts_before = len([
                    start for start in FixtureProvider.starts if start[0] == failed['id']
                ])
                await terminal(
                    args.executable.resolve(), state, cache, (48, 16), 'daily',
                    args.captures, env_overrides=fixture_env,
                    health_counter=health_counter, update_counter=update_counter,
                )
                assert_health_module_origin(args.executable, module_origin)
                retry_starts = [start for start in FixtureProvider.starts if start[0] == failed['id']]
                assert len(retry_starts) == retry_starts_before + 1, 'failed prompt did not start exactly one new provider run'
                failed_events = store.history(failed['id'], limit=10_000)['events']
                failed_runs = {event.get('run_id') for event in failed_events if event.get('run_id')}
                assert len(failed_runs) == 2, 'failed recovery did not retain exactly the original and retry runs'
                assert store.thread(failed['id'])['archived'] is False, 'restored thread remained archived'
                saved = store.thread(completed['id'])
                assert saved['model'] == 'fixture-fast'
                assert saved['settings'].get('reasoning_effort') == 'low'

                reopened = Workspace(data_dir=state, cache_path=cache)
                assert reopened.state['selected_thread'] == completed['id'], 'selected conversation was not remembered'
                assert reopened.thread_pinned('local', completed['id']), 'thread pin was not remembered'
                completed_view = reopened.thread_view(completed['id'], 'local')
                failed_view = reopened.thread_view(failed['id'], 'local')
                assert completed_view['draft'] == 'Draft stays here for daily reopen'
                assert completed_view['scroll'] == 10, 'history position was not retained across thread switches'
                assert failed_view['draft'] == 'Draft stays here in search target'
                await reopened.close()
                await terminal(
                    args.executable.resolve(), state, cache, (48, 16), 'remembered',
                    args.captures, env_overrides=fixture_env,
                    health_counter=health_counter, update_counter=update_counter,
                )
                assert len([
                    start for start in FixtureProvider.starts if start[0] == failed['id']
                ]) == retry_starts_before + 1, 'reopening replayed the failed prompt'

                for thread in live.values():
                    FixtureProvider.gates[thread['id']].set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and any(
                    store.thread(thread['id'])['state'] == 'running' for thread in live.values()
                ):
                    await asyncio.sleep(.05)
                assert all(store.thread(thread['id'])['state'] == 'completed' for thread in live.values())

            expected_threads = len(scenes) + (0 if args.baseline else 2)
            assert len(store.threads()) == expected_threads, 'Navigation unexpectedly created a thread'
            print('PASS: baseline terminal captures and retained drafts' if args.baseline else
                  'PASS: real terminal scenes, daily recovery/navigation/health/files, live switching, wide agents and retained state')
        finally:
            await daemon.close()


if __name__ == '__main__':
    asyncio.run(main())
