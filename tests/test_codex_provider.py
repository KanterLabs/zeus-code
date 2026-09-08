from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
import sys
from unittest.mock import patch

from zeus_code.providers.base import ProviderError, RunContext
from zeus_code.providers.codex import CodexProvider


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / ".work"


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
from pathlib import Path
import sys

path = Path(__file__)
log_path = path.with_suffix(".log")

def log(value):
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")

def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.test")
    raise SystemExit(0)

if sys.argv[1:] != ["app-server", "--stdio"]:
    print("unexpected arguments", file=sys.stderr)
    raise SystemExit(2)

log({"pid": __import__("os").getpid(), "argv": sys.argv[1:]})
state = None
turn_id = "turn-test"
thread_id = "thread-new"
permission_profile = {
    "network": {"enabled": True},
    "fileSystem": {"read": ["/opt/data"], "write": None},
}

for raw in sys.stdin:
    message = json.loads(raw)
    log({"recv": message})
    method = message.get("method")

    if method == "initialize":
        send({"id": message["id"], "result": {
            "userAgent": "fake", "platformFamily": "unix", "platformOs": "linux"
        }})
    elif method == "initialized":
        pass
    elif method == "account/read":
        if "unauth" in path.name:
            send({"id": message["id"], "result": {
                "account": None, "requiresOpenaiAuth": True,
            }})
        else:
            send({"id": message["id"], "result": {
                "account": {"type": "chatgpt", "email": "must-not-leak@example.com"},
                "requiresOpenaiAuth": True,
            }})
    elif method == "model/list":
        if message["params"].get("cursor"):
            send({"id": message["id"], "result": {"data": [{
                "id": "gpt-second", "model": "gpt-second", "displayName": "Second",
                "description": "Second fixture model", "hidden": False,
                "isDefault": False, "defaultReasoningEffort": "high",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium", "description": "Balanced"},
                    {"reasoningEffort": "high", "description": "More reasoning"},
                ],
            }], "nextCursor": None}})
        else:
            send({"id": message["id"], "result": {"data": [{
                "id": "gpt-first", "model": "gpt-first", "displayName": "First",
                "description": "First fixture model", "hidden": False,
                "isDefault": True, "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Fast"},
                    {"reasoningEffort": "medium", "description": "Balanced"},
                    {"reasoningEffort": "high", "description": "More reasoning"},
                ],
            }], "nextCursor": "page-two"}})
    elif method == "thread/start":
        thread_id = "thread-new"
        send({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
        send({"id": message["id"], "result": {"thread": {
            "id": thread_id, "sessionId": "tree-root"
        }}})
    elif method == "thread/resume":
        thread_id = message["params"]["threadId"]
        send({"id": message["id"], "result": {"thread": {
            "id": thread_id, "sessionId": "different-tree-root"
        }}})
    elif method == "turn/start":
        prompt = message["params"]["input"][0]["text"]
        send({"id": message["id"], "result": {"turn": {
            "id": turn_id, "status": "inProgress", "items": [], "error": None
        }}})
        if prompt == "normal":
            send({"method": "item/started", "params": {
                "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1,
                "item": {"type": "reasoning", "id": "reason-1",
                         "summary": ["private reasoning summary"],
                         "content": ["private reasoning content"]},
            }})
            send({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id, "completedAtMs": 1,
                "item": {"type": "reasoning", "id": "reason-1",
                         "summary": ["private reasoning summary"],
                         "content": ["private reasoning content"]},
            }})
            send({"method": "item/started", "params": {
                "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1,
                "item": {"type": "commandExecution", "id": "cmd-1",
                         "command": "echo ok", "cwd": "/repo", "commandActions": [],
                         "status": "inProgress"},
            }})
            send({"method": "item/commandExecution/requestApproval",
                  "id": "approval-command", "params": {
                      "threadId": thread_id, "turnId": turn_id, "itemId": "cmd-1",
                      "startedAtMs": 2, "command": "echo ok", "cwd": "/repo",
                      "reason": "test exact data", "commandActions": [],
                  }})
            state = "normal-approval"
        elif prompt == "file permission":
            send({"method": "item/fileChange/requestApproval",
                  "id": 71, "params": {
                      "threadId": thread_id, "turnId": turn_id, "itemId": "patch-1",
                      "startedAtMs": 3, "reason": "modify", "grantRoot": "/repo/src",
                  }})
            state = "file-approval"
        elif prompt == "wait":
            send({"method": "warning", "params": {
                "threadId": thread_id, "message": "waiting"
            }})
            state = "waiting"
        elif prompt == "unsupported":
            send({"method": "item/tool/requestUserInput", "id": "question-1",
                  "params": {"threadId": thread_id, "turnId": turn_id,
                             "itemId": "question-item", "isBlocking": True,
                             "questions": []}})
            state = "unsupported"
        elif prompt == "failed":
            send({"method": "turn/completed", "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "failed", "items": [],
                         "error": {"message": "quota exhausted",
                                   "codexErrorInfo": "UsageLimitExceeded"}},
            }})
        elif prompt == "oversize":
            send({"method": "warning", "params": {
                "threadId": thread_id, "message": "x" * 4096,
            }})
        elif prompt == "long output":
            text = "Ω" * 40000
            send({"method": "item/agentMessage/delta", "params": {
                "threadId": thread_id, "turnId": turn_id,
                "itemId": "long-message", "delta": text,
            }})
            send({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id, "completedAtMs": 6,
                "item": {"type": "agentMessage", "id": "long-message", "text": text},
            }})
            send({"method": "turn/completed", "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed", "items": []},
            }})
        elif prompt == "agents":
            send({"method": "item/started", "params": {
                "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1000,
                "item": {
                    "type": "collabAgentToolCall", "id": "spawn-a",
                    "tool": "spawnAgent", "status": "inProgress",
                    "senderThreadId": thread_id, "receiverThreadIds": [],
                    "prompt": "Inspect the parser", "model": "gpt-5.6-sol",
                    "reasoningEffort": "high", "agentsStates": {},
                },
            }})
            send({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id, "completedAtMs": 1100,
                "item": {
                    "type": "collabAgentToolCall", "id": "spawn-a",
                    "tool": "spawnAgent", "status": "completed",
                    "senderThreadId": thread_id,
                    "receiverThreadIds": ["agent-a"],
                    "prompt": "Inspect the parser", "model": "gpt-5.6-sol",
                    "reasoningEffort": "high",
                    "agentsStates": {"agent-a": {"status": "running"}},
                },
            }})
            send({"method": "item/started", "params": {
                "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1200,
                "item": {
                    "type": "collabAgentToolCall", "id": "spawn-b",
                    "tool": "spawnAgent", "status": "inProgress",
                    "senderThreadId": "agent-a", "receiverThreadIds": [],
                    "prompt": "Validate nested behavior", "model": "gpt-5.6-luna",
                    "reasoningEffort": "medium", "agentsStates": {},
                },
            }})
            send({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id, "completedAtMs": 1300,
                "item": {
                    "type": "collabAgentToolCall", "id": "spawn-b",
                    "tool": "spawnAgent", "status": "completed",
                    "senderThreadId": "agent-a",
                    "receiverThreadIds": ["agent-b"],
                    "prompt": "Validate nested behavior", "model": "gpt-5.6-luna",
                    "reasoningEffort": "medium",
                    "agentsStates": {"agent-b": {"status": "running"}},
                },
            }})
            send({"method": "item/started", "params": {
                "threadId": thread_id, "turnId": turn_id, "startedAtMs": 1400,
                "item": {
                    "type": "collabAgentToolCall", "id": "wait-agents",
                    "tool": "wait", "status": "inProgress",
                    "senderThreadId": thread_id,
                    "receiverThreadIds": ["agent-a", "agent-b"],
                    "prompt": None, "model": None, "reasoningEffort": None,
                    "agentsStates": {},
                },
            }})
            send({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id, "completedAtMs": 2000,
                "item": {
                    "type": "collabAgentToolCall", "id": "wait-agents",
                    "tool": "wait", "status": "failed",
                    "senderThreadId": thread_id,
                    "receiverThreadIds": ["agent-a", "agent-b"],
                    "prompt": None, "model": None, "reasoningEffort": None,
                    "agentsStates": {
                        "agent-a": {"status": "completed", "message": "Parser supports variants"},
                        "agent-b": {"status": "errored", "message": "Nested fixture failed"},
                    },
                },
            }})
            send({"method": "item/started", "params": {
                "threadId": thread_id, "turnId": turn_id, "startedAtMs": 2200,
                "item": {
                    "type": "collabAgentToolCall", "id": "resume-a",
                    "tool": "resumeAgent", "status": "inProgress",
                    "senderThreadId": thread_id,
                    "receiverThreadIds": ["agent-a"],
                    "prompt": None, "model": None, "reasoningEffort": None,
                    "agentsStates": {},
                },
            }})
            send({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id, "completedAtMs": 2300,
                "item": {
                    "type": "collabAgentToolCall", "id": "resume-a",
                    "tool": "resumeAgent", "status": "completed",
                    "senderThreadId": thread_id,
                    "receiverThreadIds": ["agent-a"],
                    "prompt": None, "model": None, "reasoningEffort": None,
                    "agentsStates": {"agent-a": {"status": "running"}},
                },
            }})
            send({"method": "item/completed", "params": {
                "threadId": thread_id, "turnId": turn_id, "completedAtMs": 2400,
                "item": {
                    "type": "collabAgentToolCall", "id": "list-agents",
                    "tool": "listAgents", "status": "completed",
                    "senderThreadId": thread_id,
                    "receiverThreadIds": ["agent-future"],
                    "prompt": None, "model": None, "reasoningEffort": None,
                    "agentsStates": {
                        "agent-future": {"status": "pausedByHost", "message": "Future state"},
                    },
                },
            }})
            send({"method": "turn/completed", "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed", "items": []},
            }})
    elif method == "turn/interrupt":
        send({"id": message["id"], "result": {}})
        state = "interrupted"
    elif message.get("id") == "approval-command" and state == "normal-approval":
        send({"method": "item/commandExecution/outputDelta", "params": {
            "threadId": thread_id, "turnId": turn_id, "itemId": "cmd-1",
            "delta": "ok\n",
        }})
        send({"method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id, "completedAtMs": 4,
            "item": {"type": "commandExecution", "id": "cmd-1",
                     "command": "echo ok", "cwd": "/repo", "commandActions": [],
                     "status": "completed", "aggregatedOutput": "ok\n"},
        }})
        send({"method": "item/agentMessage/delta", "params": {
            "threadId": thread_id, "turnId": turn_id, "itemId": "msg-1",
            "delta": "Done",
        }})
        send({"method": "item/completed", "params": {
            "threadId": thread_id, "turnId": turn_id, "completedAtMs": 5,
            "item": {"type": "agentMessage", "id": "msg-1", "text": "Done"},
        }})
        send({"method": "turn/completed", "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": "completed", "items": []},
        }})
        state = "done"
    elif message.get("id") == 71 and state == "file-approval":
        send({"method": "item/permissions/requestApproval", "id": "permission-1",
              "params": {"threadId": thread_id, "turnId": turn_id,
                         "itemId": "perm-item", "startedAtMs": 4,
                         "cwd": "/repo", "reason": "read data",
                         "permissions": permission_profile}})
        state = "permission-approval"
    elif message.get("id") == "permission-1" and state == "permission-approval":
        send({"method": "turn/completed", "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": "completed", "items": []},
        }})
        state = "done"
