"""Native request reconstruction in disposable homes against a rejecting localhost server."""

import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid

import csl_claude
from csl_diagnostics import metrics


def native_replay(source, provider, model=None, expected_markers=(), forbidden_markers=()):
    from csl_transfer import read_records, rewrite_import_record
    executable = shutil.which(provider)
    if not executable:
        raise ValueError(f"{provider} is not installed; cannot verify native replay")
    if Path(source).stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Native replay probe is limited to 64 MiB inputs")
    captures = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 32 * 1024 * 1024:
                captures.append({"path": self.path, "error": "request exceeds 32 MiB capture limit"})
            else:
                try:
                    data = json.loads(self.rfile.read(size))
                    items = data.get("input" if provider == "codex" else "messages")
                    if isinstance(items, list):
                        serialized = json.dumps(items, ensure_ascii=False)
                        captures.append({"path": self.path, "bytes": size, "metrics": metrics(items),
                                         "missing_marker_indexes": [i for i, marker in enumerate(expected_markers) if marker not in serialized],
                                         "forbidden_marker_indexes": [i for i, marker in enumerate(forbidden_markers) if marker in serialized]})
                except (ValueError, TypeError):
                    pass
            body = json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                "message": "CSL localhost continuity probe stopped before model generation"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with tempfile.TemporaryDirectory(prefix="csl-native-probe-") as temp:
        root = Path(temp)
        cwd = root / "project"
        cwd.mkdir()
        codex_home, claude_home = root / "codex", root / "claude"
        sid = str(uuid.uuid4())
        now = dt.datetime.now(dt.UTC)
        if provider == "codex":
            target = codex_home / "sessions" / now.strftime("%Y/%m/%d") / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"
        else:
            encoded = "".join(char if char.isascii() and char.isalnum() else "-" for char in str(cwd))
            target = claude_home / "projects" / encoded / f"{sid}.jsonl"
        target.parent.mkdir(parents=True)
        source_id = sid
        with Path(source).open("rb") as stream, target.open("xb") as output:
            os.chmod(target, 0o600)
            for record in read_records(stream):
                if provider == "codex":
                    if record.get("type") == "session_meta":
                        source_id = record.get("payload", {}).get("id", sid)
                    record = rewrite_import_record(record, sid, {"id": source_id}, cwd, now.isoformat())
                    if record.get("type") in ("session_meta", "turn_context"):
                        record["payload"]["model_provider"] = "csl_probe"
                else:
                    record = csl_claude.rewrite(record, sid, cwd, source_id)
                if record is not None:
                    output.write((json.dumps(record, ensure_ascii=False) + "\n").encode())
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        env = {"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8",
               "CODEX_HOME": str(codex_home), "CLAUDE_CONFIG_DIR": str(claude_home),
               "OPENAI_API_KEY": "csl-localhost-not-a-real-key", "ANTHROPIC_API_KEY": "csl-localhost-not-a-real-key",
               "ANTHROPIC_BASE_URL": base, "NO_PROXY": "127.0.0.1,localhost",
               "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        if provider == "codex":
            command = [executable, "exec", "-s", "read-only", "-c", 'model_provider="csl_probe"',
                "-c", 'model_providers.csl_probe.name="CSL localhost verification"',
                "-c", f'model_providers.csl_probe.base_url="{base}/v1"',
                "-c", 'model_providers.csl_probe.wire_api="responses"',
                "-c", 'model_providers.csl_probe.requires_openai_auth=false',
                "-c", "model=" + json.dumps(model or "csl-offline-probe"),
                "resume", "--all", "--skip-git-repo-check", "--json", sid, "-"]
            prompt = "Do not execute tools. Reply ACK only."
        else:
            command = [executable, "--bare", "--strict-mcp-config", "--setting-sources", "", "--tools", "",
                "--model", model or "claude-haiku-5-5", "--resume", sid, "--max-turns", "1",
                "--print", "--output-format", "json", "Do not execute tools. Reply ACK only."]
            prompt = None
        process = None
        timed_out = False
        try:
            version = subprocess.run([executable, "--version"], env=env, cwd=cwd,
                                     capture_output=True, text=True, timeout=10).stdout.strip()[:200]
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                process.communicate(prompt, timeout=45)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
        except subprocess.SubprocessError as exc:
            raise ValueError("Native CLI probe failed or timed out before reconstruction") from exc
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        measured = next((capture for capture in reversed(captures) if "metrics" in capture), None)
        return {"status": "request-reconstructed" if measured else "not-reconstructed", "cli_version": version,
                "isolated": True, "endpoint": "localhost", "generation": "not-performed",
                "live_continuation": "not-tested", "timed_out": timed_out,
                "metrics": measured["metrics"] if measured else None,
                "missing_marker_indexes": measured["missing_marker_indexes"] if measured else [],
                "forbidden_marker_indexes": measured["forbidden_marker_indexes"] if measured else [],
                "request_path": measured["path"] if measured else None,
                "capture_errors": [capture["error"] for capture in captures if "error" in capture]}
