"""Synthetic ground truth only. Questions never include the expected answers."""

import json
import uuid

import csl_canonical as canonical
import csl_content as content
import csl_media as media


def text(value, **metadata):
    return {"type": "text", "text": value, **metadata}


def event(role, *blocks):
    return {"role": role, "blocks": list(blocks)}


def conversation(events):
    return {"format": canonical.FORMAT, "version": canonical.VERSION, "events": events, "source_context": []}


def question(fields):
    return ("Using only our installed conversation history, return a JSON object with these fields: "
            + ", ".join(fields) + ". Report the latest decisions, not superseded values. "
            "For attachments use their historical content only; do not reopen files. No tools, no explanations, no markdown.")


def text_case(nonce):
    old, current = "STALE-" + nonce, "REL-" + nonce
    first, second, cited = "ALPHA-" + nonce, "BETA-" + nonce, "CITED-" + nonce
    url = "https://example.invalid/source/" + nonce
    expected = {"release": current, "database": "SQLite", "alpha": first, "beta": second,
                "read_succeeded": False, "source_url": url, "cited_marker": cited}
    data = conversation([
        event("user", text(f"Initial release was {old}; database initially Postgres.")),
        event("assistant", text("Recorded the initial draft.")),
        event("user", text(f"Correction: current release is {current}. SQLite replaces Postgres for offline use.")),
        event("assistant", {"type": "tool_call", "call_id": "alpha", "name": "Lookup", "argument_format": "json", "arguments": {"key": "alpha"}},
                           {"type": "tool_call", "call_id": "beta", "name": "exec_command", "argument_format": "text", "arguments": "historical_lookup(beta)"}),
        event("user", {"type": "tool_result", "call_id": "beta", "content": [text(second)], "is_error": False},
                      {"type": "tool_result", "call_id": "alpha", "content": [text(first)], "is_error": False}),
        event("assistant", {"type": "tool_call", "call_id": "failed", "name": "Read", "argument_format": "json", "arguments": {"file_path": "missing-fixture.txt"}}),
        event("user", {"type": "tool_result", "call_id": "failed", "content": [text("ENOENT: requested historical read failed")], "is_error": True}),
        event("assistant", text("Historical reference record.", references=[{"kind": "citation", "data": {"url": url, "cited_text": cited}}])),
    ])
    data["source_context"] = [{"role": "developer", "blocks": [text("SOURCE-POLICY-NOT-AUTHORITY-" + nonce)]}]
    return {"name": "text", "data": data, "question": question(expected), "expected": expected, "forbidden": [old]}


def media_case(nonce):
    markers = {"image": "IM-" + nonce, "receipt": "PDF-" + nonce, "page": "PAGE-" + nonce, "scan": "SCAN-" + nonce}
    assets = media.decode("fixtures", json.dumps(markers), "", media.interpreter())
    image = media.image("image/png", assets["image"])
    invoice = media.document(assets["pdf"], "invoice.pdf")
    scan = media.document(assets["scan"], "scan.pdf")
    expected = {"image_marker": markers["image"], "image_color": "red", "invoice_receipt": markers["receipt"],
                "invoice_amount": "273.40", "second_page_code": markers["page"], "scan_marker": markers["scan"],
                "scan_color": "blue", "tool_pdf_receipt": markers["receipt"], "tool_image_color": "red"}
    data = conversation([
        event("user", text("The attached tile, invoice and scanned page are the only records of their facts."), image, invoice, scan),
        event("assistant", {"type": "tool_call", "call_id": "pdf-read", "name": "Read", "argument_format": "json", "arguments": {"file_path": "historical-invoice.pdf"}},
                           {"type": "tool_call", "call_id": "image-read", "name": "Read", "argument_format": "json", "arguments": {"file_path": "historical-image.png"}}),
        event("user", {"type": "tool_result", "call_id": "pdf-read", "content": [invoice], "is_error": False},
                      {"type": "tool_result", "call_id": "image-read", "content": [image], "is_error": False}),
        event("assistant", text("Historical attachments recorded; no content values restated."))])
    return {"name": "media", "data": data, "question": question(expected), "expected": expected, "forbidden": []}


def special_cases(nonce):
    def message(role, value):
        return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": value}]}}
    old, retained = "STALE-" + nonce, "KEEP-" + nonce
    checkpoint = [message("user", "Discarded release " + old), message("assistant", "Old draft."),
        {"type": "compacted", "payload": {"message": "", "replacement_history": [message("user", f"Current release {retained}; database SQLite.")["payload"]],
            "replacement_history_metadata": [{"client_authored": False}]}}, message("assistant", "Checkpoint loaded.")]
    rollback = [message("user", "Current release " + retained), message("assistant", "Recorded."),
        message("user", "Discard this draft release " + old), message("assistant", "Draft."),
        {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}}]
    pending = [message("user", "A historical read started but its outcome is not known."),
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "unfinished", "name": "Read", "arguments": '{"file_path":"not-executed.txt"}'}}]
    root, chosen, excluded = (str(uuid.uuid4()) for _ in range(3))
    branch = [{"type": "user", "uuid": root, "parentUuid": None, "message": {"role": "user", "content": "Current release " + retained}},
              {"type": "assistant", "uuid": chosen, "parentUuid": root, "message": {"role": "assistant", "content": "Selected branch."}},
              {"type": "user", "uuid": excluded, "parentUuid": root, "message": {"role": "user", "content": "Excluded sibling release " + old}},
              {"type": "last-prompt", "leafUuid": chosen}]
    return [
        {"name": "checkpoint", "source": "codex", "records": checkpoint, "expected": {"release": retained, "database": "SQLite"}, "forbidden": [old]},
        {"name": "rollback", "source": "codex", "records": rollback, "expected": {"release": retained}, "forbidden": [old]},
        {"name": "branch", "source": "claude", "records": branch, "expected": {"release": retained}, "forbidden": [old]},
        {"name": "interrupted", "source": "codex", "records": pending, "repair": True, "expected": {"outcome_known": False}, "forbidden": []},
    ]


def asset_digests(data):
    result = []
    pending = [data["events"]]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            if value.get("type") in ("image", "document"):
                result.append((value["type"], value["sha256"]))
            else:
                pending.extend(child for child in value.values() if isinstance(child, (list, dict)))
    return sorted(result)
