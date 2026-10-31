import copy
import json
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import csl_canonical as bridge


def sample():
    return {"format": bridge.FORMAT, "version": bridge.VERSION, "source_context": [], "events": [
        {"role": "user", "blocks": [{"type": "text", "text": "Read fixture"}]},
        {"role": "assistant", "blocks": [{"type": "tool_call", "call_id": "call-1",
         "name": "Read", "arguments": {"file_path": "fixture.txt"}, "argument_format": "json"}]},
        {"role": "user", "blocks": [{"type": "tool_result", "call_id": "call-1",
         "content": [{"type": "text", "text": "first line\nsecond line"}], "is_error": False}]},
        {"role": "assistant", "blocks": [{"type": "text", "text": "Done"}]}]}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def roundtrip(self, data):
        for provider, writer in (("claude", bridge.restore_claude), ("codex", bridge.restore_codex)):
            path = self.root / f"{provider}.jsonl"
            writer(data, path, self.root)
            actual = bridge.normalize(provider, path)
            self.assertEqual(bridge.semantic_events(data), bridge.semantic_events(actual))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_json_tool_roundtrip(self):
        self.roundtrip(sample())

    def test_custom_tool_roundtrip(self):
        data = sample()
        data["events"][1]["blocks"][0].update(argument_format="text", arguments="run({value: 1})")
        self.roundtrip(data)
        records = bridge.read_jsonl(self.root / "codex.jsonl")
        self.assertIn("custom_tool_call_output", [r.get("payload", {}).get("type") for r in records])

    def test_error_result_roundtrip(self):
        data = sample()
        data["events"][2]["blocks"][0]["is_error"] = True
        self.roundtrip(data)

    def test_policies_not_replayed(self):
        data = sample()
        data["source_context"] = [{"role": "developer", "blocks": [{"type": "text", "text": "UNTRUSTED_POLICY"}]}]
        for provider, writer in (("claude", bridge.restore_claude), ("codex", bridge.restore_codex)):
            path = self.root / f"{provider}.jsonl"
            info = writer(data, path, self.root)
            self.assertEqual(info["quarantined_source_context"], 1)
            self.assertNotIn("UNTRUSTED_POLICY", path.read_text())

    def test_no_overwrite(self):
        path = self.root / "history.jsonl"
        bridge.restore_claude(sample(), path, self.root)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            bridge.restore_claude(sample(), path, self.root)
        self.assertEqual(before, path.read_bytes())

    def test_orphan_result(self):
        data = sample()
        del data["events"][1]
        with self.assertRaisesRegex(ValueError, "Orphan"):
            bridge.validate(data)

    def test_unfinished_call(self):
        data = sample()
        del data["events"][2]
        with self.assertRaisesRegex(ValueError, "Unfinished"):
            bridge.validate(data)

    def test_bad_arguments(self):
        data = sample()
        data["events"][1]["blocks"][0]["arguments"] = "not an object"
        with self.assertRaisesRegex(ValueError, "arguments"):
            bridge.validate(data)

    def test_unknown_block(self):
        data = sample()
        data["events"][0]["blocks"] = [{"type": "unknown_future_block", "url": "unknown"}]
        with self.assertRaisesRegex(ValueError, "Unknown"):
            bridge.validate(data)

    def test_schema_identity_matches_runtime(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "docs/canonical-v2.schema.json").read_text())
        self.assertEqual(schema["properties"]["format"]["const"], bridge.FORMAT)
        self.assertEqual(schema["properties"]["version"]["const"], bridge.VERSION)
        self.assertNotIn("image", schema["$defs"])

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        for value in ('{"type":"user","type":"assistant"}', '{"value":NaN}', '{"value":Infinity}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                bridge.strict_json(value)

    def test_future_fields_fail_closed(self):
        data = sample()
        data["events"][0]["unknown_future_authority"] = True
        with self.assertRaisesRegex(ValueError, "canonical event fields"):
            bridge.validate(data)

    def test_reference_roundtrip_and_stale_projection_rejected(self):
        data = sample()
        data["events"][0]["blocks"][0]["references"] = [{"kind": "citation", "data": {
            "url": "https://invalid.test/not-fetched", "cited_text": "MARKER-617"}}]
        self.roundtrip(data)
        path = self.root / "claude.jsonl"
        records = bridge.read_jsonl(path)
        records[0]["message"]["content"][0]["text"] += "edited after conversion"
        altered = self.root / "altered.jsonl"
        bridge.write_jsonl(altered, records)
        with self.assertRaisesRegex(ValueError, "projection changed"):
            bridge.from_claude(altered)

    def claude_graph(self):
        def node(key, parent, text):
            return {"type": "user", "uuid": key, "parentUuid": parent,
                    "message": {"role": "user", "content": text}}
        # Deliberately out of physical order; only graph order is authoritative.
        return [node("b", "a", "selected"), node("a", None, "root"),
                node("c", "a", "excluded sibling"), {"type": "last-prompt", "leafUuid": "b"}]

    def graph_file(self, records):
        path = self.root / "graph.jsonl"
        bridge.write_jsonl(path, records)
        return path

    def test_branch_and_parent_order(self):
        result = bridge.from_claude(self.graph_file(self.claude_graph()))
        self.assertEqual([e["blocks"][0]["text"] for e in result["events"]], ["root", "selected"])
        self.assertEqual(result["report"]["ignored_native_state"]["off_branch_records"], 1)

    def test_explicit_branch(self):
        result = bridge.from_claude(self.graph_file(self.claude_graph()), "c")
        self.assertEqual(result["events"][-1]["blocks"][0]["text"], "excluded sibling")

    def test_ambiguous_branch(self):
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            bridge.from_claude(self.graph_file(self.claude_graph()[:-1]))

    def test_missing_parent(self):
        records = self.claude_graph()
        records[1]["parentUuid"] = "missing"
        with self.assertRaisesRegex(ValueError, "Missing"):
            bridge.from_claude(self.graph_file(records))

    def test_cycle(self):
        records = self.claude_graph()
        records[1]["parentUuid"] = "b"
        with self.assertRaisesRegex(ValueError, "Cycle"):
            bridge.from_claude(self.graph_file(records))

    def test_compaction_rejected(self):
        path = self.graph_file([{"type": "compacted", "payload": {}}])
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            bridge.from_codex(path)

    def test_claude_compaction_rejected(self):
        path = self.graph_file([{"type": "system", "subtype": "compact_boundary"}])
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            bridge.from_claude(path)

    def test_duplicate_tool_call(self):
        data = sample()
        data["events"].insert(2, copy.deepcopy(data["events"][1]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            bridge.validate(data)


if __name__ == "__main__":
    unittest.main()

