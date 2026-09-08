#!/usr/bin/env python3
"""Download with npx, open the real terminal UI, quit and reopen without a model."""
import argparse
import codecs
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

ROOT = Path(__file__).resolve().parents[1]


def open_and_quit(spec, cwd, env):
    pid, terminal = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
            command = (['npx', '--yes', '--package', spec, '--', 'zeus-code']
                       if Path(spec).is_file() else ['npx', '--yes', spec])
            os.execvpe('npx', command, env)
        except BaseException as exc:
            os.write(2, f'Cannot start npx: {exc}\n'.encode())
            os._exit(127)  # Never unwind the parent's temporary-directory cleanup.
    output = ''
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    exited = False
    try:
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 110, 0, 0))
        deadline = time.monotonic() + 90
        ready = False
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], .1)[0]:
                try:
                    data = os.read(terminal, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b''
                output = (output + decoder.decode(data))[-200000:]
                if 'Your next idea starts here.' in output and '● connected' in output:
                    ready = True
                    break
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                exited = True
                raise AssertionError(f'npx exited before connected UI (status {status}): {output[-4000:]!r}')
        assert ready, f'Connected welcome UI did not appear within 90s: {output[-4000:]!r}'
        os.write(terminal, b'\x11')  # Ctrl+Q, as a user would quit the UI.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            # Drain terminal output so child shutdown cannot block on a full PTY.
            if select.select([terminal], [], [], .1)[0]:
                try:
                    os.read(terminal, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                exited = True
                assert os.waitstatus_to_exitcode(status) == 0, f'UI quit failed: {status}'
                return
        raise AssertionError('Ctrl+Q did not exit npx within 15s')
    finally:
        if not exited:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(terminal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', help='Registry package to download instead of packing this checkout')
    args = parser.parse_args()
    work = ROOT / '.work'
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='npx-e2e-', dir=work) as temporary:
        base = Path(temporary)
        cwd, home = base / 'unrelated', base / 'home'
        cwd.mkdir()
        home.mkdir()
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env.update(HOME=str(home), TERM='xterm-256color',
                   npm_config_cache=str(base / 'npm-cache'), npm_config_registry='https://registry.npmjs.org/',
                   XDG_CACHE_HOME=str(base / 'cache'), ZEUS_CODE_DATA_DIR=str(base / 's'),
                   ZEUS_CODE_CLIENT_STATE=str(base / 'client.json'), ZEUS_CODE_PYTHON=sys.executable)

        def run(command, at=cwd):
            result = subprocess.run(command, cwd=at, env=env, text=True, capture_output=True, timeout=90)
            assert result.returncode == 0, f'Command failed: {result.stderr} {result.stdout}'
            return result.stdout

        spec = args.package
        if spec is None:
            run(['npm', 'run', 'prepare'], ROOT)
            packed = json.loads(run(['npm', 'pack', '--ignore-scripts', '--json', '--pack-destination', str(base)], ROOT))[0]
            spec = str(base / packed['filename'])
        assert not (base / 'npm-cache/_npx').exists(), 'npx execution cache must start empty'
        sock = base / 's/server.sock'
        try:
            open_and_quit(spec, cwd, env)
            bundles = list((base / 'cache/zeus-code/npm').rglob('zeus-code.pyz'))
            assert len(bundles) == 1, 'Expected exactly one installed runtime'
            bundle = [sys.executable, str(bundles[0])]
            first = json.loads(run([*bundle, 'status']))
            assert first['threads'] == [], 'Welcome launch unexpectedly created or ran a thread'
            assert list((base / 'npm-cache/_npx').glob('*/node_modules/@kanterlabs/zeus-code/package.json')), 'npx did not install the package'
            open_and_quit(spec, cwd, env)
            second = json.loads(run([*bundle, 'status']))
            assert second['pid'] == first['pid'], 'Reopening started another daemon'
            assert second['server_id'] == first['server_id'], 'Reopening changed the workspace identity'
            print('PASS: fresh npx download, automatic daemon, connected terminal UI, Ctrl+Q, reopen with same daemon')
        finally:
            if sock.exists():
                bundles = list((base / 'cache/zeus-code/npm').rglob('zeus-code.pyz'))
                assert bundles, 'Cannot clean up daemon: runtime missing'
                run([sys.executable, str(bundles[0]), 'stop'])
                deadline = time.monotonic() + 10
                while sock.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                assert not sock.exists(), 'Test daemon did not stop'


if __name__ == '__main__':
    main()
