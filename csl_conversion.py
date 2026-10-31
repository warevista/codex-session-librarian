"""Explicit canonical conversion, plus isolated continuity diagnostics."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid
import zipfile

import csl_canonical as canonical
from csl_diagnostics import analyze, apply_budget


def canonical_input(source, provider=None, *, leaf=None, history_policy="active", repair_interrupted=False):
    path = Path(source)
    if path.is_file() and path.suffix.lower() == ".json":
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("Canonical input exceeds 64 MiB")
        data = canonical.strict_json(path.read_text())
        canonical.validate(data)
        if leaf or history_policy != "active" or repair_interrupted:
            raise ValueError("History selection/repair must be applied to native sources, not an existing canonical document")
        if provider and data.get("source", {}).get("provider") != provider:
            raise ValueError("Canonical source provider differs from --provider")
        return data
    return canonical.normalize(provider, path, leaf, repair_interrupted, history_policy)


def restore(data, target, path, cwd):
    function = canonical.restore_codex if target == "codex" else canonical.restore_claude
    info = function(data, path, cwd)
    info["conversion"] = {"format": canonical.FORMAT, "version": canonical.VERSION,
                          "source": data.get("source", {}), "report": data.get("report", {}),
                          "quarantined_source_context": len(data["source_context"])}
    return info


def convert_bundle(args, csl, archive, manifest):
    from csl_transfer import detect_provider, export_bundle, import_bundle
    import hashlib
    target = args.target
    if getattr(args, "leaf", None) and len(manifest["sessions"]) != 1:
        raise ValueError("--leaf requires a single-session package")
    with tempfile.TemporaryDirectory(prefix="csl-cross-import-") as temp:
        paths = []
        for entry in manifest["sessions"]:
            if entry.get("compaction_state_removed") and getattr(args, "history_policy", "active") == "active":
                raise ValueError("Package lost encrypted compaction state; exact conversion is unavailable. Select --history-policy visible only for explicit readable-history recovery")
            source = Path(temp) / f"{entry['id']}.jsonl"
            digest = hashlib.sha256()
            with archive.open(entry["file"]) as stream, source.open("xb") as output:
                for line in stream:
                    digest.update(line)
                    output.write(line)
            if digest.hexdigest() != entry["sha256"]:
                raise ValueError("Package checksum mismatch; conversion refused")
            provider = entry.get("provider", "codex")
            if detect_provider(source) != provider:
                raise ValueError("Native source format differs from the package provider")
            converted = Path(temp) / f"converted-{uuid.uuid4()}.jsonl"
            convert_native(source, provider, target, converted, args.cwd, args)
            paths.append(str(converted))
        bundle = export_bundle(SimpleNamespace(sessions=paths, provider=target, raw=False,
                                               to=Path(temp) / "converted.zip", db=args.db), csl, quiet=True)
        options = dict(vars(args), bundle=bundle, provider=target, target=None,
                       leaf=None, history_policy="active", repair_interrupted=False)
        return import_bundle(SimpleNamespace(**options), csl)


def convert_native(source, provider, target, path, cwd, args):
    data = canonical_input(source, provider, leaf=getattr(args, "leaf", None),
                           history_policy=getattr(args, "history_policy", "active"),
                           repair_interrupted=getattr(args, "repair_interrupted", False))
    info = restore(data, target, path, cwd)
    print(f"converted: {provider} -> {target}; canonical v{canonical.VERSION}; "
          f"events={len(data['events'])}; source policy records quarantined={len(data['source_context'])}")
    conversion_notice(data)
    return info


def conversion_notice(data):
    ignored = data.get("report", {}).get("ignored_native_state", {})
    if ignored:
        print("conversion native-state report: " + json.dumps(ignored, sort_keys=True))
    for warning in data.get("report", {}).get("warnings", []):
        print("conversion warning: " + json.dumps(warning, ensure_ascii=True))
    if data.get("report", {}).get("history_policy") == "visible":
        print("WARNING: explicit visible-history recovery; not exact compacted context restoration")
    repaired = data.get("report", {}).get("synthetic_interruption_results", [])
    if repaired:
        print(f"WARNING: {len(repaired)} historical tool results marked UNKNOWN; no tool was executed")


def convert(args, csl):
    from csl_transfer import export_bundle, import_bundle, resolve_sources
    path = Path(args.session).expanduser()
    source_provider = args.provider
    if not (path.is_file() and path.suffix.lower() == ".json"):
        sources = resolve_sources([args.session], csl, args.db, args.provider)
        path, header = sources[0]
        source_provider = header.get("provider", "codex")
    data = canonical_input(path, source_provider, leaf=args.leaf, history_policy=args.history_policy,
                           repair_interrupted=args.repair_interrupted)
    if args.to == "canonical":
        if args.open or not args.out:
            raise ValueError("Canonical export requires --out FILE.json and cannot use --open")
        if args.max_context_chars is not None:
            from csl_diagnostics import metrics
            if metrics(data["events"])["text_characters"] > args.max_context_chars:
                raise ValueError("Explicit canonical context character limit exceeded")
        canonical.write_json(args.out.expanduser(), data)
        print(f"canonical: {args.out.expanduser().resolve()} (v{canonical.VERSION}, {len(data['events'])} events)")
        return
    if args.out:
        raise ValueError("--out is for canonical JSON; native targets install a fresh session under --cwd")
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"Working directory does not exist: {cwd}")
    with tempfile.TemporaryDirectory(prefix="csl-convert-") as temp:
        conversion_notice(data)
        source = Path(temp) / "converted.jsonl"
        restore(data, args.to, source, cwd)
        bundle = export_bundle(SimpleNamespace(sessions=[str(source)], provider=args.to, raw=False,
                                               to=Path(temp) / "converted.zip", db=args.db), csl, quiet=True)
        return import_bundle(SimpleNamespace(bundle=bundle, cwd=cwd, name=args.name, list=False,
            open=args.open, open_args=args.open_args, provider=args.to, db=args.db,
            max_context_chars=args.max_context_chars), csl)


def doctor(args, csl):
    from csl_transfer import inspect_bundle, resolve_sources
    if (args.expect_text or args.absent_text) and not args.native:
        raise ValueError("Text transport assertions require --native; they are not live model recall checks")
    with tempfile.TemporaryDirectory(prefix="csl-doctor-") as temp:
        path = Path(args.session).expanduser()
        sources = []
        if path.is_file() and path.suffix.lower() == ".zip":
            import hashlib
            with zipfile.ZipFile(path) as archive:
                manifest = inspect_bundle(archive)
                for entry in manifest["sessions"]:
                    if args.provider and args.provider != entry.get("provider", "codex"):
                        raise ValueError("Package source does not match --provider")
                    local = Path(temp) / f"{entry['id']}.jsonl"
                    digest = hashlib.sha256()
                    with archive.open(entry["file"]) as stream, local.open("xb") as output:
                        for line in stream:
                            digest.update(line)
                            output.write(line)
                    if digest.hexdigest() != entry["sha256"]:
                        raise ValueError("Package checksum mismatch")
                    sources.append((local, entry))
        else:
            # Resolve paths without changing/importing the source.
            sources = resolve_sources([args.session], csl, args.db, args.provider)
        reports = []
        for source, header in sources:
            provider = header.get("provider", "codex")
            report = analyze(source, provider, leaf=args.leaf, context_window=args.context_window,
                             max_context_chars=None if args.native else args.max_context_chars)
            report["source_id"] = header["id"]
            if header.get("compaction_state_removed"):
                report["warnings"].append("lost_compaction_state: portable package removed encrypted summary state")
                report["active_context"] = "not-reconstructed"
            if args.native and not report["errors"]:
                from csl_probe import native_replay
                static = report["metrics"]
                native = native_replay(source, provider, args.model, args.expect_text, args.absent_text)
                report["native"] = native
                report["native_replay"] = native["status"]
                if native["timed_out"]:
                    report["warnings"].append("native_probe_timeout: only captured request evidence is available")
                if native.get("metrics"):
                    report["metrics"] = native["metrics"]
                    apply_budget(report, args.context_window, args.max_context_chars)
                    if (static and report.get("record_types", {}).get("compacted") and
                        native["metrics"]["text_characters"] > max(static["text_characters"] * 2, static["text_characters"] + 200_000)):
                        report["warnings"].append("native_replay_exceeds_checkpoint_estimate: inspect ignored compaction or extra context")
                if native["status"] != "request-reconstructed":
                    report["errors"].append("Native CLI did not reconstruct a measurable request")
                if native["missing_marker_indexes"] or native["forbidden_marker_indexes"]:
                    report["errors"].append("Native request text transport assertions failed (indexes reported, source text not echoed)")
            if args.max_context_chars is not None and report["metrics"] is None:
                report["errors"].append("Cannot enforce the explicit character limit: context unavailable")
            reports.append(report)
        if args.json:
            print(json.dumps({"schema_version": 1, "sessions": reports}, ensure_ascii=False, indent=2))
        else:
            for report in reports:
                print(f"{report['source_id']} [{report['provider']}]: structure={report['structure']}; "
                      f"context={report['active_context']}; native={report['native_replay']}; live=not-tested")
                if report["metrics"]:
                    print("  " + json.dumps(report["metrics"]))
                for message in report["errors"] + report["warnings"]:
                    print(f"  {message}")
        if any(report["errors"] for report in reports):
            raise SystemExit(2)


def positive(value):
    number = int(value)
    if number <= 0:
        raise ValueError("must be positive")
    return number


def add_conversion_options(parser, target=True):
    if target:
        parser.add_argument("--target", choices=("codex", "claude"), help="Explicitly convert into this CLI before importing (experimental).")
    parser.add_argument("--history-policy", choices=("active", "visible"), default="active",
                        help="Conversion context selection; visible is explicitly lossy recovery, never automatic.")
    parser.add_argument("--leaf", help="Select a Claude branch for conversion.")
    parser.add_argument("--repair-interrupted", action="store_true", help="Add marked UNKNOWN historical tool outcomes during conversion; never rerun tools.")
    parser.add_argument("--max-context-chars", type=positive, help="Reject imports exceeding this explicit active-text character budget.")


def add_commands(subparsers, csl):
    from csl_transfer import add_open_argument
    def run(function, args):
        try:
            return function(args, csl)
        except (ValueError, KeyError, TypeError, OSError, RecursionError, zipfile.BadZipFile) as exc:
            import sys
            print(f"csl {args.command}: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
    conversion = subparsers.add_parser("convert", help="Convert native/canonical conversations into a new native session or canonical JSON.")
    conversion.add_argument("session")
    conversion.add_argument("--provider", choices=("codex", "claude"), help="Optional source filter; normally auto-detected.")
    conversion.add_argument("--to", choices=("codex", "claude", "canonical"), required=True)
    conversion.add_argument("--out", type=Path, help="Destination for canonical JSON; existing files are never overwritten.")
    conversion.add_argument("--cwd", type=Path, default=Path.cwd())
    conversion.add_argument("--name")
    add_conversion_options(conversion, target=False)
    add_open_argument(conversion)
    conversion.set_defaults(func=lambda args: run(convert, args))
    diagnostic = subparsers.add_parser("doctor", help="Read-only continuity checks; optional isolated localhost native replay without model generation.")
    diagnostic.add_argument("session", help="UUID, JSONL path or .csl.zip bundle.")
    diagnostic.add_argument("--provider", choices=("codex", "claude"))
    diagnostic.add_argument("--leaf")
    diagnostic.add_argument("--json", action="store_true")
    diagnostic.add_argument("--native", action="store_true", help="Reconstruct the request using an isolated CLI against localhost, not a paid endpoint.")
    diagnostic.add_argument("--expect-text", action="append", default=[], help="Require this text in the localhost request; repeatable, not a model recall test.")
    diagnostic.add_argument("--absent-text", action="append", default=[], help="Require this text to be absent from the localhost request; repeatable.")
    diagnostic.add_argument("--model", help="Model name for the localhost replay only; not a live availability check.")
    diagnostic.add_argument("--context-window", type=positive, help="Explicit token window for approximate risk reporting, not CLI configuration.")
    diagnostic.add_argument("--max-context-chars", type=positive)
    diagnostic.set_defaults(func=lambda args: run(doctor, args))
