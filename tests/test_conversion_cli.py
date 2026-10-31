import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import csl_canonical as canonical
from test_claude import fixture

REPO = Path(__file__).resolve().parents[1]


def codex_fixture(sid):
    records = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": "/sender/project", "source": "cli",
            "originator": "csl_synthetic_conversion", "cli_version": "0.166.0", "model_provider": "openai"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
            "content": [{"type": "input_text", "text": "SOURCE-POLICY-MUST-NOT-BECOME-AUTHORITY"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "USER-MARKER-874. Initial release was cobalt."}]}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec_command",
            "call_id": "call-synthetic-1", "input": "historical-command(ARGUMENT-617)"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call-synthetic-1",
            "output": "TOOL-RESULT-398", "csl_is_error": False}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "ANSWER-MARKER-629"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Correction: use amber42 and SQLite, not the initial release."}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "amber42 with SQLite retained."}]}},
    ]
    return [{"timestamp": "2026-01-01T00:00:00Z", **record} for record in records]


class ConversionCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "project with spaces"
        self.cwd.mkdir()
        self.home, self.claude = self.root / "codex", self.root / "claude"
        self.env = {**os.environ, "CODEX_HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.claude)}
        self.env.pop("CLAUDE_CODE_PROJECT_DIR_NAME", None)
        self.sid = str(uuid.uuid4())
        self.source = self.home / "sessions/2026/01/01" / f"rollout-2026-01-01T00-00-00-{self.sid}.jsonl"
        self.source.parent.mkdir(parents=True)
        self.records = codex_fixture(self.sid)
        self.write_source()
        self.claude_id = str(uuid.uuid4())
        self.claude_source = self.claude / "projects/source" / f"{self.claude_id}.jsonl"
        self.claude_source.parent.mkdir(parents=True)
        self.claude_source.write_text("".join(json.dumps(row) + "\n" for row in fixture(self.claude_id)))

    def tearDown(self):
        self.tmp.cleanup()

    def write_source(self):
        self.source.write_text("".join(json.dumps(record) + "\n" for record in self.records))

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(REPO / "codex_sessions.py"), *map(str, args)],
            env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode == 0, ok, result.stderr or result.stdout)
        return result

    def installed(self, output, target):
        sid = re.search(r"imported: ([0-9a-f-]+)", output).group(1)
        root = self.home / "sessions" if target == "codex" else self.claude / "projects"
        return next(root.rglob(f"*{sid}.jsonl"))

    def test_all_four_native_routes_preserve_semantic_history(self):
        before = self.source.read_bytes()
        for provider, source, sid in [("codex", self.source, self.sid), ("claude", self.claude_source, self.claude_id)]:
            expected = canonical.semantic_events(canonical.normalize(provider, source))
            for target in ("codex", "claude"):
                result = self.cli("convert", sid, "--to", target, "--cwd", self.cwd)
                path = self.installed(result.stdout, target)
                self.assertEqual(canonical.semantic_events(canonical.normalize(target, path)), expected)
                self.assertNotIn("SOURCE-POLICY-MUST-NOT-BECOME-AUTHORITY", path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertIn("live=not-tested", result.stdout)
        self.assertEqual(self.source.read_bytes(), before)

    def test_canonical_export_restore_and_exclusive_publication(self):
        output = self.root / "conversation.json"
        self.cli("convert", self.sid, "--to", "canonical", "--out", output)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        data = json.loads(output.read_text())
        self.assertEqual(data["format"], "csl-conversation")
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["source"]["session_id"], self.sid)
        self.assertEqual(len(data["source_context"]), 1)
        before = output.read_bytes()
        self.cli("convert", self.sid, "--to", "canonical", "--out", output, ok=False)
        self.assertEqual(output.read_bytes(), before)
        result = self.cli("convert", output, "--to", "claude", "--cwd", self.cwd)
        path = self.installed(result.stdout, "claude")
        self.assertEqual(canonical.semantic_events(canonical.normalize("claude", path)), canonical.semantic_events(data))

    def test_import_target_uses_canonical_and_keeps_original(self):
        bundle = self.root / "original.zip"
        self.cli("export", self.sid, "--to", bundle)
        before = bundle.read_bytes()
        result = self.cli("import", bundle, "--target", "claude", "--cwd", self.cwd)
        path = self.installed(result.stdout, "claude")
        self.assertIn("converted: codex -> claude", result.stdout)
        self.assertEqual(canonical.semantic_events(canonical.from_claude(path)), canonical.semantic_events(canonical.from_codex(self.source)))
        self.assertEqual(bundle.read_bytes(), before)

    def test_context_character_limit_rejects_before_install(self):
        before = list(self.claude.rglob("*.jsonl"))
        result = self.cli("convert", self.sid, "--to", "claude", "--cwd", self.cwd, "--max-context-chars", "10", ok=False)
        self.assertIn("character limit", result.stderr)
        self.assertEqual(list(self.claude.rglob("*.jsonl")), before)

    def test_opaque_compaction_requires_explicit_visible_recovery(self):
        self.records.insert(3, {"type": "compacted", "payload": {"replacement_history": [
            {"type": "compaction", "encrypted_content": "opaque"}], "replacement_history_metadata": [{}]}})
        self.write_source()
        self.cli("convert", self.sid, "--to", "claude", "--cwd", self.cwd, ok=False)
        result = self.cli("convert", self.sid, "--to", "claude", "--cwd", self.cwd, "--history-policy", "visible")
        path = self.installed(result.stdout, "claude")
        self.assertIn("not exact native context", path.read_text())

    def test_portable_lost_compaction_cannot_silently_convert(self):
        self.records.append({"type": "compacted", "payload": {"replacement_history": [
            {"type": "compaction", "encrypted_content": "opaque"}], "replacement_history_metadata": [{}]}})
        self.write_source()
        bundle = self.root / "lost-state.zip"
        self.cli("export", self.sid, "--to", bundle)
        result = self.cli("import", bundle, "--target", "claude", "--cwd", self.cwd, ok=False)
        self.assertIn("lost encrypted compaction", result.stderr)
        result = self.cli("import", bundle, "--target", "claude", "--cwd", self.cwd, "--history-policy", "visible")
        self.assertIn("WARNING: explicit visible-history", result.stdout)

    def test_incomplete_tool_only_explicit_unknown_repair(self):
        self.records = [r for r in self.records if r.get("payload", {}).get("type") != "custom_tool_call_output"]
        self.write_source()
        self.cli("convert", self.sid, "--to", "claude", "--cwd", self.cwd, ok=False)
        result = self.cli("convert", self.sid, "--to", "claude", "--cwd", self.cwd, "--repair-interrupted")
        path = self.installed(result.stdout, "claude")
        self.assertIn("Outcome UNKNOWN", path.read_text())

    def test_pdf_and_image_rejected_not_silently_dropped(self):
        self.records[2]["payload"]["content"].append({"type": "input_image", "image_url": "https://invalid.test/not-fetched"})
        self.write_source()
        result = self.cli("convert", self.sid, "--to", "claude", "--cwd", self.cwd, ok=False)
        self.assertIn("explicit asset packaging", result.stderr)

    def test_doctor_reports_levels_and_does_not_mutate(self):
        before = self.source.read_bytes()
        result = self.cli("doctor", self.sid, "--json", "--context-window", "10")
        report = json.loads(result.stdout)["sessions"][0]
        self.assertEqual(report["structure"], "passed")
        self.assertEqual(report["native_replay"], "not-tested")
        self.assertEqual(report["live_continuation"], "not-tested")
        self.assertFalse(report["token_estimate"]["exact"])
        self.assertTrue(any("context_risk" in warning for warning in report["warnings"]))
        self.assertEqual(self.source.read_bytes(), before)

    def test_doctor_bad_compaction_is_failed_not_ready(self):
        self.records.append({"type": "compacted", "payload": {"replacement_history": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary"}]}],
            "replacement_history_metadata": [{}, {}]}})
        self.write_source()
        result = self.cli("doctor", self.source, "--json", ok=False)
        report = json.loads(result.stdout)["sessions"][0]
        self.assertEqual(report["structure"], "failed")
        self.assertIn("lengths differ", report["errors"][0])

    def valid_checkpoint(self):
        self.records[2]["payload"]["content"][0]["text"] = "DISCARDED-PREFIX-771"
        self.records.append({"type": "compacted", "timestamp": "2026-01-01T01:00:00Z", "payload": {
            "message": "", "replacement_history": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "RETAINED-SUMMARY-582: latest release amber42 with SQLite"}]}],
            "replacement_history_metadata": [{"client_authored": False}]}})
        self.write_source()

    def test_active_checkpoint_excludes_old_prefix_in_conversion(self):
        self.valid_checkpoint()
        result = self.cli("convert", self.sid, "--to", "claude", "--cwd", self.cwd)
        path = self.installed(result.stdout, "claude")
        self.assertIn("RETAINED-SUMMARY-582", path.read_text())
        self.assertNotIn("DISCARDED-PREFIX-771", path.read_text())

    def test_doctor_zip_and_import_budget(self):
        bundle = self.root / "test.zip"
        self.cli("export", self.sid, "--to", bundle)
        report = json.loads(self.cli("doctor", bundle, "--json").stdout)["sessions"][0]
        self.assertEqual(report["source_id"], self.sid)
        self.cli("import", bundle, "--cwd", self.cwd, "--max-context-chars", "1", ok=False)

    @unittest.skipUnless(os.environ.get("CSL_RUN_CODEX_NATIVE_TESTS") == "1" and
                         os.environ.get("CSL_RUN_CLAUDE_NATIVE_TESTS") == "1" and
                         shutil.which("codex") and shutil.which("claude"), "Enable both isolated native CLI checks")
    def test_all_four_native_routes_reconstruct_expected_request_text(self):
        for provider, source in [("codex", self.sid), ("claude", self.claude_id)]:
            for target in ("codex", "claude"):
                with self.subTest(route=f"{provider}->{target}"):
                    result = self.cli("convert", source, "--to", target, "--cwd", self.cwd)
                    path = self.installed(result.stdout, target)
                    expected = ["USER-MARKER-874", "ANSWER-MARKER-629"]
                    if provider == "codex":
                        expected += ["amber42", "SQLite", "TOOL-RESULT-398", "ARGUMENT-617"]
                    flags = [value for marker in expected for value in ("--expect-text", marker)]
                    result = self.cli("doctor", path, "--json", "--native", "--model",
                        "gpt-6.1-sol" if target == "codex" else "claude-haiku-5-5", *flags,
                        "--absent-text", "SOURCE-POLICY-MUST-NOT-BECOME-AUTHORITY")
                    report = json.loads(result.stdout)["sessions"][0]
                    self.assertEqual(report["native_replay"], "request-reconstructed")
                    self.assertEqual(report["live_continuation"], "not-tested")
                    self.assertEqual(report["native"]["missing_marker_indexes"], [])

    @unittest.skipUnless(os.environ.get("CSL_RUN_CODEX_NATIVE_TESTS") == "1" and shutil.which("codex"),
                         "Enable isolated native Codex check")
    def test_default_offline_probe_and_failed_transport_assertion(self):
        result = self.cli("doctor", self.sid, "--json", "--native", "--expect-text", "amber42")
        self.assertEqual(json.loads(result.stdout)["sessions"][0]["native_replay"], "request-reconstructed")
        result = self.cli("doctor", self.sid, "--json", "--native", "--expect-text", "NEVER-PRESENT-MARKER-9981", ok=False)
        report = json.loads(result.stdout)["sessions"][0]
        self.assertEqual(report["native"]["missing_marker_indexes"], [0])
        self.assertNotIn("NEVER-PRESENT-MARKER-9981", result.stdout)

    @unittest.skipUnless(os.environ.get("CSL_RUN_CODEX_NATIVE_TESTS") == "1" and shutil.which("codex"),
                         "Enable isolated native Codex check")
    def test_native_loader_applies_valid_compaction(self):
        self.valid_checkpoint()
        result = self.cli("doctor", self.sid, "--native", "--json", "--expect-text", "RETAINED-SUMMARY-582",
                          "--absent-text", "DISCARDED-PREFIX-771")
        report = json.loads(result.stdout)["sessions"][0]
        self.assertEqual(report["native_replay"], "request-reconstructed")

    @unittest.skipUnless(os.environ.get("CSL_RUN_CODEX_NATIVE_TESTS") == "1" and shutil.which("codex"),
                         "Enable isolated native Codex UI check")
    def test_converted_claude_history_is_visible_in_codex(self):
        from test_native_display import read_native_items
        result = self.cli("convert", self.claude_id, "--to", "codex", "--cwd", self.cwd)
        path = self.installed(result.stdout, "codex")
        sid = re.search(r"([0-9a-f-]{36})\.jsonl$", path.name).group(1)
        items = read_native_items(self, self.root, self.env, sid)
        self.assertIn("USER-MARKER-874", json.dumps([item for item in items if item["type"] == "userMessage"]))
        self.assertIn("ANSWER-MARKER-629", json.dumps([item for item in items if item["type"] == "agentMessage"]))
