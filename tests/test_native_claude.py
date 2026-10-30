"""Opt-in native Claude replay check against localhost, never a paid API."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

from test_claude import fixture

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("CSL_RUN_CLAUDE_NATIVE_TESTS") == "1" and shutil.which("claude"),
                     "Set CSL_RUN_CLAUDE_NATIVE_TESTS=1 with Claude installed for localhost replay verification")
class NativeClaudeTests(unittest.TestCase):
    def test_native_loader_sends_imported_history(self):
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                if size > 4 * 1024 * 1024:
                    self.send_error(413)
                    return
                captured.append(json.loads(self.rfile.read(size)))
                body = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                    "message": "CSL localhost replay verification stopped before generation"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cwd = root / "project"
            cwd.mkdir()
            home = root / "claude"
            sid = str(uuid.uuid4())
            source = home / "projects/source" / f"{sid}.jsonl"
            source.parent.mkdir(parents=True)
            rows = fixture(sid)
            assistant = rows[1]
            assistant["message"]["content"].append({"type": "tool_use", "id": "toolu_synthetic_read",
                "name": "Read", "input": {"file_path": "/historical-only/TOOL-ARGUMENT-617"}})
            assistant["message"]["stop_reason"] = "tool_use"
            tool_result = {**rows[0], "uuid": str(uuid.uuid4()), "parentUuid": assistant["uuid"],
                "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_synthetic_read",
                    "content": "TOOL-RESULT-398", "is_error": False}]}}
            rows.insert(2, tool_result)
            rows[3]["leafUuid"] = tool_result["uuid"]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8",
                   "CLAUDE_CONFIG_DIR": str(home), "CODEX_HOME": str(root / "codex"),
                   "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                   "ANTHROPIC_API_KEY": "csl-localhost-synthetic-not-a-real-key", "NO_PROXY": "127.0.0.1,localhost"}
            cli = [sys.executable, str(REPO / "codex_sessions.py")]
            bundle = root / "sample.zip"
            subprocess.run(cli + ["export", sid, "--provider", "claude", "--to", str(bundle)],
                           env=env, text=True, capture_output=True, timeout=20, check=True)
            imported = subprocess.run(cli + ["import", str(bundle), "--cwd", str(cwd)],
                                      env=env, text=True, capture_output=True, timeout=20, check=True)
            target = re.search(r"imported: ([0-9a-f-]+)", imported.stdout).group(1)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"
            try:
                result = subprocess.run(["claude", "--bare", "--strict-mcp-config", "--setting-sources", "",
                    "--tools", "", "--model", "claude-haiku-5-5", "--resume", target,
                    "--max-turns", "1", "--print", "--output-format", "json", "Do not use tools. Reply ACK."],
                    env=env, cwd=cwd, capture_output=True, text=True, timeout=40)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            messages = [body.get("messages", []) for body in captured if "messages" in body]
            self.assertTrue(messages, result.stderr[:1000] + result.stdout[:1000])
            self.assertIn("USER-MARKER-874", json.dumps(messages))
            self.assertIn("ANSWER-MARKER-629", json.dumps(messages))
            self.assertIn("TOOL-ARGUMENT-617", json.dumps(messages))
            self.assertIn("TOOL-RESULT-398", json.dumps(messages))
