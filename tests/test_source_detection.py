import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_sessions as csl
import csl_transfer as transfer
from test_claude import fixture

REPO = Path(__file__).resolve().parents[1]


class SourceDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex"
        self.claude_home = self.root / "claude"
        self.db = self.codex_home / "index.sqlite"
        self.env = {**os.environ, "CODEX_HOME": str(self.codex_home), "CLAUDE_CONFIG_DIR": str(self.claude_home)}
        self.codex_id, self.claude_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.codex_path = self.codex(self.codex_id)
        self.claude_path = self.claude(self.claude_id)

    def tearDown(self):
        self.tmp.cleanup()

    def codex(self, sid, archived=False):
        folder = self.codex_home / ("archived_sessions" if archived else "sessions/2026/01/01")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": sid, "cwd": "/source/project"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Synthetic Codex marker"}]}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in records))
        return path

    def claude(self, sid):
        path = self.claude_home / "projects/source" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in fixture(sid)))
        return path

    def run_cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(REPO / "codex_sessions.py"), *map(str, args)],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, ok, result.stderr or result.stdout)
        return result

    def export(self, selectors, filename="result.zip", extra=(), ok=True):
        bundle = self.root / filename
        result = self.run_cli("export", *map(str, selectors), "--to", bundle, *extra, ok=ok)
        if not ok:
            self.assertFalse(bundle.exists())
            return result
        with zipfile.ZipFile(bundle) as archive:
            return json.loads(archive.read("manifest.json"))

    def test_auto_detects_uuid_and_prefix_for_both(self):
        for sid, expected in [(self.codex_id, "codex"), (self.claude_id, "claude")]:
            for selector in (sid, sid[:8]):
                with self.subTest(selector=selector):
                    manifest = self.export([selector], filename=f"{selector}.zip")
                    self.assertEqual(manifest["sessions"][0].get("provider", "codex"), expected)
                    self.assertEqual(manifest["version"], 1 if expected == "codex" else 2)

    def test_path_detection_uses_content_not_filename(self):
        for path, expected in [(self.codex_path, "codex"), (self.claude_path, "claude")]:
            renamed = self.root / f"arbitrary-{expected}.jsonl"
            renamed.write_bytes(path.read_bytes())
            manifest = self.export([renamed], filename=f"path-{expected}.zip")
            self.assertEqual(manifest["sessions"][0].get("provider", "codex"), expected)

    def test_same_uuid_across_providers_requires_provider(self):
        self.claude(self.codex_id)
        result = self.export([self.codex_id], ok=False)
        self.assertIn("--provider codex or --provider claude", result.stderr)
        self.assertIn("Ambiguous", result.stderr)
        for provider in ("codex", "claude"):
            manifest = self.export([self.codex_id], filename=f"explicit-{provider}.zip", extra=("--provider", provider))
            self.assertEqual(manifest["sessions"][0].get("provider", "codex"), provider)

    def test_path_disambiguates_identical_uuid(self):
        path = self.claude(self.codex_id)
        self.assertEqual(self.export([path])["sessions"][0]["provider"], "claude")

    def test_title_matches_across_providers_require_provider(self):
        conn = csl.init_db(self.db)
        try:
            csl.upsert_session(conn, csl.parse_session(self.codex_path, {
                self.codex_id: {"thread_name": "Synthetic Claude handoff"}}))
            conn.commit()
        finally:
            conn.close()
        result = self.run_cli("--db", self.db, "export", "Synthetic Claude handoff", "--to", self.root / "ambiguous.zip", ok=False)
        self.assertIn("Ambiguous", result.stderr)
        self.assertFalse((self.root / "ambiguous.zip").exists())

    def test_claude_title_detected_without_provider(self):
        manifest = self.export(["Synthetic Claude handoff"])
        self.assertEqual(manifest["sessions"][0]["provider"], "claude")

    def test_no_match_is_clear_and_creates_no_package(self):
        result = self.export([str(uuid.uuid4())], ok=False)
        self.assertIn("0 matches", result.stderr)

    def test_same_provider_ambiguity_still_needs_uuid_or_path(self):
        sid = str(uuid.uuid4())
        self.claude(sid)
        result = self.export(["Synthetic Claude handoff"], ok=False)
        self.assertIn("2 matches", result.stderr)
        self.assertIn("UUID or path", result.stderr)

    def test_archived_codex_discovered_without_index(self):
        sid = str(uuid.uuid4())
        self.codex(sid, archived=True)
        self.assertEqual(self.export([sid])["sessions"][0]["id"], sid)

    def test_mixed_provider_batch_rejected_before_export(self):
        result = self.export([self.codex_id, self.claude_id], ok=False)
        self.assertIn("Mixed-provider", result.stderr)

    def test_malformed_matched_claude_does_not_fall_back(self):
        self.claude_path.write_text('{"sessionId":')
        result = self.export([self.claude_id], ok=False)
        self.assertIn("Invalid JSONL", result.stderr)

    def test_wrong_explicit_provider_on_path_is_not_overridden(self):
        self.export([self.codex_path], extra=("--provider", "claude"), ok=False)
        self.export([self.claude_path], extra=("--provider", "codex"), ok=False)

    def test_no_selector_picker_keeps_chosen_codex_path(self):
        self.claude(self.codex_id)
        conn = csl.init_db(self.db)
        try:
            csl.upsert_session(conn, csl.parse_session(self.codex_path, {}))
            conn.commit()
        finally:
            conn.close()
        with patch.dict(os.environ, self.env), patch.object(csl, "DEFAULT_CODEX_HOME", self.codex_home), \
             patch.object(csl, "DEFAULT_SESSIONS_DIR", self.codex_home / "sessions"), \
             patch.object(csl, "DEFAULT_SESSION_INDEX", self.codex_home / "session_index.jsonl"), \
             patch.object(transfer.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="1"), \
             contextlib.redirect_stdout(io.StringIO()):
            selected = transfer.resolve_sources([], csl, self.db)
        self.assertEqual(selected[0][0], self.codex_path)
