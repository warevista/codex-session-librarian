#!/usr/bin/env python3
"""Local Codex session indexer and search CLI.

The tool intentionally uses only the Python standard library. It builds a
compact SQLite index from ~/.codex/sessions JSONL files so Codex can answer
"which conversation was about X?" without repeatedly reading the full archive.
"""

from __future__ import annotations

import argparse
import curses
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
DEFAULT_SESSIONS_DIR = DEFAULT_CODEX_HOME / "sessions"
DEFAULT_DB = DEFAULT_CODEX_HOME / "session_librarian.sqlite"
DEFAULT_SESSION_INDEX = DEFAULT_CODEX_HOME / "session_index.jsonl"
DEFAULT_CUTS_DIR = DEFAULT_CODEX_HOME / "session_cuts"

STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "artifact",
    "assistant",
    "because",
    "being",
    "cargo",
    "can",
    "codex",
    "command",
    "con",
    "content",
    "could",
    "ctx",
    "curl",
    "date",
    "did",
    "does",
    "doing",
    "event",
    "evidence",
    "find",
    "for",
    "from",
    "git",
    "have",
    "help",
    "here",
    "how",
    "into",
    "just",
    "kubectl",
    "las",
    "like",
    "line",
    "linux",
    "local",
    "los",
    "ls",
    "make",
    "more",
    "msg",
    "name",
    "need",
    "not",
    "now",
    "para",
    "please",
    "por",
    "proceed",
    "project",
    "que",
    "rg",
    "run",
    "should",
    "some",
    "src",
    "status",
    "succeeded",
    "t17",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "this",
    "tool",
    "tool_call",
    "true",
    "type",
    "una",
    "use",
    "user",
    "using",
    "version",
    "want",
    "what",
    "when",
    "where",
    "which",
    "with",
    "work",
    "would",
    "you",
    "your",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{2,}")
PATH_RE = re.compile(r"(?:/[\w.\-+]+)+|(?:[\w.\-+]+/){1,}[\w.\-+]+")
SESSION_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
SHORT_COMMIT_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
GIT_COMMIT_RE = re.compile(r"\bgit\s+commit\b.*?(?:-m\s+(['\"])(.*?)\1|$)", re.IGNORECASE)
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)
COMMIT_CREATED_RE = re.compile(r"^\[[^\]]+\s+([0-9a-f]{7,40})\]\s+(.+)$", re.MULTILINE)


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def one_line(text: str, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def load_thread_names(index_path: Path) -> dict[str, dict[str, str]]:
    names: dict[str, dict[str, str]] = {}
    if not index_path.exists():
        return names
    with index_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = row.get("id")
            if not sid:
                continue
            current = names.get(sid)
            if current is None or str(row.get("updated_at", "")) >= current.get("updated_at", ""):
                names[sid] = {
                    "thread_name": str(row.get("thread_name") or ""),
                    "updated_at": str(row.get("updated_at") or ""),
                }
    return names


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS sessions (
            path TEXT PRIMARY KEY,
            session_id TEXT,
            thread_name TEXT,
            started_at TEXT,
            updated_at TEXT,
            cwd TEXT,
            project TEXT,
            summary TEXT,
            tags TEXT,
            first_user_message TEXT,
            last_user_message TEXT,
            user_messages TEXT,
            assistant_messages TEXT,
            commands TEXT,
            files TEXT,
            llm_summary TEXT,
            llm_tags TEXT,
            notes TEXT,
            summarized_at TEXT,
            has_fork_hint INTEGER NOT NULL DEFAULT 0,
            line_count INTEGER NOT NULL DEFAULT 0,
            json_errors INTEGER NOT NULL DEFAULT 0,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
        CREATE INDEX IF NOT EXISTS idx_sessions_tags ON sessions(tags);

        CREATE TABLE IF NOT EXISTS session_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_path TEXT NOT NULL,
            session_id TEXT,
            hit_type TEXT NOT NULL,
            role TEXT,
            turn_index INTEGER,
            line_no INTEGER,
            text TEXT NOT NULL,
            detail TEXT,
            created_at TEXT,
            is_inherited INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(session_path) REFERENCES sessions(path) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_hits_session_path ON session_hits(session_path);
        CREATE INDEX IF NOT EXISTS idx_hits_session_id ON session_hits(session_id);
        CREATE INDEX IF NOT EXISTS idx_hits_type ON session_hits(hit_type);
        CREATE INDEX IF NOT EXISTS idx_hits_text ON session_hits(text);
        """
    )
    existing_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    for column, definition in {
        "llm_summary": "TEXT",
        "llm_tags": "TEXT",
        "notes": "TEXT",
        "summarized_at": "TEXT",
    }.items():
        if column not in existing_columns:
            try:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
    existing_hit_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(session_hits)").fetchall()
    }
    if "is_inherited" not in existing_hit_columns:
        conn.execute("ALTER TABLE session_hits ADD COLUMN is_inherited INTEGER NOT NULL DEFAULT 0")
    return conn


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "output_text"}:
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def parse_function_call(payload: dict[str, Any]) -> tuple[str, str | None]:
    name = str(payload.get("name") or "")
    arguments = payload.get("arguments") or ""
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=True)
    command = None
    if name == "exec_command":
        try:
            args = json.loads(arguments)
            command = args.get("cmd")
        except json.JSONDecodeError:
            command = arguments
    return name, command


def is_noise_user_text(text: str) -> bool:
    compact = text.strip()
    return compact.startswith("<environment_context>") or compact.startswith("# AGENTS.md instructions")


def collect_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in PATH_RE.finditer(text):
        value = match.group(0).strip("'\"`.,:;)")
        if len(value) < 3:
            continue
        if value.startswith("http") or value.startswith("//"):
            continue
        paths.append(value)
    return paths


def append_unique(items: list[str], text: str) -> None:
    cleaned = text.strip()
    if not cleaned:
        return
    if items and items[-1].strip() == cleaned:
        return
    items.append(cleaned)


def add_hit(
    hits: list[dict[str, Any]],
    *,
    hit_type: str,
    text: str,
    role: str | None = None,
    turn_index: int | None = None,
    line_no: int | None = None,
    detail: str | None = None,
    created_at: str | None = None,
) -> None:
    cleaned = text.strip()
    if not cleaned:
        return
    hits.append(
        {
            "hit_type": hit_type,
            "role": role or "",
            "turn_index": turn_index,
            "line_no": line_no,
            "text": cleaned,
            "detail": detail or "",
            "created_at": created_at or "",
            "is_inherited": 0,
        }
    )


def file_id_from_path(path: Path) -> str:
    match = SESSION_ID_RE.search(path.name)
    return match.group(1) if match else ""


def extract_commit_hints(command: str) -> list[str]:
    hints: list[str] = []
    has_commit_context = bool(re.search(r"\bgit\s+(?:show|log|push|commit|reflog)\b", command, re.IGNORECASE))
    commit_match = GIT_COMMIT_RE.search(command)
    if commit_match:
        message = commit_match.group(2) or command
        hints.append(f"git commit: {message}")
    elif has_commit_context:
        hints.append(command)
    if has_commit_context:
        for commit_hash in SHORT_COMMIT_RE.findall(command):
            if any(ch.isdigit() for ch in commit_hash) and any(ch.isalpha() for ch in commit_hash):
                hints.append(f"commit hash: {commit_hash}")
    return hints


def extract_patch_files(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    input_text = payload.get("input")
    if isinstance(input_text, str):
        paths.extend(match.group(1).strip() for match in PATCH_FILE_RE.finditer(input_text))
    changes = payload.get("changes")
    if isinstance(changes, dict):
        paths.extend(str(path) for path in changes)
    return [path for path in paths if path]


def extract_created_commits(output: str) -> list[str]:
    commits: list[str] = []
    for match in COMMIT_CREATED_RE.finditer(output):
        commits.append(f"commit created: {match.group(1)} {match.group(2).strip()}")
    return commits


def make_tags(texts: list[str], cwd: str | None, commands: list[str], limit: int = 10) -> list[str]:
    counter: Counter[str] = Counter()
    for text in texts:
        for word in WORD_RE.findall(text.lower()):
            if word in STOPWORDS or len(word) < 3:
                continue
            counter[word] += 1
    if cwd:
        project = Path(cwd).name.lower()
        if project and project not in {"projects", "documents"}:
            counter[project] += 4
    return [word for word, _ in counter.most_common(limit)]


def make_summary(thread_name: str, first_user: str, tags: list[str], project: str) -> str:
    if thread_name:
        base = thread_name
    elif first_user:
        base = first_user
    else:
        base = "Codex session"
    summary = one_line(base, 180)
    extras: list[str] = []
    if project:
        extras.append(f"project: {project}")
    if tags:
        extras.append("tags: " + ", ".join(tags[:5]))
    if extras:
        return f"{summary} ({'; '.join(extras)})"
    return summary


def parse_session(path: Path, thread_names: dict[str, dict[str, str]]) -> dict[str, Any]:
    stat = path.stat()
    session_id = ""
    started_at = ""
    first_meta_timestamp = ""
    updated_at = ""
    cwd = ""
    user_messages: list[str] = []
    assistant_messages: list[str] = []
    commands: list[str] = []
    files: Counter[str] = Counter()
    hits: list[dict[str, Any]] = []
    line_count = 0
    json_errors = 0
    has_fork_hint = 0
    turn_index = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line_count += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                json_errors += 1
                continue

            timestamp = str(row.get("timestamp") or "")
            if timestamp:
                if not started_at:
                    started_at = timestamp
                updated_at = timestamp

            kind = row.get("type")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}

            if kind == "session_meta":
                session_id = str(payload.get("id") or session_id)
                if not first_meta_timestamp:
                    first_meta_timestamp = str(payload.get("timestamp") or timestamp or "")
                started_at = str(payload.get("timestamp") or started_at)
                cwd = str(payload.get("cwd") or cwd)
                continue

            if kind == "turn_context":
                cwd = str(payload.get("cwd") or cwd)
                continue

            payload_type = payload.get("type")
            if payload_type == "user_message":
                text = str(payload.get("message") or "")
                if text and not is_noise_user_text(text):
                    append_unique(user_messages, text)
                    turn_index += 1
                    add_hit(
                        hits,
                        hit_type="user_turn",
                        role="user",
                        turn_index=turn_index,
                        line_no=line_count,
                        text=text,
                        created_at=timestamp,
                    )
                    if "fork" in text.lower():
                        has_fork_hint = 1
                continue

            if payload_type == "agent_message":
                text = str(payload.get("message") or "")
                if text:
                    append_unique(assistant_messages, text)
                    add_hit(
                        hits,
                        hit_type="assistant_event",
                        role="assistant",
                        line_no=line_count,
                        text=text,
                        created_at=timestamp,
                    )
                continue

            if payload_type == "function_call":
                _, command = parse_function_call(payload)
                if command:
                    commands.append(command)
                    command_paths = collect_paths(command)
                    files.update(command_paths)
                    add_hit(
                        hits,
                        hit_type="command",
                        line_no=line_count,
                        text=command,
                        detail=str(payload.get("name") or ""),
                        created_at=timestamp,
                    )
                    for command_path in command_paths:
                        add_hit(
                            hits,
                            hit_type="file",
                            line_no=line_count,
                            text=command_path,
                            detail="command path",
                            created_at=timestamp,
                        )
                    for commit_hint in extract_commit_hints(command):
                        add_hit(
                            hits,
                            hit_type="commit",
                            line_no=line_count,
                            text=commit_hint,
                            detail=command,
                            created_at=timestamp,
                        )
                continue

            if payload_type == "function_call_output":
                output = str(payload.get("output") or "")
                for created_commit in extract_created_commits(output):
                    add_hit(
                        hits,
                        hit_type="commit_created",
                        line_no=line_count,
                        text=created_commit,
                        detail=one_line(output, 1000),
                        created_at=timestamp,
                    )
                continue

            if payload_type == "custom_tool_call":
                tool_name = str(payload.get("name") or "")
                if tool_name == "apply_patch":
                    input_text = str(payload.get("input") or "")
                    add_hit(
                        hits,
                        hit_type="patch",
                        line_no=line_count,
                        text=one_line(input_text, 2000),
                        detail=tool_name,
                        created_at=timestamp,
                    )
                    patch_files = extract_patch_files(payload)
                    files.update(patch_files)
                    for patch_file in patch_files:
                        add_hit(
                            hits,
                            hit_type="file",
                            line_no=line_count,
                            text=patch_file,
                            detail="patch file",
                            created_at=timestamp,
                        )
                continue

            if payload_type == "message":
                role = payload.get("role")
                text = extract_text_from_content(payload.get("content"))
                if role == "user" and text and not is_noise_user_text(text):
                    append_unique(user_messages, text)
                    turn_index += 1
                    add_hit(
                        hits,
                        hit_type="user_turn",
                        role="user",
                        turn_index=turn_index,
                        line_no=line_count,
                        text=text,
                        created_at=timestamp,
                    )
                    if "fork" in text.lower():
                        has_fork_hint = 1
                elif role == "assistant" and text:
                    append_unique(assistant_messages, text)
                    turn_index += 1
                    phase = str(payload.get("phase") or "")
                    add_hit(
                        hits,
                        hit_type="assistant_final" if phase == "final_answer" else "assistant_turn",
                        role="assistant",
                        turn_index=turn_index,
                        line_no=line_count,
                        text=text,
                        detail=phase,
                        created_at=timestamp,
                    )

            if payload_type == "patch_apply_end":
                patch_files = extract_patch_files(payload)
                files.update(patch_files)
                for patch_file in patch_files:
                    add_hit(
                        hits,
                        hit_type="file",
                        line_no=line_count,
                        text=patch_file,
                        detail="patch apply",
                        created_at=timestamp,
                    )

    # Prefer the rollout filename id. Forked/cloned JSONL files may contain
    # inherited session_meta rows, but the filename id is the resumable handle.
    filename_id = file_id_from_path(path)
    if filename_id:
        session_id = filename_id

    thread_info = thread_names.get(session_id, {})
    thread_name = thread_info.get("thread_name", "")
    if thread_info.get("updated_at"):
        updated_at = max(updated_at, thread_info["updated_at"])

    first_user = one_line(user_messages[0], 500) if user_messages else ""
    last_user = one_line(user_messages[-1], 500) if user_messages else ""
    text_for_tags = [thread_name] + user_messages
    tags = make_tags(text_for_tags, cwd, commands)
    project = Path(cwd).name if cwd else ""
    summary = make_summary(thread_name, first_user, tags, project)

    files.update(path for msg in user_messages[:20] for path in collect_paths(msg))
    top_files = [value for value, _ in files.most_common(25)]
    if first_meta_timestamp:
        for hit in hits:
            hit_timestamp = hit.get("created_at") or ""
            if hit_timestamp and hit_timestamp < first_meta_timestamp:
                hit["is_inherited"] = 1

    return {
        "path": str(path),
        "session_id": session_id,
        "thread_name": thread_name,
        "started_at": started_at,
        "updated_at": updated_at,
        "cwd": cwd,
        "project": project,
        "summary": summary,
        "tags": ", ".join(tags),
        "first_user_message": first_user,
        "last_user_message": last_user,
        "user_messages": "\n---\n".join(one_line(msg, 1000) for msg in user_messages[:50]),
        "assistant_messages": "\n---\n".join(one_line(msg, 1000) for msg in assistant_messages[:30]),
        "commands": "\n".join(one_line(cmd, 500) for cmd in commands[:100]),
        "files": "\n".join(top_files),
        "has_fork_hint": has_fork_hint or int("fork" in (thread_name or "").lower()),
        "line_count": line_count,
        "json_errors": json_errors,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "indexed_at": utc_now(),
        "_hits": hits,
    }


def upsert_session(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    hits = row.pop("_hits", [])
    keys = list(row)
    placeholders = ", ".join("?" for _ in keys)
    columns = ", ".join(keys)
    updates = ", ".join(f"{key}=excluded.{key}" for key in keys if key != "path")
    conn.execute(
        f"INSERT INTO sessions ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(path) DO UPDATE SET {updates}",
        [row[key] for key in keys],
    )
    conn.execute("DELETE FROM session_hits WHERE session_path = ?", (row["path"],))
    for hit in hits:
        conn.execute(
            """
            INSERT INTO session_hits (
                session_path, session_id, hit_type, role, turn_index, line_no,
                text, detail, created_at, is_inherited
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["path"],
                row["session_id"],
                hit["hit_type"],
                hit["role"],
                hit["turn_index"],
                hit["line_no"],
                hit["text"],
                hit["detail"],
                hit["created_at"],
                hit["is_inherited"],
            ),
        )


def find_session(conn: sqlite3.Connection, needle: str) -> sqlite3.Row | None:
    like = f"%{needle}%"
    return conn.execute(
        """
        SELECT * FROM sessions
        WHERE session_id LIKE ? OR path LIKE ? OR thread_name LIKE ?
        ORDER BY coalesce(updated_at, started_at) DESC
        LIMIT 1
        """,
        (like, like, like),
    ).fetchone()


def display_summary(row: sqlite3.Row) -> str:
    return row["llm_summary"] or row["summary"] or ""


def display_tags(row: sqlite3.Row) -> str:
    return row["llm_tags"] or row["tags"] or ""


def hit_type_filter(args: argparse.Namespace) -> list[str]:
    types = list(getattr(args, "hit_type", None) or [])
    if getattr(args, "turns_only", False):
        types.extend(["user_turn", "assistant_turn", "assistant_final"])
    if getattr(args, "artifacts", False):
        types.extend(["command", "commit", "commit_created", "patch", "file"])
    return sorted(set(types))


def matching_hits(
    conn: sqlite3.Connection,
    session_path: str,
    terms: list[str],
    *,
    types: list[str] | None = None,
    limit: int = 6,
    include_inherited: bool = False,
) -> list[sqlite3.Row]:
    where = ["session_path = ?"]
    params: list[Any] = [session_path]
    if not include_inherited:
        where.append("coalesce(is_inherited, 0) = 0")
    if types:
        where.append(f"hit_type IN ({', '.join('?' for _ in types)})")
        params.extend(types)
    for term in terms:
        where.append("lower(text || ' ' || coalesce(detail,'')) LIKE ?")
        params.append(f"%{term.lower()}%")
    rows = list(
        conn.execute(
            f"""
            SELECT *
            FROM session_hits
            WHERE {' AND '.join(where)}
            ORDER BY coalesce(turn_index, 999999), line_no
            LIMIT ?
            """,
            [*params, max(limit * 8, limit)],
        )
    )
    rows.sort(
        key=lambda hit: (
            len(matched_terms(f"{hit['text']} {hit['detail'] or ''}", terms)),
            hit_weight(hit["hit_type"]),
            -(hit["line_no"] or 0),
        ),
        reverse=True,
    )
    return rows[:limit]


def hit_weight(hit_type: str) -> int:
    return {
        "commit_created": 60,
        "commit": 50,
        "patch": 42,
        "file": 36,
        "command": 30,
        "assistant_final": 24,
        "assistant_turn": 14,
        "user_turn": 14,
        "assistant_event": 8,
    }.get(hit_type, 5)


def matched_terms(text: str, terms: list[str]) -> set[str]:
    haystack = text.lower()
    return {term for term in terms if term.lower() in haystack}


def is_context_package_hit(text: str) -> bool:
    lower = text.lower()
    return (
        "codex session cut" in lower
        or "use this as the starting context for a new codex session" in lower
        or "clean cloned continuation" in lower
    )


RECALL_STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "but",
    "can",
    "did",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "into",
    "its",
    "not",
    "our",
    "that",
    "the",
    "this",
    "use",
    "using",
    "was",
    "were",
    "what",
    "when",
    "where",
    "with",
    "you",
}


