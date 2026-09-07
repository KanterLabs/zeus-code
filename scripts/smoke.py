#!/usr/bin/env python3
"""Verify detached lifecycle, RPC commands and backups without using a model."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", default="zeus-code")
    args = parser.parse_args()
    executable = Path(args.executable)
    if not executable.is_absolute():
        executable = ROOT / executable
    work = ROOT / ".work"
    work.mkdir(exist_ok=True)
    # Short socket path and no reliance on PYTHONPATH from the caller.
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=work) as temporary:
        base = Path(temporary)
        state, repo = base / "s", base / "r"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "README.md").write_text("smoke test\n")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)

        def run(*command, parsed=False):
            result = subprocess.run([sys.executable, str(executable), "--data-dir", str(state), *command],
                                    env=env, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(f"{' '.join(command)} failed: {result.stderr.strip()} {result.stdout.strip()}")
            return json.loads(result.stdout) if parsed else result.stdout

        unavailable = subprocess.run([sys.executable, str(executable), "--data-dir", str(state), "status"],
                                     env=env, capture_output=True, text=True, timeout=10)
        assert unavailable.returncode == 1, "Missing-server errors must exit nonzero, including packaged execution"
        assert "Traceback" not in unavailable.stderr

        try:
            run("serve", "--background")
            status = run("status", parsed=True)
            run("serve", "--background")
            assert run("status", parsed=True)["pid"] == status["pid"], "Duplicate serve started a competing daemon"
            project = run("project", "add", str(repo), parsed=True)
            thread = run("thread", "create", project["id"], "Smoke thread", "--provider", "codex", parsed=True)
            diff = run("diff", thread["id"], parsed=True)
            assert any(f["path"] == "README.md" for f in diff["files"])
            target = base / "backup.sqlite3"
            run("backup", str(target))
            with sqlite3.connect(f"file:{target}?mode=ro", uri=True) as db:
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
            run("stop")
            for _ in range(100):
                if not (state / "server.sock").exists():
                    break
                time.sleep(.05)
            assert not (state / "server.sock").exists(), "Graceful stop left the socket active"
            run("serve", "--background")
            restored = run("status", parsed=True)
            assert restored["server_id"] == status["server_id"]
            assert restored["threads"][0]["id"] == thread["id"]
            print("PASS: detached server, duplicate start, project/thread registration, diff, verified backup, stop and persistent restart")
        finally:
            if (state / "server.sock").exists():
                run("stop")
                for _ in range(100):
                    if not (state / "server.sock").exists():
                        break
                    time.sleep(.05)


if __name__ == "__main__":
    main()
