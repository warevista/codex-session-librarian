"""Canonical conversation adapters, promoted from the isolated portability lab."""

from collections import Counter
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import csl_content as codec
from csl_history import codex_effective

FORMAT = "csl-conversation"
VERSION = 2
CLAUDE_VERSION = "2.1.308"


def now():
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def read_jsonl(path):
    records = []
    document_count = 0
    if Path(path).stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Transcript exceeds 64 MiB limit")

    with Path(path).open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    value = strict_json(line)
                except ValueError as exc:
                    raise ValueError(f"Invalid JSONL at line {number}: malformed/duplicate/non-finite JSON") from exc
                if not isinstance(value, dict):
                    raise ValueError("Native records must be objects")
                pending = [value]
                while pending:
                    item = pending.pop()
                    if isinstance(item, dict):
                        document_count += item.get("type") in ("document", "input_file")
                        pending.extend(item.values())
                    elif isinstance(item, list):
                        pending.extend(item)
                if document_count > 32:
                    raise ValueError("Transcript exceeds embedded-document limit")
                records.append(value)
    return records


def text_blocks(content):
    return codec.blocks(content)


def tool_output(content):
    if isinstance(content, (str, list)):
        return text_blocks(content)
    raise ValueError("Unsupported tool result shape")


def canonical(provider, path, events, source_context, ignored, leaf=None, repair_interrupted=False):
    data = {"format": FORMAT, "version": VERSION,
            "source": {"provider": provider, "path": str(Path(path).resolve()),
                       "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(), "leaf": leaf},
            "events": events, "source_context": source_context,
            "report": {"ignored_native_state": dict(ignored),
                       "source_policy_replayed_as_authority": False}}
    if repair_interrupted:
        calls = {b["call_id"] for e in events for b in e["blocks"] if b["type"] == "tool_call"}
        results = {b["call_id"] for e in events for b in e["blocks"] if b["type"] == "tool_result"}
        missing = sorted(calls - results)
        if missing:
            events.append({"role": "user", "blocks": [{"type": "tool_result", "call_id": key,
                "content": [{"type": "text", "text": "IMPORT NOTICE: Historical tool execution was interrupted. Outcome UNKNOWN. Import did not execute or retry the tool."}],
                "is_error": True} for key in missing]})
            data["report"]["synthetic_interruption_results"] = missing
    validate(data)
    stats = content_stats(events)
    data["report"]["portable_content"] = stats
    if stats["possible_projection_without_original"]:
        data["report"]["warnings"] = ["Possible document/reference projection without its original portable metadata; only the visible text/images can be recovered."]
    return data


def content_stats(events):
    result = {"pdf_originals": 0, "referenced_text_blocks": 0, "possible_projection_without_original": 0}
    for event in events:
        pending = list(event["blocks"])
        while pending:
            block = pending.pop()
            if block["type"] == "tool_result":
                pending.extend(block["content"])
            elif block["type"] == "document":
                result["pdf_originals"] += 1
            elif block["type"] == "text":
                result["referenced_text_blocks"] += bool(block.get("references"))
                if not block.get("references") and (block["text"].startswith("[PDF projection:") or
                        "\n[Historical references; source locations not revalidated]" in block["text"]):
                    result["possible_projection_without_original"] += 1
    return result


def from_codex(path, repair_interrupted=False, history_policy="active"):
    events, context, ignored = [], [], Counter()
    original = read_jsonl(path)
    missing_parent = any(r.get("type") == "session_meta" and r.get("payload", {}).get("history_base") for r in original)
    if missing_parent and history_policy == "active" and not any(r.get("type") == "compacted" for r in original):
        raise ValueError("Fork parent history is absent; include ancestry or explicitly select visible-history recovery")
    records, effective_report = codex_effective(original, history_policy)
    ignored.update(effective_report)
    for index, record in enumerate(records):
        kind, payload = record.get("type"), record.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Native payload must be an object")
        transport = record.get("portability_content", {})
        if not isinstance(transport, dict):
            raise ValueError("Invalid portable content metadata")
        if kind != "response_item":
            if kind not in ("session_meta", "event_msg", "turn_context", "world_state", "token_usage_record",
                            "inter_agent_communication_metadata", "retained_context"):
                raise ValueError("Unknown native record; explicit adapter required")
            ignored[kind] += 1
            continue
        ptype = payload.get("type")
        event = {"id": payload.get("id") or f"codex-{index}", "time": record.get("timestamp"),
                 "blocks": [], "role": "assistant"}
        if ptype == "message":
            role = payload["role"]
            blocks = text_blocks(codec.restore_spans(payload.get("content", []), transport.get("message")))
            joined = "\n".join(b["text"] for b in blocks if b["type"] == "text")
            if role in ("system", "developer") or (role == "user" and joined.startswith(
                    ("<environment_context>", "# AGENTS.md instructions"))):
                context.append({"role": role, "blocks": blocks})
                continue
            event.update(role=role, blocks=blocks, phase=payload.get("phase"))
        elif ptype in ("function_call", "custom_tool_call"):
            raw = payload.get("arguments") if ptype == "function_call" else payload["input"]
            if ptype == "function_call":
                arguments = strict_json(raw) if isinstance(raw, str) else raw
                if not isinstance(arguments, dict):
                    raise ValueError("Function arguments must be an object")
            else:
                arguments = raw
            event["blocks"] = [{"type": "tool_call", "call_id": payload["call_id"],
                                "name": payload["name"], "arguments": arguments,
                                "argument_format": "json" if ptype == "function_call" else "text"}]
        elif ptype in ("function_call_output", "custom_tool_call_output"):
            if not isinstance(payload.get("csl_is_error", False), bool):
                raise ValueError("Native tool error flag must be boolean")
            event["role"] = "user"
            event["blocks"] = [{"type": "tool_result", "call_id": payload["call_id"],
                                "content": tool_output(codec.restore_spans(payload["output"], transport.get("tool_result"))),
                                "is_error": bool(payload.get("csl_is_error", False))}]
        elif ptype in ("web_search_call", "tool_search_call", "tool_search_output"):
            if payload.get("status", "completed") not in ("completed", "failed"):
                raise ValueError("Incomplete provider action requires explicit handling")
            event["blocks"] = [{"type": "text", "text": "[Historical provider action; not executed by import]\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)}]
            ignored["rendered_provider_actions"] += 1
        elif ptype == "agent_message":
            visible = [b for b in payload.get("content", []) if b.get("type") != "encrypted_content"]
            ignored["omitted_encrypted_agent_blocks"] += len(payload.get("content", [])) - len(visible)
            event["blocks"] = [{"type": "text", "text": "[Historical inter-agent message; no agent spawned]\n" +
                               json.dumps({"author": payload.get("author"), "recipient": payload.get("recipient")})}] + text_blocks(visible)
        elif ptype == "compaction":
            raise ValueError("Opaque compaction cannot be reconstructed without readable history")
        elif ptype in ("reasoning", "additional_tools", "configuration_update", "ghost_snapshot"):
            ignored[ptype] += 1
            continue
        else:
            raise ValueError("Unimplemented Codex response item; explicit adapter required")
        events.append(event)
    data = canonical("codex", path, events, context, ignored, repair_interrupted=repair_interrupted)
    data["source"]["session_id"] = next((r.get("payload", {}).get("id") for r in original if r.get("type") == "session_meta"), None)
    data["report"]["history_policy"] = history_policy
    if history_policy == "visible":
        data["report"]["warning"] = "VISIBLE HISTORY RECOVERY: not an exact reconstruction of the native compacted model context; opaque state is not restored."
        data["events"].insert(0, {"role": "user", "blocks": [{"type": "text", "text": "[Import provenance notice: visible-history recovery, not exact native context restoration. Encrypted compaction state was not restored. Only the readable history available in the source was recovered.]"}]})
    return data


def claude_chain(records, leaf=None):
    nodes = {}
    for record in records:
        if record.get("uuid"):
            key = record["uuid"]
            if key in nodes and nodes[key] != record:
                raise ValueError("Conflicting duplicate Claude UUID")
            nodes[key] = record
    if not nodes and any(r.get("subtype") == "compact_boundary" for r in records):
        raise ValueError("Compacted Claude history needs a readable summary checkpoint")
    if leaf is None:
        leaf = next((r.get("leafUuid") for r in reversed(records) if r.get("type") == "last-prompt"), None)
        if leaf is not None and leaf not in nodes:
            raise ValueError("Latest Claude leaf pointer is missing; choose an explicit leaf")
        if leaf in nodes and nodes[leaf].get("isSidechain"):
            raise ValueError("Sidechain selection requires an explicit leaf")
    if leaf is None:
        parents = {r.get("parentUuid") for r in nodes.values()}
        leaves = [key for key, r in nodes.items() if key not in parents and not r.get("isSidechain")
                  and r.get("type") in ("user", "assistant")]
        if len(leaves) != 1:
            raise ValueError("Ambiguous Claude branches; select an explicit --leaf UUID")
        leaf = leaves[0]
    if leaf not in nodes:
        raise ValueError("Selected Claude leaf is missing")
    chain, seen, cursor = [], set(), leaf
    while cursor:
        if cursor in seen:
            raise ValueError("Cycle in Claude parent graph")
        if cursor not in nodes:
            raise ValueError("Missing Claude parent in selected branch")
        seen.add(cursor)
        node = nodes[cursor]
        chain.append(node)
        if node.get("isCompactSummary"):
            break
        cursor = node.get("parentUuid")
    return list(reversed(chain)), seen, leaf


def from_claude(path, leaf=None, repair_interrupted=False):
    records = read_jsonl(path)
    chain, seen, leaf = claude_chain(records, leaf)
    events, context, ignored = [], [], Counter()
    for record in chain:
        if record.get("type") not in ("user", "assistant"):
            if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
                raise ValueError("Compacted Claude history needs a readable summary checkpoint")
            if record.get("type") == "attachment":
                atype = record.get("attachment", {}).get("type")
                if atype not in ("date", "session_context"):
                    raise ValueError("Claude attachment requires an explicit asset adapter")
                context.append({"role": "system", "attachment": record.get("attachment"),
                                "rendered": record.get("rendered", [])})
            elif record.get("type") not in ("system", "progress"):
                raise ValueError("Unknown node in selected Claude branch")
            ignored[record.get("type")] += 1
            continue
        message = record["message"]
        if not isinstance(message, dict):
            raise ValueError("Claude message must be an object")
        if message.get("role") != record["type"]:
            raise ValueError("Claude record/message role mismatch")
        transport = record.get("portability_content", {})
        if not isinstance(transport, dict):
            raise ValueError("Invalid portable content metadata")
        if not isinstance(transport.get("tool_results", {}), dict):
            raise ValueError("Invalid portable tool-result metadata")
        content = codec.restore_spans(message.get("content", []), transport.get("message"))
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list) or any(not isinstance(b, dict) for b in content):
            raise ValueError("Claude content must be a string or block array")
        blocks = []
        for block in content:
            btype = block.get("type")
            if btype in ("text", "image", "document"):
                blocks.extend(text_blocks([block]))
            elif btype == "tool_use":
                arguments = block["input"]
                fmt = "json"
                if block["id"] in record.get("portability_text_calls", []) and isinstance(arguments, dict) and set(arguments) == {"_csl_text_arguments"}:
                    arguments, fmt = arguments["_csl_text_arguments"], "text"
                blocks.append({"type": "tool_call", "call_id": block["id"], "name": block["name"],
                               "arguments": arguments, "argument_format": fmt})
            elif btype == "tool_result":
                if not isinstance(block.get("is_error", False), bool):
                    raise ValueError("Native tool error flag must be boolean")
                blocks.append({"type": "tool_result", "call_id": block["tool_use_id"],
                               "content": tool_output(codec.restore_spans(block["content"], transport.get("tool_results", {}).get(block["tool_use_id"]))), "is_error": bool(block.get("is_error"))})
            elif btype in ("thinking", "redacted_thinking"):
                ignored[btype] += 1
            else:
                raise ValueError("Unimplemented Claude block; explicit adapter required")
        if blocks:
            events.append({"id": record["uuid"], "time": record.get("timestamp"),
                           "role": message["role"], "blocks": blocks, "phase": record.get("portability_phase")})
        if record.get("isCompactSummary"):
            ignored["applied_compaction_checkpoints"] += 1
    ignored["off_branch_records"] = sum(r.get("type") in ("user", "assistant") and r.get("uuid") not in seen
                                         for r in records)
    data = canonical("claude", path, events, context, ignored, leaf, repair_interrupted)
    data["source"]["session_id"] = next((r.get("sessionId") for r in records if r.get("sessionId")), None)
    return data


def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get("events"), list) or not isinstance(data.get("source_context"), list):
        raise ValueError("Invalid canonical envelope")
    try:
        json.dumps(data, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Canonical data must be finite JSON") from exc
    if data.get("format") != FORMAT or type(data.get("version")) is not int or data.get("version") != VERSION or not data.get("events"):
        raise ValueError("Invalid/empty canonical conversation")
    if set(data) - {"format", "version", "source", "source_context", "events", "report"}:
        raise ValueError("Unknown canonical envelope fields")
    if any(not isinstance(value, dict) for value in data["source_context"]):
        raise ValueError("Canonical source context must contain objects")
    if any(key in data and not isinstance(data[key], dict) for key in ("source", "report")):
        raise ValueError("Canonical source/report must be objects")
    calls, results = {}, set()
    for event in data["events"]:
        if not isinstance(event, dict) or event.get("role") not in ("user", "assistant") or not isinstance(event.get("blocks"), list) or not event["blocks"]:
            raise ValueError("Invalid event role/blocks")
        if event.get("phase") not in (None, "commentary", "final_answer"):
            raise ValueError("Unsupported assistant phase")
        if set(event) - {"role", "blocks", "phase", "id", "time"} or any(event.get(key) is not None and not isinstance(event[key], str) for key in ("id", "time")):
            raise ValueError("Unknown/invalid canonical event fields")
        for block in event["blocks"]:
            if not isinstance(block, dict):
                raise ValueError("Canonical blocks must be objects")
            kind = block.get("type")
            allowed = {"text": {"type", "text", "references"},
                       "tool_call": {"type", "call_id", "name", "arguments", "argument_format"},
                       "tool_result": {"type", "call_id", "content", "is_error"}}
            if kind in allowed and set(block) - allowed[kind]:
                raise ValueError("Unknown canonical content fields")
            if kind == "text":
                if not isinstance(block.get("text"), str):
                    raise ValueError("Text must be a string")
                codec.blocks([block])
            elif kind in ("image", "document"):
                raise ValueError("Image/PDF conversion is not integrated; use a native transfer instead")
            elif kind == "tool_call":
                key = block.get("call_id")
                if not isinstance(key, str) or not key or not isinstance(block.get("name"), str) or not block["name"]:
                    raise ValueError("Tool call requires nonempty ID/name")
                if key in calls or event["role"] != "assistant":
                    raise ValueError("Duplicate tool call or wrong role")
                calls[key] = block
                if block.get("argument_format") not in ("json", "text"):
                    raise ValueError("Invalid argument format")
                expected = dict if block["argument_format"] == "json" else str
                if not isinstance(block.get("arguments"), expected):
                    raise ValueError("Tool arguments do not match their format")
            elif kind == "tool_result":
                key = block.get("call_id")
                if not isinstance(key, str) or not key:
                    raise ValueError("Tool result requires a nonempty call ID")
                if key not in calls or key in results or event["role"] != "user":
                    raise ValueError("Orphan/duplicate tool result or wrong role")
                if not isinstance(block.get("content"), list):
                    raise ValueError("Canonical tool result content must be a block array")
                if any(not isinstance(part, dict) or set(part) - {"type", "text", "references"} for part in block["content"]):
                    raise ValueError("Unknown canonical tool-result content fields")
                text_blocks(block["content"])
                if not isinstance(block.get("is_error"), bool):
                    raise ValueError("Tool result error flag must be boolean")
                results.add(key)
            else:
                raise ValueError("Unknown canonical block; explicit adapter required")
    if set(calls) != results:
        raise ValueError("Unfinished tool calls require explicit interruption handling")


def atomic_text(path, serialized):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publication without replacing files or following destination symlinks.
        os.link(temporary, path, follow_symlinks=False)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_jsonl(path, records):
    atomic_text(path, "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n" for record in records))


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n")


def provenance(data):
    source = data.get("source", {})
    return {"format": FORMAT, "version": VERSION,
            "source_provider": source.get("provider"), "source_session_id": source.get("session_id"),
            "source_sha256": source.get("sha256"), "history_policy": data.get("report", {}).get("history_policy", "active"),
            "quarantined_source_context": len(data["source_context"]),
            "omitted_native_state": data.get("report", {}).get("ignored_native_state", {}),
            "synthetic_tool_results": len(data.get("report", {}).get("synthetic_interruption_results", []))}


def restore_claude(data, path, cwd, sid=None):
    validate(data)
    sid, parent, records = sid or str(uuid.uuid4()), None, []
    uuid.UUID(sid)
    grouped = []
    for event in data["events"]:
        if grouped and grouped[-1]["role"] == event["role"] and grouped[-1].get("phase") == event.get("phase"):
            grouped[-1]["blocks"].extend(event["blocks"])
        else:
            grouped.append({**event, "blocks": list(event["blocks"])})
    for event in grouped:
        blocks = []
        spans, tool_spans = [], {}
        for block in event["blocks"]:
            kind = block["type"]
            if kind in ("text", "image", "document"):
                projected, entries = codec.encode([block], "claude", event["role"])
                spans.extend([{**entry, "start": entry["start"] + len(blocks)} for entry in entries])
                blocks.extend(projected)
            elif kind == "tool_call":
                arguments = block["arguments"] if block["argument_format"] == "json" else {
                    "_csl_text_arguments": block["arguments"]}
                blocks.append({"type": "tool_use", "id": block["call_id"],
                               "name": block["name"], "input": arguments})
            else:
                projected, entries = codec.encode(block["content"], "claude", "tool")
                if entries:
                    tool_spans[block["call_id"]] = entries
                blocks.append({"type": "tool_result", "tool_use_id": block["call_id"],
                               "content": projected, "is_error": block["is_error"]})
        key = str(uuid.uuid4())
        message = {"role": event["role"], "content": blocks}
        if event["role"] == "assistant":
            message.update(id=f"msg_{uuid.uuid4().hex}", type="message", model="<synthetic>",
                           stop_reason="tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
                           stop_sequence=None, usage={"input_tokens": 0, "output_tokens": 0})
        records.append({"type": event["role"], "message": message, "uuid": key,
                        "parentUuid": parent, "sessionId": sid, "cwd": str(Path(cwd).resolve()),
                        "timestamp": event.get("time") or now(), "version": CLAUDE_VERSION,
                        "isSidechain": False, "userType": "external", "entrypoint": "sdk-cli",
                        "portability_phase": event.get("phase"),
                        "portability_content": {"message": spans, "tool_results": tool_spans},
                        "portability_text_calls": [b["call_id"] for b in event["blocks"] if b["type"] == "tool_call" and b["argument_format"] == "text"]})
        parent = key
    records.append({"type": "last-prompt", "sessionId": sid, "leafUuid": parent,
                    "lastPrompt": "Imported CSL conversation"})
    records[0]["csl_conversion"] = provenance(data)
    write_jsonl(path, records)
    return {"provider": "claude", "id": sid, "path": str(Path(path).resolve()),
            "portable_content": content_stats(data["events"]),
            "quarantined_source_context": len(data["source_context"])}


def restore_codex(data, path, cwd, sid=None):
    validate(data)
    sid, timestamp = sid or str(uuid.uuid4()), now()
    uuid.UUID(sid)
    records = [{"type": "session_meta", "timestamp": timestamp, "payload": {
        "id": sid, "timestamp": timestamp, "cwd": str(Path(cwd).resolve()), "originator": "csl-canonical",
        "cli_version": "0.166.0", "source": "cli", "model_provider": "openai", "history_mode": "legacy"}}]
    call_formats = {}
    turn = None
    for event in data["events"]:
        pending = []
        pending_spans = []

        def flush():
            nonlocal turn
            if pending:
                payload = {"type": "message", "role": event["role"], "content": list(pending)}
                if event.get("phase"):
                    payload["phase"] = event["phase"]
                records.append({"type": "response_item", "timestamp": event.get("time") or timestamp,
                                "portability_content": {"message": list(pending_spans)},
                                "payload": payload})
                text = "\n".join(block.get("text", "") for block in pending)
                if event["role"] == "user":
                    if turn:
                        records.append({"type": "event_msg", "timestamp": timestamp,
                                        "payload": {"type": "task_complete", "turn_id": turn}})
                    turn = str(uuid.uuid4())
                    records.append({"type": "event_msg", "timestamp": timestamp,
                                    "payload": {"type": "task_started", "turn_id": turn}})
                records.append({"type": "event_msg", "timestamp": timestamp, "payload": {
                    "type": "user_message" if event["role"] == "user" else "agent_message", "message": text,
                    **({"images": None, "local_images": [], "text_elements": []} if event["role"] == "user"
                       else {"phase": event.get("phase") or "final_answer"})}})
                pending.clear()
                pending_spans.clear()

        for block in event["blocks"]:
            kind = block["type"]
            if kind in ("text", "image", "document"):
                projected, entries = codec.encode([block], "codex", event["role"])
                pending_spans.extend([{**entry, "start": entry["start"] + len(pending)} for entry in entries])
                pending.extend(projected)
                continue
            flush()
            if kind == "tool_call":
                transport = {}
                call_formats[block["call_id"]] = block["argument_format"]
                if block["argument_format"] == "text":
                    payload = {"type": "custom_tool_call", "call_id": block["call_id"], "name": block["name"],
                               "input": block["arguments"]}
                else:
                    payload = {"type": "function_call", "call_id": block["call_id"], "name": block["name"],
                               "arguments": json.dumps(block["arguments"], ensure_ascii=False)}
            else:
                output, entries = codec.encode(block["content"], "codex", "tool")
                transport = {"tool_result": entries}
                result_type = "custom_tool_call_output" if call_formats[block["call_id"]] == "text" else "function_call_output"
                payload = {"type": result_type, "call_id": block["call_id"],
                           "output": output, "csl_is_error": block["is_error"]}
            records.append({"type": "response_item", "timestamp": event.get("time") or timestamp,
                            "portability_content": transport,
                            "payload": payload})
        flush()
    if turn:
        records.append({"type": "event_msg", "timestamp": timestamp,
                        "payload": {"type": "task_complete", "turn_id": turn}})
    records[0]["csl_conversion"] = provenance(data)
    write_jsonl(path, records)
    return {"provider": "codex", "id": sid, "path": str(Path(path).resolve()),
            "portable_content": content_stats(data["events"]),
            "quarantined_source_context": len(data["source_context"])}


def semantic_events(data):
    result = []
    for event in data["events"]:
        if result and result[-1]["role"] == event["role"]:
            result[-1]["blocks"].extend(event["blocks"])
        else:
            result.append({"role": event["role"], "blocks": list(event["blocks"])})
    return result


def normalize(provider, source, leaf=None, repair_interrupted=False, history_policy="active"):
    if provider not in ("codex", "claude"):
        raise ValueError("Unknown provider")
    if provider == "claude" and history_policy != "active":
        raise ValueError("Visible-history override is only supported for Codex")
    return from_codex(source, repair_interrupted, history_policy) if provider == "codex" else from_claude(source, leaf, repair_interrupted)



