"""Turn free speech into {app, inputs, confidence} with one model call."""
from __future__ import annotations

import json
from datetime import datetime

from .ai import AIClient, AIError

SYSTEM = "You route spoken requests to the right app. Reply with JSON only."

PROMPT = """Apps:
{catalog}

Attachments: {attachments}
Request: {utterance}

Pick the best app and fill any non-file inputs you can infer from the request.
Reply with JSON only:
{{"app": "<id or null>", "inputs": {{}}, "confidence": <0.0 to 1.0>, "alternatives": ["<id>"]}}"""


def catalog_entry(m) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "description": m.description,
        "mode": m.mode,
        "privacy": m.privacy,
        "actions": list(m.actions),
        "confirm": bool(m.router.get("confirm", False)),
        "examples": m.router.get("examples", []),
        "inputs": {
            name: {k: v for k, v in spec.items()
                   if k in ("type", "required", "prompt", "options", "accept", "ask", "default")}
            for name, spec in m.inputs.items()
        },
    }


def write_catalog(cfg, manifests: list) -> None:
    data = {"generated": datetime.now().isoformat(timespec="seconds"),
            "apps": [catalog_entry(m) for m in manifests]}
    cfg.catalog_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def load_catalog(cfg) -> list:
    if not cfg.catalog_path.exists():
        raise RuntimeError("No catalog yet: run `python -m harness sync` first")
    return json.loads(cfg.catalog_path.read_text(encoding="utf-8"))["apps"]


def route(utterance: str, cfg, catalog: list, attachments=()) -> dict:
    slim = [{"id": a["id"], "description": a["description"], "examples": a["examples"],
             "inputs": {n: {k: v for k, v in s.items() if k in ("type", "options")}
                        for n, s in a["inputs"].items()}}
            for a in catalog]
    prompt = PROMPT.format(catalog=json.dumps(slim, indent=1, default=str),
                           attachments=", ".join(attachments) or "none", utterance=utterance)
    try:
        out = AIClient(cfg)(cfg.router_capability, prompt, system=SYSTEM, max_tokens=400, as_json=True)
    except AIError as e:
        return {"app": None, "inputs": {}, "confidence": 0.0, "alternatives": [], "warning": str(e)}
    if not isinstance(out, dict):
        out = {}
    try:
        confidence = max(0.0, min(1.0, float(out.get("confidence") or 0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "app": out.get("app"),
        "inputs": out.get("inputs") if isinstance(out.get("inputs"), dict) else {},
        "confidence": confidence,
        "alternatives": [a for a in out.get("alternatives") or [] if isinstance(a, str)],
    }