'''


class CodexProviderTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        WORK.mkdir(exist_ok=True)

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.directory = Path(self.temp.name)
        self.executable = self.directory / "codex-fixture"
        self.executable.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
        self.executable.chmod(0o700)
        self.cwd = self.directory / "repo"
        self.cwd.mkdir()
        self.provider = CodexProvider(str(self.executable), request_timeout=2.0)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def context(self, *, session_id=None, settings=None, decisions=None):
        events = []
        approvals = []
        remaining = iter(decisions or ["allow"])

        async def emit(kind, data):
            events.append((kind, data))

        async def approve(request):
            approvals.append(request)
            return next(remaining)

        context = RunContext(
            thread_id="zeus-thread",
            run_id="zeus-run",
            cwd=str(self.cwd),
            session_id=session_id,
            model="gpt-first",
            settings=settings or {},
            emit=emit,
            approve=approve,
        )
        return context, events, approvals

    def received(self):
        rows = [
            json.loads(line)
            for line in self.executable.with_suffix(".log").read_text().splitlines()
        ]
        return [row["recv"] for row in rows if "recv" in row]

    async def test_run_streams_events_and_answers_exact_command_approval(self):
        context, events, approvals = self.context(
            settings={
                "approval_policy": "onRequest",
                "sandbox": "workspaceWrite",
                "reasoning_effort": "high",
            }
        )

        await self.provider.run(context, "normal")

        self.assertEqual(
            events[0],
            ("status", {"phase": "starting", "text": "Starting Codex"}),
        )
        self.assertIn(("provider_session", {"session_id": "thread-new"}), events)
        self.assertIn(("message_delta", {"item_id": "msg-1", "text": "Done"}), events)
        self.assertIn(
            ("message", {"role": "assistant", "text": "Done", "item_id": "msg-1"}),
            events,
        )
        command_delta = next(
            data
            for kind, data in events
            if kind == "tool" and data.get("item_id") == "cmd-1" and data.get("delta")
        )
        self.assertEqual(command_delta["text"], "ok\n")
        self.assertEqual(command_delta["tool_type"], "commandExecution")
        self.assertEqual(approvals[0]["provider_request_id"], "approval-command")
        self.assertEqual(approvals[0]["command"], "echo ok")
        self.assertEqual(approvals[0]["details"]["reason"], "test exact data")

        received = self.received()
        thread_start = next(row for row in received if row.get("method") == "thread/start")
        self.assertEqual(thread_start["params"]["approvalPolicy"], "on-request")
        self.assertEqual(thread_start["params"]["sandbox"], "workspace-write")
        turn_start = next(row for row in received if row.get("method") == "turn/start")
        self.assertEqual(turn_start["params"]["effort"], "high")
        approval = next(row for row in received if row.get("id") == "approval-command")
        self.assertEqual(approval, {"id": "approval-command", "result": {"decision": "accept"}})

    async def test_resume_uses_persisted_thread_id_not_session_tree_id(self):
        context, events, _ = self.context(
            session_id="thread-saved",
            settings={"approval_policy": None, "sandbox": None},
        )

        await self.provider.run(context, "normal")

        request = next(row for row in self.received() if row.get("method") == "thread/resume")
        self.assertEqual(request["params"]["threadId"], "thread-saved")
        self.assertEqual(request["params"]["approvalPolicy"], "never")
        self.assertEqual(request["params"]["sandbox"], "danger-full-access")
        self.assertNotIn("serviceName", request["params"])
        self.assertEqual(
            events[0],
            ("status", {"phase": "starting", "text": "Resuming Codex"}),
        )
        self.assertIn(("provider_session", {"session_id": "thread-saved"}), events)

    async def test_start_defaults_to_yolo_app_server_settings(self):
        context, _, _ = self.context()

        await self.provider.run(context, "normal")

        request = next(
            row for row in self.received() if row.get("method") == "thread/start"
        )
        self.assertEqual(request["params"]["approvalPolicy"], "never")
        self.assertEqual(request["params"]["sandbox"], "danger-full-access")

    async def test_activity_phases_are_structured_without_reasoning_text(self):
        context, events, _ = self.context()

        await self.provider.run(context, "normal")

        statuses = [data for kind, data in events if kind == "status"]
        self.assertIn({"phase": "thinking", "text": "Thinking"}, statuses)
        self.assertIn(
            {"phase": "thinking", "text": "Thinking", "item_id": "reason-1"},
            statuses,
        )
        self.assertIn(
            {"phase": "working", "text": "Working", "item_id": "cmd-1"},
            statuses,
        )
        self.assertIn(
            {
                "phase": "responding",
                "text": "Writing response",
                "item_id": "msg-1",
            },
            statuses,
        )
        self.assertNotIn("private reasoning", repr(events))

    async def test_collab_agents_are_normalized_with_nested_lifecycle_data(self):
        context, events, _ = self.context()

        await self.provider.run(context, "agents")

        tools = [data for kind, data in events if kind == "tool"]
        self.assertTrue(tools)
        self.assertTrue(all("agents" in event for event in tools))
        spawn_a = next(
            event
            for event in tools
            if event["item_id"] == "spawn-a" and event["status"] == "completed"
        )
        self.assertEqual(
            spawn_a["agents"],
            [{
                "id": "agent-a",
                "parent_id": "thread-new",
                "label": "Inspect the parser",
                "state": "running",
                "model": "gpt-5.6-sol",
                "started_at": 1000,
            }],
        )
        spawn_b = next(
            event
            for event in tools
            if event["item_id"] == "spawn-b" and event["status"] == "completed"
        )
        self.assertEqual(spawn_b["agents"][0]["parent_id"], "agent-a")
        self.assertEqual(spawn_b["agents"][0]["started_at"], 1200)

        finished = next(event for event in tools if event["item_id"] == "wait-agents" and event["status"] == "failed")
        self.assertEqual(
            finished["agents"],
            [
                {
                    "id": "agent-a", "state": "completed",
                    "result": "Parser supports variants", "finished_at": 2000,
                },
                {
                    "id": "agent-b", "state": "errored",
                    "result": "Nested fixture failed", "finished_at": 2000,
                },
            ],
        )
        future = next(event for event in tools if event["item_id"] == "list-agents")
        self.assertEqual(future["agents"][0]["state"], "pausedByHost")
        resume_started = next(
            event
            for event in tools
            if event["item_id"] == "resume-a" and event["status"] == "running"
        )
        self.assertEqual(
            resume_started["agents"],
            [{"id": "agent-a", "state": "running", "started_at": 2200}],
        )
        self.assertNotIn("reasoningEffort", repr(tools))

    async def test_file_and_permission_approvals_fail_closed_and_echo_grant(self):
        context, _, approvals = self.context(decisions=["reject", "allow"])

        await self.provider.run(context, "file permission")

        self.assertEqual([row["kind"] for row in approvals], ["file_change", "permission"])
        received = self.received()
        file_reply = next(row for row in received if row.get("id") == 71)
        self.assertEqual(file_reply["result"], {"decision": "decline"})
        permission_reply = next(row for row in received if row.get("id") == "permission-1")
        self.assertEqual(permission_reply["result"]["scope"], "turn")
        self.assertEqual(
            permission_reply["result"]["permissions"],
            approvals[1]["details"]["permissions"],
        )

    async def test_cancel_interrupts_exact_turn_and_reaps_process(self):
        waiting = asyncio.Event()
        context, _, _ = self.context()
        original_emit = context.emit

        async def emit(kind, data):
            await original_emit(kind, data)
            if kind == "status" and data.get("text") == "waiting":
                waiting.set()

        context.emit = emit
        task = asyncio.create_task(self.provider.run(context, "wait"))
        await asyncio.wait_for(waiting.wait(), 2.0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        interrupt = next(
            row for row in self.received() if row.get("method") == "turn/interrupt"
        )
        self.assertEqual(
            interrupt["params"], {"threadId": "thread-new", "turnId": "turn-test"}
        )
        pid = next(row["pid"] for row in (
            json.loads(line)
            for line in self.executable.with_suffix(".log").read_text().splitlines()
        ) if "pid" in row)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_oversized_protocol_line_fails_and_reaps_process(self):
        context, _, _ = self.context()
        provider = CodexProvider(
            str(self.executable), line_limit=1024, request_timeout=2.0
        )

        with self.assertRaisesRegex(ProviderError, "larger than 1024 bytes"):
            await provider.run(context, "oversize")

        pid = next(
            row["pid"]
            for row in (
                json.loads(line)
                for line in self.executable.with_suffix(".log").read_text().splitlines()
            )
            if "pid" in row
        )
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_valid_protocol_line_preserves_output_larger_than_64_kib(self):
        context, events, _ = self.context()

        await self.provider.run(context, "long output")

        delta = next(data for kind, data in events if kind == "message_delta")
        final = next(data for kind, data in events if kind == "message")
        self.assertEqual(len(delta["text"].encode("utf-8")), 80000)
        self.assertEqual(final["text"], delta["text"])
        self.assertNotIn("truncated", final["text"])

    async def test_unknown_server_request_is_rejected_and_run_fails(self):
        context, _, _ = self.context()

        with self.assertRaisesRegex(ProviderError, "cannot answer safely"):
            await self.provider.run(context, "unsupported")

        rejection = next(row for row in self.received() if row.get("id") == "question-1")
        self.assertEqual(rejection["error"]["code"], -32601)

    async def test_turn_failure_is_actionable(self):
        context, _, _ = self.context()

        with self.assertRaisesRegex(ProviderError, "quota exhausted.*UsageLimitExceeded"):
            await self.provider.run(context, "failed")

    async def test_check_discovers_version_auth_and_all_model_pages_without_turn(self):
        result = await self.provider.check()

        self.assertTrue(result["available"])
        self.assertEqual(result["version"], "codex-cli 0.test")
        self.assertEqual(
            result["models"],
            [
                {
                    "id": "gpt-first",
                    "name": "First",
                    "reasoning_efforts": ["low", "medium", "high"],
                    "default_reasoning_effort": "medium",
                    "is_default": True,
                },
                {
                    "id": "gpt-second",
                    "name": "Second",
                    "reasoning_efforts": ["medium", "high"],
                    "default_reasoning_effort": "high",
                    "is_default": False,
                },
            ],
        )
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertFalse(any(row.get("method") == "turn/start" for row in self.received()))

    async def test_check_reports_missing_auth_without_exposing_account_data(self):
        executable = self.directory / "codex-unauth"
        executable.write_text(textwrap.dedent(FAKE_CODEX), encoding="utf-8")
        executable.chmod(0o700)

        result = await CodexProvider(str(executable), request_timeout=2.0).check()

        self.assertFalse(result["available"])
        self.assertIn("codex login", result["detail"])
        self.assertEqual(len(result["models"]), 2)

    async def test_user_local_codex_works_without_interactive_shell_path(self):
        executable = self.directory / ".local/bin/codex"
        executable.parent.mkdir(parents=True)
        executable.write_text(FAKE_CODEX.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        executable.chmod(0o700)
        with patch.dict(os.environ, {"HOME": str(self.directory), "PATH": "/usr/bin:/bin"}):
            provider = CodexProvider(request_timeout=2.0)
            result = await provider.check()
            self.assertTrue(result['available'])
            context, events, _ = self.context()
            await provider.run(context, "normal")
        self.assertIn(("message", {"role": "assistant", "text": "Done", "item_id": "msg-1"}), events)

    async def test_missing_executable_is_unavailable(self):
        result = await CodexProvider(str(self.directory / "missing")).check()
        self.assertFalse(result["available"])
        self.assertIn("not installed", result["detail"])


if __name__ == "__main__":
    unittest.main()
