"""Execute one request: route if needed, resolve inputs, run the app, route outputs."""
from __future__ import annotations

import datetime as dt
import importlib
import json
import sys
import time
import traceback
import uuid
from pathlib import Path

from . import deps, manifest, router
from .context import Ctx
from .paths import resolve

RELATIVE_DAYS = {"today": 0, "yesterday": -1, "tomorrow": 1}


class InputError(Exception):
    pass


# ---------- inputs ----------

def _parse_date(value) -> dt.date:
    if isinstance(value, dt.date):
        return value
    s = str(value).strip().lower()
    if s in RELATIVE_DAYS:
        return dt.date.today() + dt.timedelta(days=RELATIVE_DAYS[s])
    try:
        return dt.date.fromisoformat(s[:10])
    except ValueError:
        raise InputError(f"Not a date: {value}") from None


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
    if t == "date":
        return _parse_date(raw)
    if t == "choice":
        if raw not in spec["options"]:
            raise InputError(f"'{name}' must be one of: {', '.join(map(str, spec['options']))}")
        return raw
    if t == "url":
        if not str(raw).startswith(("http://", "https://")):
            raise InputError(f"'{name}' must be a URL")
        return str(raw)
    if t == "json":
        return json.loads(raw) if isinstance(raw, str) else raw
    return str(raw)


def _ask_spec(name: str, spec: dict) -> dict:
    out = {"name": name, "type": spec["type"],
           "prompt": spec.get("prompt") or name.replace("_", " ").capitalize()}
    for key in ("options", "accept"):
        if key in spec:
            out[key] = spec[key]
    return out


def resolve_inputs(m, provided: dict, cfg, search_dirs) -> tuple:
    """Returns (values, ask). A non-empty ask list means the Shortcut must collect more."""
    values, ask = {}, []
    for name, spec in m.inputs.items():
        raw = provided.get(name)
        mode = spec.get("ask", "if_missing")
        if raw in (None, ""):
            if "default" in spec and mode != "always":
                raw = spec["default"]
            elif mode != "never" and (spec.get("required") or mode == "always"):
                ask.append(_ask_spec(name, spec))
                continue
            elif spec.get("required"):
                raise InputError(f"Missing required input '{name}'")
            else:
                values[name] = None
                continue
        values[name] = _coerce(name, spec, raw, cfg, search_dirs)
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


def route_outputs(m, result: dict, ctx: Ctx) -> dict:
    resp, texts = {"status": "ok", "app": m.id}, {}
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
    if "_open_url" in result:
        resp["open_url"] = str(result["_open_url"])
    if ctx.messages:
        resp["log"] = ctx.messages
    if ctx.dry_run:
        resp["dry_run_writes"] = ctx.writes
    return resp


# ---------- routing ----------

def route_request(req: dict, cfg) -> dict:
    catalog = router.load_catalog(cfg)
    apps = {a["id"]: a for a in catalog}
    menu = [{"app": a["id"], "name": a["name"]} for a in catalog]
    utterance = (req.get("utterance") or "").strip()
    if not utterance:
        return {"status": "choose", "choices": menu}

    attachments = req.get("attachments") or []
    r = router.route(utterance, cfg, catalog, attachments)
    app = apps.get(r["app"])
    if app:  # files come from attachments, never from the model
        inputs = {k: v for k, v in r["inputs"].items()
                  if k in app["inputs"] and app["inputs"][k].get("type") != "file"}
        file_inputs = [n for n, s in app["inputs"].items() if s.get("type") == "file" and n not in inputs]
        for name, attachment in zip(file_inputs, attachments):
            inputs[name] = attachment
        inputs.update(req.get("inputs") or {})
        if r["confidence"] >= cfg.router_threshold and not app["confirm"]:
            return {"status": "run", "app": app["id"], "inputs": inputs}

    ranked = [i for i in [r["app"], *r["alternatives"]] if i in apps]
    choices = [{"app": i, "name": apps[i]["name"]} for i in dict.fromkeys(ranked)] or menu
    resp = {"status": "choose", "choices": choices, "utterance": utterance}
    if r.get("warning"):
        resp["warning"] = r["warning"]
    return resp


def _log_run(cfg, entry: dict) -> None:
    with open(cfg.runs_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def handle(req: dict, cfg) -> dict:
    run_id = req.get("id") or uuid.uuid4().hex[:12]
    start = time.monotonic()
    resp, ctx, app_id = {"id": run_id}, None, req.get("app")
    try:
        if not app_id:
            routed = route_request(req, cfg)
            if routed["status"] != "run":
                resp.update(routed)
                return resp
            app_id = routed["app"]
            req = {**req, "app": app_id, "inputs": routed["inputs"]}

        m = manifest.load(cfg.apps_dir / app_id)
        provided = req.get("inputs") or {}
        values, ask = resolve_inputs(m, provided, cfg, [cfg.inbox / run_id, cfg.inbox, m.root])
        if ask:
            resp.update(status="ask", app=m.id, ask=ask, inputs=provided)
            return resp

        deps_dir = deps.ensure(m, cfg)
        files = [v for v in values.values() if isinstance(v, Path)]
        ctx = Ctx(m, cfg, values, run_id, extra_read=[cfg.inbox / run_id, *files])
        result = invoke(m, deps_dir, ctx, values)
        resp.update(route_outputs(m, result, ctx))

        elapsed = time.monotonic() - start
        if elapsed > m.timeout_s:
            resp["warning"] = f"Took {elapsed:.0f}s, over the {m.timeout_s}s budget"
    except Exception as e:
        resp.update(status="error", app=app_id, error=f"{type(e).__name__}: {e}")
        if app_id:
            (cfg.errors / f"{app_id}.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finally:
        _log_run(cfg, {
            "ts": dt.datetime.now().isoformat(timespec="seconds"), "id": run_id,
            "app": app_id, "status": resp.get("status"),
            "ms": int((time.monotonic() - start) * 1000),
            "ai": ctx.calls if ctx else [], "error": resp.get("error"),
        })
    return resp
