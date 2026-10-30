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
import csl_claude as claude
import csl_transfer as transfer

REPO = Path(__file__).resolve().parents[1]


def fixture(sid, cwd="/sender/project"):
    user, assistant, result = (str(uuid.uuid4()) for _ in range(3))
    common = {"sessionId": sid, "cwd": cwd, "version": "2.1.308", "isSidechain": False,
              "userType": "external", "timestamp": "2026-01-01T00:00:00Z"}
    return [
        {**common, "type": "user", "uuid": user, "parentUuid": None,
         "message": {"role": "user", "content": [{"type": "text", "text": "USER-MARKER-874"}]}},
        {**common, "type": "assistant", "uuid": assistant, "parentUuid": user,
         "message": {"role": "assistant", "id": "msg_synthetic", "type": "message", "model": "claude-haiku-5-5",
                     "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
                     "content": [{"type": "text", "text": "ANSWER-MARKER-629"}]}},
        {**common, "type": "last-prompt", "leafUuid": assistant, "lastPrompt": "USER-MARKER-874"},
        {**common, "type": "custom-title", "customTitle": "Synthetic Claude handoff"},
    ]


class ClaudeTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "claude"
        self.codex = self.root / "codex"
        self.cwd = self.root / "recipient project"
        self.cwd.mkdir()
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.home), "CODEX_HOME": str(self.codex)}
        self.env.pop("CLAUDE_CODE_PROJECT_DIR_NAME", None)
        self.sid = str(uuid.uuid4())
        self.source = self.home / "projects/sender" / f"{self.sid}.jsonl"
        self.source.parent.mkdir(parents=True)
        self.records = fixture(self.sid)
        self.write_source()
        self.bundle = self.root / "handoff.csl.zip"

    def tearDown(self):
        self.tmp.cleanup()

    def write_source(self):
        self.source.write_text("".join(json.dumps(record) + "\n" for record in self.records))

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(REPO / "codex_sessions.py"), *map(str, args)],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode == 0, ok, result.stderr or result.stdout)
        return result

    def export(self, *extra, ok=True):
        return self.cli("export", self.sid, "--provider", "claude", "--to", self.bundle, *extra, ok=ok)

    def imports(self):
        return [p for p in (self.home / "projects").glob("*/*.jsonl") if p != self.source]

    def test_native_roundtrip_preserves_messages_and_remaps_graph(self):
        original = self.source.read_bytes()
        self.export()
        with zipfile.ZipFile(self.bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["version"], 2)
            self.assertEqual(manifest["sessions"][0]["provider"], "claude")
        result = self.cli("import", self.bundle, "--cwd", self.cwd)
        path, = self.imports()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(path.stem, self.sid)
        self.assertEqual(rows[1]["parentUuid"], rows[0]["uuid"])
        self.assertEqual(rows[2]["leafUuid"], rows[1]["uuid"])
        self.assertNotEqual(rows[0]["uuid"], self.records[0]["uuid"])
        for row, source in zip(rows, self.records):
            self.assertEqual(row["cwd"], str(self.cwd))
            self.assertEqual(row["sessionId"], path.stem)
            if "message" in source:
                self.assertEqual(row["message"], source["message"])
        self.assertIn("claude --resume " + path.stem, result.stdout)
        self.assertIn("cd ", result.stdout)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse((self.codex / "sessions").exists())
        self.cli("import", self.bundle, "--cwd", self.cwd)
        self.assertEqual(len(self.imports()), 2)

    def test_list_validates_without_installing(self):
        self.export()
        self.cli("import", self.bundle, "--list")
        self.assertEqual(self.imports(), [])

    def test_cross_provider_rejected_without_installing(self):
        self.export()
        result = self.cli("import", self.bundle, "--provider", "codex", "--cwd", self.cwd, ok=False)
        self.assertIn("cross-CLI conversion", result.stderr)
        self.assertEqual(self.imports(), [])

    def test_discovery_by_uuid_title_and_path(self):
        for selector in (self.sid[:8], "Synthetic Claude", str(self.source)):
            with patch.dict(os.environ, self.env):
                selected = claude.resolve([selector], transfer.read_records)
                self.assertEqual(selected[0][1]["id"], self.sid)

    def test_sessions_listing_and_title_filter(self):
        result = self.cli("sessions", "--provider", "claude", "--query", "handoff", "--json")
        rows = json.loads(result.stdout)["sessions"]
        self.assertEqual([row["id"] for row in rows], [self.sid])
        result = self.cli("sessions", "--provider", "claude", "--query", "no match", "--json")
        self.assertEqual(json.loads(result.stdout)["sessions"], [])

    def test_sidechain_transcripts_not_discovered(self):
        subagent = self.source.parent / self.sid / "subagents/agent-test.jsonl"
        subagent.parent.mkdir(parents=True)
        subagent.write_bytes(self.source.read_bytes())
        with patch.dict(os.environ, self.env):
            self.assertEqual(claude.paths(), [self.source])

    def test_portable_thinking_removed_raw_retained(self):
        self.records[1]["message"]["content"].insert(0, {"type": "thinking", "thinking": "hidden", "signature": "signed"})
        self.write_source()
        self.export()
        with zipfile.ZipFile(self.bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertNotIn(b'"thinking": "hidden"', archive.read(manifest["sessions"][0]["file"]))
            self.assertEqual(manifest["sessions"][0]["encrypted_items_removed"], 1)
        self.cli("export", self.sid, "--provider", "claude", "--raw", "--to", self.root / "raw.zip")
        with zipfile.ZipFile(self.root / "raw.zip") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertIn(b'"thinking": "hidden"', archive.read(manifest["sessions"][0]["file"]))

    def test_tool_arguments_and_results_are_not_rewritten(self):
        original = {"type": "user", "sessionId": self.sid, "uuid": "original-node", "parentUuid": None,
                    "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-original",
                     "content": json.dumps({"sessionId": self.sid, "uuid": "application-data"})}]},
                    "toolUseResult": {"sessionId": self.sid, "uuid": "application-data"}}
        rewritten = claude.rewrite(original, str(uuid.uuid4()), self.cwd, self.sid)
        self.assertEqual(original["message"], rewritten["message"])
        self.assertEqual(original["toolUseResult"], rewritten["toolUseResult"])

    def test_missing_parent_rejected(self):
        self.records[0]["parentUuid"] = str(uuid.uuid4())
        self.write_source()
        self.assertIn("missing parent", self.export(ok=False).stderr)

    def test_graph_cycle_rejected(self):
        self.records[0]["parentUuid"] = self.records[1]["uuid"]
        self.write_source()
        self.assertIn("Cycle", self.export(ok=False).stderr)

    def test_compact_summary_can_start_without_ancestor(self):
        self.records[0].update(isCompactSummary=True, parentUuid=str(uuid.uuid4()))
        self.write_source()
        self.export()
        self.cli("import", self.bundle, "--cwd", self.cwd)
        self.assertEqual(len(self.imports()), 1)

    def test_multiple_session_ids_rejected(self):
        self.records[1]["sessionId"] = str(uuid.uuid4())
        self.write_source()
        self.assertIn("multiple session IDs", self.export(ok=False).stderr)

    def test_missing_leaf_rejected(self):
        self.records[2]["leafUuid"] = str(uuid.uuid4())
        self.write_source()
        self.assertIn("missing leaf", self.export(ok=False).stderr)

    def test_malformed_content_rejected_with_clear_error(self):
        self.records[0]["message"]["content"] = ["not a native block"]
        self.write_source()
        result = self.export(ok=False)
        self.assertIn("Invalid Claude content", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_project_override_is_respected_and_cannot_escape(self):
        with patch.dict(os.environ, {**self.env, "CLAUDE_CODE_PROJECT_DIR_NAME": "custom-project"}):
            self.assertEqual(claude.target_path(self.sid, self.cwd).parent.name, "custom-project")
        with patch.dict(os.environ, {**self.env, "CLAUDE_CODE_PROJECT_DIR_NAME": "../../escape"}), self.assertRaises(ValueError):
            claude.target_path(self.sid, self.cwd)

    def test_claude_launch_uses_local_cwd_and_literal_flags(self):
        flags = ["--dangerously-skip-permissions", "--model", "haiku"]
        with patch.object(transfer.subprocess, "call", return_value=0) as launch, self.assertRaises(SystemExit):
            transfer.open_sessions([self.sid], self.cwd, flags, "claude")
        launch.assert_called_once_with(["claude", "--resume", self.sid, *flags], cwd=self.cwd)

    def test_name_override_is_applied_in_native_transcript(self):
        self.export()
        self.cli("import", self.bundle, "--cwd", self.cwd, "--name", "New local title")
        path, = self.imports()
        self.assertEqual(claude.header(path, transfer.read_records)["title"], "New local title")

    def test_sender_permission_settings_and_prompt_not_replayed(self):
        row = {"type": "user", "permissionMode": "bypassPermissions", "allowedTools": ["Bash"],
               "message": {"role": "user", "content": "Keep original message"}}
        rewritten = claude.rewrite(row, str(uuid.uuid4()), self.cwd, self.sid)
        self.assertNotIn("permissionMode", rewritten)
        self.assertNotIn("allowedTools", rewritten)
        self.assertEqual(rewritten["message"], row["message"])
        self.assertIsNone(claude.rewrite({"type": "system-prompt", "content": "sender prompt"},
                                       str(uuid.uuid4()), self.cwd, self.sid))

    def test_unknown_and_mixed_bundle_providers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            transfer.selected_provider([{"provider": "other"}])
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            transfer.selected_provider([{"provider": ["claude"]}])
        with self.assertRaisesRegex(ValueError, "Mixed-provider"):
            transfer.selected_provider([{"provider": "codex"}, {"provider": "claude"}])
