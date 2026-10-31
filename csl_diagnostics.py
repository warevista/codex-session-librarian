"""Continuity evidence levels: structure, active context, native replay, never live recall."""

from collections import Counter
import json
from pathlib import Path

import csl_canonical as canonical
import csl_claude
from csl_history import codex_effective


def metrics(items):
    text, opaque, multimodal = 0, 0, 0
    pending = [items]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            opaque += value.get("type") in ("compaction", "context_compaction", "compaction_summary")
            multimodal += value.get("type") in ("image", "input_image", "document", "input_file")
            for key, child in value.items():
                if key in ("encrypted_content", "thinking", "signature"):
                    continue
                if key in ("input", "output", "arguments") and isinstance(child, dict):
                    text += len(json.dumps(child, ensure_ascii=False))
                    continue
                if isinstance(child, str) and key in ("text", "output", "arguments", "input", "content"):
                    text += len(child)
                elif isinstance(child, (dict, list)):
                    pending.append(child)
    return {"input_items": len(items), "text_characters": text, "opaque_items": opaque,
            "multimodal_blocks": multimodal}


def analyze(path, provider, *, leaf=None, context_window=None, max_context_chars=None):
    result = {"provider": provider, "structure": "not-checked", "active_context": "not-reconstructed",
              "native_replay": "not-tested", "live_continuation": "not-tested",
              "errors": [], "warnings": [], "metrics": None}
    if Path(path).stat().st_size > 64 * 1024 * 1024:
        result["warnings"].append("analysis_limit: transcript exceeds the 64 MiB diagnostic limit")
        return result
    try:
        records = canonical.read_jsonl(path)
        for record in records:
            provenance = record.get("csl_conversion")
            if isinstance(provenance, dict):
                if provenance.get("history_policy") == "visible":
                    result["warnings"].append("visible_history_recovery: this history is labeled recovery, not exact original compacted context")
                if provenance.get("synthetic_tool_results"):
                    result["warnings"].append("synthetic_tool_results: interrupted historical tool outcomes were explicitly marked UNKNOWN")
        counts = Counter(record.get("type") for record in records)
        result["record_types"] = dict(counts)
        if provider == "codex":
            from csl_transfer import validate_compaction
            for record in records:
                validate_compaction(record)
            if not records or records[0].get("type") != "session_meta":
                raise ValueError("Codex history lacks its leading session metadata")
            result["structure"] = "passed"
            if any(not isinstance(record.get("timestamp"), str) for record in records):
                result["warnings"].append("native_record_timestamps_missing: the installed CLI may skip records; use native verification")
            if records[0].get("payload", {}).get("history_base"):
                result["warnings"].append("missing_ancestry: only records in this transcript can be inspected")
            try:
                effective, history_report = codex_effective(records)
            except ValueError as exc:
                result["warnings"].append(f"active_context_unavailable: {exc}")
                return result
            items = [record["payload"] for record in effective if record.get("type") == "response_item"]
            result["history_report"] = dict(history_report)
        else:
            csl_claude.validate(Path(path), _read_records)
            result["structure"] = "passed"
            try:
                chain, _, selected_leaf = canonical.claude_chain(records, leaf)
            except ValueError as exc:
                result["warnings"].append(f"active_context_unavailable: {exc}")
                return result
            items = [record["message"] for record in chain if record.get("type") in ("user", "assistant")]
            result["selected_leaf"] = selected_leaf
        result["metrics"] = metrics(items)
        if not items:
            result["warnings"].append("no_model_history: visible events alone do not establish model context")
            return result
        result["active_context"] = "reconstructed-statically"
        if result["metrics"]["opaque_items"]:
            result["warnings"].append("opaque_context: encrypted context cannot be measured or verified across accounts")
        if result["metrics"]["multimodal_blocks"]:
            result["warnings"].append("multimodal_context: text estimates exclude image/document token costs")
        apply_budget(result, context_window, max_context_chars)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result["structure"] = "failed"
        result["errors"].append(str(exc))
    return result


def _read_records(stream):
    for line in stream:
        if line.strip():
            yield canonical.strict_json(line)


def apply_budget(report, context_window=None, max_context_chars=None):
    values = report.get("metrics")
    if values is None:
        return
    chars = values["text_characters"]
    report["token_estimate"] = {"tokens": (chars + 3) // 4, "exact": False,
                               "method": "text characters / 4; excludes tokenizer, tool schema, hidden and multimodal overhead"}
    if context_window:
        report["requested_context_window"] = context_window
        if report["token_estimate"]["tokens"] >= context_window:
            report["warnings"].append("context_risk: rough text estimate reaches/exceeds the supplied window; not an exact token count")
    if chars >= 1_000_000:
        report["warnings"].append("large_context: inspect native replay and the recipient model limit before continuing")
    if max_context_chars is not None and chars > max_context_chars:
        report["errors"].append(f"Explicit context character limit exceeded: {chars} > {max_context_chars}")


def preflight(path, provider, *, max_context_chars=None):
    report = analyze(path, provider, max_context_chars=max_context_chars)
    if max_context_chars is not None and report["metrics"] is None:
        report["errors"].append("Cannot enforce an explicit context character limit without reconstructing the active context")
    if report["errors"]:
        raise ValueError("Continuity preflight failed: " + "; ".join(report["errors"]))
    print(f"continuity: structure={report['structure']}; context={report['active_context']}; native/live=not-tested")
    for warning in report["warnings"]:
        print(f"continuity warning: {warning}")
    return report
