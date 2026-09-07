#!/usr/bin/env python3
"""Opt-in real Codex/OpenCode validation; may consume provider credits."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from zeus_code.client import RPCClient
from zeus_code.daemon import Daemon


async def verify(args):
    work = ROOT / ".work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="live-", dir=work) as temporary:
        root = Path(temporary)
        daemons, threads, run_ids, sessions = [], [], [], []
        try:
            for index, provider in enumerate(("codex", "opencode")):
                repo = root / f"repo{index}"
                repo.mkdir()
                subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
                daemon = Daemon(root / f"s{index}")
                await daemon.start()
                daemons.append(daemon)
                async with RPCClient(daemon.data_dir) as client:
                    project = await client.call("add_project", {"path": str(repo), "name": provider + " validation"})
                    params = {"project_id": project["id"], "title": "Zeus v1 live verification", "provider": provider}
                    model = args.codex_model if provider == "codex" else args.opencode_model
                    if model:
                        params["model"] = model
                    threads.append(await client.call("create_thread", params))

            async def submit(index):
                daemon = daemons[index]
                params = {"thread_id": threads[index]["id"], "request_id": str(uuid.uuid4()),
                          "prompt": "Reply exactly ZEUS_LIVE_OK. Do not call tools or change any files."}
                async with RPCClient(daemon.data_dir) as client:
                    first = await client.call("send", params)
                # The initiating client is now gone. Reconnect while work continues.
                async with RPCClient(daemon.data_dir) as client:
                    duplicate = await client.call("send", params)
                    assert first["id"] == duplicate["id"], "Reconnect duplicated a run"
                return first["id"]

            run_ids = await asyncio.gather(submit(0), submit(1))
            both_running = all(d.store.run(run_ids[i])["state"] == "running" for i, d in enumerate(daemons))
            assert both_running, "Expected overlapping independent runs"

            async def completed(index, run_id):
                daemon = daemons[index]
                async with asyncio.timeout(90):
                    while daemon.store.run(run_id)["state"] in {"running", "awaiting_approval"}:
                        if daemon.store.approvals():
                            raise RuntimeError("A no-tools verification unexpectedly requested approval")
                        await asyncio.sleep(.1)
                result = daemon.store.run(run_id)
                if result["state"] != "completed":
                    raise RuntimeError(f"{threads[index]['provider']} validation failed: {result.get('error')}")
                history = daemon.store.history(threads[index]["id"], limit=500)["events"]
                text = "".join(e["data"].get("text", "") for e in history if e["run_id"] == run_id and e["kind"] in {"message", "message_delta"} and e["data"].get("role") != "user")
                assert "ZEUS_LIVE_OK" in text, "Provider did not return expected verification token"
                session = daemon.store.thread(threads[index]["id"])["session_id"]
                assert session, "Provider session identifier was not persisted"
                return session

            sessions = await asyncio.gather(*(completed(i, run_id) for i, run_id in enumerate(run_ids)))

            async def resume(index):
                async with RPCClient(daemons[index].data_dir) as client:
                    run = await client.call("send", {"thread_id": threads[index]["id"], "request_id": str(uuid.uuid4()),
                                                   "prompt": "Again reply exactly ZEUS_LIVE_OK. Do not call tools or change files."})
                resumed_session = await completed(index, run["id"])
                assert resumed_session == sessions[index], "Resume created a different provider session"

            await asyncio.gather(resume(0), resume(1))
            result = {"providers": ["codex", "opencode"], "concurrent": True,
                      "client_disconnect_survived": True, "duplicate_submission_deduplicated": True,
                      "live_resume_preserved_sessions": True, "daemon_instances": 2,
                      "physical_machines": 1, "remote_ssh_validation": "not covered by this local test"}
            (ROOT / ".work/live-result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
        finally:
            await asyncio.gather(*(d.close() for d in daemons))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="confirm using configured providers for four tiny live turns")
    parser.add_argument("--codex-model", default="gpt-5.6-sol")
    parser.add_argument("--opencode-model", default=None)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; this check can consume provider credits")
    asyncio.run(verify(args))


if __name__ == "__main__":
    main()
