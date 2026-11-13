"""Typed actions: side effects an app asks for and the Shortcut performs.

An app returns {"_actions": [{"type": "reminder.create", "title": ...}]}.
The harness checks each one against the app's [actions].allow list and the
schema below, then passes it to Shortcuts, which runs the Shortcut named in
its "shortcut" field with the action as input. Adding a type means adding an
entry here and one small Shortcut; Harness Run itself never changes.

Tiers: "write" changes something on the device; "outbound" reaches other
people or other Shortcuts and always asks first.
"""
from __future__ import annotations

import datetime as dt

from . import when

SHORTCUT_PREFIX = "Harness Act "
DISPATCHER = "Harness Act"  # one Shortcut for every type, if [actions] single_shortcut is on
URL_ALWAYS_REFUSED = {"shortcuts", "javascript", "data", "file"}

# field -> (kind, required). Kinds: str, when, bool, strlist.
ACTIONS = {
    "reminder.create": {"tier": "write", "label": "Create reminder", "fields": {
        "title": ("str", True), "due": ("when", False), "notes": ("str", False), "list": ("str", False)}},
    "event.create": {"tier": "write", "label": "Create event", "fields": {
        "title": ("str", True), "start": ("when", True), "end": ("when", False), "all_day": ("bool", False),
        "location": ("str", False), "notes": ("str", False), "calendar": ("str", False)}},
    "note.create": {"tier": "write", "label": "Create note", "fields": {
        "body": ("str", True), "title": ("str", False), "folder": ("str", False)}},
    "message.draft": {"tier": "outbound", "label": "Draft message", "fields": {
        "to": ("strlist", True), "body": ("str", True)}},
    "mail.draft": {"tier": "outbound", "label": "Draft email", "fields": {
        "to": ("strlist", True), "subject": ("str", True), "body": ("str", True)}},
    "shortcut.run": {"tier": "outbound", "label": "Run shortcut", "fields": {
        "name": ("str", True), "input": ("str", False)}},
}


class ActionError(Exception):
    pass


def _when(value) -> str:
    """Date -> 'YYYY-MM-DD' (no time of day); datetime -> ISO 8601 to the second."""
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    try:
        parsed = when.parse(value)
    except ValueError:
        raise ActionError(f"not a date or ISO date-time: {value!r}") from None
    return parsed.isoformat(timespec="seconds") if isinstance(parsed, dt.datetime) else parsed.isoformat()


def _coerce(kind: str, value):
    if kind == "when":
        return _when(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise ActionError(f"expected true or false, got {value!r}")
        return value
    if kind == "strlist":
        items = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else []
        if not items or not all(isinstance(i, str) and i.strip() for i in items):
            raise ActionError("expected one or more non-empty strings")
        return items
    if not isinstance(value, str) or not value.strip():
        raise ActionError("expected non-empty text")
    return value


def _summary(kind: str, a: dict) -> str:
    """One line a person can approve without reading JSON."""
    main = a.get("title") or a.get("subject") or a.get("name") or a.get("body", "")
    main = main if len(main) <= 80 else main[:77] + "..."
    extra = [f"{k} {', '.join(a[k]) if isinstance(a[k], list) else a[k]}"
             for k in ("to", "due", "start", "list", "calendar") if a.get(k)]
    return f"{ACTIONS[kind]['label']}: {main}" + (f" ({'; '.join(extra)})" if extra else "")


def validate(raw, allowed, cfg, run_id: str, force_confirm: bool = False) -> list:
    """Check and normalize an app's _actions. Raises ActionError on the first problem."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ActionError("_actions must be a list")
    out = []
    for n, item in enumerate(raw, 1):
        where = f"action {n}"
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ActionError(f"{where} must be a dict with a 'type'")
        kind = item["type"]
        where = f"{where} ({kind})"
        if kind not in ACTIONS:
            raise ActionError(f"{where}: unknown type; known types are {', '.join(sorted(ACTIONS))}")
        if kind not in allowed:
            raise ActionError(f"{where}: not declared in [actions].allow")
        spec = ACTIONS[kind]
        unknown = set(item) - set(spec["fields"]) - {"type", "confirm"}
        if unknown:
            raise ActionError(f"{where}: unknown fields {sorted(unknown)}")
        action = {"id": f"{run_id}-{n}", "type": kind, "tier": spec["tier"],
                  "shortcut": DISPATCHER if cfg.single_action_shortcut else SHORTCUT_PREFIX + kind}
        for field, (field_kind, required) in spec["fields"].items():
            value = item.get(field)
            if value is None or value == "":
                if required:
                    raise ActionError(f"{where}: '{field}' is required")
                continue
            try:
                action[field] = _coerce(field_kind, value)
            except ActionError as e:
                raise ActionError(f"{where}: '{field}' {e}") from None
        if kind == "shortcut.run" and action["name"] not in cfg.action_shortcuts:
            raise ActionError(f"{where}: '{action['name']}' is not listed under [actions].shortcuts in config.toml")
        action["confirm"] = spec["tier"] == "outbound" or force_confirm or item.get("confirm") is True
        action["summary"] = _summary(kind, action)
        out.append(action)
    return out


def check_url(url, schemes, strict: bool = False) -> str:
    """Check an app's _open_url against the schemes its manifest declares.

    Opening a URL can send data anywhere and can start other apps, so it is
    declared ([actions] open_url), shown at Forge approval, and refused for
    strict apps. shortcuts:// is never allowed: use the shortcut.run action,
    which has its own allowlist."""
    url = str(url).strip()
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if strict:
        raise ActionError("a strict app may not open URLs")
    if not scheme or scheme in URL_ALWAYS_REFUSED:
        raise ActionError(f"_open_url may not use the scheme '{scheme or url[:20]}'")
    if scheme not in schemes:
        raise ActionError(f"_open_url scheme '{scheme}' is not declared in [actions].open_url")
    return url
