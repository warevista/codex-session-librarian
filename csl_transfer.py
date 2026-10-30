"""Portable native session bundles. No credentials or project files are bundled."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

from csl_display import project_file

FORMAT = "codex-session-librarian"
VERSION = 1
MAX_MANIFEST = 1024 * 1024
MAX_SESSION = 2 * 1024**3
MAX_TOTAL = 4 * 1024**3


class TransferError(ValueError):
    pass


class OpenAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        namespace.open = True
        namespace.open_args = values


def add_open_argument(parser):
    parser.add_argument("--open", nargs=argparse.REMAINDER, action=OpenAction,
                        help="Open in Codex; all following arguments are passed literally to Codex. Put CSL options first.")
    parser.set_defaults(open=False, open_args=[])


def resume_command(sid, cwd, open_args=()):
    return ["codex", "-C", str(cwd), "resume", "--all", *open_args, sid]


def safe_text(value):
    return "".join(char if char.isprintable() else " " for char in str(value))


def read_records(stream):
    for number, line in enumerate(stream, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise TransferError(f"Invalid JSONL at line {number}") from exc
        if not isinstance(record, dict):
            raise TransferError(f"JSONL line {number} is not an object")
        yield record


def validate_compaction(record):
    if record.get("type") != "compacted":
        return
    payload = record.get("payload", {})
    history = payload.get("replacement_history")
    metadata = payload.get("replacement_history_metadata")
    if isinstance(history, list) and isinstance(metadata, list) and metadata and len(history) != len(metadata):
        raise TransferError("Compaction history/metadata lengths differ; re-export the original session with updated CSL. This package cannot safely restore its active context.")


def portable_record(record):
    """Drop encrypted reasoning; retain readable compaction and all messages/tools."""
    validate_compaction(record)
    payload = record.get("payload", {})

    def encrypted_item(value):
        if not isinstance(value, dict):
            return False
        item = value.get("item", value)
        return (isinstance(item, dict) and item.get("type") in
                ("reasoning", "compaction", "compaction_summary", "context_compaction")
                and "encrypted_content" in item)

    if record.get("type") == "response_item" and encrypted_item(payload):
        return None, 1

    def clean(value):
        if isinstance(value, list):
            result, removed = [], 0
            for item in value:
                if encrypted_item(item):
                    removed += 1
                else:
                    child, count = clean(item)
                    result.append(child)
                    removed += count
            return result, removed
        if isinstance(value, dict):
            result, removed = {}, 0
            history = value.get("replacement_history")
            annotations = value.get("replacement_history_metadata")
            keep = None
            if (record.get("type") == "compacted" and value is payload
                    and isinstance(history, list) and isinstance(annotations, list) and annotations):
                if len(history) != len(annotations):
                    raise TransferError("Compaction history/metadata lengths differ; export from the original session instead of a previously damaged portable copy")
                keep = [i for i, entry in enumerate(history) if not encrypted_item(entry)]
            for key, item in value.items():
                if key == "replacement_history_metadata" and keep is not None:
                    item = [item[i] for i in keep]
                if key == "encrypted_content" and (
                    value.get("type") in ("reasoning", "compaction")
                    or (record.get("type") == "compacted" and value is payload)
                ):
                    removed += 1
                    continue
                child, count = clean(item)
                result[key] = child
                removed += count
            return result, removed
        return value, 0

    cleaned, count = clean(record)
    if (record.get("type") == "compacted" and count and
            cleaned.get("payload", {}).get("replacement_history") == []):
        if not cleaned["payload"].get("message"):
            return None, count
        cleaned["payload"].pop("replacement_history")
    return cleaned, count


def json_line(record):
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def session_header(path, csl):
    with path.open("rb") as stream:
        for record in read_records(stream):
            if record.get("type") == "session_meta":
                payload = record.get("payload", {})
                if not isinstance(payload, dict):
                    raise TransferError(f"Invalid session metadata: {path}")
                sid = csl.SESSION_ID_RE.search(path.name)
                sid = sid.group(1) if sid else payload.get("id")
                try:
                    uuid.UUID(sid)
                except (ValueError, TypeError, AttributeError) as exc:
                    raise TransferError(f"Missing valid session UUID: {path}") from exc
                names = csl.load_thread_names(csl.DEFAULT_SESSION_INDEX)
                return {"id": sid, "title": names.get(sid, {}).get("thread_name") or sid,
                        "cwd": payload.get("cwd"), "created_at": payload.get("timestamp")}
    raise TransferError(f"No session metadata: {path}")


def resolve_sources(needles, csl, db):
    conn = csl.init_db(db.expanduser())
    try:
        if not needles:
            if not sys.stdin.isatty():
                raise TransferError("Specify a session UUID, name, or JSONL path")
            rows = conn.execute("SELECT session_id,thread_name FROM sessions "
                                "ORDER BY coalesce(updated_at,started_at) DESC LIMIT 30").fetchall()
            if not rows:
                raise TransferError("No indexed sessions. Run csl index first")
            for i, row in enumerate(rows, 1):
                print(f"{i:2}. {row['session_id']}  {row['thread_name']}")
            try:
                picks = [int(n) for n in input("Sessions (numbers separated by spaces): ").split()]
                if not picks or any(n < 1 or n > len(rows) for n in picks):
                    raise ValueError
                needles = [rows[n - 1]["session_id"] for n in picks]
            except ValueError as exc:
                raise TransferError("Invalid session selection") from exc
        sources = {}
        for needle in needles:
            path = Path(needle).expanduser()
            if path.is_file():
                candidates = [path.resolve()]
            else:
                # Search filenames too: archived or newly created sessions may not be indexed.
                candidates = []
                if re.fullmatch(r"[0-9a-fA-F-]{6,36}", needle):
                    for root in [csl.DEFAULT_SESSIONS_DIR, csl.DEFAULT_CODEX_HOME / "archived_sessions"]:
                        candidates.extend(root.rglob(f"*{needle.lower()}*.jsonl"))
                if not candidates:
                    rows = conn.execute("SELECT path FROM sessions WHERE instr(session_id, ?) > 0 "
                                        "OR instr(lower(thread_name), lower(?)) > 0",
                                        (needle, needle)).fetchall()
                    candidates = [Path(r["path"]) for r in rows if Path(r["path"]).is_file()]
            candidates = sorted(set(candidates))
            if len(candidates) != 1:
                raise TransferError(f"Session {needle!r}: {len(candidates)} matches; use a UUID or path")
            header = session_header(candidates[0], csl)
            sources[header["id"]] = (candidates[0], header)
        return list(sources.values())
    finally:
        conn.close()


def export_bundle(args, csl, *, quiet=False):
    sources = resolve_sources(args.sessions, csl, args.db)
    destination = args.to.expanduser()
    if not destination.name.lower().endswith(".zip"):
        if len(sources) == 1:
            title = re.sub(r"[^A-Za-z0-9_.-]+", "-", sources[0][1]["title"]).strip("-.")[:70]
            filename = f"{title or 'session'}-{sources[0][1]['id'][:8]}.csl.zip"
        else:
            filename = f"sessions-{dt.datetime.now(dt.UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.csl.zip"
        destination = destination / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise TransferError(f"Refusing to overwrite {destination}")
    manifest = {"format": FORMAT, "version": VERSION, "created_at": csl.utc_now(),
                "mode": "raw" if args.raw else "portable", "sessions": []}
    total = 0
    with tempfile.TemporaryDirectory(prefix=".csl-export-", dir=destination.parent) as tmp:
        staged = Path(tmp) / "bundle.zip"
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path, header in sources:
                member = f"sessions/{header['id']}.jsonl"
                digest = hashlib.sha256()
                size = removed = messages = records = metadata = 0
                # Bound the snapshot at the original file length; a partial final append is omitted.
                with path.open("rb") as source, archive.open(member, "w", force_zip64=True) as target:
                    remaining = os.fstat(source.fileno()).st_size

                    def snapshot():
                        nonlocal remaining
                        while remaining > 0:
                            line = source.readline(remaining)
                            remaining -= len(line)
                            if not line:
                                break
                            if remaining == 0 and not line.endswith(b"\n"):
                                try:
                                    json.loads(line)
                                except (ValueError, UnicodeError):
                                    break
                            yield line

                    for record in read_records(snapshot()):
                        payload = record.get("payload", {})
                        metadata += record.get("type") == "session_meta"
                        if isinstance(payload, dict):
                            messages += (payload.get("type") == "message" and payload.get("role")
                                         in ("user", "assistant")) or payload.get("type") in ("user_message", "agent_message")
                        if not args.raw:
                            record, count = portable_record(record)
                            removed += count
                            if record is None:
                                continue
                        data = json_line(record)
                        size += len(data)
                        total += len(data)
                        if size > MAX_SESSION or total > MAX_TOTAL:
                            raise TransferError("Bundle exceeds the 2 GiB/session or 4 GiB total limit")
                        target.write(data)
                        digest.update(data)
                        records += 1
                if not metadata or not messages:
                    raise TransferError(f"Session {header['id']} has no local conversation history; metadata alone cannot be exported")
                manifest["sessions"].append({**header, "file": member, "bytes": size,
                                             "sha256": digest.hexdigest(), "records": records,
                                             "encrypted_items_removed": removed})
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.chmod(staged, 0o600)
        # Atomic, exclusive publication: even concurrent exports cannot overwrite a package.
        os.link(staged, destination)
    if not quiet:
        print(f"exported: {destination.resolve()}")
        print(f"sessions: {len(sources)}; mode: {manifest['mode']}")
        print("Contains conversation/tool history, which may include sensitive text; no auth files or project assets are included.")
        print(f"recipient: csl import {shlex.quote(destination.name)} --cwd /path/to/project --open")
    return destination.resolve()


def inspect_bundle(archive):
    infos = archive.infolist()
    names = [i.filename for i in infos]
    if len(names) != len(set(names)):
        raise TransferError("Duplicate ZIP members")
    if "manifest.json" not in names or archive.getinfo("manifest.json").file_size > MAX_MANIFEST:
        raise TransferError("Missing or oversized manifest")
    manifest = json.loads(archive.read("manifest.json"))
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or manifest.get("version") != VERSION or manifest.get("mode") not in ("raw", "portable")):
        raise TransferError("Unsupported CSL bundle format/version/mode")
    entries = manifest.get("sessions")
    if not isinstance(entries, list) or not entries or len(entries) > 1000:
        raise TransferError("Manifest must contain 1 to 1000 sessions")
    expected = {"manifest.json"}
    ids = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise TransferError("Invalid session entry")
        sid = entry.get("id")
        try:
            if str(uuid.UUID(sid)) != sid:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise TransferError("Invalid source UUID in manifest") from exc
        if sid in ids or entry.get("file") != f"sessions/{sid}.jsonl":
            raise TransferError("Duplicate source UUID or invalid member path")
        if not isinstance(entry.get("title"), str) or not isinstance(entry.get("bytes"), int):
            raise TransferError("Missing title or size")
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
            raise TransferError("Invalid checksum")
        expected.add(entry["file"])
        ids.add(sid)
        if entry["file"] not in names:
            raise TransferError("Missing session member")
        info = archive.getinfo(entry["file"])
        if info.file_size != entry["bytes"] or not 0 < info.file_size <= MAX_SESSION:
            raise TransferError("Invalid session size")
        total += info.file_size
        if total > MAX_TOTAL:
            raise TransferError("Bundle exceeds the 4 GiB total limit")
    if set(names) != expected:
        raise TransferError("Unexpected ZIP members; only manifest and session JSONL are allowed")
    if any(stat.S_ISLNK(i.external_attr >> 16) or i.flag_bits & 1 for i in infos):
        raise TransferError("Symlink or encrypted ZIP members are not supported")
    return manifest


def rewrite_import_record(record, sid, entry, cwd, now):
    kind = record.get("type")
    payload = record.get("payload", {})
    if not isinstance(payload, dict):
        return record
    if kind == "session_meta":
        payload.update(id=sid, timestamp=now, cwd=str(cwd), forked_from_id=entry["id"])
        if "session_id" in payload:
            payload["session_id"] = sid
        # Rebuild a local legacy replay; source account identities/pagination aren't portable.
        payload["history_mode"] = "legacy"
        for key in ("creator_user_id", "creator_account_id", "context_window", "runtime_workspace_roots",
                    "history_base", "forked_from_ordinal_exclusive"):
            payload.pop(key, None)
        record["timestamp"] = now
    elif kind == "turn_context":
        payload["cwd"] = str(cwd)
        if "runtime_workspace_roots" in payload:
            payload["runtime_workspace_roots"] = [str(cwd)]
    return record


def import_bundle(args, csl):
    cwd = args.cwd.expanduser().resolve()
    if not args.list and not cwd.is_dir():
        raise TransferError(f"Working directory does not exist: {cwd}")
    if args.open and not shutil.which("codex"):
        raise TransferError("codex is not on PATH; install it or omit --open")
    imported = []
    with zipfile.ZipFile(args.bundle.expanduser()) as archive, tempfile.TemporaryDirectory(prefix="csl-import-") as tmp:
        manifest = inspect_bundle(archive)
        entries = manifest["sessions"]
        if args.open and len(entries) > 1 and not sys.stdin.isatty():
            raise TransferError("--open with multiple sessions needs an interactive terminal; import without --open")
        if args.name and len(entries) != 1:
            raise TransferError("--name requires a single-session bundle")
        staged = []
        # Validate every member, checksum and replay before installing any session.
        for entry in entries:
            sid = str(uuid.uuid4())
            if csl.session_id_exists(sid):
                raise TransferError("Generated UUID already exists; retry the import")
            now = csl.utc_now()
            digest = hashlib.sha256()
            local = Path(tmp) / f"{sid}.jsonl"
            metadata = messages = 0
            source_bases = []
            first_kind = None
            with archive.open(entry["file"]) as source, local.open("wb") as target:
                def checked_lines():
                    for line in source:
                        digest.update(line)
                        yield line
                for record in read_records(checked_lines()):
                    validate_compaction(record)
                    first_kind = first_kind or record.get("type")
                    metadata += record.get("type") == "session_meta"
                    payload = record.get("payload", {})
                    if isinstance(payload, dict):
                        if record.get("type") == "session_meta" and payload.get("history_base"):
                            source_bases.append(payload["history_base"])
                        messages += (payload.get("type") == "message" and payload.get("role")
                                     in ("user", "assistant")) or payload.get("type") in ("user_message", "agent_message")
                    target.write(json_line(rewrite_import_record(record, sid, entry, cwd, now)))
            if digest.hexdigest() != entry["sha256"]:
                raise TransferError(f"Checksum mismatch for {entry['id']}")
            if first_kind != "session_meta" or not metadata or not messages:
                raise TransferError(f"Session {entry['id']} lacks replayable local history")
            projected = Path(tmp) / f"{sid}.display.jsonl"
            display = project_file(local, projected, read_records, json_line, {entry["id"], sid})
            local = projected
            staged.append((local, sid, entry, now))
            print(f"{entry['id']}  {safe_text(entry['title'])}  ({entry['bytes']} bytes, {manifest['mode']})")
            if display:
                print(f"visible-history projection: {display.get('user_message', 0)} user, {display.get('agent_message', 0)} assistant messages")
            if source_bases:
                print("Note: source fork references an inherited parent history; this import includes only the records present in this package, not missing parent files.")
        if args.list:
            print("Package validated; no sessions installed.")
            return
        conn = csl.init_db(args.db.expanduser())
        try:
            for local, sid, entry, now in staged:
                target = csl.target_session_path(sid, dt.datetime.now(dt.UTC))
                with target.open("xb") as output, local.open("rb") as source:
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(source, output)
                title = safe_text(args.name or entry["title"])
                csl.register_native_session(conn, target, sid, title, now)
                imported.append(sid)
                print(f"imported: {sid} (source: {entry['id']})")
                print(f"open: {shlex.join(resume_command(sid, cwd, getattr(args, 'open_args', ())))}")
        finally:
            conn.close()
    if args.open:
        open_sessions(imported, cwd, getattr(args, "open_args", ()))
    return imported


def open_sessions(imported, cwd, open_args=()):
    if not imported:
        raise TransferError("No imported sessions to open")
    selected = 0
    if len(imported) > 1:
        if not sys.stdin.isatty():
            raise TransferError("Opening multiple sessions needs a terminal; use a printed resume command")
        try:
            selected = int(input(f"Open which session (1-{len(imported)})? ")) - 1
            if not 0 <= selected < len(imported):
                raise ValueError
        except ValueError as exc:
            raise TransferError("Sessions imported; invalid selection, use a printed resume command") from exc
    raise SystemExit(subprocess.call(resume_command(imported[selected], cwd, open_args)))


def run_transfer(args, csl):
    try:
        (export_bundle if args.command == "export" else import_bundle)(args, csl)
    except (TransferError, OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"csl {args.command}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def add_commands(subparsers, csl):
    export = subparsers.add_parser("export", help="Package native conversations in a portable .csl.zip.")
    export.add_argument("sessions", nargs="*", help="UUIDs, unambiguous names, or JSONL paths; omit for a picker.")
    export.add_argument("--to", type=Path, default=Path.home() / "Downloads", help="Output directory or .zip path.")
    export.add_argument("--raw", action="store_true", help="Preserve encrypted reasoning instead of portable cleanup.")
    export.set_defaults(func=lambda args: run_transfer(args, csl))
    imp = subparsers.add_parser("import", help="Validate and install a session bundle with fresh local UUIDs.")
    imp.add_argument("bundle", type=Path)
    imp.add_argument("--cwd", type=Path, default=Path.cwd(), help="Local project directory (default: current directory).")
    mode = imp.add_mutually_exclusive_group()
    add_open_argument(mode)
    mode.add_argument("--list", action="store_true", help="Validate and list contents without installing.")
    imp.add_argument("--name", help="Rename a single imported session.")
    imp.set_defaults(func=lambda args: run_transfer(args, csl))
