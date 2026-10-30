"""Optional offline native UI test. No model requests or personal session writes."""

import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid


@unittest.skipUnless(os.environ.get("CSL_RUN_CODEX_NATIVE_TESTS") == "1" and shutil.which("codex"),
                     "Set CSL_RUN_CODEX_NATIVE_TESTS=1 with Codex installed for offline native UI verification")
class NativeDisplayTests(unittest.TestCase):
    def test_imported_modern_messages_are_visible_in_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home, cwd = root / "codex", root / "project"
            cwd.mkdir()
            sid, turn = str(uuid.uuid4()), str(uuid.uuid4())
            folder = home / "sessions/2026/01/01"
            folder.mkdir(parents=True)
            source = folder / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
            stamp = "2026-01-01T00:00:00Z"
            records = [
                {"type": "session_meta", "payload": {"id": sid, "session_id": sid, "timestamp": stamp,
                    "cwd": str(cwd), "originator": "csl_native_display_test", "cli_version": "0.164.0",
                    "source": "cli", "model_provider": "openai", "history_mode": "paginated"}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "USER-MARKER-874"}]}},
                {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": sid, "turn_id": turn,
                    "item": {"type": "UserMessage", "id": "u", "client_id": None, "content": [{"type": "text", "text": "USER-MARKER-874", "text_elements": []}]}}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ANSWER-MARKER-629"}]}},
                {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": sid, "turn_id": turn,
                    "item": {"type": "AgentMessage", "id": "a", "phase": "final_answer", "content": [{"type": "Text", "text": "ANSWER-MARKER-629"}]}}},
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn, "last_agent_message": "placeholder"}},
            ]
            source.write_text("".join(json.dumps({"timestamp": stamp, **record}) + "\n" for record in records))
            env = dict(os.environ, CODEX_HOME=str(home), CLAUDE_CONFIG_DIR=str(root / "claude"))
            for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"):
                env.pop(key, None)
            cli = [sys.executable, str(Path(__file__).resolve().parents[1] / "codex_sessions.py")]
            bundle = root / "synthetic.csl.zip"
            subprocess.run(cli + ["export", sid, "--to", str(bundle)], env=env, capture_output=True, text=True, check=True, timeout=20)
            result = subprocess.run(cli + ["import", str(bundle), "--cwd", str(cwd)], env=env, capture_output=True, text=True, check=True, timeout=20)
            imported = re.search(r"imported: ([0-9a-f-]+)", result.stdout).group(1)
            responses = queue.Queue()
            with (root / "stderr").open("w") as error:
                process = subprocess.Popen(["codex", "app-server"], env=env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=error, text=True, start_new_session=True)
                def reader():
                    for line in process.stdout:
                        responses.put(json.loads(line))
                threading.Thread(target=reader, daemon=True).start()
                def send(value):
                    process.stdin.write(json.dumps(value) + "\n")
                    process.stdin.flush()
                def call(number, method, params):
                    send({"id": number, "method": method, "params": params})
                    while True:
                        reply = responses.get(timeout=20)
                        if reply.get("id") == number:
                            self.assertNotIn("error", reply)
                            return reply["result"]
                try:
                    call(1, "initialize", {"clientInfo": {"name": "csl_native_display_test", "version": "1.0"}})
                    send({"method": "initialized", "params": {}})
                    result = call(2, "thread/read", {"threadId": imported, "includeTurns": True})
                    items = [item for turn_data in result["thread"]["turns"] for item in turn_data["items"]]
                    users = [item for item in items if item["type"] == "userMessage"]
                    answers = [item for item in items if item["type"] == "agentMessage"]
                    self.assertEqual(len(users), 1)
                    self.assertEqual(len(answers), 1)
                    self.assertIn("USER-MARKER-874", json.dumps(users))
                    self.assertEqual(answers[0]["text"], "ANSWER-MARKER-629")
                finally:
                    process.stdin.close()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=5)
                    process.stdout.close()
