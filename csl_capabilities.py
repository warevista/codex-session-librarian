"""Public command coverage registry. Keep docs/PROVIDER_MATRIX.md in sync."""

import argparse
import json

import csl_claude


CODEX_ONLY = ("index", "search", "recent", "show", "related", "find-work", "patches", "pinpoint", "recall",
              "why", "summarize", "annotate", "queue", "cockpit", "timeline", "cut", "native-cut", "native-clone",
              "native-sanitize", "stats")
SHARED = ("export", "import", "share", "receive", "inbox", "team", "sessions", "capabilities", "doctor")


def coverage():
    commands = {name: {"codex": "supported", "claude": "not-integrated"} for name in CODEX_ONLY}
    commands.update({name: {"codex": "supported", "claude": "supported"} for name in SHARED})
    commands["convert"] = {"codex": "experimental", "claude": "experimental"}
    commands["verify-live"] = {"codex": "experimental", "claude": "experimental"}
    for name in ("native-cut", "native-clone", "native-sanitize"):
        commands[name]["codex"] = "experimental"
    return {"schema_version": 1, "providers": ["codex", "claude"], "commands": commands,
            "source_lookup": {"default": "auto", "commands": ["export", "share", "convert", "doctor"],
                              "cross_cli_ambiguity": "require --provider codex or --provider claude",
                              "within_cli_ambiguity": "require a more specific UUID or file path"},
            "routes": {"codex->codex": "native", "claude->claude": "native",
                       "codex->claude": "experimental-multimodal", "claude->codex": "experimental-multimodal"},
            "canonical": {"format": "csl-conversation", "version": 2, "profile": "text-tools-references-images-pdf",
                          "assets": "embedded-only-optional-decoder", "source_policy": "quarantined"},
            "continuity_checks": {"default_import": "structural-and-static-context",
                                  "doctor_native": "isolated-localhost-request-no-generation",
                                  "live_continuation": "opt-in-bounded-synthetic-suite"},
            "authentication": "Recipient CLI owns authentication; CSL never copies credentials or selects billing accounts.",
            "continuation": "Native file transfer is not a guarantee of model availability, context fit, or universal CLI-version compatibility."}


def markdown():
    lines = ["| Command | Codex | Claude Code |", "| --- | --- | --- |"]
    for name, status in sorted(coverage()["commands"].items()):
        lines.append(f"| `{name}` | {status['codex']} | {status['claude']} |")
    return "\n".join(lines)


def capabilities(args):
    print(json.dumps(coverage(), indent=2) if args.json else markdown())


def sessions(args, csl):
    from csl_transfer import read_records, safe_text
    if args.provider == "claude":
        rows = []
        for path in csl_claude.paths():
            try:
                info = csl_claude.header(path, read_records)
            except (ValueError, OSError, TypeError, AttributeError) as exc:
                # One broken transcript must not hide other sessions.
                import sys
                print(f"Skipping {path.name}: {exc}", file=sys.stderr)
                continue
            rows.append({**info, "path": str(path), "modified": path.stat().st_mtime})
        rows.sort(key=lambda row: row["modified"], reverse=True)
    else:
        conn = csl.init_db(args.db.expanduser())
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT session_id AS id,thread_name AS title,path FROM sessions "
                "ORDER BY coalesce(updated_at,started_at) DESC")]
        finally:
            conn.close()
        rows = [{**row, "provider": "codex"} for row in rows]
    if args.query:
        rows = [row for row in rows if args.query.casefold() in (str(row["title"]) + " " + row["id"]).casefold()]
    rows = rows[:args.limit]
    if args.json:
        print(json.dumps({"provider": args.provider, "sessions": rows}, ensure_ascii=False))
    else:
        for row in rows:
            print(f"{row['id']}  [{args.provider}]  {safe_text(row['title'])}\n  {row['path']}")


def positive_limit(value):
    number = int(value)
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000")
    return number


def add_commands(subparsers, csl):
    caps = subparsers.add_parser("capabilities", help="Show the maintained command/provider coverage matrix.")
    caps.add_argument("--json", action="store_true", help="Machine-readable command and transfer-route coverage.")
    caps.set_defaults(func=capabilities)
    listing = subparsers.add_parser("sessions", help="List sessions for one CLI; Codex uses its index, Claude reads transcripts.")
    listing.add_argument("--provider", choices=("codex", "claude"), default="codex")
    listing.add_argument("--limit", type=positive_limit, default=20)
    listing.add_argument("--query", help="Filter session title or UUID; not full-history search.")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=lambda args: sessions(args, csl))
