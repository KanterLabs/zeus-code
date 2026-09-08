#!/usr/bin/env python3
"""Exercise the real terminal UX against an isolated fixture daemon; no model calls."""
import argparse
import asyncio
import errno
import fcntl
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
    async def check(self):
        return {'available': True, 'detail': 'Isolated UX fixture', 'models': [
            {'id': 'fixture-balanced', 'name': 'Balanced', 'reasoning_efforts': ['low', 'high']},
            {'id': 'fixture-fast', 'name': 'Fast', 'reasoning_efforts': ['low']},
        ]}


async def terminal(executable, state, cache, size, scene, captures, baseline=False):
    pid, fd = pty.fork()
    if pid == 0:
        try:
            env = os.environ.copy()
            env.update(TERM='xterm-256color', ZEUS_CODE_CLIENT_STATE=str(cache))
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

    try:
        await read_for(1)
        assert b'Traceback' not in output
        if not baseline:
            assert b'Draft stays here' in output, f'{size} {scene}: conversation not visible'
        assert baseline or b'too small' not in output.lower(), f'{size}: unsupported terminal size'
        captures.mkdir(parents=True, exist_ok=True)
        (captures / f'{size[0]}x{size[1]}-{scene}.ansi').write_bytes(output)
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
            (captures / f'{size[0]}x{size[1]}-light.ansi').write_bytes(output)
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
        daemon = Daemon(state, providers={'codex': FixtureProvider})
        await daemon.start()
        try:
            store = daemon.store
            project = store.add_project(str(repo), 'UX fixture', 'main')
            scenes = {}
            for scene in ('empty', 'running', 'approval', 'error', 'completed'):
                thread = store.create_thread(project['id'], 'UX ' + scene, 'codex', repo, 'main', False,
                                             model='fixture-balanced', settings={'reasoning_effort': 'high'})
                scenes[scene] = thread
                if scene == 'empty':
                    continue
                run, _ = store.create_run(thread['id'], 'Check this fixture', str(uuid.uuid4()))
                store.append_event(thread['id'], run['id'], 'tool', {
                    'item_id': 'collab', 'tool_type': 'collabAgentToolCall', 'title': 'Review agents',
                    'status': 'completed', 'agents': [
                        {'id': 'agent-one', 'parent_id': thread['id'], 'label': 'Review tests', 'state': 'completed', 'result': 'Tests checked'},
                        {'id': 'agent-two', 'parent_id': thread['id'], 'label': 'Review docs', 'state': 'completed', 'result': 'Docs checked'},
                    ],
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
            assert len(store.threads()) == len(scenes), 'Navigation unexpectedly created a thread'
            print('PASS: baseline terminal captures and retained drafts' if args.baseline else
                  'PASS: real terminal scenes, model/palette/attention controls, narrow layouts and retained drafts')
        finally:
            await daemon.close()


if __name__ == '__main__':
    asyncio.run(main())
