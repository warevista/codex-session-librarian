"""Portable text, embedded image/PDF and historical reference codecs. No fetching."""

import hashlib
import json

import csl_media as pdf_assets

image = pdf_assets.image


def blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        raise ValueError("Unsupported content shape")
    result = []
    for block in content:
        if not isinstance(block, dict):
            raise ValueError("Content blocks must be objects")
        kind = block.get("type")
        if kind in ("text", "Text", "input_text", "output_text"):
            if not isinstance(block.get("text"), str):
                raise ValueError("Text must be a string")
            references = block.get("references", [])
            if "references" in block:
                reference_array(references)
            if "references" in block and (block.get("annotations") or block.get("citations")):
                raise ValueError("Ambiguous canonical and native references")
            if not references:
                references = [{"kind": kind, "data": item} for key, kind in (("annotations", "annotation"), ("citations", "citation"))
                              for item in reference_array(block.get(key, []))]
            checked = reference_array(references)
            if any(r.get("kind") not in ("annotation", "citation") or not isinstance(r.get("data"), dict) or set(r) - {"kind", "data"} for r in checked):
                raise ValueError("Invalid canonical reference")
            normalized = {"type": "text", "text": block["text"]}
            if checked:
                normalized["references"] = checked
            result.append(normalized)
        elif kind in ("document", "input_file"):
            if kind == "input_file":
                data = block.get("file_data", "")
                if not isinstance(data, str) or not data.startswith("data:application/pdf;base64,"):
                    raise ValueError("PDF file references require explicit packaging; no file or URL was fetched")
                result.append(pdf_assets.document(data.split(",", 1)[1], block.get("filename", "attachment.pdf")))
            elif "source" in block:
                source = block["source"]
                if not isinstance(source, dict) or source.get("type") != "base64" or source.get("media_type") != "application/pdf":
                    raise ValueError("PDF references require embedded bytes; no file or URL was fetched")
                result.append(pdf_assets.document(source.get("data"), block.get("title", "attachment.pdf")))
            elif block.get("mime_type") == "application/pdf":
                result.append(pdf_assets.document(block.get("data"), block.get("name", "attachment.pdf"), block.get("sha256")))
            else:
                raise ValueError("Unsupported document MIME")
        elif kind == "input_image":
            url = block.get("image_url")
            if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
                raise ValueError("External image references require explicit asset packaging; no fetch performed")
            mime, data = url[5:].split(";base64,", 1)
            result.append(image(mime, data))
        elif kind == "image":
            if "source" in block:
                source = block["source"]
                if not isinstance(source, dict) or source.get("type") != "base64":
                    raise ValueError("External image references require explicit asset packaging; no fetch performed")
                result.append(image(source.get("media_type"), source.get("data")))
            else:
                result.append(image(block.get("mime_type"), block.get("data"), block.get("sha256")))
        else:
            raise ValueError("Unimplemented content block; explicit adapter required")
    return result


def native(content, provider, role="user"):
    output = []
    for block in blocks(content):
        if block["type"] == "text":
            kind = "text" if provider == "claude" else "output_text" if role == "assistant" else "input_text"
            value = block["text"]
            if block.get("references"):
                value += "\n[Historical references; source locations not revalidated]\n" + json.dumps(block["references"], ensure_ascii=False, sort_keys=True)
            output.append({"type": kind, "text": value})
        elif block["type"] == "document":
            pages = pdf_assets.project(block)
            if provider == "claude" and role == "user":
                output.append({"type": "document", "title": block["name"], "source": {
                    "type": "base64", "media_type": "application/pdf", "data": block["data"]}})
            else:
                label = "text" if provider == "claude" else "input_text"
                output.append({"type": label, "text": f"[PDF projection: {block['name']}; {len(pages)} pages. Original bytes retained in transcript metadata. Page images are bounded-resolution views.]"})
                for number, page in enumerate(pages, 1):
                    output.append({"type": label, "text": f"[PDF page {number}]\n{page['text']}"})
                    output.extend(native([image("image/png", page["png"])], provider, "user"))
        elif provider == "claude":
            output.append({"type": "image", "source": {"type": "base64", "media_type": block["mime_type"], "data": block["data"]}})
        else:
            output.append({"type": "input_image", "image_url": f"data:{block['mime_type']};base64,{block['data']}"})
    return output


def reference_array(value):
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, dict) for v in value):
        raise ValueError("References must be a bounded array of objects")
    try:
        if len(json.dumps(value, allow_nan=False)) > 64 * 1024:
            raise ValueError("References exceed byte limit")
    except (TypeError, RecursionError) as exc:
        raise ValueError("References must be finite JSON") from exc
    return value


def projection_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def encode(values, provider, role="user"):
    output, spans = [], []
    for block in blocks(values):
        projection = native([block], provider, role)
        if block["type"] == "document" or block.get("references"):
            spans.append({"start": len(output), "length": len(projection), "block": block,
                          "provider": provider, "role": role,
                          "projection_sha256": projection_hash(projection)})
        output.extend(projection)
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
        if restored["type"] != "document" and not restored.get("references"):
            raise ValueError("Portable spans may only restore documents/references")
        if span.get("provider") not in ("codex", "claude") or span.get("role") not in ("user", "assistant", "tool"):
            raise ValueError("Invalid portable projection profile")
        if projection_hash(native([restored], span["provider"], span["role"])) != span["projection_sha256"]:
            raise ValueError("Original asset and native projection are inconsistent")
        output.extend(values[cursor:start])
        output.append(restored)
        cursor = start + length
    output.extend(values[cursor:])
    return output
