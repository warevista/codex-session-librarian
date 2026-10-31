"""Select effective native history without resurrecting discarded conversation."""

from collections import Counter


def codex_effective(records, policy="active"):
    if policy not in ("active", "visible"):
        raise ValueError("Unknown history policy")
    active, report = [], Counter()
    compacted = False
    for record in records:
        kind, payload = record.get("type"), record.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Native payload must be an object")
        if policy == "active" and kind == "session_meta" and payload.get("csl_portability", {}).get("compaction_state_removed"):
            raise ValueError("Portable export removed encrypted compaction state; active context cannot be restored exactly")
        if policy == "visible" and kind == "response_item" and payload.get("type") == "compaction":
            report["opaque_state_not_restored"] += 1
            continue
        if kind == "compacted":
            replacement = payload.get("replacement_history")
            metadata = payload.get("replacement_history_metadata")
            if isinstance(replacement, list) and isinstance(metadata, list) and metadata and len(replacement) != len(metadata):
                raise ValueError("Compaction history/metadata lengths differ; re-export the original with updated CSL")
            if policy == "active" and record.get("csl_portable_compaction", {}).get("opaque_summary_removed"):
                raise ValueError("Encrypted compaction summary was removed; exact active context is unavailable. Use explicit visible-history recovery instead")
            if policy == "visible" and replacement is None and isinstance(payload.get("message"), str) and payload["message"].strip():
                active.append({"type": "response_item", "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "[Recovered legacy compaction summary; active native context not reconstructed]\n" + payload["message"]}]}})
                report["legacy_summary_recovered"] += 1
                continue
            if not isinstance(replacement, list) or not replacement:
                raise ValueError("Compaction needs a nonempty replacement_history checkpoint")
            replacement = [item["item"] if isinstance(item, dict) and isinstance(item.get("item"), dict) else item for item in replacement]
            if any(not isinstance(item, dict) or "type" not in item for item in replacement):
                raise ValueError("Malformed compaction checkpoint")
            if policy == "visible" and any(r.get("type") == "response_item" and r.get("payload", {}).get("type") == "message" for r in active):
                report["checkpoint_not_used_visible_history"] += 1
                continue
            if policy == "visible":
                replacement = [r for r in replacement if r.get("type") != "compaction"]
                report["visible_prefix_only_checkpoint"] += 1
            active = [{"type": "response_item", "timestamp": record.get("timestamp"), "payload": item}
                      for item in replacement]
            report["applied_compaction_checkpoints"] += 1
            compacted = policy == "active"
        elif kind == "event_msg" and payload.get("type") == "thread_rolled_back":
            if compacted:
                raise ValueError("Rollback crossing compaction requires native window reconstruction")
            count = payload.get("num_turns")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("Invalid rollback turn count")
            boundaries = []
            for index, item in enumerate(active):
                p = item.get("payload", {})
                if item.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                    parts = p.get("content", [])
                    text = parts if isinstance(parts, str) else "\n".join(b.get("text", "") for b in parts if isinstance(b, dict))
                    if not text.startswith(("<environment_context>", "# AGENTS.md instructions")):
                        boundaries.append(index)
            if count > len(boundaries):
                raise ValueError("Rollback exceeds known user boundaries")
            if count:
                active = active[:boundaries[-count]]
            report["applied_rollback_turns"] += count
        else:
            active.append(record)
    return active, report

