"""File-based bridge to Shortcuts.

A Shortcut uses a-Shell's Put File to drop inbox/<id>.json (plus any
attachments in inbox/<id>/), runs `python -m harness run --request <id>`,
then uses Get File to read outbox/<id>.json.
"""
from __future__ import annotations

import json
import os
import shutil
import time


def read_request(cfg, req_id: str) -> dict:
    path = cfg.inbox / f"{req_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"No request file {path.name} in inbox")
    req = json.loads(path.read_text(encoding="utf-8"))
    req.setdefault("id", req_id)
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
