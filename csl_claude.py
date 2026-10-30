"""Native Claude Code transcript transfer; no authentication or model calls."""

import copy
import os
from pathlib import Path
import re
import uuid

MAX_TRANSCRIPT = 64 * 1024 * 1024


def home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser().resolve()


def projects():
    return home() / "projects"


def target_path(sid, cwd):
    # The CLI's project directory override takes precedence over path encoding.
    project = os.environ.get("CLAUDE_CODE_PROJECT_DIR_NAME") or re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    if project in (".", "..") or "/" in project or "\\" in project:
        raise ValueError("Invalid CLAUDE_CODE_PROJECT_DIR_NAME")
    return projects() / project / f"{sid}.jsonl"


def paths():
    # Deliberately exclude subagent transcripts nested below project directories.
    return sorted(projects().glob("*/*.jsonl"))


def header(path, read_records):
    if path.stat().st_size > MAX_TRANSCRIPT:
        raise ValueError("Claude transcript exceeds the 64 MiB adapter limit")
    sid = title = cwd = stamp = None
    messages = 0
    with path.open("rb") as stream:
        for record in read_records(stream):
            if record.get("type") == "session_meta":
                raise ValueError("Codex transcript selected with --provider claude")
            if record.get("sessionId"):
                if not isinstance(record["sessionId"], str):
                    raise ValueError("Invalid Claude session ID")
                candidate = str(uuid.UUID(record["sessionId"]))
                if sid and candidate != sid:
                    raise ValueError("Claude transcript contains multiple session IDs")
                sid = candidate
            cwd = cwd or record.get("cwd")
            stamp = stamp or record.get("timestamp")
            if record.get("type") in ("user", "assistant"):
                messages += 1
                message = record.get("message")
                if not isinstance(message, dict):
                    raise ValueError("Invalid Claude message")
                content = message.get("content", [])
                if not isinstance(content, (str, list)) or (isinstance(content, list) and any(not isinstance(b, dict) for b in content)):
                    raise ValueError("Invalid Claude content")
                if not title and record.get("type") == "user":
                    title = content[:100] if isinstance(content, str) else next(
                        (b.get("text", "")[:100] for b in content if isinstance(b, dict) and b.get("type") == "text"), None)
            if record.get("type") == "custom-title":
                title = record.get("customTitle") or title
    if not sid or not messages:
        raise ValueError("Claude transcript lacks session ID or conversation messages")
    return {"id": sid, "title": title or sid, "cwd": cwd, "created_at": stamp, "provider": "claude"}


def resolve(needles, read_records):
    if not needles:
        raise ValueError("Specify a Claude session UUID or JSONL path; use csl sessions --provider claude")
    result = {}
    for needle in needles:
        path = Path(needle).expanduser()
        if path.is_file():
            candidates = [path.resolve()]
        elif re.fullmatch(r"[0-9a-fA-F-]{6,36}", needle):
            candidates = [p for p in paths() if p.stem.startswith(needle.lower())]
        else:
            candidates = [p for p in paths() if needle.casefold() in header(p, read_records)["title"].casefold()]
        if len(candidates) != 1:
            raise ValueError(f"Claude session {needle!r}: {len(candidates)} matches; use a UUID or path")
        info = header(candidates[0], read_records)
        result[info["id"]] = (candidates[0], info)
    return list(result.values())


def portable_record(record):
    record = copy.deepcopy(record)
    # System-prompt snapshots and internal thinking are account/machine specific.
    if record.get("type") == "system-prompt":
        return None, 1
    message = record.get("message")
    removed = 0
    if isinstance(message, dict) and isinstance(message.get("content"), list):
        content = message["content"]
        kept = [b for b in content if b.get("type") not in ("thinking", "redacted_thinking")]
        removed = len(content) - len(kept)
        # Keep the node and its graph references even for thinking-only messages.
        if removed:
            message["content"] = kept or [{"type": "text", "text": "[CSL: non-portable thinking omitted]"}]
    return record, removed


REFERENCE_KEYS = {"uuid", "parentUuid", "leafUuid", "logicalParentUuid", "sourceToolAssistantUUID",
                  "messageUuid", "firstUuid", "lastUuid", "anchorUuid", "originalParentUuid",
                  "headUuid", "tailUuid", "boundaryUuid"}


def rewrite(record, sid, cwd, source_id):
    record = copy.deepcopy(record)
    namespace = uuid.UUID(sid)

    def remap(value):
        if isinstance(value, list):
            return [remap(v) for v in value]
        if not isinstance(value, dict):
            return value
        return {key: (str(uuid.uuid5(namespace, child)) if key in REFERENCE_KEYS and isinstance(child, str)
                      else sid if key == "sessionId" else child if key in ("message", "toolUseResult", "data")
                      else remap(child)) for key, child in value.items()}

    record = remap(record)
    for key in ("permissionMode", "permission_mode", "allowedTools", "disallowedTools"):
        record.pop(key, None)
    if "cwd" in record:
        record["cwd"] = str(cwd)
    # Do not inherit a sender's session-pinned system prompt on resume.
    if record.get("type") == "system-prompt":
        return None
    record["csl_source_session_id"] = source_id
    return record


def validate(path, read_records):
    if path.stat().st_size > MAX_TRANSCRIPT:
        raise ValueError("Claude transcript exceeds the 64 MiB adapter limit")
    nodes, parents, leaves, messages = {}, [], [], 0
    with path.open("rb") as stream:
        records = list(read_records(stream))
    for record in records:
        if record.get("type") == "session_meta":
            raise ValueError("Codex transcript cannot be imported as Claude")
        key = record.get("uuid")
        if key:
            if key in nodes and nodes[key] != record:
                raise ValueError("Conflicting duplicate Claude message UUID")
            nodes[key] = record
        if record.get("type") in ("user", "assistant"):
            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") != record["type"] or not key:
                raise ValueError("Invalid Claude message role or UUID")
            content = message.get("content")
            if not isinstance(content, (str, list)) or (isinstance(content, list) and any(not isinstance(b, dict) for b in content)):
                raise ValueError("Invalid Claude content")
            messages += 1
        if record.get("parentUuid") and not record.get("isCompactSummary"):
            parents.append(record["parentUuid"])
        if record.get("leafUuid"):
            leaves.append(record["leafUuid"])
    if not messages:
        raise ValueError("Claude transcript contains no conversation messages")
    if any(parent not in nodes for parent in parents):
        raise ValueError("Claude transcript references a missing parent; export the complete original")
    if any(leaf not in nodes for leaf in leaves):
        raise ValueError("Claude transcript references a missing leaf; export the complete original")
    finished = set()
    for key in nodes:
        current, seen = key, set()
        while current in nodes and current not in finished:
            if current in seen:
                raise ValueError("Cycle in Claude parent graph")
            seen.add(current)
            node = nodes[current]
            current = None if node.get("isCompactSummary") else node.get("parentUuid")
        finished.update(seen)
