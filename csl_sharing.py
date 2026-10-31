"""Private GitHub release inbox for immutable CSL snapshots."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid
import zipfile

from csl_transfer import TransferError, add_open_argument, export_bundle, import_bundle, inspect_bundle, installed_path, open_sessions, resume_command, safe_text, selected_provider
import csl_claude

SHARE_FORMAT = "csl-share"
ASSET = "conversation.csl.zip"
REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
TAG_RE = re.compile(r"csl-[0-9a-f]{12}")


def run_gh(arguments):
    if not shutil.which("gh"):
        raise TransferError("GitHub CLI is required for sharing. Install gh, then run gh auth login")
    try:
        result = subprocess.run(["gh", *arguments], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise TransferError("GitHub request timed out; check the release before retrying a share") from exc
    if result.returncode:
        raise TransferError(safe_text(result.stderr.strip() or "GitHub request failed"))
    return result.stdout


def gh_json(arguments):
    try:
        return json.loads(run_gh(arguments))
    except json.JSONDecodeError as exc:
        raise TransferError("GitHub returned invalid JSON") from exc


def state_dir(csl):
    return csl.DEFAULT_CODEX_HOME / "csl-sharing"


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise TransferError(f"Invalid local sharing state: {path}") from exc
    if not isinstance(data, dict):
        raise TransferError(f"Invalid local sharing state: {path}")
    return data


def verify_repo(repo, *, write=False):
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise TransferError("Repository must be OWNER/REPO on github.com")
    info = gh_json(["repo", "view", f"github.com/{repo}", "--json",
                    "nameWithOwner,isPrivate,viewerPermission"])
    if not info.get("isPrivate"):
        raise TransferError("Session exchange must be a private repository; refusing public access")
    if write and info.get("viewerPermission") not in ("ADMIN", "MAINTAIN", "WRITE"):
        raise TransferError("You need write access to share into this repository")
    canonical = info.get("nameWithOwner")
    if not isinstance(canonical, str) or not REPO_RE.fullmatch(canonical):
        raise TransferError("GitHub did not return a valid repository")
    return canonical, info.get("viewerPermission", "unknown")


def configured_repo(csl, *, write=False):
    config = read_json(state_dir(csl) / "config.json", {})
    if not config.get("repo"):
        raise TransferError("Connect first: csl team connect OWNER/PRIVATE-REPO")
    # Visibility is rechecked on every operation, not only at initial connection.
    return verify_repo(config["repo"], write=write)[0]


def team_command(args, csl):
    if args.team_action == "connect":
        repo, permission = verify_repo(args.repo)
        save_json(state_dir(csl) / "config.json", {"version": 1, "repo": repo})
    else:
        repo = configured_repo(csl)
        permission = "available"
    print(f"team: https://github.com/{repo} (private; {permission})")
    print("next: csl share UUID --title 'Conversation title'")


def file_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def share(args, csl):
    repo = configured_repo(csl, write=True)
    tag = f"csl-{uuid.uuid4().hex[:12]}"
    print(f"sharing: {tag} -> {repo}; preparing snapshot...", flush=True)
    with tempfile.TemporaryDirectory(prefix="csl-share-") as tmp:
        bundle = Path(tmp) / ASSET
        export_bundle(SimpleNamespace(sessions=args.sessions, to=bundle, raw=args.raw, db=args.db,
                                      provider=getattr(args, "provider", None)),
                      csl, quiet=True)
        if bundle.stat().st_size >= 2 * 1024**3:
            raise TransferError("GitHub release assets must be below 2 GiB; share fewer sessions per package")
        with zipfile.ZipFile(bundle) as archive:
            manifest = inspect_bundle(archive)
        title = args.title if args.title is not None else manifest["sessions"][0]["title"][:200]
        title = safe_text(title).strip()
        if not title or len(title) > 200:
            raise TransferError("Share title must have 1 to 200 characters; use --title")
        receipts = read_json(state_dir(csl) / "received.json", {})
        source_ids = {entry["id"] for entry in manifest["sessions"]}
        source_provider = selected_provider(manifest["sessions"])
        parents = [receipt.get("share_id", key.rsplit("/", 1)[1].split("@", 1)[0]) for key, receipt in receipts.items()
                   if key.startswith(repo.lower() + "/") and source_ids.intersection(receipt.get("ids", []))
                   and receipt.get("provider", "codex") == source_provider]
        metadata = {"format": SHARE_FORMAT, "version": manifest["version"], "asset": ASSET,
                    "sha256": file_sha256(bundle), "bytes": bundle.stat().st_size,
                    "continues": sorted(set(parents)),
                    "mode": manifest["mode"], "sessions": [
                        {"id": s["id"], "title": s["title"], **({"provider": s["provider"]} if "provider" in s else {})}
                        for s in manifest["sessions"]]}
        notes = Path(tmp) / "notes.json"
        notes.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"uploading: {metadata['bytes'] / 1024:.1f} KiB ({metadata['mode']})...", flush=True)
        try:
            run_gh(["release", "create", tag, str(bundle), "--repo", f"github.com/{repo}",
                    "--title", title, "--notes-file", str(notes), "--latest=false"])
        except TransferError as exc:
            raise TransferError(f"Share {tag} did not finish: {exc}. Inspect its release before retrying") from exc
    print(f"shared: {tag}  {title}")
    print(f"https://github.com/{repo}/releases/tag/{tag}")
    print(f"recipient: csl receive {tag} --cwd /path/to/project --open")
    return tag


def release_metadata(release):
    if not isinstance(release, dict) or release.get("draft"):
        raise TransferError("Share is not published")
    if not TAG_RE.fullmatch(str(release.get("tag_name", ""))):
        raise TransferError("Release is not a CSL share")
    try:
        meta = json.loads(release.get("body") or "")
    except (ValueError, TypeError) as exc:
        raise TransferError("Share has invalid metadata") from exc
    if (not isinstance(meta, dict) or meta.get("format") != SHARE_FORMAT or meta.get("version") not in (1, 2)
            or meta.get("asset") != ASSET or not re.fullmatch(r"[0-9a-f]{64}", str(meta.get("sha256", "")))
            or not isinstance(meta.get("bytes"), int) or not 0 < meta["bytes"] < 2 * 1024**3
            or meta.get("mode") not in ("raw", "portable") or not isinstance(meta.get("sessions"), list)
            or not 1 <= len(meta["sessions"]) <= 1000):
        raise TransferError("Unsupported or incomplete CSL share metadata")
    for session in meta["sessions"]:
        if not isinstance(session, dict) or not isinstance(session.get("title"), str):
            raise TransferError("Invalid shared session metadata")
        try:
            uuid.UUID(session["id"])
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise TransferError("Invalid shared session UUID") from exc
    provider = selected_provider(meta["sessions"])
    if provider == "claude" and meta["version"] != 2:
        raise TransferError("Claude shares require format version 2; update CSL")
    parents = meta.get("continues", [])
    if not isinstance(parents, list) or len(parents) > 1000 or any(
            not isinstance(parent, str) or not TAG_RE.fullmatch(parent) for parent in parents):
        raise TransferError("Invalid parent snapshot IDs")
    assets = [a for a in release.get("assets", []) if isinstance(a, dict) and a.get("name") == ASSET]
    if len(assets) != 1 or assets[0].get("size") != meta["bytes"] or not assets[0].get("id"):
        raise TransferError("Share asset is missing or inconsistent")
    if not release.get("id"):
        raise TransferError("Share release identity is missing")
    return meta, assets[0]


def list_shares(repo, limit):
    shares = []
    page = 1
    while len(shares) < limit:
        releases = gh_json(["api", "--hostname", "github.com",
                            f"repos/{repo}/releases?per_page=100&page={page}"])
        if not isinstance(releases, list):
            raise TransferError("GitHub did not return a release list")
        for release in releases:
            try:
                metadata, asset = release_metadata(release)
            except TransferError:
                continue
            shares.append({"id": release["tag_name"], "title": release.get("name") or release["tag_name"],
                           "author": (release.get("author") or {}).get("login", "unknown"),
                           "date": release.get("published_at"), "bytes": asset["size"],
                           "sessions": len(metadata["sessions"]), "mode": metadata["mode"],
                           "provider": selected_provider(metadata["sessions"]),
                           "continues": metadata.get("continues", [])})
            if len(shares) == limit:
                break
        if len(releases) < 100:
            break
        page += 1
        if page > 10:
            break
    return shares


def inbox(args, csl):
    repo = configured_repo(csl)
    shares = list_shares(repo, args.limit)
    if args.json:
        print(json.dumps({"repo": repo, "shares": shares}, ensure_ascii=False))
    else:
        print(f"inbox: {repo}")
        for entry in shares:
            print(f"{entry['id']}  {safe_text(entry['author'])}  {entry['date'] or ''}  "
                  f"{entry['bytes'] / 1024:.1f} KiB  [{entry['sessions']} {entry['provider']}]  {safe_text(entry['title'])}")
        if not shares:
            print("No published CSL shares.")
    return shares


@contextmanager
def receive_lock(csl):
    directory = state_dir(csl)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "receive.lock").open("a") as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TransferError("Another CSL receive is running; retry when it finishes") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def receive(args, csl):
    repo = configured_repo(csl)
    tag = args.share_id
    if not tag:
        if not sys.stdin.isatty():
            raise TransferError("Specify a share ID from csl inbox")
        choices = list_shares(repo, 20)
        for i, choice in enumerate(choices, 1):
            print(f"{i:2}. {choice['id']}  {safe_text(choice['title'])}")
        try:
            choice = int(input("Receive which share? ")) - 1
            if not 0 <= choice < len(choices):
                raise ValueError
            tag = choices[choice]["id"]
        except ValueError as exc:
            raise TransferError("Invalid share selection") from exc
    if re.fullmatch(r"[0-9a-f]{12}", tag):
        tag = "csl-" + tag
    if not TAG_RE.fullmatch(tag):
        raise TransferError("Use the csl-xxxxxxxxxxxx ID shown by csl inbox")
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise TransferError(f"Working directory does not exist: {cwd}")
    release = gh_json(["api", "--hostname", "github.com", f"repos/{repo}/releases/tags/{tag}"])
    meta, asset = release_metadata(release)
    provider = selected_provider(meta["sessions"], getattr(args, "provider", None))
    destination_provider = getattr(args, "target", None) or provider
    if not getattr(args, "target", None) and (getattr(args, "leaf", None) or getattr(args, "repair_interrupted", False) or getattr(args, "history_policy", "active") != "active"):
        raise TransferError("History selection/repair requires --target (explicit conversion)")
    if args.open and not shutil.which(destination_provider):
        raise TransferError(f"{destination_provider} is not on PATH; omit --open or install it")
    if release["tag_name"] != tag:
        raise TransferError("GitHub returned a different share identity")
    if args.open and len(meta["sessions"]) > 1 and not sys.stdin.isatty():
        raise TransferError("--open with several sessions needs an interactive terminal")
    fingerprint = {"sha256": meta["sha256"], "release_id": release["id"], "asset_id": asset["id"]}
    key = f"{repo.lower()}/{tag}"
    if getattr(args, "target", None):
        route = {"target": args.target, "history_policy": args.history_policy,
                 "leaf": args.leaf, "repair_interrupted": args.repair_interrupted, "canonical_version": 2}
        key += "@" + hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()[:16]
    receipt_path = state_dir(csl) / "received.json"
    with receive_lock(csl):
        receipts = read_json(receipt_path, {})
        previous = receipts.get(key)
        ids = []
        if previous:
            if previous.get("snapshot") != fingerprint:
                raise TransferError("Shared snapshot changed remotely; publish it under a new share ID")
            paths = [Path(p) for p in previous.get("paths", [])]
            allowed_root = csl_claude.projects().resolve() if destination_provider == "claude" else csl.DEFAULT_SESSIONS_DIR.resolve()
            existing = (previous.get("provider", "codex") == destination_provider and bool(paths)
                        and all(p.is_file() and p.resolve().is_relative_to(allowed_root) for p in paths))
            if existing and not args.new:
                if previous["cwd"] != str(cwd):
                    raise TransferError(f"Already received in {previous['cwd']}; use that --cwd or --new")
                ids = previous["ids"]
                print(f"already received: {tag}; reusing local sessions")
        if not ids:
            with tempfile.TemporaryDirectory(prefix="csl-receive-") as tmp:
                bundle = Path(tmp) / ASSET
                print(f"downloading: {tag} ({meta['bytes'] / 1024:.1f} KiB)...", flush=True)
                run_gh(["release", "download", tag, "--repo", f"github.com/{repo}",
                        "--pattern", ASSET, "--output", str(bundle)])
                if bundle.stat().st_size != meta["bytes"] or file_sha256(bundle) != meta["sha256"]:
                    raise TransferError("Downloaded package failed its release checksum/size check")
                with zipfile.ZipFile(bundle) as archive:
                    manifest = inspect_bundle(archive)
                expected = [{"id": s["id"], "title": s["title"], **({"provider": s["provider"]} if "provider" in s else {})}
                            for s in manifest["sessions"]]
                if expected != meta["sessions"] or manifest["mode"] != meta["mode"]:
                    raise TransferError("Package contents do not match the share metadata")
                title = safe_text(release.get("name") or tag) if len(meta["sessions"]) == 1 else None
                ids = import_bundle(SimpleNamespace(bundle=bundle, cwd=cwd, open=False, list=False,
                                                    name=title, db=args.db,
                                                    open_args=getattr(args, "open_args", ()), provider=provider,
                                                    target=getattr(args, "target", None),
                                                    leaf=getattr(args, "leaf", None),
                                                    history_policy=getattr(args, "history_policy", "active"),
                                                    repair_interrupted=getattr(args, "repair_interrupted", False),
                                                    max_context_chars=getattr(args, "max_context_chars", None)), csl)
                paths = [str(installed_path(sid, cwd, destination_provider, csl)) for sid in ids]
                receipts[key] = {"snapshot": fingerprint, "ids": ids, "paths": paths,
                                 "cwd": str(cwd), "received_at": csl.utc_now(), "provider": destination_provider,
                                 "share_id": tag}
                save_json(receipt_path, receipts)
        else:
            from csl_diagnostics import preflight
            for path in paths:
                preflight(path, destination_provider, max_context_chars=getattr(args, "max_context_chars", None))
            for sid in ids:
                command = shlex.join(resume_command(sid, cwd, getattr(args, 'open_args', ()), destination_provider))
                print(f"open: {'cd ' + shlex.quote(str(cwd)) + ' && ' if destination_provider == 'claude' else ''}{command}")
    if args.open:
        open_sessions(ids, cwd, getattr(args, "open_args", ()), destination_provider)
    return ids


def run_sharing(args, csl):
    try:
        return {"team": team_command, "share": share, "inbox": inbox,
                "receive": receive}[args.command](args, csl)
    except (TransferError, OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        print(f"csl {args.command}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def positive_limit(value):
    import argparse
    try:
        number = int(value)
        if not 1 <= number <= 1000:
            raise ValueError
        return number
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000") from exc


def add_commands(subparsers, csl):
    team = subparsers.add_parser("team", help="Connect CSL to a private GitHub session exchange.")
    team_sub = team.add_subparsers(dest="team_action", required=True)
    connect = team_sub.add_parser("connect", help="Verify and save OWNER/PRIVATE-REPO.")
    connect.add_argument("repo")
    team_sub.add_parser("status", help="Check the configured private exchange.")
    team.set_defaults(func=lambda args: run_sharing(args, csl))
    share_parser = subparsers.add_parser("share", help="Export and publish a snapshot to the private team inbox.")
    share_parser.add_argument("sessions", nargs="*", help="Session selectors; omit for a local picker.")
    share_parser.add_argument("--title", help="Short title shown in the team inbox.")
    share_parser.add_argument("--raw", action="store_true", help="Keep encrypted reasoning (default: portable).")
    share_parser.add_argument("--provider", choices=("codex", "claude"), help="Limit source lookup to one CLI (default: auto-detect both).")
    share_parser.set_defaults(func=lambda args: run_sharing(args, csl))
    inbox_parser = subparsers.add_parser("inbox", help="List shared snapshots without downloading transcripts.")
    inbox_parser.add_argument("--limit", type=positive_limit, default=20)
    inbox_parser.add_argument("--json", action="store_true", help="Structured output for agents.")
    inbox_parser.set_defaults(func=lambda args: run_sharing(args, csl))
    receive_parser = subparsers.add_parser("receive", help="Download/import a shared snapshot, reusing prior imports.")
    receive_parser.add_argument("share_id", nargs="?", help="ID from csl inbox; omit for a picker.")
    receive_parser.add_argument("--cwd", type=Path, default=Path.cwd())
    add_open_argument(receive_parser)
    receive_parser.add_argument("--new", action="store_true", help="Import a fresh independent copy even if already received.")
    receive_parser.add_argument("--provider", choices=("codex", "claude"), help="Require the package's native CLI; not a conversion option.")
    from csl_conversion import add_conversion_options
    add_conversion_options(receive_parser)
    receive_parser.set_defaults(func=lambda args: run_sharing(args, csl))
