"""Project modern visible messages for Codex's standalone legacy-history UI."""

from collections import Counter
import hashlib
import json


def message_event(record, allowed_threads):
    payload = record.get("payload", {})
    if record.get("type") != "event_msg" or payload.get("type") != "item_completed":
        return None
    if payload.get("thread_id") not in (None, *allowed_threads):
        return None
    item = payload.get("item", {})
    kind = item.get("type")
    if kind not in ("UserMessage", "AgentMessage"):
        return None
    content = item.get("content", [])
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise ValueError("Invalid modern visible-message content")
    texts, images = [], []
    for block in content:
        if block.get("type") in ("Text", "text", "input_text", "output_text"):
            if not isinstance(block.get("text"), str):
                raise ValueError("Visible message text must be a string")
            texts.append(block["text"])
        elif kind == "UserMessage" and block.get("type") in ("image", "Image"):
            reference = block.get("image", {})
            url = block.get("url") or block.get("image_url")
            if isinstance(reference, dict):
                url = url or reference.get("url") or reference.get("image_url")
            if isinstance(url, str) and url.startswith("data:image/"):
                images.append(url)
            else:
                texts.append("[External image reference retained in source item; not fetched by CSL]")
        else:
            texts.append("[Non-text content retained in source item]")
    message = "\n".join(texts)
    if not message and not images:
        return None
    output = {"type": "user_message" if kind == "UserMessage" else "agent_message", "message": message}
    if kind == "UserMessage":
        output.update(client_id=item.get("client_id"), images=images or None, local_images=[], text_elements=[])
    else:
        output["phase"] = item.get("phase")
    return output


def turn_after(record, current):
    payload = record.get("payload", {})
    if record.get("type") == "event_msg" and payload.get("type") == "task_started":
        return payload.get("turn_id") or current
    return current


def signature(payload, current):
    # Counts, rather than sets, preserve intentionally repeated identical messages.
    images = payload.get("images") or []
    body = json.dumps([payload.get("message", ""), images], ensure_ascii=False)
    return (current, payload.get("type"), hashlib.sha256(body.encode()).hexdigest())


def project_file(source, target, read_records, encode, allowed_threads):
    existing, latest = Counter(), {}
    current = None
    with source.open("rb") as stream:
        for index, record in enumerate(read_records(stream)):
            current = turn_after(record, current)
            payload = record.get("payload", {})
            if record.get("type") == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
                existing[signature(payload, current)] += 1
            converted = message_event(record, allowed_threads)
            if converted is not None:
                item = payload["item"]
                key = (payload.get("turn_id") or current, item.get("type"), item.get("id"))
                if item.get("id"):
                    latest[key] = index
    stats, current = Counter(), None
    with source.open("rb") as stream, target.open("xb") as output:
        for index, record in enumerate(read_records(stream)):
            current = turn_after(record, current)
            output.write(encode(record))
            converted = message_event(record, allowed_threads)
            if converted is None:
                continue
            payload = record["payload"]
            item = payload["item"]
            key = (payload.get("turn_id") or current, item.get("type"), item.get("id"))
            if item.get("id") and latest[key] != index:
                continue
            match = signature(converted, payload.get("turn_id") or current)
            if existing[match]:
                existing[match] -= 1
                continue
            event = {"timestamp": record.get("timestamp"), "type": "event_msg", "payload": converted,
                     "csl_display_projection": {"source_item_id": item.get("id"), "source_turn_id": payload.get("turn_id")}}
            output.write(encode(event))
            stats[converted["type"]] += 1
    return dict(stats)
