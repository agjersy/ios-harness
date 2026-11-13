"""Execute one request: route if needed, resolve inputs, run the app, route outputs."""
from __future__ import annotations

import datetime as dt
import importlib
import json
import math
import sys
import time
import uuid
from pathlib import Path

from . import PROTOCOL, actions, bridge, deps, manifest, quarantine, router, when
from .context import Ctx
from .paths import resolve
from .redact import Redactor, safe_traceback

FORGE = {"app": "forge", "name": "Build a new app"}
BUILTINS = {"forge", "repair"}  # app ids handled by the harness itself
ASKED = "_asked"  # travels in `inputs`: names the person has already been asked for
TRUE, FALSE = {"true", "yes", "y", "1", "on"}, {"false", "no", "n", "0", "off"}


class InputError(Exception):
    pass


# ---------- inputs ----------

def _find_file(raw, cfg, search_dirs) -> Path:
    s = str(raw)
    if s.startswith("@"):
        candidates = [resolve(s, cfg)]
    else:
        candidates = [Path(s)] if Path(s).is_absolute() else [Path(d) / s for d in search_dirs]
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise InputError(f"File not found: {raw}")


def _coerce(name: str, spec: dict, raw, cfg, search_dirs):
    t = spec["type"]
    if t == "file":
        path = _find_file(raw, cfg, search_dirs)
        accept = [a.lower().lstrip(".") for a in spec.get("accept", [])]
        if accept and path.suffix.lower().lstrip(".") not in accept:
            raise InputError(f"'{name}' must be one of: {', '.join(accept)}")
        return path
    if t in ("date", "datetime"):
        try:
            return when.as_date(raw) if t == "date" else when.as_datetime(raw)
        except ValueError:
            raise InputError(f"Not a date: {raw}") from None
    if t == "choice":
        if raw in spec["options"]:
            return raw
        loose = {str(o).strip().lower(): o for o in spec["options"]}
        if str(raw).strip().lower() in loose:  # dictated or retyped answers
            return loose[str(raw).strip().lower()]
        raise InputError(f"'{name}' must be one of: {', '.join(map(str, spec['options']))}")
    if t == "number":
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return raw
        text = str(raw).strip().replace(",", "")
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            number = math.nan
        if not math.isfinite(number):
            raise InputError(f"'{name}' must be a number")
        return number
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text not in TRUE | FALSE:
            raise InputError(f"'{name}' must be yes or no")
        return text in TRUE
    if t == "url":
        if not str(raw).startswith(("http://", "https://")):
            raise InputError(f"'{name}' must be a URL")
        return str(raw)
    if t == "json":
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as e:
            raise InputError(f"'{name}' is not valid JSON: {e}") from None
    return str(raw)


def _ask_spec(name: str, spec: dict) -> dict:
    out = {"name": name, "type": spec["type"], "required": bool(spec.get("required")),
           "prompt": spec.get("prompt") or name.replace("_", " ").capitalize()}
    for key in ("options", "accept", "default"):
        if key in spec:
            out[key] = spec[key]
    return out


def asked_names(provided: dict) -> set:
    """Names already put to the person. Shortcuts may flatten the list into text."""
    raw = provided.get(ASKED) or []
    items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return {str(i).strip() for i in items if str(i).strip()}


def resolve_inputs(m, provided: dict, cfg, search_dirs, asked=()) -> tuple:
    """Returns (values, ask). A non-empty ask list means the Shortcut must collect more.

    An empty answer to an optional input that was already asked for counts as
    "skip". A value that can't be read (a bad date, a missing file) is asked
    for again with an `error` note instead of failing the run.
    """
    values, ask = {}, []
    for name, spec in m.inputs.items():
        raw = provided.get(name)
        mode = spec.get("ask", "if_missing")
        from_person = True
        if raw in (None, ""):
            skipped = name in asked and not spec.get("required")
            from_person = False
            if "default" in spec and (mode != "always" or skipped):
                raw = spec["default"]
            elif mode != "never" and (spec.get("required") or mode == "always") and not skipped:
                ask.append(_ask_spec(name, spec))
                continue
            elif spec.get("required"):
                raise InputError(f"Missing required input '{name}'")
            else:
                values[name] = None
                continue
        try:
            values[name] = _coerce(name, spec, raw, cfg, search_dirs)
        except InputError as e:
            if not from_person or mode == "never":
                raise
            ask.append({**_ask_spec(name, spec), "error": str(e)})
    return values, ask


# ---------- execution ----------

