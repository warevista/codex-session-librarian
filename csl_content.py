"""Bounded text/reference codec. Unsupported assets are rejected, never fetched."""

import hashlib
import json


def reference_array(value):
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, dict) for v in value):
        raise ValueError("References must be a bounded array of objects")
    if len(json.dumps(value, allow_nan=False)) > 64 * 1024:
        raise ValueError("References exceed byte limit")
    return value


def blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise ValueError("Unsupported content shape")
    output = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Content blocks must be objects")
        if block.get("type") not in ("text", "Text", "input_text", "output_text"):
            raise ValueError("Cross-CLI conversion currently supports text/references and text tools only; images, PDFs and external assets require an explicit adapter")
        if not isinstance(block.get("text"), str):
            raise ValueError("Text must be a string")
        references = reference_array(block.get("references", []))
        if references and (block.get("annotations") or block.get("citations")):
            raise ValueError("Ambiguous canonical/native references")
        if not references:
            references = [{"kind": kind, "data": entry}
                          for key, kind in (("annotations", "annotation"), ("citations", "citation"))
                          for entry in reference_array(block.get(key, []))]
        if any(r.get("kind") not in ("annotation", "citation") or not isinstance(r.get("data"), dict) or set(r) - {"kind", "data"} for r in references):
            raise ValueError("Invalid canonical reference")
        output.append({"type": "text", "text": block["text"], **({"references": references} if references else {})})
    return output


def projection_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def native(content, provider, role="user"):
    output = []
    for block in blocks(content):
        text = block["text"]
        if block.get("references"):
            text += "\n[Historical references; source locations not revalidated]\n" + json.dumps(block["references"], ensure_ascii=False, sort_keys=True)
        kind = "text" if provider == "claude" else "output_text" if role == "assistant" else "input_text"
        output.append({"type": kind, "text": text})
    return output


def encode(content, provider, role="user"):
    output, spans = [], []
    for block in blocks(content):
        projected = native([block], provider, role)
        if block.get("references"):
            spans.append({"start": len(output), "length": len(projected), "block": block,
                          "provider": provider, "role": role, "projection_sha256": projection_hash(projected)})
        output.extend(projected)
    return output, spans


def restore_spans(values, spans=None):
    if not spans:
        return values
    if not isinstance(values, list) or not isinstance(spans, list) or len(spans) > len(values):
        raise ValueError("Invalid portable content spans")
    output, cursor = [], 0
    for span in spans:
        if not isinstance(span, dict):
            raise ValueError("Invalid portable content span")
        start, length = span.get("start"), span.get("length")
        if type(start) is not int or type(length) is not int or start < cursor or length < 1 or start + length > len(values):
            raise ValueError("Overlapping/out-of-range portable content span")
        projected = values[start:start + length]
        if projection_hash(projected) != span.get("projection_sha256"):
            raise ValueError("Native projection changed; refusing stale portable metadata")
        restored = blocks([span.get("block")])[0]
        if not restored.get("references") or span.get("provider") not in ("codex", "claude") or span.get("role") not in ("user", "assistant", "tool"):
            raise ValueError("Invalid reference projection profile")
        if projection_hash(native([restored], span["provider"], span["role"])) != span["projection_sha256"]:
            raise ValueError("Original reference and native projection are inconsistent")
        output.extend(values[cursor:start])
        output.append(restored)
        cursor = start + length
    output.extend(values[cursor:])
    return output
