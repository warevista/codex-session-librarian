import copy
import json
from pathlib import Path
import tempfile
import unittest

import csl_display as display
from csl_transfer import json_line, read_records


def completed(kind, text, item_id="item-1", thread="source", turn="turn-1"):
    return {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg", "ordinal": 10,
            "payload": {"type": "item_completed", "thread_id": thread, "turn_id": turn,
                        "item": {"type": kind, "id": item_id,
                                 "content": [{"type": "Text" if kind == "AgentMessage" else "text", "text": text}]}}}


class DisplayTests(unittest.TestCase):
    def project(self, records):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "source", Path(temp) / "target"
            source.write_bytes(b"".join(json_line(r) for r in records))
            original = source.read_bytes()
            stats = display.project_file(source, target, read_records, json_line, {"source", "imported"})
            with target.open("rb") as stream:
                result = list(read_records(stream))
            self.assertEqual(source.read_bytes(), original)
            return stats, result

    def test_modern_messages_get_visible_events_without_changing_model_items(self):
        model = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Answer"}]}}
        records = [completed("UserMessage", "Question", "u"), model, completed("AgentMessage", "Answer", "a")]
        stats, projected = self.project(records)
        self.assertEqual(stats, {"user_message": 1, "agent_message": 1})
        self.assertEqual([r for r in projected if r.get("type") == "response_item"], [model])
        self.assertEqual([r for r in projected if "csl_display_projection" not in r], records)

    def test_existing_legacy_message_is_not_duplicated(self):
        modern = completed("AgentMessage", "Answer")
        legacy = {"type": "event_msg", "payload": {"type": "agent_message", "message": "Answer"}}
        start = {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}}
        for records in ([start, modern, legacy], [start, legacy, modern]):
            stats, _ = self.project(records)
            self.assertEqual(stats, {})

    def test_duplicate_snapshot_uses_latest_content(self):
        stats, projected = self.project([completed("AgentMessage", "Old"), completed("AgentMessage", "Updated")])
        self.assertEqual(stats, {"agent_message": 1})
        self.assertEqual([r["payload"]["message"] for r in projected if "csl_display_projection" in r], ["Updated"])

    def test_identical_messages_with_different_ids_are_kept(self):
        stats, _ = self.project([completed("UserMessage", "OK", "one"), completed("UserMessage", "OK", "two")])
        self.assertEqual(stats, {"user_message": 2})

    def test_other_agent_thread_is_not_promoted_to_root_conversation(self):
        stats, projected = self.project([completed("AgentMessage", "Worker message", thread="worker")])
        self.assertEqual(stats, {})
        self.assertEqual(len(projected), 1)

    def test_unicode_multiblock_and_phase(self):
        modern = completed("AgentMessage", "Espa\u00f1ol")
        modern["payload"]["item"].update(phase="final_answer")
        modern["payload"]["item"]["content"].append({"type": "Text", "text": "\u4e2d\u6587"})
        legacy = display.message_event(modern, {"source"})
        self.assertEqual(legacy["message"], "Espa\u00f1ol\n\u4e2d\u6587")
        self.assertEqual(legacy["phase"], "final_answer")

    def test_inline_image_is_preserved_external_references_not_fetched(self):
        modern = completed("UserMessage", "Images")
        modern["payload"]["item"]["content"].extend([
            {"type": "image", "image": {"url": "data:image/png;base64,fixture"}},
            {"type": "image", "url": "https://example.com/image"},
            {"type": "localImage", "path": "/sender/private.png"}])
        legacy = display.message_event(modern, {"source"})
        self.assertEqual(legacy["images"], ["data:image/png;base64,fixture"])
        self.assertEqual(legacy["local_images"], [])
        self.assertIn("not fetched", legacy["message"])

    def test_empty_and_unrelated_items_add_nothing(self):
        for modern in (completed("AgentMessage", ""), completed("Reasoning", "not a chat message")):
            self.assertIsNone(display.message_event(modern, {"source"}))

    def test_malformed_content_is_rejected(self):
        for content in (None, [1], [{"type": "Text", "text": None}]):
            modern = completed("AgentMessage", "Answer")
            modern["payload"]["item"]["content"] = content
            with self.assertRaises(ValueError):
                self.project([modern])