def invoke(m, deps_dir: Path, ctx: Ctx, values: dict) -> dict:
    mod_name, fn_name = m.entry.split(":")
    before = set(sys.modules)
    added = [str(m.root), str(deps_dir)]
    sys.path[:0] = added  # app folder first, then its packages
    sys.modules.pop(mod_name, None)
    try:
        module = importlib.import_module(mod_name)
        result = getattr(module, fn_name)(ctx, **values)
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)
        for name in set(sys.modules) - before:  # a-Shell may reuse the interpreter
            sys.modules.pop(name, None)

    if result is None:
        return {}
    if isinstance(result, str) and len(m.outputs) == 1:
        return {next(iter(m.outputs)): result}
    if not isinstance(result, dict):
        raise TypeError(f"{m.entry} must return a dict keyed by output names")
    return result


def _as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False, default=str)


def route_outputs(m, result: dict, ctx: Ctx, confirm_all: bool = False) -> dict:
    resp, texts = {"status": "ok", "app": m.id}, {}
    # Check actions and URLs before writing files, so a bad one leaves nothing half done.
    todo = actions.validate(result.get("_actions"), m.actions, ctx.cfg, ctx.run_id, confirm_all)
    if todo:
        resp["actions"] = todo
    if result.get("_open_url"):
        resp["open_url"] = actions.check_url(result["_open_url"], m.open_url, strict=m.privacy == "strict")
        if confirm_all:  # app in quarantine: the Shortcut asks before opening
            resp["open_url_confirm"] = True
    for name, spec in m.outputs.items():
        value = result.get(name)
        if value is None:
            continue
        if spec["type"] == "file":
            path = ctx.write_text(ctx.path(spec["path"]), _as_text(value), spec.get("write", "replace"))
            resp.setdefault("files", []).append(str(path))
        else:
            for target in spec.get("to", ["show"]):
                texts.setdefault(target, []).append(_as_text(value))
    if "_show" in result:
        texts.setdefault("show", []).append(_as_text(result["_show"]))
    for target, chunks in texts.items():
        resp[target] = "\n\n".join(chunks)
    if ctx.messages:
        resp["log"] = ctx.messages
    if ctx.dry_run:
        resp["dry_run_writes"] = ctx.writes
    return resp


# ---------- routing ----------

def _filled(inputs) -> dict:
    """Shortcuts sends keys it has no value for as empty text. Those are not answers."""
    return {k: v for k, v in (inputs or {}).items() if v not in (None, "")} if isinstance(inputs, dict) else {}


def _menu(choices: list) -> dict:
    menu = {}
    for c in choices:
        menu[c["name"] if c["name"] not in menu else f"{c['name']} ({c['app']})"] = c["app"]
    return menu


def _plain(inputs: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in inputs.items()}


def route_request(req: dict, cfg) -> dict:
    catalog = router.load_catalog(cfg)
    apps = {a["id"]: a for a in catalog}
    everything = [{"app": a["id"], "name": a["name"]} for a in catalog] + [FORGE]
    utterance = (req.get("utterance") or "").strip()
    if not utterance:
        return {"status": "choose", "choices": everything, "menu": _menu(everything)}

    attachments = req.get("attachments") or []
    # The utterance goes to a model: take the person's identifiers out first,
    # and put them back into whatever inputs the model lifts from it.
    redactor = Redactor(cfg.identifiers)
    r = router.route(redactor.redact(utterance), cfg, catalog, attachments)
    app = apps.get(r["app"])
    inferred = {}
    if app:  # files come from attachments, never from the model
        for name, value in redactor.restore(r["inputs"]).items():
            spec = app["inputs"].get(name)
            if not spec or spec.get("type") == "file" or value in (None, ""):
                continue
            try:  # a guess the app can't read is dropped, so the person is asked instead
                _coerce(name, spec, value, cfg, [])
            except Exception:
                continue
            inferred[name] = value
        inputs = dict(inferred)
        file_inputs = [n for n, s in app["inputs"].items() if s.get("type") == "file"]
        for name, attachment in zip(file_inputs, attachments):
            inputs[name] = attachment
        inputs.update(_filled(req.get("inputs")))
        if r["confidence"] >= cfg.router_threshold and not app["confirm"]:
            return {"status": "run", "app": app["id"], "inputs": inputs}

    ranked = [i for i in [r["app"], *r["alternatives"]] if i in apps]
    choices = [{"app": i, "name": apps[i]["name"]} for i in dict.fromkeys(ranked)] + [FORGE] if ranked else everything
    resp = {"status": "choose", "choices": choices, "utterance": utterance, "menu": _menu(choices)}
    if app:
        resp["suggested"] = app["id"]
        # Keep the guess so picking this app doesn't ask for what was already said.
        if inferred and req.get("id") and app.get("privacy") != "strict":
            stash = {"app": app["id"], "inputs": _plain(inferred)}
            (cfg.pending / f"{req['id']}.json").write_text(json.dumps(stash, default=str), encoding="utf-8")
    if r.get("warning"):
        resp["warning"] = r["warning"]
    return resp


