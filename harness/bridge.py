"""File-based bridge to Shortcuts.

A Shortcut uses a-Shell's Put File to drop inbox/<id>.json (plus any
attachments in inbox/<id>/), runs `python -m harness run --request <id>`,
then uses Get File to read outbox/<id>.json.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time


def _decode(raw: bytes) -> str:
    """Shortcuts may write UTF-8 with a BOM, or UTF-16."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig")


def _loads(text: str):
    """Parse JSON that Shortcuts may have wrapped in an extra pair of quotes.

    Seen on device: "{"app":"","utterance":"two words"}" (raw quotes around
    the object when a value contains spaces). Also handles a properly escaped
    JSON string that itself contains JSON.
    """
    text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        if not (len(text) >= 2 and text[0] == '"' and text[-1] == '"'):
            raise
        inner = text[1:-1]
        try:
            value = json.loads(inner)
        except json.JSONDecodeError:
            # Last resort: nested objects that were also raw-quoted, e.g.
            # "inputs":"{"text":"two words"}"
            value = json.loads(re.sub(r'\}"(?=\s*[,}])', "}", inner.replace(':"{', ":{")))
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        value = json.loads(value)
    return value


def read_request(cfg, req_id: str) -> dict:
    path = cfg.inbox / f"{req_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No request file {path.name} in inbox")
    req = _loads(_decode(path.read_bytes()))
    if not isinstance(req, dict):
        raise ValueError("Request file must contain a JSON object")
    req["id"] = req_id  # the file name is authoritative

    # Shortcuts flattens nested values into text; unpack them.
    inputs = req.get("inputs")
    if isinstance(inputs, str):
        req["inputs"] = _loads(inputs) if inputs.strip().lstrip('"').startswith("{") else {}
    att = req.get("attachments")
    if isinstance(att, str):
        att = att.strip()
        req["attachments"] = json.loads(att) if att.startswith("[") else [att] if att else []

    # Files dropped in inbox/<id>/ are this request's attachments.
    folder = cfg.inbox / req_id
    if not req.get("attachments") and folder.is_dir():
        req["attachments"] = sorted(p.name for p in folder.iterdir()
                                    if p.is_file() and not p.name.startswith("."))
    return req


def write_response(cfg, req_id: str, resp: dict) -> None:
    out = cfg.outbox / f"{req_id}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(resp, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out)


def cleanup(cfg, days: int = 7) -> int:
    cutoff, removed = time.time() - days * 86400, 0
    for folder in (cfg.inbox, cfg.outbox):
        for item in folder.iterdir():
            if item.stat().st_mtime < cutoff:
                shutil.rmtree(item) if item.is_dir() else item.unlink()
                removed += 1
    return removed
