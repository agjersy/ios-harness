"""Probation for apps Forge installed: their first runs confirm every action.

State is per device (quarantine.json), so a model-written manifest can't
opt out of it.
"""
from __future__ import annotations

import json
import os


def _load(cfg) -> dict:
    try:
        return json.loads(cfg.quarantine_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(cfg, data: dict) -> None:
    tmp = cfg.quarantine_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, cfg.quarantine_path)


def remaining(cfg, app_id: str) -> int:
    return int(_load(cfg).get(app_id, 0))


def add(cfg, app_id: str, runs: int) -> None:
    data = _load(cfg)
    data[app_id] = runs
    _save(cfg, data)


def tick(cfg, app_id: str) -> int:
    """Count one clean run. Returns runs left; 0 means the app has graduated."""
    data = _load(cfg)
    left = max(0, int(data.get(app_id, 0)) - 1)
    if left:
        data[app_id] = left
    else:
        data.pop(app_id, None)
    _save(cfg, data)
    return left


def promote(cfg, app_id: str) -> bool:
    data = _load(cfg)
    found = data.pop(app_id, None) is not None
    _save(cfg, data)
    return found
