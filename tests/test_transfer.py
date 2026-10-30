import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_sessions as csl
import csl_transfer as transfer

REPO = Path(__file__).resolve().parents[1]


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex"
        self.cwd = self.root / "recipient project"
        self.cwd.mkdir()
        self.env = {**os.environ, "CODEX_HOME": str(self.home)}
        self.sid = str(uuid.uuid4())
        self.source = self.make_session(self.sid)
        self.bundle = self.root / "package.csl.zip"

    def tearDown(self):
        self.tmp.cleanup()

    def records(self, sid):
        return [
            {"type": "session_meta", "payload": {"id": sid, "session_id": sid,
             "cwd": "/source/project", "history_mode": "paginated",
             "history_base": {"thread_id": "unavailable-parent", "end_ordinal_exclusive": 10},
             "forked_from_ordinal_exclusive": 10,
             "creator_account_id": "old-account", "context_window": {"window_id": "old"}}},
            {"type": "turn_context", "payload": {"cwd": "/source/project"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "Remember cobalt 73. Do not execute tasks."}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "encrypted_content": "cipher"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "example",
             "arguments": "{}", "call_id": "call-original"}},
            {"type": "response_item", "payload": {"type": "function_call_output",
             "call_id": "call-original", "output": "tool result preserved"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "Cobalt 73 recorded."}]}},
        ]

    def make_session(self, sid, archived=False):
        directory = self.home / ("archived_sessions" if archived else "sessions/2026/01/01")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in self.records(sid)))
        return path

    def run_cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, str(REPO / "codex_sessions.py"), *map(str, args)],
                                env=self.env, cwd=self.cwd, text=True, capture_output=True, timeout=20)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def export(self, *args):
        return self.run_cli("export", self.sid, "--to", self.bundle, *args)

    def imported(self):
        return [p for p in (self.home / "sessions").rglob("*.jsonl") if p != self.source]

    def alter(self, mutate, extra=None):
        with zipfile.ZipFile(self.bundle) as archive:
            contents = {n: archive.read(n) for n in archive.namelist()}
        manifest = json.loads(contents["manifest.json"])
        mutate(manifest, contents)
        contents["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(self.bundle, "w") as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
            if extra:
                archive.writestr(*extra)

    def test_portable_roundtrip_preserves_messages_tools_and_source(self):
        original = self.source.read_bytes()
        self.export()
        result = self.run_cli("import", self.bundle, "--cwd", self.cwd)
        self.assertIn("imported:", result.stdout)
        target, = self.imported()
        records = [json.loads(l) for l in target.read_text().splitlines()]
        self.assertEqual(self.source.read_bytes(), original)
        expected = [r for r in self.records(self.sid) if r["payload"].get("type") != "reasoning"]
        self.assertEqual(records[2:], expected[2:])
        meta = records[0]["payload"]
        self.assertNotEqual(meta["id"], self.sid)
        self.assertEqual(meta["session_id"], meta["id"])
        self.assertEqual(meta["cwd"], str(self.cwd))
        self.assertEqual(records[1]["payload"]["cwd"], str(self.cwd))
        self.assertEqual(meta["history_mode"], "legacy")
        self.assertNotIn("creator_account_id", meta)
        self.assertNotIn("context_window", meta)
        self.assertNotIn("history_base", meta)
        self.assertNotIn("forked_from_ordinal_exclusive", meta)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.run_cli("show", meta["id"])

    def test_raw_preserves_encrypted_reasoning(self):
        self.export("--raw")
        with zipfile.ZipFile(self.bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            data = archive.read(manifest["sessions"][0]["file"])
        self.assertEqual(manifest["mode"], "raw")
        self.assertIn(b'"encrypted_content": "cipher"', data)
        self.assertEqual(self.bundle.stat().st_mode & 0o777, 0o600)

    def test_import_rejects_damaged_compaction_before_installing(self):
        records = self.records(self.sid) + [
            {"type": "compacted", "payload": {"message": "", "replacement_history": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "retained"}]}],
                "replacement_history_metadata": [{"client_authored": False}, {"client_authored": False}]}}
        ]
        self.source.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.export("--raw")
        result = self.run_cli("import", self.bundle, "--cwd", self.cwd, ok=False)
        self.assertIn("re-export the original session", result.stderr)
        self.assertEqual(self.imported(), [])

    def test_empty_compaction_metadata_uses_native_defaults(self):
        record = {"type": "compacted", "payload": {"replacement_history": [
            {"type": "message", "role": "user", "content": []}], "replacement_history_metadata": []}}
        transfer.validate_compaction(record)
        record["payload"].pop("replacement_history_metadata")
        transfer.validate_compaction(record)

    def test_portable_compaction_keeps_parallel_metadata_aligned(self):
        original = {"type": "compacted", "payload": {"replacement_history": [
            {"type": "message", "role": "user", "content": "Keep first"},
            {"type": "compaction", "encrypted_content": "cipher"},
            {"type": "message", "role": "assistant", "content": "Keep last"}],
            "replacement_history_metadata": [{"position": 0}, {"position": 1}, {"position": 2}]}}
        before = json.dumps(original, sort_keys=True)
        cleaned, count = transfer.portable_record(original)
        self.assertEqual(count, 1)
        self.assertEqual(len(cleaned["payload"]["replacement_history"]), 2)
        self.assertEqual(cleaned["payload"]["replacement_history_metadata"], [{"position": 0}, {"position": 2}])
        self.assertEqual(json.dumps(original, sort_keys=True), before)

    def test_wrapped_encrypted_items_keep_metadata_aligned(self):
        original = {"type": "compacted", "payload": {"replacement_history": [
            {"item": {"type": "reasoning", "encrypted_content": "cipher"}},
            {"item": {"type": "message", "role": "user", "content": "Keep"}}],
            "replacement_history_metadata": [{"position": 0}, {"position": 1}]}}
        cleaned, _ = transfer.portable_record(original)
        self.assertEqual(cleaned["payload"]["replacement_history_metadata"], [{"position": 1}])

    def test_inconsistent_compaction_metadata_is_not_guessed(self):
        damaged = {"type": "compacted", "payload": {"replacement_history": [
            {"type": "message", "role": "user", "content": "Keep"}],
            "replacement_history_metadata": [{"position": 0}, {"position": 1}]}}
        with self.assertRaisesRegex(transfer.TransferError, "lengths differ"):
            transfer.portable_record(damaged)

    def test_application_history_fields_are_not_compaction_metadata(self):
        record = {"type": "response_item", "payload": {"type": "function_call_output",
            "call_id": "app", "output": {"replacement_history": [{"type": "entry"}],
            "replacement_history_metadata": [{"index": 0}, {"index": 1}]}}}
        cleaned, removed = transfer.portable_record(record)
        self.assertEqual(cleaned, record)
        self.assertEqual(removed, 0)

    def test_multiple_and_archived_sessions_without_index(self):
        sid = str(uuid.uuid4())
        self.make_session(sid, archived=True)
        self.run_cli("export", self.sid, sid, "--to", self.bundle)
        self.run_cli("import", self.bundle, "--cwd", self.cwd)
        self.assertEqual(len(self.imported()), 2)

    def test_reimport_creates_new_uuids(self):
        self.export()
        self.run_cli("import", self.bundle)
        self.run_cli("import", self.bundle)
        self.assertEqual(len(self.imported()), 2)

    def test_import_does_not_print_or_store_terminal_control_sequences(self):
        self.export()
        self.alter(lambda m, c: m["sessions"][0].update(title="\x1b]0;bad\x07Title\nline"))
        result = self.run_cli("import", self.bundle)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("\x07", result.stdout)
        index = (self.home / "session_index.jsonl").read_text()
        self.assertNotIn("\x1b", json.loads(index)["thread_name"])

    def test_list_validates_without_installing(self):
        self.export()
        result = self.run_cli("import", self.bundle, "--list")
        self.assertIn("no sessions installed", result.stdout)
        self.assertEqual(self.imported(), [])

    def test_checksum_failure_does_not_install(self):
        self.export()
        self.alter(lambda m, c: m["sessions"][0].update(sha256="0" * 64))
        self.assertIn("Checksum mismatch", self.run_cli("import", self.bundle, ok=False).stderr)
        self.assertEqual(self.imported(), [])

    def test_rejects_traversal_member(self):
        self.export()
        self.alter(lambda m, c: None, extra=("../escape", b"bad"))
        self.run_cli("import", self.bundle, ok=False)
        self.assertFalse((self.root / "escape").exists())
        self.assertEqual(self.imported(), [])

    def test_rejects_duplicate_zip_member(self):
        self.export()
        with zipfile.ZipFile(self.bundle, "a") as archive:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                archive.writestr("manifest.json", "{}")
        self.assertIn("Duplicate ZIP", self.run_cli("import", self.bundle, ok=False).stderr)

    def test_rejects_unknown_format(self):
        self.export()
        self.alter(lambda m, c: m.update(version=999))
        self.run_cli("import", self.bundle, ok=False)
        self.assertEqual(self.imported(), [])

    def test_metadata_only_source_rejected(self):
        self.source.write_text(json.dumps(self.records(self.sid)[0]) + "\n")
        self.assertIn("no local conversation history", self.run_cli("export", self.sid, "--to", self.bundle, ok=False).stderr)
        self.assertFalse(self.bundle.exists())

    def test_partial_append_ignored_but_middle_corruption_rejected(self):
        with self.source.open("a") as stream:
            stream.write('{"partial":')
        self.export()
        other = self.root / "bad.zip"
        with self.source.open("a") as stream:
            stream.write("\n{}\n")
        self.run_cli("export", self.sid, "--to", other, ok=False)
        self.assertFalse(other.exists())

    def test_export_refuses_overwrite(self):
        self.export()
        before = self.bundle.read_bytes()
        self.run_cli("export", self.sid, "--to", self.bundle, ok=False)
        self.assertEqual(self.bundle.read_bytes(), before)

    def test_multiple_metadata_rows_rebound(self):
        with self.source.open("a") as stream:
            stream.write(json.dumps(self.records(str(uuid.uuid4()))[0]) + "\n")
        self.export()
        self.run_cli("import", self.bundle)
        target, = self.imported()
        metas = [json.loads(l)["payload"] for l in target.read_text().splitlines()
                 if json.loads(l)["type"] == "session_meta"]
        self.assertEqual(metas[0]["id"], metas[1]["id"])
        self.assertEqual(metas[0]["session_id"], metas[1]["session_id"])

    def test_noninteractive_export_requires_selection(self):
        self.run_cli("export", "--to", self.bundle, ok=False)
        self.assertFalse(self.bundle.exists())

    def test_multiple_open_rejected_before_installing(self):
        sid = str(uuid.uuid4())
        self.make_session(sid)
        self.run_cli("export", self.sid, sid, "--to", self.bundle)
        self.run_cli("import", self.bundle, "--open", ok=False)
        self.assertEqual(len(self.imported()), 1)  # the second source, no imports

    def test_missing_cwd_rejected(self):
        self.export()
        self.run_cli("import", self.bundle, "--cwd", self.root / "missing", ok=False)
        self.assertEqual(self.imported(), [])

    def test_nested_reasoning_and_compaction_cleanup(self):
        original = {"type": "compacted", "payload": {"message": "readable summary",
                    "encrypted_content": "cipher", "replacement_history": [
                        {"type": "reasoning", "encrypted_content": "cipher"},
                        {"type": "message", "role": "user", "content": []}]}}
        cleaned, count = transfer.portable_record(original)
        self.assertEqual(count, 2)
        self.assertEqual(cleaned["payload"]["message"], "readable summary")
        self.assertEqual(len(cleaned["payload"]["replacement_history"]), 1)

    def test_tool_data_named_encrypted_content_is_not_redacted(self):
        record = {"type": "response_item", "payload": {"type": "function_call_output",
                  "output": {"encrypted_content": "user application data"}}}
        cleaned, removed = transfer.portable_record(record)
        self.assertEqual(cleaned, record)
        self.assertEqual(removed, 0)

    def test_opaque_compaction_not_replayed_with_missing_required_field(self):
        item = {"type": "compaction", "encrypted_content": "cipher"}
        self.assertEqual(transfer.portable_record({"type": "response_item", "payload": item}), (None, 1))
        checkpoint = {"type": "compacted", "payload": {"replacement_history": [{"item": item}]}}
        self.assertEqual(transfer.portable_record(checkpoint), (None, 1))

    def test_open_uses_new_uuid_local_cwd_and_no_shell(self):
        self.export()
        args = SimpleNamespace(bundle=self.bundle, cwd=self.cwd, list=False, open=True,
                               name="Imported title", db=self.home / "test.sqlite",
                               open_args=["--model", "example-model"])
        with patch.object(csl, "DEFAULT_CODEX_HOME", self.home), \
             patch.object(csl, "DEFAULT_SESSIONS_DIR", self.home / "sessions"), \
             patch.object(csl, "DEFAULT_SESSION_INDEX", self.home / "session_index.jsonl"), \
             patch.object(transfer.shutil, "which", return_value="/bin/codex"), \
             patch.object(transfer.subprocess, "call", return_value=0) as launch, \
             patch("sys.stdout", new_callable=io.StringIO), \
             self.assertRaises(SystemExit) as result:
            transfer.import_bundle(args, csl)
        self.assertEqual(result.exception.code, 0)
        argv, = launch.call_args.args
        self.assertEqual(argv[:5], ["codex", "-C", str(self.cwd), "resume", "--all"])
        self.assertEqual(argv[5:7], ["--model", "example-model"])
        self.assertNotEqual(argv[-1], self.sid)
        self.assertFalse(launch.call_args.kwargs.get("shell", False))

    def test_clean_team_install_launches_from_symlink(self):
        prefix = self.root / "fresh prefix"
        installed = subprocess.run([str(REPO / "install.sh"), str(prefix)],
                                   text=True, capture_output=True, timeout=10)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        result = subprocess.run([str(prefix / "bin/csl"), "import", "--help"],
                                cwd=self.cwd, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--cwd", result.stdout)
        self.assertFalse((prefix / "bin/codex_sessions.py").exists())
        exported = subprocess.run([str(prefix / "bin/csl"), "export", self.sid, "--to", str(self.bundle)],
                                  cwd=self.cwd, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertTrue(self.bundle.exists())

    def test_symlink_member_rejected(self):
        self.export()
        link = zipfile.ZipInfo("extra")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        with zipfile.ZipFile(self.bundle, "a") as archive:
            archive.writestr(link, "../../target")
        self.run_cli("import", self.bundle, ok=False)
        self.assertEqual(self.imported(), [])

    def test_all_members_validated_before_any_install(self):
        sid = str(uuid.uuid4())
        self.make_session(sid, archived=True)
        self.run_cli("export", self.sid, sid, "--to", self.bundle)
        self.alter(lambda m, c: m["sessions"][1].update(sha256="0" * 64))
        self.run_cli("import", self.bundle, ok=False)
        self.assertEqual(self.imported(), [])

    def test_ambiguous_prefix_rejected(self):
        other = self.sid[:8] + str(uuid.uuid4())[8:]
        self.make_session(other)
        self.run_cli("export", self.sid[:8], "--to", self.bundle, ok=False)
        self.assertFalse(self.bundle.exists())

    def test_manifest_size_limit(self):
        archive_file = io.BytesIO()
        with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", " " * (transfer.MAX_MANIFEST + 1))
        archive_file.seek(0)
        with zipfile.ZipFile(archive_file) as archive, self.assertRaises(transfer.TransferError):
            transfer.inspect_bundle(archive)


if __name__ == "__main__":
    unittest.main()
