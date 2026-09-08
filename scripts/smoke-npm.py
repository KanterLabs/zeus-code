#!/usr/bin/env python3
"""Exercise an actual packed npm install, persistent runtime and populated state."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    work = ROOT / '.work'
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='npm-', dir=work) as directory:
        base = Path(directory)
        env = os.environ.copy()
        env.update(npm_config_cache=str(base / 'npm-cache'), XDG_CACHE_HOME=str(base / 'cache'),
                   ZEUS_CODE_DATA_DIR=str(base / 's'), ZEUS_CODE_CLIENT_STATE=str(base / 'client.json'))
        env.pop('PYTHONPATH', None)
        cwd = base / 'unrelated'
        cwd.mkdir()

        def run(command, *, at=cwd):
            result = subprocess.run(command, cwd=at, env=env, capture_output=True, text=True, timeout=90)
            if result.returncode:
                raise RuntimeError(f'{command[0]} failed: {result.stderr} {result.stdout}')
            return result.stdout

        packed = json.loads(run(['npm', 'pack', '--json', '--pack-destination', str(base)], at=ROOT))[0]
        allowed = {'package.json', 'README.md', 'bin/zeus-code.cjs', 'dist/zeus-code.pyz',
                   'dist/release.json', 'dist/SHA256SUMS'}
        assert {item['path'] for item in packed['files']} == allowed, 'Unexpected npm package contents'
        prefix = base / 'installed'
        run(['npm', 'install', '--global', '--prefix', str(prefix), '--no-audit', '--no-fund',
             str(base / packed['filename'])])
        executable = prefix / 'bin/zeus-code'
        version = json.loads((ROOT / 'package.json').read_text())['version']
        assert run([str(executable), '--version']).strip() == f'zeus-code {version}'
        assert run(['npm', 'exec', '--yes', '--package', str(base / packed['filename']),
                    '--', 'zeus-code', '--version']).strip() == f'zeus-code {version}'
        assert not (base / 's/server.sock').exists(), '--version started daemon'
        runtime = list((base / 'cache/zeus-code/npm').rglob('zeus-code.pyz'))
        assert len(runtime) == 1, 'Missing stable runtime bundle'
        repo = base / 'repo'
        run(['git', 'init', '-q', str(repo)])
        sock = base / 's/server.sock'
        try:
            run([str(executable), 'serve', '--background'])
            before = json.loads(run([str(executable), 'status']))
            project = json.loads(run([str(executable), 'project', 'add', str(repo)]))
            thread = json.loads(run([str(executable), 'thread', 'create', project['id'], 'Retained npm thread', '--provider', 'codex']))
            shutil.rmtree(base / 'npm-cache')
            shutil.rmtree(prefix)
            python = env.get('ZEUS_CODE_PYTHON', 'python3')
            def bundle(*args):
                return run([python, str(runtime[0]), *args])
            after = json.loads(bundle('status'))
            assert before['pid'] == after['pid'], 'Daemon changed after npm cache removal'
            assert after['threads'][0]['id'] == thread['id']
            backup = base / 'backup.sqlite3'
            bundle('backup', str(backup))
            with sqlite3.connect(f'file:{backup}?mode=ro', uri=True) as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert db.execute('SELECT COUNT(*) FROM threads').fetchone()[0] == 1
            bundle('stop')
            for _ in range(100):
                if not sock.exists():
                    break
                time.sleep(.05)
            bundle('serve', '--background')
            restored = json.loads(bundle('status'))
            assert restored['server_id'] == before['server_id']
            assert restored['threads'][0]['id'] == thread['id']
            print('PASS: packed npm install outside checkout, persistent runtime after npm removal, populated backup and restart')
        finally:
            if sock.exists():
                run([env.get('ZEUS_CODE_PYTHON', 'python3'), str(runtime[0]), 'stop'])
                for _ in range(100):
                    if not sock.exists():
                        break
                    time.sleep(.05)


if __name__ == '__main__':
    main()