def _take_pending(cfg, run_id: str, app_id: str) -> dict:
    """Inputs the router lifted from the utterance before the person picked this app."""
    path = cfg.pending / f"{run_id}.json"
    try:
        stash = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    path.unlink(missing_ok=True)
    return stash.get("inputs", {}) if stash.get("app") == app_id and isinstance(stash.get("inputs"), dict) else {}


def _log_run(cfg, entry: dict) -> None:
    with open(cfg.runs_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def handle(req: dict, cfg) -> dict:
    start = time.monotonic()
    resp, ctx, m = {"id": None, "protocol": PROTOCOL}, None, None
    app_id, run_id, logged_error = req.get("app") or None, None, None
    try:
        run_id = resp["id"] = bridge.check_id(req["id"]) if req.get("id") else uuid.uuid4().hex[:12]
        if app_id is not None:
            app_id = app_id.strip() if isinstance(app_id, str) else ""
            if not manifest.ID_RE.match(app_id):
                bad, app_id = app_id, None
                raise InputError(f"'{bad[:40]}' is not an app id")
        if app_id in BUILTINS:
            from . import forge, repair  # imported here: forge -> sync -> runner
            builtin = forge.handle if app_id == "forge" else repair.handle
            resp.update(builtin({**req, "id": run_id}, cfg))
            return resp
        stashed = {}
        if not app_id:
            routed = route_request({**req, "id": req.get("id") and run_id}, cfg)
            if routed["status"] != "run":
                resp.update(routed)
                return resp
            app_id = routed["app"]
            req = {**req, "app": app_id, "inputs": routed["inputs"]}
        else:
            stashed = _take_pending(cfg, run_id, app_id)

        if not (cfg.apps_dir / app_id / "app.toml").exists():
            raise InputError(f"No app named '{app_id}'")
        m = manifest.load(cfg.apps_dir / app_id)
        if m.privacy == "strict":
            resp["privacy"] = "strict"  # tells the bridge to keep this response off disk
        provided = req.get("inputs") if isinstance(req.get("inputs"), dict) else {}
        asked = asked_names(provided)
        provided = {**stashed, **_filled(provided)}
        provided.pop(ASKED, None)
        unfilled = [n for n, s in m.inputs.items() if s.get("type") == "file" and not provided.get(n)]
        for name, attachment in zip(unfilled, req.get("attachments") or []):
            provided[name] = attachment
        values, ask = resolve_inputs(m, provided, cfg, [cfg.inbox / run_id, cfg.inbox, m.root], asked)
        if ask:
            for item in ask:
                if item.get("error"):
                    provided.pop(item["name"], None)  # don't send the unreadable value back
            echo = {**_plain(provided), ASKED: ",".join(sorted(asked | {a["name"] for a in ask}))}
            resp.update(status="ask", app=m.id, ask=ask, inputs=echo)
            return resp

        deps_dir = deps.ensure(m, cfg)
        files = [v for v in values.values() if isinstance(v, Path)]
        ctx = Ctx(m, cfg, values, run_id, extra_read=[cfg.inbox / run_id, *files])
        on_probation = quarantine.remaining(cfg, m.id) > 0
        result = invoke(m, deps_dir, ctx, values)
        resp.update(route_outputs(m, result, ctx, confirm_all=on_probation))
        if on_probation:
            resp["quarantine_runs_left"] = quarantine.tick(cfg, m.id)

        elapsed = time.monotonic() - start
        if elapsed > m.timeout_s:
            resp["warning"] = f"Took {elapsed:.0f}s, over the {m.timeout_s}s budget"
    except Exception as e:
        # The person sees the full message. What is stored, and what `repair`
        # later sends to a model, is redacted; strict apps keep no message at all.
        strict = m is not None and m.privacy == "strict"
        redactor = ctx.redactor if ctx else Redactor(cfg.identifiers)
        resp.update(status="error", app=app_id, error=f"{type(e).__name__}: {e}")
        logged_error = type(e).__name__ if strict else redactor.redact(resp["error"])
        # Only a crash in the app itself is something `repair` can fix.
        if ctx is not None:
            (cfg.errors / f"{app_id}.txt").write_text(safe_traceback(e, redactor, withhold=strict),
                                                      encoding="utf-8")
            resp["repairable"] = True  # the Shortcut may offer {"app": "repair", "inputs": {"target": ...}}
    finally:
        _log_run(cfg, {
            "ts": dt.datetime.now().isoformat(timespec="seconds"), "id": run_id,
            "app": app_id, "status": resp.get("status"),
            "ms": int((time.monotonic() - start) * 1000),
            "ai": ctx.calls if ctx else [], "error": logged_error,
            "actions": [a["type"] for a in resp.get("actions", [])],
        })
    return resp