def recall_terms(query: list[str]) -> tuple[str, list[str]]:
    raw = " ".join(part for part in query if part.strip()).strip()
    normalized = raw.replace("Linked In", "LinkedIn").replace("linked in", "linkedin")
    tokens = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*", normalized.lower()):
        token = token.strip("._:/+-")
        if len(token) < 3 or token in RECALL_STOPWORDS:
            continue
        tokens.append(token)
    deduped = list(dict.fromkeys(tokens))
    return raw, deduped


def recall_sessions(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    raw_query, terms = recall_terms(args.query)
    must_terms = list(dict.fromkeys(term.lower() for term in (args.must or []) if term.strip()))
    anchor_terms = list(dict.fromkeys(term.lower() for term in (args.anchor or []) if term.strip()))
    if not terms and not must_terms:
        print("Provide a fuzzy memory query or --must term.", file=sys.stderr)
        raise SystemExit(2)

    types = hit_type_filter(args)
    if not types and not args.all_types:
        types = ["user_turn", "assistant_turn", "assistant_final"]

    where = []
    params: list[Any] = []
    if types:
        where.append(f"h.hit_type IN ({', '.join('?' for _ in types)})")
        params.extend(types)
    if not args.include_inherited:
        where.append("coalesce(h.is_inherited, 0) = 0")
    if args.project:
        where.append("lower(s.project) = lower(?)")
        params.append(args.project)

    match_terms = list(dict.fromkeys([*terms, *must_terms, *anchor_terms]))
    term_clauses = []
    for term in match_terms:
        term_clauses.append("lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?")
        params.append(f"%{term}%")
    if term_clauses:
        where.append("(" + " OR ".join(term_clauses) + ")")
    sql_where = " AND ".join(where) if where else "1 = 1"

    rows = conn.execute(
        f"""
        SELECT
            h.*,
            s.session_id,
            s.thread_name,
            s.summary,
            s.llm_summary,
            s.tags,
            s.llm_tags,
            s.cwd,
            s.project,
            s.started_at,
            s.updated_at,
            s.has_fork_hint,
            s.first_user_message
        FROM session_hits h
        JOIN sessions s ON s.path = h.session_path
        WHERE {sql_where}
        ORDER BY coalesce(s.updated_at, s.started_at) DESC, h.line_no DESC
        LIMIT ?
        """,
        [*params, args.scan_limit],
    ).fetchall()

    by_path: dict[str, dict[str, Any]] = {}
    for hit in rows:
        text = f"{hit['text']} {hit['detail'] or ''}"
        lower_text = text.lower()
        if not args.include_context_packages and is_context_package_hit(text):
            continue
        if anchor_terms and not all(term in lower_text for term in anchor_terms):
            continue
        covered = matched_terms(lower_text, match_terms)
        if must_terms and not all(term in lower_text for term in must_terms):
            # A must term may still be covered elsewhere in the same session; filter after aggregation.
            pass
        exact_bonus = 50 if raw_query and raw_query.lower() in lower_text else 0
        same_hit_bonus = 15 * max(0, len(covered) - 1)
        score = hit_weight(hit["hit_type"]) + same_hit_bonus + exact_bonus
        candidate = by_path.setdefault(
            hit["session_path"],
            {
                "row": hit,
                "score": 0,
                "coverage": set(),
                "types": Counter(),
                "hits": [],
                "best_hit_coverage": 0,
            },
        )
        candidate["coverage"].update(covered)
        candidate["types"][hit["hit_type"]] += 1
        candidate["score"] += score
        candidate["best_hit_coverage"] = max(candidate["best_hit_coverage"], len(covered))
        candidate["hits"].append(hit)

    candidates: list[dict[str, Any]] = []
    for candidate in by_path.values():
        coverage = candidate["coverage"]
        if must_terms and not all(term in coverage for term in must_terms):
            continue
        if args.min_coverage and len(coverage) < args.min_coverage:
            continue
        candidate["score"] += len(coverage) * 35 + candidate["best_hit_coverage"] * 20
        candidates.append(candidate)

    if not candidates:
        print("No recall candidates found in indexed sessions.")
        print("Tried fuzzy clues: " + (", ".join(match_terms) if match_terms else "(none)"))
        print_pinpoint_questions(match_terms, repo=None, no_results=True)
        return

    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(family_key(candidate["row"]), []).append(candidate)
    groups = []
    for items in grouped.values():
        items.sort(key=lambda item: (item["score"], item["row"]["updated_at"] or item["row"]["started_at"]), reverse=True)
        groups.append(items)
    groups.sort(key=lambda items: (items[0]["score"], items[0]["row"]["updated_at"] or items[0]["row"]["started_at"]), reverse=True)

    shown = 0
    full_terms = set(match_terms)
    for group in groups:
        if shown >= args.limit:
            break
        best = group[0]
        row = best["row"]
        coverage = sorted(best["coverage"])
        missing = sorted(full_terms - set(coverage))
        confidence = "high" if not missing and best["best_hit_coverage"] >= max(2, len(full_terms) // 2) else "medium" if len(coverage) >= max(2, len(full_terms) // 2) else "low"
        print(f"{row['updated_at'] or row['started_at']}  {row['session_id']}  confidence={confidence} score={best['score']}")
        if len(group) > 1:
            siblings = ", ".join(item["row"]["session_id"] for item in group[1:4])
            print(f"  family: {len(group)} related rollout files" + (f"; alternates: {siblings}" if siblings else ""))
        print(f"  {display_summary(row)}")
        if row["cwd"]:
            print(f"  cwd: {row['cwd']}")
        print(f"  path: {row['session_path']}")
        print(f"  covered: {', '.join(coverage) if coverage else '(none)'}")
        if missing:
            print(f"  missing: {', '.join(missing)}")
        print("  trace:")
        hits = sorted(
            best["hits"],
            key=lambda hit: (
                len(matched_terms(f"{hit['text']} {hit['detail'] or ''}", match_terms)),
                hit_weight(hit["hit_type"]),
                -(hit["line_no"] or 0),
            ),
            reverse=True,
        )
        for hit in hits[: args.evidence]:
            location = []
            if hit["turn_index"] is not None:
                location.append(f"turn {hit['turn_index']}")
            if hit["line_no"] is not None:
                location.append(f"line {hit['line_no']}")
            suffix = f" ({', '.join(location)})" if location else ""
            print(f"    [{hit['hit_type']}]{suffix} {one_line(hit['text'], args.width)}")
        print()
        shown += 1

    if args.ask or groups[0][0]["score"] < 180 or len(groups) > args.limit:
        print_pinpoint_questions(match_terms, repo=None, no_results=False)


def patch_segments(text: str) -> tuple[list[str], list[str]]:
    # Patch hits are compacted to one line in the index, but +/- markers remain.
    markers = list(re.finditer(r"\s([+-])\s", text))
    removed_parts: list[str] = []
    added_parts: list[str] = []
    for index, marker in enumerate(markers):
        start = marker.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        segment = text[start:end].strip()
        if not segment or segment.startswith(("+++", "---")):
            continue
        if marker.group(1) == "-":
            removed_parts.append(segment)
        else:
            added_parts.append(segment)
    return removed_parts, added_parts


def patch_removed_added(text: str) -> tuple[str, str]:
    removed_parts, added_parts = patch_segments(text)
    return " ".join(removed_parts), " ".join(added_parts)


def patch_change_warnings(hits: list[sqlite3.Row], terms: list[str]) -> list[str]:
    warnings: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        if hit["hit_type"] != "patch":
            continue
        removed_parts, added_parts = patch_segments(hit["text"])
        removed_non_comment = " ".join(part for part in removed_parts if not part.lstrip().startswith("#")).lower()
        added_non_comment = " ".join(part for part in added_parts if not part.lstrip().startswith("#")).lower()
        for term in terms:
            if not (len(term) >= 8 or any(ch in term for ch in ".:/_=-") or any(ch.isdigit() for ch in term)):
                continue
            term_lower = term.lower()
            if term_lower in removed_non_comment and term_lower not in added_non_comment:
                warning = f"`{term}` appears in removed patch content but not added content"
                if warning not in seen:
                    warnings.append(warning)
                    seen.add(warning)
    return warnings


def family_key(row: sqlite3.Row) -> str:
    thread = re.sub(r"\s+", " ", str(row["thread_name"] or "").strip().lower())
    first = re.sub(r"\s+", " ", str(row["first_user_message"] or "").strip().lower())
    if thread:
        return f"thread:{thread}"
    if first:
        return f"first:{first[:220]}"
    return f"session:{row['session_id']}"


def evidence_hits_any(
    conn: sqlite3.Connection,
    session_path: str,
    terms: list[str],
    *,
    include_inherited: bool = False,
    limit: int = 10,
) -> list[sqlite3.Row]:
    where = ["session_path = ?"]
    params: list[Any] = [session_path]
    if not include_inherited:
        where.append("coalesce(is_inherited, 0) = 0")
    if terms:
        term_clauses = []
        for term in terms:
            term_clauses.append("lower(text || ' ' || coalesce(detail,'')) LIKE ?")
            params.append(f"%{term.lower()}%")
        where.append("(" + " OR ".join(term_clauses) + ")")
    return list(
        conn.execute(
            f"""
            SELECT *
            FROM session_hits
            WHERE {' AND '.join(where)}
            ORDER BY
                CASE hit_type
                    WHEN 'commit_created' THEN 0
                    WHEN 'patch' THEN 1
                    WHEN 'file' THEN 2
                    WHEN 'command' THEN 3
                    WHEN 'assistant_final' THEN 4
                    WHEN 'commit' THEN 5
                    WHEN 'user_turn' THEN 6
                    ELSE 7
                END,
                coalesce(turn_index, 999999),
                line_no
            LIMIT ?
            """,
            [*params, limit],
        )
    )


def index_sessions(args: argparse.Namespace) -> None:
    sessions_dir = args.sessions_dir.expanduser()
    conn = init_db(args.db.expanduser())
    thread_names = load_thread_names(args.session_index.expanduser())
    paths = sorted(sessions_dir.rglob("*.jsonl"))
    changed = 0
    skipped = 0
    errors = 0
    indexed_paths: set[str] = set()

    for path in paths:
        stat = path.stat()
        indexed_paths.add(str(path))
        current = conn.execute(
            "SELECT size_bytes, mtime_ns FROM sessions WHERE path = ?",
            (str(path),),
        ).fetchone()
        if current and not args.force and current["size_bytes"] == stat.st_size and current["mtime_ns"] == stat.st_mtime_ns:
            skipped += 1
            continue
        try:
            row = parse_session(path, thread_names)
            upsert_session(conn, row)
            changed += 1
        except OSError as exc:
            errors += 1
            print(f"error: {path}: {exc}", file=sys.stderr)

    if args.prune:
        db_paths = [row["path"] for row in conn.execute("SELECT path FROM sessions")]
        for db_path in db_paths:
            if db_path not in indexed_paths:
                conn.execute("DELETE FROM session_hits WHERE session_path = ?", (db_path,))
                conn.execute("DELETE FROM sessions WHERE path = ?", (db_path,))

    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()["count"]
    print(f"indexed={changed} skipped={skipped} errors={errors} total={total} db={args.db.expanduser()}")


def query_sessions(args: argparse.Namespace) -> list[sqlite3.Row]:
    conn = init_db(args.db.expanduser())
    params: list[Any] = []
    where: list[str] = []
    types = hit_type_filter(args)
    inherited_filter = "" if getattr(args, "include_inherited", False) else " AND coalesce(h.is_inherited, 0) = 0"
    if types:
        return query_sessions_from_hits(conn, args, types)
    if getattr(args, "project", None):
        where.append("lower(project) = lower(?)")
        params.append(args.project)
    if getattr(args, "forks", False):
        where.append("has_fork_hint = 1")
    if getattr(args, "query", None):
        terms = [term for term in args.query if term.strip()]
        for term in terms:
            like = f"%{term.lower()}%"
            hit_filter = ""
            hit_params: list[Any] = []
            if types:
                hit_filter = f" AND h.hit_type IN ({', '.join('?' for _ in types)})"
                hit_params.extend(types)
            if getattr(args, "turns_only", False) or getattr(args, "artifacts", False) or getattr(args, "hit_type", None):
                where.append(
                    "EXISTS (SELECT 1 FROM session_hits h "
                    "WHERE h.session_path = sessions.path "
                    f"{hit_filter}{inherited_filter} "
                    "AND lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?)"
                )
                params.extend([*hit_params, like])
            else:
                where.append(
                    "("
                    "lower(coalesce(thread_name,'')) LIKE ? OR "
                    "lower(coalesce(summary,'')) LIKE ? OR "
                    "lower(coalesce(llm_summary,'')) LIKE ? OR "
                    "lower(coalesce(tags,'')) LIKE ? OR "
                    "lower(coalesce(llm_tags,'')) LIKE ? OR "
                    "lower(coalesce(user_messages,'')) LIKE ? OR "
                    "lower(coalesce(assistant_messages,'')) LIKE ? OR "
                    "lower(coalesce(commands,'')) LIKE ? OR "
                    "lower(coalesce(files,'')) LIKE ? OR "
                    "EXISTS (SELECT 1 FROM session_hits h "
                    "WHERE h.session_path = sessions.path "
                    f"{hit_filter}{inherited_filter} "
                    "AND lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?)"
                    ")"
                )
                params.extend([like] * 9)
                params.extend([*hit_params, like])
    score_bits: list[str] = []
    score_params: list[Any] = []
    if where:
        sql_where = " WHERE " + " AND ".join(where)
    else:
        sql_where = ""
    for term in getattr(args, "query", []) or []:
        like = f"%{term.lower()}%"
        type_filter_sql = ""
        type_score_params: list[Any] = []
        if types:
            type_filter_sql = f" AND h.hit_type IN ({', '.join('?' for _ in types)})"
            type_score_params.extend(types)
        type_filter_sql = type_filter_sql + inherited_filter
        score_bits.extend(
            [
                "CASE WHEN lower(coalesce(thread_name,'')) LIKE ? THEN 20 ELSE 0 END",
                "CASE WHEN lower(coalesce(llm_summary,'')) LIKE ? THEN 18 ELSE 0 END",
                "CASE WHEN lower(coalesce(summary,'')) LIKE ? THEN 12 ELSE 0 END",
                "CASE WHEN lower(coalesce(llm_tags,'')) LIKE ? THEN 10 ELSE 0 END",
                "CASE WHEN lower(coalesce(tags,'')) LIKE ? THEN 8 ELSE 0 END",
                "CASE WHEN lower(coalesce(first_user_message,'')) LIKE ? THEN 6 ELSE 0 END",
                "CASE WHEN lower(coalesce(user_messages,'')) LIKE ? THEN 3 ELSE 0 END",
                "CASE WHEN lower(coalesce(commands,'')) LIKE ? THEN 1 ELSE 0 END",
                "CASE WHEN lower(coalesce(files,'')) LIKE ? THEN 1 ELSE 0 END",
                "CASE WHEN EXISTS (SELECT 1 FROM session_hits h WHERE h.session_path = sessions.path "
                f"{type_filter_sql} AND h.hit_type = 'commit_created' AND lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?) THEN 45 ELSE 0 END",
                "CASE WHEN EXISTS (SELECT 1 FROM session_hits h WHERE h.session_path = sessions.path "
                f"{type_filter_sql} AND h.hit_type = 'commit' AND lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?) THEN 35 ELSE 0 END",
                "CASE WHEN EXISTS (SELECT 1 FROM session_hits h WHERE h.session_path = sessions.path "
                f"{type_filter_sql} AND h.hit_type = 'patch' AND lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?) THEN 28 ELSE 0 END",
                "CASE WHEN EXISTS (SELECT 1 FROM session_hits h WHERE h.session_path = sessions.path "
                f"{type_filter_sql} AND h.hit_type = 'file' AND lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?) THEN 24 ELSE 0 END",
                "CASE WHEN EXISTS (SELECT 1 FROM session_hits h WHERE h.session_path = sessions.path "
                f"{type_filter_sql} AND h.hit_type = 'command' AND lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?) THEN 18 ELSE 0 END",
                "CASE WHEN EXISTS (SELECT 1 FROM session_hits h WHERE h.session_path = sessions.path "
                f"{type_filter_sql} AND h.hit_type = 'assistant_final' AND lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?) THEN 15 ELSE 0 END",
            ]
        )
        score_params.extend([like] * 9)
        for _ in range(6):
            score_params.extend([*type_score_params, like])
    score_sql = " + ".join(score_bits) if score_bits else "0"
    sql = (
        f"SELECT *, ({score_sql}) AS score FROM sessions"
        f"{sql_where} ORDER BY score DESC, coalesce(updated_at, started_at) DESC, path DESC LIMIT ?"
    )
    return list(conn.execute(sql, [*score_params, *params, args.limit]))


def query_sessions_from_hits(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    types: list[str],
) -> list[sqlite3.Row]:
    terms = [term for term in getattr(args, "query", []) if term.strip()]
    where = [f"h.hit_type IN ({', '.join('?' for _ in types)})"]
    params: list[Any] = [*types]
    if not getattr(args, "include_inherited", False):
        where.append("coalesce(h.is_inherited, 0) = 0")
    for term in terms:
        where.append("lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?")
        params.append(f"%{term.lower()}%")
    if getattr(args, "project", None):
        where.append("lower(s.project) = lower(?)")
        params.append(args.project)
    if getattr(args, "forks", False):
        where.append("s.has_fork_hint = 1")

    score_sql = """
                CASE h.hit_type
                    WHEN 'commit_created' THEN 50
                    WHEN 'patch' THEN 40
                    WHEN 'file' THEN 34
                    WHEN 'command' THEN 28
                    WHEN 'commit' THEN 26
                    WHEN 'assistant_final' THEN 20
                    WHEN 'assistant_turn' THEN 12
                    WHEN 'user_turn' THEN 10
            ELSE 5
        END
    """
    return list(
        conn.execute(
            f"""
            SELECT s.*, max({score_sql}) AS score, count(h.id) AS evidence_count
            FROM session_hits h
            JOIN sessions s ON s.path = h.session_path
            WHERE {' AND '.join(where)}
            GROUP BY s.path
            ORDER BY score DESC, evidence_count DESC, coalesce(s.updated_at, s.started_at) DESC
            LIMIT ?
            """,
            [*params, args.limit],
        )
    )


def print_session_row(
    row: sqlite3.Row,
    verbose: bool = False,
    *,
    conn: sqlite3.Connection | None = None,
    terms: list[str] | None = None,
    types: list[str] | None = None,
    show_hits: bool = False,
    include_inherited: bool = False,
) -> None:
    marker = " [fork?]" if row["has_fork_hint"] else ""
    source = " [summarized]" if row["llm_summary"] else ""
    print(f"{row['updated_at'] or row['started_at']}  {row['session_id']}{marker}")
    print(f"  {display_summary(row)}{source}")
    if row["cwd"]:
        print(f"  cwd: {row['cwd']}")
    tags = display_tags(row)
    if tags:
        print(f"  tags: {tags}")
    print(f"  path: {row['path']}")
    if verbose:
        if row["first_user_message"]:
            print(f"  first: {row['first_user_message']}")
        if row["last_user_message"] and row["last_user_message"] != row["first_user_message"]:
            print(f"  last: {row['last_user_message']}")
        if row["commands"]:
            print("  commands:")
            for command in row["commands"].splitlines()[:8]:
                print(f"    {command}")
    if show_hits and conn is not None:
        hits = matching_hits(
            conn,
            row["path"],
            terms or [],
            types=types,
            limit=6,
            include_inherited=include_inherited,
        )
        if hits:
            print("  evidence:")
            for hit in hits:
                location = []
                if hit["turn_index"] is not None:
                    location.append(f"turn {hit['turn_index']}")
                if hit["line_no"] is not None:
                    location.append(f"line {hit['line_no']}")
                suffix = f" ({', '.join(location)})" if location else ""
                print(f"    [{hit['hit_type']}]{suffix} {one_line(hit['text'], 180)}")
    print()


def search_sessions(args: argparse.Namespace) -> None:
    rows = query_sessions(args)
    if not rows:
        print("No matching sessions. Run `./codex_sessions.py index` if the index is stale.")
        return
    conn = init_db(args.db.expanduser())
    terms = [term for term in getattr(args, "query", []) if term.strip()]
    types = hit_type_filter(args)
    seen_evidence: set[tuple[str, str]] = set()
    for row in rows:
        if types and not getattr(args, "no_dedupe", False):
            hits = matching_hits(
                conn,
                row["path"],
                terms,
                types=types,
                limit=1,
                include_inherited=args.include_inherited,
            )
            if hits:
                signature = (hits[0]["hit_type"], hits[0]["text"])
                if signature in seen_evidence:
                    continue
                seen_evidence.add(signature)
        print_session_row(
            row,
            verbose=args.verbose,
            conn=conn,
            terms=terms,
            types=types,
            show_hits=args.show_hits,
            include_inherited=args.include_inherited,
        )


def show_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)
    print_session_row(row, verbose=True)
    if row["user_messages"]:
        print("User messages:")
        for msg in row["user_messages"].split("\n---\n")[: args.messages]:
            print(f"- {msg}")
    if row["assistant_messages"] and args.include_assistant:
        print("\nAssistant messages:")
        for msg in row["assistant_messages"].split("\n---\n")[: args.messages]:
            print(f"- {msg}")


def related_sessions(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    base = find_session(conn, args.session)
    if not base:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)
    tags = [tag.strip() for tag in display_tags(base).split(",") if tag.strip()]
    project = base["project"]
    session_id = base["session_id"]
    thread_name = base["thread_name"]
    params: list[Any] = [base["path"]]
    clauses: list[str] = ["path <> ?"]
    score_bits: list[str] = []
    score_params: list[Any] = []
    if session_id:
        score_bits.append("CASE WHEN session_id = ? THEN 30 ELSE 0 END")
        score_params.append(session_id)
    if thread_name:
        score_bits.append("CASE WHEN thread_name = ? THEN 20 ELSE 0 END")
        score_params.append(thread_name)
    if project:
        score_bits.append("CASE WHEN project = ? THEN 1 ELSE 0 END")
        score_params.append(project)
    for tag in tags[:6]:
        score_bits.append(
            "CASE WHEN lower(coalesce(tags,'') || ',' || coalesce(llm_tags,'')) LIKE ? THEN 3 ELSE 0 END"
        )
        score_params.append(f"%{tag.lower()}%")
    if not score_bits:
        score_bits.append("0")
    score_sql = " + ".join(score_bits)
    rows = conn.execute(
        f"""
        SELECT *, ({score_sql}) AS score
        FROM sessions
        WHERE {' AND '.join(clauses)}
        ORDER BY score DESC, coalesce(updated_at, started_at) DESC
        LIMIT ?
        """,
        [*score_params, *params, args.limit],
    ).fetchall()
    print("Base session:")
    print_session_row(base)
    print("Related sessions:")
    for row in rows:
        if row["score"] <= 1:
            continue
        print(f"score={row['score']}")
        print_session_row(row)


def find_work(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    terms = [term for term in getattr(args, "query", []) if term.strip()]
    for value in getattr(args, "file", []) or []:
        terms.append(value)
    for value in getattr(args, "commit", []) or []:
        terms.append(value)
    if not terms:
        print("Provide query terms, --file, or --commit.", file=sys.stderr)
        raise SystemExit(2)

    types = hit_type_filter(args) or ["command", "commit", "commit_created", "patch", "file", "assistant_final"]
    where = [f"h.hit_type IN ({', '.join('?' for _ in types)})"]
    params: list[Any] = [*types]
    if not args.include_inherited:
        where.append("coalesce(h.is_inherited, 0) = 0")
    for term in terms:
        where.append("lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?")
        params.append(f"%{term.lower()}%")
    if args.repo:
        where.append(
            "(lower(coalesce(s.cwd,'')) LIKE ? OR lower(coalesce(s.path,'')) LIKE ? OR lower(coalesce(s.files,'')) LIKE ?)"
        )
        repo_like = f"%{args.repo.lower()}%"
        params.extend([repo_like, repo_like, repo_like])

    score_sql = """
        CASE h.hit_type
            WHEN 'commit_created' THEN 50
            WHEN 'patch' THEN 40
            WHEN 'file' THEN 34
            WHEN 'command' THEN 28
            WHEN 'commit' THEN 26
            WHEN 'assistant_final' THEN 18
            ELSE 5
        END
    """
    rows = conn.execute(
        f"""
        SELECT s.*, max({score_sql}) AS score, count(h.id) AS evidence_count
        FROM session_hits h
        JOIN sessions s ON s.path = h.session_path
        WHERE {' AND '.join(where)}
        GROUP BY s.path
        ORDER BY score DESC, evidence_count DESC, coalesce(s.updated_at, s.started_at) DESC
        LIMIT ?
        """,
        [*params, args.limit],
    ).fetchall()
    if not rows:
        print("No matching work sessions. Try fewer terms or run `csl index --force`.")
        return
    seen_evidence: set[tuple[str, str]] = set()
    for row in rows:
        if not args.no_dedupe:
            hits = matching_hits(
                conn,
                row["path"],
                terms,
                types=types,
                limit=1,
                include_inherited=args.include_inherited,
            )
            if hits:
                signature = (hits[0]["hit_type"], hits[0]["text"])
                if signature in seen_evidence:
                    continue
                seen_evidence.add(signature)
        print_session_row(
            row,
            verbose=args.verbose,
            conn=conn,
            terms=terms,
            types=types,
            show_hits=True,
            include_inherited=args.include_inherited,
        )


def patches_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    terms = [term for term in getattr(args, "query", []) if term.strip()]
    terms.extend(value for value in getattr(args, "contains", []) or [] if value.strip())
    where = ["h.hit_type = 'patch'"]
    params: list[Any] = []
    if not args.include_inherited:
        where.append("coalesce(h.is_inherited, 0) = 0")
    if args.session:
        session_like = f"%{args.session}%"
        where.append("(s.session_id LIKE ? OR s.path LIKE ? OR s.thread_name LIKE ?)")
        params.extend([session_like, session_like, session_like])
    if args.repo:
        repo_like = f"%{args.repo.lower()}%"
        where.append(
            "(lower(coalesce(s.cwd,'')) LIKE ? OR lower(coalesce(s.path,'')) LIKE ? OR lower(coalesce(s.files,'')) LIKE ?)"
        )
        params.extend([repo_like, repo_like, repo_like])
    for file_term in args.file or []:
        file_like = f"%{file_term.lower()}%"
        where.append(
            "EXISTS (SELECT 1 FROM session_hits f WHERE f.session_path = s.path AND f.hit_type = 'file' "
            "AND lower(f.text || ' ' || coalesce(f.detail,'')) LIKE ?)"
        )
        params.append(file_like)
    for term in terms:
        where.append("lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?")
        params.append(f"%{term.lower()}%")

    rows = conn.execute(
        f"""
        SELECT
            s.session_id AS session_id,
            s.thread_name AS thread_name,
            s.summary AS summary,
            s.llm_summary AS llm_summary,
            s.cwd AS cwd,
            s.path AS path,
            s.updated_at AS updated_at,
            s.started_at AS started_at,
            h.line_no AS line_no,
            h.text AS patch_text
        FROM session_hits h
        JOIN sessions s ON s.path = h.session_path
        WHERE {' AND '.join(where)}
        ORDER BY coalesce(s.updated_at, s.started_at) DESC, h.line_no
        LIMIT ?
        """,
        [*params, args.limit],
    ).fetchall()
    if not rows:
        print("No matching patches. Try fewer --contains terms, remove --file, or run `csl index --force`.")
        return

    for row in rows:
        print(f"{row['updated_at'] or row['started_at']}  {row['session_id']}  line={row['line_no']}")
        print(f"  {row['llm_summary'] or row['summary'] or row['thread_name'] or '(no summary)'}")
        if row["cwd"]:
            print(f"  cwd: {row['cwd']}")
        print(f"  path: {row['path']}")
        warnings = patch_change_warnings(
            [{"hit_type": "patch", "text": row["patch_text"]}],  # type: ignore[list-item]
            terms,
        )
        if warnings:
            print("  warnings:")
            for warning in warnings:
                print(f"    - {warning}")
        print(f"  patch: {one_line(row['patch_text'], args.width)}")
        print()


def pinpoint_sessions(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    terms = [term for term in getattr(args, "query", []) if term.strip()]
    required_terms = list(terms)
    required_terms.extend(value for value in getattr(args, "file", []) or [] if value.strip())
    required_terms.extend(value for value in getattr(args, "commit", []) or [] if value.strip())
    if not required_terms and not args.repo:
        print("Provide remembered terms, --file, --commit, or --repo.", file=sys.stderr)
        raise SystemExit(2)

    where = []
    params: list[Any] = []
    if not args.include_inherited:
        where.append("coalesce(h.is_inherited, 0) = 0")
    if required_terms:
        term_clauses = []
        for term in required_terms:
            term_clauses.append("lower(h.text || ' ' || coalesce(h.detail,'')) LIKE ?")
            params.append(f"%{term.lower()}%")
        where.append("(" + " OR ".join(term_clauses) + ")")
    if args.repo:
        repo_like = f"%{args.repo.lower()}%"
        where.append(
            "(lower(coalesce(s.cwd,'')) LIKE ? OR lower(coalesce(s.path,'')) LIKE ? OR lower(coalesce(s.files,'')) LIKE ?)"
        )
        params.extend([repo_like, repo_like, repo_like])
    sql_where = " AND ".join(where) if where else "1 = 1"
    hit_rows = conn.execute(
        f"""
        SELECT
            s.path AS session_path,
            h.hit_type AS hit_type,
            h.text AS hit_text,
            h.detail AS hit_detail,
            s.*
        FROM session_hits h
        JOIN sessions s ON s.path = h.session_path
        WHERE {sql_where}
        ORDER BY coalesce(s.updated_at, s.started_at) DESC
        LIMIT 5000
        """,
        params,
    ).fetchall()

    by_path: dict[str, dict[str, Any]] = {}
    for hit in hit_rows:
        path = hit["session_path"]
        candidate = by_path.setdefault(
            path,
            {
                "row": hit,
                "score": 0,
                "coverage": set(),
                "types": Counter(),
                "hits": [],
            },
        )
        text = f"{hit['hit_text']} {hit['hit_detail'] or ''}"
        covered = matched_terms(text, required_terms)
        candidate["coverage"].update(covered)
        candidate["types"][hit["hit_type"]] += 1
        candidate["score"] += hit_weight(hit["hit_type"]) + (len(covered) * 10)
        candidate["hits"].append(hit)

    candidates: list[dict[str, Any]] = []
    for candidate in by_path.values():
        row = candidate["row"]
        coverage = candidate["coverage"]
        if args.require_all and required_terms and len(coverage) < len(set(required_terms)):
            continue
        if args.file:
            file_blob = f"{row['files']}\n{row['commands']}".lower()
            if not all(value.lower() in file_blob or value.lower() in " ".join(h["hit_text"].lower() for h in candidate["hits"]) for value in args.file):
                continue
        if args.commit:
            hit_blob = " ".join(h["hit_text"].lower() for h in candidate["hits"])
            if not all(value.lower() in hit_blob for value in args.commit):
                continue
        work_bonus = 0
        types = candidate["types"]
        if types["patch"] and types["command"]:
            work_bonus += 40
        if types["patch"] and types["assistant_final"]:
            work_bonus += 30
        if types["commit_created"]:
            work_bonus += 60
        candidate["score"] += work_bonus + (len(coverage) * 35)
        candidates.append(candidate)

    if not candidates:
        print("No pinpoint candidates found.")
        print_pinpoint_questions(required_terms, repo=args.repo, no_results=True)
        return

    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        grouped.setdefault(family_key(candidate["row"]), []).append(candidate)
    groups = []
    for items in grouped.values():
        items.sort(key=lambda item: (item["score"], item["row"]["updated_at"] or item["row"]["started_at"]), reverse=True)
        groups.append(items)
    groups.sort(key=lambda items: (items[0]["score"], items[0]["row"]["updated_at"] or items[0]["row"]["started_at"]), reverse=True)

    shown = 0
    for group in groups:
        if shown >= args.limit:
            break
        best = group[0]
        row = best["row"]
        coverage = sorted(best["coverage"])
        missing = sorted(set(required_terms) - set(coverage))
        confidence = "high" if len(missing) == 0 and best["score"] >= 120 else "medium" if len(coverage) >= max(1, len(set(required_terms)) // 2) else "low"
        print(f"{row['updated_at'] or row['started_at']}  {row['session_id']}  confidence={confidence} score={best['score']}")
        if len(group) > 1:
            siblings = ", ".join(item["row"]["session_id"] for item in group[1:4])
            print(f"  family: {len(group)} related rollout files" + (f"; alternates: {siblings}" if siblings else ""))
        print(f"  {display_summary(row)}")
        if row["cwd"]:
            print(f"  cwd: {row['cwd']}")
        print(f"  path: {row['path']}")
        print(f"  covered: {', '.join(coverage) if coverage else '(none)'}")
        if missing:
            print(f"  missing: {', '.join(missing)}")
        evidence_hits = evidence_hits_any(
            conn,
            row["path"],
            required_terms,
            include_inherited=args.include_inherited,
            limit=max(args.evidence, 12),
        )
        warnings = patch_change_warnings(evidence_hits, required_terms)
        if warnings:
            print("  warnings:")
            for warning in warnings[:4]:
                print(f"    - {warning}")
        print("  trace:")
        for hit in evidence_hits[: args.evidence]:
            location = []
            if hit["turn_index"] is not None:
                location.append(f"turn {hit['turn_index']}")
            if hit["line_no"] is not None:
                location.append(f"line {hit['line_no']}")
            suffix = f" ({', '.join(location)})" if location else ""
            print(f"    [{hit['hit_type']}]{suffix} {one_line(hit['text'], args.width)}")
        print()
        shown += 1

    if args.ask or groups[0][0]["score"] < 160 or len(groups) > 3:
        print_pinpoint_questions(required_terms, repo=args.repo, no_results=False)


def print_pinpoint_questions(terms: list[str], *, repo: str | None = None, no_results: bool = False) -> None:
    print("Clarifying questions to ask before the next search:")
    if no_results:
        print("- Do you remember the approximate day/week/month when this happened?")
    else:
        print("- Is the top candidate the right project/thread family, or should I search a different time window?")
    if not repo:
        print("- Which repo or folder was open when the work happened?")
    print("- Was there an actual patch/commit/terminal command, or was it only a discussion?")
    print("- Do you remember one exact file path, error string, setting name, branch name, or commit message?")
    if terms:
        print(f"- Which of these clues is most reliable: {', '.join(terms[:8])}?")


def why_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)
    terms = [term for term in getattr(args, "query", []) if term.strip()]
    types = hit_type_filter(args)
    print_session_row(row, verbose=True)
    print_session_identity(Path(row["path"]), requested=args.session)
    print()
    if not terms:
        print("No query terms provided; showing strongest artifact evidence.")
    hits = matching_hits(
        conn,
        row["path"],
        terms,
        types=types or None,
        limit=args.limit,
        include_inherited=args.include_inherited,
    )
    if not hits:
        print("No matching indexed evidence hits for this session.")
        return
    print("Evidence:")
    for hit in hits:
        print(f"- [{hit['hit_type']}] line={hit['line_no']} turn={hit['turn_index']} {one_line(hit['text'], args.width)}")


def stats(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    total = conn.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()["count"]
    forks = conn.execute("SELECT COUNT(*) AS count FROM sessions WHERE has_fork_hint = 1").fetchone()["count"]
    summarized = conn.execute("SELECT COUNT(*) AS count FROM sessions WHERE llm_summary IS NOT NULL AND llm_summary <> ''").fetchone()["count"]
    print(f"sessions: {total}")
    print(f"fork hints: {forks}")
    print(f"durable summaries: {summarized}")
    print("\nProjects:")
    for row in conn.execute(
        "SELECT coalesce(nullif(project,''),'(unknown)') AS project, COUNT(*) AS count "
        "FROM sessions GROUP BY project ORDER BY count DESC, project LIMIT ?",
        (args.limit,),
    ):
        print(f"- {row['project']}: {row['count']}")
    print("\nTop tags:")
    tag_counter: Counter[str] = Counter()
    for row in conn.execute("SELECT tags, llm_tags FROM sessions"):
        tag_text = row["llm_tags"] or row["tags"] or ""
        tag_counter.update(tag.strip() for tag in tag_text.split(",") if tag.strip())
    for tag, count in tag_counter.most_common(args.limit):
        print(f"- {tag}: {count}")


def summarize_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)

    payload = {
        "task": "Summarize this Codex session for a durable local session index.",
        "output_contract": {
            "summary": "One concise sentence describing what the session was about and the outcome when clear.",
            "tags": "5-10 comma-separated tags useful for future search.",
            "notes": "Optional caveats, follow-ups, or fork relationship observations.",
        },
        "session": {
            "session_id": row["session_id"],
            "thread_name": row["thread_name"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "cwd": row["cwd"],
            "path": row["path"],
            "heuristic_summary": row["summary"],
            "heuristic_tags": row["tags"],
            "existing_summary": row["llm_summary"] or "",
            "existing_tags": row["llm_tags"] or "",
            "first_user_message": row["first_user_message"],
            "last_user_message": row["last_user_message"],
            "files": row["files"].splitlines()[: args.files],
            "commands": row["commands"].splitlines()[: args.commands],
            "user_messages": row["user_messages"].split("\n---\n")[: args.messages],
            "assistant_messages": row["assistant_messages"].split("\n---\n")[: args.assistant_messages],
        },
        "save_command_template": (
            "./codex_sessions.py annotate "
            f"{row['session_id']} --summary '<summary>' --tags '<tag1, tag2>' --notes '<optional notes>'"
        ),
    }

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print("Summarize this Codex session for the local session index.")
    print("Return: summary, tags, notes. Then persist it with the save command template.\n")
    print(f"Session: {payload['session']['session_id']}")
    print(f"Thread: {payload['session']['thread_name']}")
    print(f"Updated: {payload['session']['updated_at']}")
    print(f"CWD: {payload['session']['cwd']}")
    print(f"Path: {payload['session']['path']}")
    print(f"Heuristic summary: {payload['session']['heuristic_summary']}")
    print(f"Heuristic tags: {payload['session']['heuristic_tags']}")
    if payload["session"]["existing_summary"]:
        print(f"Existing durable summary: {payload['session']['existing_summary']}")
    print("\nUser messages:")
    for msg in payload["session"]["user_messages"]:
        print(f"- {msg}")
    if payload["session"]["commands"]:
        print("\nCommands:")
        for cmd in payload["session"]["commands"]:
            print(f"- {cmd}")
    if payload["session"]["files"]:
        print("\nFiles:")
        for file_path in payload["session"]["files"]:
            print(f"- {file_path}")
    if payload["session"]["assistant_messages"]:
        print("\nAssistant messages:")
        for msg in payload["session"]["assistant_messages"]:
            print(f"- {msg}")
    print("\nSave command template:")
    print(payload["save_command_template"])


def annotate_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)
    if args.clear:
        conn.execute(
            "UPDATE sessions SET llm_summary = NULL, llm_tags = NULL, notes = NULL, summarized_at = NULL WHERE path = ?",
            (row["path"],),
        )
        conn.commit()
        print(f"cleared annotation for {row['session_id']}")
        return
    if not args.summary and not args.tags and not args.notes:
        print("Nothing to update. Provide --summary, --tags, --notes, or --clear.", file=sys.stderr)
        raise SystemExit(2)
    conn.execute(
        """
        UPDATE sessions
        SET llm_summary = coalesce(?, llm_summary),
            llm_tags = coalesce(?, llm_tags),
            notes = coalesce(?, notes),
            summarized_at = ?
        WHERE path = ?
        """,
        (args.summary, args.tags, args.notes, utc_now(), row["path"]),
    )
    conn.commit()
    updated = find_session(conn, row["session_id"])
    if updated:
        print_session_row(updated)


def summarize_queue(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    rows = conn.execute(
        """
        SELECT * FROM sessions
        WHERE coalesce(llm_summary, '') = ''
        ORDER BY has_fork_hint DESC, coalesce(updated_at, started_at) DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for row in rows:
        print_session_row(row)


def compact_block(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def session_messages(path: Path) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "response_item":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = extract_text_from_content(payload.get("content"))
            if not text or (role == "user" and is_noise_user_text(text)):
                continue
            if messages and messages[-1]["role"] == role and messages[-1]["text"].strip() == text.strip():
                continue
            messages.append({"role": str(role), "text": text.strip()})
    return messages


def session_commands(path: Path) -> list[str]:
    commands: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("type") != "function_call":
                continue
            _, command = parse_function_call(payload)
            if command:
                commands.append(command)
    return commands


def session_meta_rows(path: Path) -> list[dict[str, str]]:
    metas: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "session_meta":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            metas.append(
                {
                    "line": str(line_no),
                    "id": str(payload.get("id") or ""),
                    "forked_from_id": str(payload.get("forked_from_id") or ""),
                    "cwd": str(payload.get("cwd") or ""),
                    "timestamp": str(payload.get("timestamp") or row.get("timestamp") or ""),
                }
            )
    return metas


def session_identity(path: Path, requested: str = "") -> dict[str, Any]:
    metas = session_meta_rows(path)
    ids = [meta["id"] for meta in metas if meta["id"]]
    filename_id = file_id_from_path(path)
    return {
        "requested": requested,
        "filename_id": filename_id,
        "first_meta_id": ids[0] if ids else "",
        "last_meta_id": ids[-1] if ids else "",
        "meta_count": len(metas),
        "metas": metas,
    }


def print_session_identity(path: Path, requested: str = "") -> None:
    identity = session_identity(path, requested=requested)
    print("Identity:")
    if requested:
        print(f"  requested: {requested}")
    print(f"  filename id: {identity['filename_id'] or '(none)'}")
    print(f"  first meta id: {identity['first_meta_id'] or '(none)'}")
    print(f"  last meta id: {identity['last_meta_id'] or '(none)'}")
    print(f"  session_meta rows: {identity['meta_count']}")
    if identity["meta_count"] > 1:
        for meta in identity["metas"]:
            fork = f" forked_from={meta['forked_from_id']}" if meta["forked_from_id"] else ""
            print(f"    line {meta['line']}: id={meta['id']}{fork} cwd={meta['cwd']}")


def timeline_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)
    messages = session_messages(Path(row["path"]))
    print(f"Session: {row['session_id']}")
    print(f"Path: {row['path']}")
    print_session_identity(Path(row["path"]), requested=args.session)
    print(f"Messages: {len(messages)}\n")
    for idx, message in enumerate(messages, start=1):
        role = message["role"]
        text = compact_block(message["text"], args.width)
        print(f"{idx:03d} {role}: {text}")


def cut_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)
    messages = session_messages(Path(row["path"]))
    if not messages:
        print(f"No user/assistant messages found for: {row['path']}", file=sys.stderr)
        raise SystemExit(1)
    to_index = args.to if args.to is not None else len(messages)
    if to_index < 1 or to_index > len(messages):
        print(f"--to must be between 1 and {len(messages)}", file=sys.stderr)
        raise SystemExit(2)

    selected = messages[:to_index]
    commands = session_commands(Path(row["path"]))[: args.commands]
    suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", args.name or row["session_id"]).strip("-")
    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{suffix}-cut-{to_index:03d}.md"

    lines = [
        "# Codex Session Cut",
        "",
        "Use this as the starting context for a new Codex session. It intentionally excludes later turns from the original conversation.",
        "",
        f"- Source session: `{row['session_id']}`",
        f"- Source path: `{row['path']}`",
        f"- Thread name: `{row['thread_name']}`",
        f"- CWD: `{row['cwd']}`",
        f"- Cut through message: `{to_index}` of `{len(messages)}`",
        f"- Created at: `{utc_now()}`",
        "",
        "## Durable Summary",
        "",
        display_summary(row) or "(none)",
        "",
        "## Tags",
        "",
        display_tags(row) or "(none)",
        "",
        "## Conversation Up To Cut",
        "",
    ]
    for idx, message in enumerate(selected, start=1):
        role = message["role"].title()
        lines.extend([f"### {idx:03d} {role}", "", compact_block(message["text"], args.message_chars), ""])
    if commands:
        lines.extend(["## Early Commands", ""])
        for command in commands:
            lines.append(f"- `{compact_block(command, args.command_chars)}`")
        lines.append("")
    if args.next_prompt:
        lines.extend(["## Next Task", "", args.next_prompt.strip(), ""])

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote: {output_path}")
    print("start a fresh Codex session from this cut with:")
    print(f"codex -C {row['cwd'] or os.getcwd()} \"$(cat {output_path})\"")


def line_contains_user_text(line: str, marker: str) -> bool:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return False
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    marker_lower = marker.lower()
    if payload.get("type") == "user_message":
        return marker_lower in str(payload.get("message") or "").lower()
    if payload.get("type") == "message" and payload.get("role") == "user":
        text = extract_text_from_content(payload.get("content"))
        return marker_lower in text.lower()
    return False


def rewrite_session_meta(line: str, new_id: str, source_id: str, now: str) -> str:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return line
    if row.get("type") != "session_meta":
        return line
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return line
    payload["id"] = new_id
    payload["forked_from_id"] = source_id
    payload["timestamp"] = now
    row["timestamp"] = now
    return json.dumps(row, ensure_ascii=False) + "\n"


def validate_new_session_id(new_id: str) -> None:
    try:
        uuid.UUID(new_id)
    except ValueError:
        print(f"Invalid UUID for --new-id: {new_id}", file=sys.stderr)
        raise SystemExit(2)


def session_id_exists(session_id: str) -> bool:
    if session_id in load_thread_names(DEFAULT_SESSION_INDEX):
        return True
    if not DEFAULT_SESSIONS_DIR.exists():
        return False
    return any(DEFAULT_SESSIONS_DIR.rglob(f"*{session_id}.jsonl"))


def target_session_path(new_id: str, now_dt: dt.datetime) -> Path:
    target_dir = DEFAULT_SESSIONS_DIR / f"{now_dt.year:04d}" / f"{now_dt.month:02d}" / f"{now_dt.day:02d}"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / f"rollout-{now_dt.strftime('%Y-%m-%dT%H-%M-%S')}-{new_id}.jsonl"


def register_native_session(conn: sqlite3.Connection, target_path: Path, new_id: str, thread_name: str, now: str) -> None:
    index_row = {
        "id": new_id,
        "thread_name": thread_name,
        "updated_at": now,
    }
    DEFAULT_SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_SESSION_INDEX.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(index_row, ensure_ascii=False) + "\n")

    # Reindex the single new session. This updates csl's DB, not Codex internals.
    thread_names = load_thread_names(DEFAULT_SESSION_INDEX)
    upsert_session(conn, parse_session(target_path, thread_names))
    conn.commit()


def native_cut_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)

    source_path = Path(row["path"])
    source_id = row["session_id"]
    marker = args.before_text
    original_lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    cutoff = None
    for idx, line in enumerate(original_lines):
        if line_contains_user_text(line, marker):
            cutoff = idx
            break
    if cutoff is None:
        print(f"Marker text not found in {source_path}", file=sys.stderr)
        raise SystemExit(1)
    if cutoff < 2:
        print("Cutoff is too early to create a usable session.", file=sys.stderr)
        raise SystemExit(2)

    new_id = args.new_id or str(uuid.uuid4())
    validate_new_session_id(new_id)
    if session_id_exists(new_id):
        print(f"Refusing to reuse existing session id: {new_id}", file=sys.stderr)
        raise SystemExit(1)
    now_dt = dt.datetime.now(dt.UTC)
    now = now_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    target_path = target_session_path(new_id, now_dt)

    if target_path.exists():
        print(f"Refusing to overwrite existing file: {target_path}", file=sys.stderr)
        raise SystemExit(1)

    output_lines: list[str] = []
    for line in original_lines[:cutoff]:
        output_lines.append(rewrite_session_meta(line, new_id, source_id, now))
    target_path.write_text("".join(output_lines), encoding="utf-8")
    register_native_session(conn, target_path, new_id, args.name, now)

    print(f"created native cut session: {new_id}")
    print(f"path: {target_path}")
    print(f"source: {source_id}")
    print(f"cut before marker: {marker}")
    print("try opening with:")
    print(f"codex resume --all {new_id}")


def native_clone_session(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)

    source_path = Path(row["path"])
    source_id = row["session_id"]
    original_lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    if len(original_lines) < 2:
        print(f"Source session is too small to clone: {source_path}", file=sys.stderr)
        raise SystemExit(2)

    new_id = args.new_id or str(uuid.uuid4())
    validate_new_session_id(new_id)
    if session_id_exists(new_id):
        print(f"Refusing to reuse existing session id: {new_id}", file=sys.stderr)
        raise SystemExit(1)

    now_dt = dt.datetime.now(dt.UTC)
    now = now_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    target_path = target_session_path(new_id, now_dt)
    if target_path.exists():
        print(f"Refusing to overwrite existing file: {target_path}", file=sys.stderr)
        raise SystemExit(1)

    thread_name = args.name or f"{row['thread_name'] or source_id} clone"
    output_lines = [rewrite_session_meta(line, new_id, source_id, now) for line in original_lines]
    target_path.write_text("".join(output_lines), encoding="utf-8")
    register_native_session(conn, target_path, new_id, thread_name, now)

    print(f"created native clone session: {new_id}")
    print(f"path: {target_path}")
    print(f"source: {source_id}")
    print(f"source path: {source_path}")
    print("try opening with:")
    print(f"codex resume --all {new_id}")


def strip_encrypted_reasoning(value: Any) -> tuple[Any, int]:
    """Remove unverifiable reasoning items while preserving replayable history."""
    if isinstance(value, list):
        cleaned: list[Any] = []
        removed = 0
        for item in value:
            if (
                isinstance(item, dict)
                and str(item.get("type") or "").lower() == "reasoning"
                and "encrypted_content" in item
            ):
                removed += 1
                continue
            cleaned_item, item_removed = strip_encrypted_reasoning(item)
            cleaned.append(cleaned_item)
            removed += item_removed
        return cleaned, removed

    if isinstance(value, dict):
        cleaned_dict: dict[str, Any] = {}
        removed = 0
        for key, item in value.items():
            cleaned_item, item_removed = strip_encrypted_reasoning(item)
            cleaned_dict[key] = cleaned_item
            removed += item_removed
        return cleaned_dict, removed

    return value, 0


def native_sanitize_session(args: argparse.Namespace) -> None:
    """Clone a session without encrypted reasoning, adding no conversation turns."""
    conn = init_db(args.db.expanduser())
    row = find_session(conn, args.session)
    if not row:
        print(f"No session matched: {args.session}", file=sys.stderr)
        raise SystemExit(1)

    source_path = Path(row["path"])
    source_id = row["session_id"]
    new_id = args.new_id or str(uuid.uuid4())
    validate_new_session_id(new_id)
    if session_id_exists(new_id):
        print(f"Refusing to reuse existing session id: {new_id}", file=sys.stderr)
        raise SystemExit(1)

    now_dt = dt.datetime.now(dt.UTC)
    now = now_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    target_path = target_session_path(new_id, now_dt)
    if target_path.exists():
        print(f"Refusing to overwrite existing file: {target_path}", file=sys.stderr)
        raise SystemExit(1)

    output_lines: list[str] = []
    removed_reasoning = 0
    rewritten_meta = False
    source_lines = 0

    with source_path.open("r", encoding="utf-8", errors="replace") as source:
        for line in source:
            source_lines += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                output_lines.append(line)
                continue

            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            if (
                record.get("type") == "response_item"
                and str(payload.get("type") or "").lower() == "reasoning"
                and "encrypted_content" in payload
            ):
                removed_reasoning += 1
                continue

            cleaned_record, nested_removed = strip_encrypted_reasoning(record)
            removed_reasoning += nested_removed

            if cleaned_record.get("type") == "session_meta" and not rewritten_meta:
                cleaned_payload = cleaned_record.get("payload")
                if isinstance(cleaned_payload, dict) and cleaned_payload.get("id") == source_id:
                    cleaned_payload["id"] = new_id
                    cleaned_payload["forked_from_id"] = source_id
                    cleaned_payload["timestamp"] = now
                    cleaned_record["timestamp"] = now
                    rewritten_meta = True

            output_lines.append(json.dumps(cleaned_record, ensure_ascii=False) + "\n")

    if not rewritten_meta:
        print(f"Source session metadata was not found in {source_path}", file=sys.stderr)
        raise SystemExit(2)

    target_path.write_text("".join(output_lines), encoding="utf-8")
    thread_name = args.name or row["thread_name"] or source_id
    register_native_session(conn, target_path, new_id, thread_name, now)

    print(f"created sanitized native session: {new_id}")
    print(f"path: {target_path}")
    print(f"source: {source_id}")
    print(f"source lines: {source_lines}")
    print(f"output lines: {len(output_lines)}")
    print(f"encrypted reasoning items removed: {removed_reasoning}")
    print("conversation turns added: 0")
    print("try opening with:")
    print(f"codex resume --all {new_id}")


def cockpit_role(row: sqlite3.Row) -> str:
    title = (row["thread_name"] or row["summary"] or "").strip().lower()
    if title.startswith("deprecate") or title.startswith("deprecated"):
        return "DEPR"
    if "contaminated" in title or "corrupta" in title or "mixed original" in title:
        return "MIXD"
    if title.startswith("main ") or " / main " in title or "main implementation" in title:
        return "MAIN"
    if title.startswith("side ") or "side branch" in title:
        return "SIDE"
    if title.startswith("cont ") or "continuation" in title:
        return "CONT"
    if row["has_fork_hint"]:
        return "FORK"
    return "SESS"


def cockpit_topic(row: sqlite3.Row) -> str:
    title = display_summary(row)
    parts = [part.strip() for part in title.split("/") if part.strip()]
    if len(parts) >= 2:
        project = re.sub(r"\s+LU\s+\d{2}-\d{2}-\d{2}.*$", "", parts[0]).strip()
        area = parts[1]
        if project and area:
            return f"{project} / {area}"
    if parts:
        return re.sub(r"\s+LU\s+\d{2}-\d{2}-\d{2}.*$", "", parts[0]).strip() or "(untitled)"
    if row["project"]:
        return str(row["project"])
    tags = [tag.strip() for tag in display_tags(row).split(",") if tag.strip()]
    return tags[0] if tags else "(untitled)"


def cockpit_rows(conn: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[Any] = []
    if args.project:
        where.append("lower(project) = lower(?)")
        params.append(args.project)
    if args.query:
        for term in args.query:
            like = f"%{term.lower()}%"
            where.append(
                "("
                "lower(coalesce(thread_name,'')) LIKE ? OR "
                "lower(coalesce(summary,'')) LIKE ? OR "
                "lower(coalesce(llm_summary,'')) LIKE ? OR "
                "lower(coalesce(tags,'')) LIKE ? OR "
                "lower(coalesce(llm_tags,'')) LIKE ? OR "
                "lower(coalesce(first_user_message,'')) LIKE ? OR "
                "lower(coalesce(last_user_message,'')) LIKE ? OR "
                "lower(coalesce(user_messages,'')) LIKE ?"
                ")"
            )
            params.extend([like] * 8)
    sql_where = " WHERE " + " AND ".join(where) if where else ""
    return list(
        conn.execute(
            f"""
            SELECT *
            FROM sessions
            {sql_where}
            ORDER BY coalesce(updated_at, started_at) DESC, path DESC
            LIMIT ?
            """,
            [*params, args.limit],
        )
    )


def cockpit_groups(rows: list[sqlite3.Row]) -> list[tuple[str, list[sqlite3.Row]]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(cockpit_topic(row), []).append(row)
    return sorted(
        grouped.items(),
        key=lambda item: max(row["updated_at"] or row["started_at"] or "" for row in item[1]),
        reverse=True,
    )


def cockpit_print(args: argparse.Namespace) -> None:
    conn = init_db(args.db.expanduser())
    groups = cockpit_groups(cockpit_rows(conn, args))
    for topic, rows in groups:
        print(topic)
        for row in rows:
            role = cockpit_role(row)
            print(f"  {role:<4} {row['session_id']}  {row['updated_at'] or row['started_at']}")
            print(f"       {one_line(display_summary(row), 120)}")
        print()


def cockpit_mark_session(row: sqlite3.Row, marker: str) -> str:
    current = row["thread_name"] or display_summary(row) or row["session_id"]
    cleaned = re.sub(r"^(MAIN|SIDE|CONT|DEPRECATE|DEPRECATED)\s+", "", current, flags=re.IGNORECASE).strip()
    if marker == "DEPRECATE":
        new_name = f"DEPRECATE {cleaned}"
    else:
        new_name = f"{marker} {cleaned}"
    now = dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    DEFAULT_SESSION_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_SESSION_INDEX.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"id": row["session_id"], "thread_name": new_name, "updated_at": now},
                ensure_ascii=False,
            )
            + "\n"
        )
    return new_name


def curses_addnstr(win: Any, y: int, x: int, text: str, width: int, attr: int = 0) -> None:
    if y < 0 or x < 0 or width <= 0:
        return
    try:
        win.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def cockpit_draw(
    stdscr: Any,
    groups: list[tuple[str, list[sqlite3.Row]]],
    group_idx: int,
    session_idx: int,
    status: str,
    focus: int,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    pane_w = max(20, (width - 4) // 3)
    left_w = pane_w
    mid_w = pane_w
    detail_w = max(0, width - left_w - mid_w - 4)
    detail_x = left_w + mid_w + 4
    detail_w = max(0, width - detail_x - 1)
    title_attr = curses.A_BOLD
    selected_attr = curses.A_REVERSE
    inactive_selected_attr = curses.A_BOLD

    curses_addnstr(stdscr, 0, 0, "CSL Cockpit", width - 1, title_attr)
    curses_addnstr(
        stdscr,
        height - 1,
        0,
        "q quit | Left/Right focus | Up/Down move | Enter details/open | Esc close details | / filter | a resume --all | m/s/d mark",
        width - 1,
        curses.A_DIM,
    )
    if status:
        curses_addnstr(stdscr, height - 2, 0, status, width - 1, curses.A_DIM)

    topic_title_attr = title_attr | (curses.A_UNDERLINE if focus == 0 else 0)
    session_title_attr = title_attr | (curses.A_UNDERLINE if focus == 1 else 0)
    curses_addnstr(stdscr, 2, 0, "Topics", left_w - 1, topic_title_attr)
    for line_no, (topic, rows) in enumerate(groups[: max(0, height - 5)], start=3):
        if line_no - 3 == group_idx:
            attr = selected_attr if focus == 0 else inactive_selected_attr
        else:
            attr = 0
        label = f"{topic} ({len(rows)})"
        curses_addnstr(stdscr, line_no, 0, label, left_w - 1, attr)

    rows = groups[group_idx][1] if groups else []
    mid_x = left_w + 2
    curses_addnstr(stdscr, 2, mid_x, "Sessions", mid_w - 1, session_title_attr)
    for offset, row in enumerate(rows[: max(0, height - 5)]):
        y = 3 + offset
        if offset == session_idx:
            attr = selected_attr if focus == 1 else inactive_selected_attr
        else:
            attr = 0
        updated = (row["updated_at"] or row["started_at"] or "")[:10]
        label = f"{cockpit_role(row):<4} {row['session_id'][:8]} {updated} {one_line(display_summary(row), max(12, mid_w - 27))}"
        curses_addnstr(stdscr, y, mid_x, label, mid_w - 1, attr)

    if rows and detail_w > 8:
        row = rows[min(session_idx, len(rows) - 1)]
        details = [
            "Details",
            f"role: {cockpit_role(row)}",
            f"id: {row['session_id']}",
            f"updated: {row['updated_at'] or row['started_at']}",
            f"cwd: {row['cwd'] or '(none)'}",
            f"path: {row['path']}",
            "",
            "title:",
            one_line(display_summary(row), 300),
            "",
            "first:",
            one_line(row["first_user_message"] or "", 300),
            "",
            "last:",
            one_line(row["last_user_message"] or "", 300),
        ]
        if row["files"]:
            details.extend(["", "files:"])
            details.extend(row["files"].splitlines()[:5])
        for y, line in enumerate(details[: max(0, height - 4)], start=2):
            attr = title_attr if y == 2 else 0
            curses_addnstr(stdscr, y, detail_x, line, detail_w, attr)
    stdscr.refresh()


def cockpit_popup(stdscr: Any, title: str, lines: list[str], *, enter_action: str = "close") -> str:
    height, width = stdscr.getmaxyx()
    popup_h = max(8, min(height - 4, 18))
    popup_w = max(40, min(width - 4, width * 4 // 5))
    y0 = max(1, (height - popup_h) // 2)
    x0 = max(1, (width - popup_w) // 2)
    win = curses.newwin(popup_h, popup_w, y0, x0)
    win.keypad(True)
    offset = 0
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        chunk_width = max(10, popup_w - 4)
        text = line
        while len(text) > chunk_width:
            wrapped.append(text[:chunk_width])
            text = text[chunk_width:]
        wrapped.append(text)
    try:
        while True:
            win.erase()
            win.box()
            curses_addnstr(win, 0, 2, f" {title} ", popup_w - 4, curses.A_BOLD)
            visible = wrapped[offset : offset + popup_h - 4]
            for idx, line in enumerate(visible, start=2):
                curses_addnstr(win, idx, 2, line, popup_w - 4)
            footer = "Enter open | Esc/q close | Up/Down scroll" if enter_action == "open" else "Enter/Esc/q close | Up/Down scroll"
            curses_addnstr(win, popup_h - 2, 2, footer, popup_w - 4, curses.A_DIM)
            win.refresh()
            key = win.getch()
            if key == ord("\n"):
                return enter_action
            if key in {27, ord("q")}:
                return "close"
            if key in {curses.KEY_DOWN, ord("j")}:
                offset = min(max(0, len(wrapped) - (popup_h - 4)), offset + 1)
            elif key in {curses.KEY_UP, ord("k")}:
                offset = max(0, offset - 1)
    except KeyboardInterrupt:
        return "close"


def cockpit_prompt(stdscr: Any, prompt: str) -> str:
    height, width = stdscr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    try:
        stdscr.move(height - 2, 0)
        stdscr.clrtoeol()
        curses_addnstr(stdscr, height - 2, 0, prompt, width - 1)
        value = stdscr.getstr(height - 2, len(prompt), max(1, width - len(prompt) - 1))
        return value.decode("utf-8", errors="replace").strip()
    finally:
        curses.noecho()
        curses.curs_set(0)


def cockpit_tui(stdscr: Any, args: argparse.Namespace) -> list[str] | None:
    curses.curs_set(0)
    stdscr.keypad(True)
    conn = init_db(args.db.expanduser())
    query = list(args.query)
    status = ""

    def load() -> list[tuple[str, list[sqlite3.Row]]]:
        args.query = query
        return cockpit_groups(cockpit_rows(conn, args))

    groups = load()
    group_idx = 0
    session_idx = 0
    focus = 0
    try:
        while True:
            if groups:
                group_idx = max(0, min(group_idx, len(groups) - 1))
                session_idx = max(0, min(session_idx, len(groups[group_idx][1]) - 1))
            cockpit_draw(stdscr, groups, group_idx, session_idx, status, focus)
            key = stdscr.getch()
            status = ""
            if key in {ord("q"), 27}:
                return None
            if key in {curses.KEY_UP, ord("k")} and groups:
                if focus == 0:
                    group_idx = max(0, group_idx - 1)
                    session_idx = min(session_idx, len(groups[group_idx][1]) - 1)
                else:
                    session_idx = max(0, session_idx - 1)
            elif key in {curses.KEY_DOWN, ord("j")} and groups:
                if focus == 0:
                    group_idx = min(len(groups) - 1, group_idx + 1)
                    session_idx = min(session_idx, len(groups[group_idx][1]) - 1)
                else:
                    session_idx = min(len(groups[group_idx][1]) - 1, session_idx + 1)
            elif key in {curses.KEY_LEFT, ord("h")}:
                focus = 0
            elif key in {curses.KEY_RIGHT, ord("l")} and groups:
                focus = 1
                session_idx = min(session_idx, len(groups[group_idx][1]) - 1)
            elif key == ord("/"):
                value = cockpit_prompt(stdscr, "filter: ")
                query = value.split()
                groups = load()
                group_idx = 0
                session_idx = 0
                focus = 0
                status = f"filter: {value or '(none)'}"
            elif key == ord("\n") and groups:
                topic, topic_rows = groups[group_idx]
                row = topic_rows[session_idx]
                if focus == 0:
                    cockpit_popup(
                        stdscr,
                        "Expanded Topic",
                        [
                            f"Topic: {topic}",
                            f"Sessions: {len(topic_rows)}",
                            "",
                            *[
                                f"{cockpit_role(item):<4} {item['session_id']}  {one_line(display_summary(item), 220)}"
                                for item in topic_rows
                            ],
                        ],
                    )
                else:
                    action = cockpit_popup(
                        stdscr,
                        "Session Details",
                        [
                            f"Topic: {topic}",
                            f"Role: {cockpit_role(row)}",
                            f"Session: {row['session_id']}",
                            f"Updated: {row['updated_at'] or row['started_at']}",
                            f"CWD: {row['cwd'] or '(none)'}",
                            f"Path: {row['path']}",
                            "",
                            "Title:",
                            display_summary(row),
                            "",
                            "First user message:",
                            row["first_user_message"] or "(none)",
                            "",
                            "Last user message:",
                            row["last_user_message"] or "(none)",
                        ],
                        enter_action="open",
                    )
                    if action == "open":
                        return ["codex", "resume", row["session_id"]]
            elif key == ord("a"):
                return ["codex", "resume", "--all"]
            elif key in {ord("m"), ord("s"), ord("d")} and groups:
                row = groups[group_idx][1][session_idx]
                marker = "MAIN" if key == ord("m") else "SIDE" if key == ord("s") else "DEPRECATE"
                new_name = cockpit_mark_session(row, marker)
                thread_names = load_thread_names(DEFAULT_SESSION_INDEX)
                upsert_session(conn, parse_session(Path(row["path"]), thread_names))
                conn.commit()
                groups = load()
                status = f"marked {row['session_id'][:8]} as {marker}: {one_line(new_name, 100)}"
    except KeyboardInterrupt:
        return None


def cockpit(args: argparse.Namespace) -> None:
    if args.print:
        cockpit_print(args)
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("csl cockpit needs an interactive terminal. Use `csl cockpit --print` for plain output.", file=sys.stderr)
        raise SystemExit(2)
    try:
        command = curses.wrapper(cockpit_tui, args)
    except KeyboardInterrupt:
        return
    if command:
        try:
            os.execvp(command[0], command)
        except OSError as exc:
            print(f"failed to run {' '.join(command)}: {exc}", file=sys.stderr)
            raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find Codex sessions and transfer native Codex/Claude Code conversations.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    from csl_transfer import add_commands
    add_commands(sub, sys.modules[__name__])
    from csl_sharing import add_commands as add_sharing_commands
    add_sharing_commands(sub, sys.modules[__name__])
    from csl_capabilities import add_commands as add_capability_commands
    add_capability_commands(sub, sys.modules[__name__])

    p_index = sub.add_parser("index", help="Build or refresh the session index.")
    p_index.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    p_index.add_argument("--session-index", type=Path, default=DEFAULT_SESSION_INDEX)
    p_index.add_argument("--force", action="store_true", help="Re-parse all session files.")
    p_index.add_argument("--prune", action="store_true", help="Remove DB rows for deleted session files.")
    p_index.set_defaults(func=index_sessions)

    for name, help_text in [("search", "Search indexed sessions."), ("recent", "Show recent sessions.")]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("query", nargs="*", help="Search terms. All terms must match.")
        p.add_argument("--limit", type=int, default=12)
        p.add_argument("--project")
        p.add_argument("--forks", action="store_true", help="Only sessions with fork hints.")
        p.add_argument("--include-inherited", action="store_true", help="Include evidence inherited from fork ancestors.")
        p.add_argument("--turns-only", action="store_true", help="Search only real user/assistant turns.")
        p.add_argument("--artifacts", action="store_true", help="Search only commands, commits, patches, and files.")
        p.add_argument(
            "--type",
            dest="hit_type",
            action="append",
            choices=["user_turn", "assistant_turn", "assistant_final", "command", "commit", "commit_created", "patch", "file"],
            help="Restrict evidence search to one hit type. May be repeated.",
        )
        p.add_argument("--show-hits", action="store_true", help="Print matching evidence snippets.")
        p.add_argument("--no-dedupe", action="store_true", help="Do not collapse duplicate evidence across fork-chain copies.")
        p.add_argument("-v", "--verbose", action="store_true")
        p.set_defaults(func=search_sessions)

    p_show = sub.add_parser("show", help="Show one session in more detail.")
    p_show.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_show.add_argument("--messages", type=int, default=8)
    p_show.add_argument("--include-assistant", action="store_true")
    p_show.set_defaults(func=show_session)

    p_related = sub.add_parser("related", help="Find sessions related to one session.")
    p_related.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_related.add_argument("--limit", type=int, default=10)
    p_related.set_defaults(func=related_sessions)

    p_find_work = sub.add_parser("find-work", help="Find sessions where work was actually done via commits, patches, commands, or files.")
    p_find_work.add_argument("query", nargs="*", help="Search terms. All terms must match evidence.")
    p_find_work.add_argument("--file", action="append", default=[], help="File/path fragment to require. May be repeated.")
    p_find_work.add_argument("--commit", action="append", default=[], help="Commit hash/message fragment to require. May be repeated.")
    p_find_work.add_argument("--repo", help="Repo/project path fragment to prefer or require.")
    p_find_work.add_argument("--limit", type=int, default=10)
    p_find_work.add_argument("--include-inherited", action="store_true", help="Include evidence inherited from fork ancestors.")
    p_find_work.add_argument("--no-dedupe", action="store_true", help="Do not collapse duplicate evidence across fork-chain copies.")
    p_find_work.add_argument("--turns-only", action="store_true", help=argparse.SUPPRESS)
    p_find_work.add_argument("--artifacts", action="store_true", default=True, help=argparse.SUPPRESS)
    p_find_work.add_argument(
        "--type",
        dest="hit_type",
        action="append",
        choices=["assistant_final", "command", "commit", "commit_created", "patch", "file"],
        help="Restrict evidence search to one hit type. May be repeated.",
    )
    p_find_work.add_argument("-v", "--verbose", action="store_true")
    p_find_work.set_defaults(func=find_work)

    p_patches = sub.add_parser("patches", help="Search indexed apply_patch edits by file, content, repo, or session.")
    p_patches.add_argument("query", nargs="*", help="Patch content terms. All terms must match.")
    p_patches.add_argument("--file", action="append", default=[], help="File/path fragment touched by the patch. May be repeated.")
    p_patches.add_argument("--contains", action="append", default=[], help="Patch content term to require. May be repeated.")
    p_patches.add_argument("--repo", help="Repo/project path fragment to constrain results.")
    p_patches.add_argument("--session", help="Session id, path fragment, or thread name fragment.")
    p_patches.add_argument("--include-inherited", action="store_true", help="Include patches inherited from fork ancestors.")
    p_patches.add_argument("--limit", type=int, default=20)
    p_patches.add_argument("--width", type=int, default=260, help="Characters to show per patch snippet.")
    p_patches.set_defaults(func=patches_session)

    p_pinpoint = sub.add_parser(
        "pinpoint",
        help="Assertively locate the most likely work conversation, group fork duplicates, and suggest narrowing questions.",
    )
    p_pinpoint.add_argument("query", nargs="*", help="Remembered terms. Partial coverage is ranked and explained.")
    p_pinpoint.add_argument("--file", action="append", default=[], help="File/path fragment that should appear in evidence.")
    p_pinpoint.add_argument("--commit", action="append", default=[], help="Commit hash/message fragment that should appear in evidence.")
    p_pinpoint.add_argument("--repo", help="Repo/project path fragment to constrain results.")
    p_pinpoint.add_argument("--limit", type=int, default=6)
    p_pinpoint.add_argument("--evidence", type=int, default=8, help="Evidence lines to show per candidate.")
    p_pinpoint.add_argument("--width", type=int, default=180, help="Characters to show per evidence line.")
    p_pinpoint.add_argument("--include-inherited", action="store_true", help="Include evidence inherited from fork ancestors.")
    p_pinpoint.add_argument("--require-all", action="store_true", help="Only show candidates that cover every clue.")
    p_pinpoint.add_argument("--ask", action="store_true", help="Always print clarifying questions for the operator/user.")
    p_pinpoint.set_defaults(func=pinpoint_sessions)

    p_recall = sub.add_parser(
        "recall",
        help="Find fuzzy remembered conversations by clue coverage instead of exact all-term matching.",
    )
    p_recall.add_argument("query", nargs="*", help="Fuzzy remembered phrase. Words are split into clues and ranked by coverage.")
    p_recall.add_argument("--must", action="append", default=[], help="Term that must appear somewhere in the candidate session. May be repeated.")
    p_recall.add_argument("--anchor", action="append", default=[], help="Term that must appear in each evidence hit used for recall scoring. Use for core concepts such as linkedin.")
    p_recall.add_argument("--limit", type=int, default=8)
    p_recall.add_argument("--evidence", type=int, default=6, help="Evidence lines to show per candidate.")
    p_recall.add_argument("--width", type=int, default=180, help="Characters to show per evidence line.")
    p_recall.add_argument("--min-coverage", type=int, default=0, help="Minimum number of fuzzy clues a candidate must cover.")
    p_recall.add_argument("--scan-limit", type=int, default=20000, help="Maximum indexed evidence rows to scan.")
    p_recall.add_argument("--project")
    p_recall.add_argument("--include-inherited", action="store_true", help="Include evidence inherited from fork ancestors.")
    p_recall.add_argument("--turns-only", action="store_true", help="Search only real user/assistant turns. This is the default when no type flags are provided.")
    p_recall.add_argument("--artifacts", action="store_true", help="Search only commands, commits, patches, and files.")
    p_recall.add_argument("--all-types", action="store_true", help="Search every indexed evidence type.")
    p_recall.add_argument("--include-context-packages", action="store_true", help="Include generated clean-clone/cut bootstrap context in recall scoring.")
    p_recall.add_argument(
        "--type",
        dest="hit_type",
        action="append",
        choices=["user_turn", "assistant_turn", "assistant_final", "command", "commit", "commit_created", "patch", "file"],
        help="Restrict evidence search to one hit type. May be repeated.",
    )
    p_recall.add_argument("--ask", action="store_true", help="Always print clarifying questions for the operator/user.")
    p_recall.set_defaults(func=recall_sessions)

    p_why = sub.add_parser("why", help="Explain why a session matches a query using indexed evidence.")
    p_why.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_why.add_argument("query", nargs="*", help="Evidence query terms.")
    p_why.add_argument("--limit", type=int, default=12)
    p_why.add_argument("--width", type=int, default=240)
    p_why.add_argument("--include-inherited", action="store_true", help="Include evidence inherited from fork ancestors.")
    p_why.add_argument("--turns-only", action="store_true", help="Show only real user/assistant turns.")
    p_why.add_argument("--artifacts", action="store_true", help="Show only commands, commits, patches, and files.")
    p_why.add_argument(
        "--type",
        dest="hit_type",
        action="append",
        choices=["user_turn", "assistant_turn", "assistant_final", "command", "commit", "commit_created", "patch", "file"],
        help="Restrict evidence to one hit type. May be repeated.",
    )
    p_why.set_defaults(func=why_session)

    p_summarize = sub.add_parser("summarize", help="Print compact context for Codex to summarize one session.")
    p_summarize.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_summarize.add_argument("--messages", type=int, default=12)
    p_summarize.add_argument("--assistant-messages", type=int, default=4)
    p_summarize.add_argument("--commands", type=int, default=12)
    p_summarize.add_argument("--files", type=int, default=12)
    p_summarize.add_argument("--json", action="store_true", help="Print machine-readable JSON context.")
    p_summarize.set_defaults(func=summarize_session)

    p_annotate = sub.add_parser("annotate", help="Persist a durable summary/tags/notes for one session.")
    p_annotate.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_annotate.add_argument("--summary")
    p_annotate.add_argument("--tags")
    p_annotate.add_argument("--notes")
    p_annotate.add_argument("--clear", action="store_true", help="Clear durable summary/tags/notes.")
    p_annotate.set_defaults(func=annotate_session)

    p_queue = sub.add_parser("queue", help="List sessions that do not yet have durable summaries.")
    p_queue.add_argument("--limit", type=int, default=12)
    p_queue.set_defaults(func=summarize_queue)

    p_cockpit = sub.add_parser("cockpit", help="Open an interactive terminal browser for grouped sessions.")
    p_cockpit.add_argument("query", nargs="*", help="Initial filter terms.")
    p_cockpit.add_argument("--project", help="Restrict to one inferred project.")
    p_cockpit.add_argument("--limit", type=int, default=80, help="Sessions to load into the cockpit.")
    p_cockpit.add_argument("--print", action="store_true", help="Print the grouped cockpit view without starting curses.")
    p_cockpit.set_defaults(func=cockpit)

    p_timeline = sub.add_parser("timeline", help="Show numbered user/assistant turns for choosing a cut point.")
    p_timeline.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_timeline.add_argument("--width", type=int, default=220, help="Characters to show per message.")
    p_timeline.set_defaults(func=timeline_session)

    p_cut = sub.add_parser("cut", help="Write a context package for a fresh session up to a chosen turn.")
    p_cut.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_cut.add_argument("--to", type=int, help="Include messages up to this timeline index. Defaults to all messages.")
    p_cut.add_argument("--name", help="Filename-friendly label for the cut.")
    p_cut.add_argument("--next-prompt", help="Optional next task to include in the cut context.")
    p_cut.add_argument("--output-dir", type=Path, default=DEFAULT_CUTS_DIR)
    p_cut.add_argument("--message-chars", type=int, default=2400, help="Max characters per included message.")
    p_cut.add_argument("--commands", type=int, default=20, help="Number of early commands to include.")
    p_cut.add_argument("--command-chars", type=int, default=500, help="Max characters per command.")
    p_cut.set_defaults(func=cut_session)

    p_native_cut = sub.add_parser(
        "native-cut",
        help="Experimental: create a trimmed Codex JSONL session that may be opened with `codex resume`.",
    )
    p_native_cut.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_native_cut.add_argument("--before-text", required=True, help="Cut before the first user message containing this text.")
    p_native_cut.add_argument("--name", required=True, help="Thread name for the cloned cut session.")
    p_native_cut.add_argument("--new-id", help="Optional UUID for the new session. Defaults to a generated UUID.")
    p_native_cut.set_defaults(func=native_cut_session)

    p_native_clone = sub.add_parser(
        "native-clone",
        help="Experimental: create a full Codex JSONL session clone that may be opened with `codex resume`.",
    )
    p_native_clone.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_native_clone.add_argument("--name", help="Thread name for the cloned session. Defaults to '<source name> clone'.")
    p_native_clone.add_argument("--new-id", help="Optional UUID for the new session. Defaults to a generated UUID.")
    p_native_clone.set_defaults(func=native_clone_session)

    p_native_sanitize = sub.add_parser(
        "native-sanitize",
        help="Create a faithful native clone with unverifiable encrypted reasoning removed.",
    )
    p_native_sanitize.add_argument("session", help="Session id, path fragment, or thread name fragment.")
    p_native_sanitize.add_argument("--name", help="Thread name for the sanitized session. Defaults to the source name.")
    p_native_sanitize.add_argument("--new-id", help="Optional UUID for the new session. Defaults to a generated UUID.")
    p_native_sanitize.set_defaults(func=native_sanitize_session)

    p_stats = sub.add_parser("stats", help="Show index statistics.")
    p_stats.add_argument("--limit", type=int, default=20)
    p_stats.set_defaults(func=stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
