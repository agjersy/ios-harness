"""Validate every app, install its packages, smoke-test it, rebuild the catalog.

Only apps that pass every step reach the catalog, so Shortcuts menus and
the router never offer a broken app.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import traceback

from . import bridge, deps, quarantine, router
from .config import pkg_name
from .context import Ctx
from .manifest import ManifestError, load
from .paths import ALIAS_REF
from .runner import invoke, resolve_inputs, route_outputs

DISCOURAGED_IMPORTS = {"subprocess", "shutil", "ctypes", "multiprocessing"}
# Ways around ctx.ai and ctx.fetch. Errors under the hard lint, warnings otherwise.
BYPASS_IMPORTS = {"urllib", "http", "socket", "ssl", "requests", "httpx", "ftplib", "smtplib",
                  "imaplib", "poplib", "webbrowser", "importlib", "asyncio", "xmlrpc", "telnetlib",
                  "nntplib", "pip", "runpy", "builtins", "sys"}
# Ways around ctx.read_text and ctx.write_text. Checked only by the hard lint.
FILE_IMPORTS = {"pathlib", "tempfile", "glob"}
BYPASS_CALLS = {"eval", "exec", "__import__", "compile", "globals"}
REFLECT_CALLS = {"getattr", "setattr", "delattr", "vars"}
DISCOURAGED_OS_CALLS = {"remove", "unlink", "rename", "replace", "rmdir", "removedirs", "makedirs",
                        "system", "popen"}
SAFE_OS = {"path", "sep", "linesep", "fspath"}  # all the hard lint lets an app use from os
CTX_PUBLIC = {"ai", "read_text", "read_bytes", "list", "write_text", "fetch", "secret", "path",
              "log", "inputs", "run_id", "dry_run", "app"}


def check_against_config(m, cfg, secrets: bool = True) -> list:
    errors = []
    for dep in m.dependencies:
        if pkg_name(dep) not in cfg.allowlist:
            errors.append(f"dependency '{dep}' is not on the allowlist in config.toml")
    for cap in m.ai_uses:
        if cap not in cfg.models:
            errors.append(f"AI capability '{cap}' has no model in config.toml")
    refs = m.permissions.get("read", []) + m.permissions.get("write", [])
    refs += [s["path"] for s in m.outputs.values() if s.get("type") == "file"]
    for ref in refs:
        match = ALIAS_REF.match(ref)
        if match and match.group(1) not in cfg.aliases:
            errors.append(f"alias @{match.group(1)} is not defined on this device")
    if secrets:
        errors += [f"secret {name} is not set on this device; add it to secrets.toml"
                   for name in missing_secrets(m, cfg)]
    return errors


def missing_secrets(m, cfg) -> list:
    missing = []
    for name in m.permissions.get("secrets", []):
        try:
            cfg.secret(name)
        except KeyError:
            missing.append(name)
    return missing


def _root_name(node):
    """'ctx' for ctx, ctx.ai, ctx.ai.gate ..."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def lint(m, hard: bool = False) -> tuple:
    """Flag code that bypasses ctx. Returns (errors, warnings).

    For ordinary apps these are warnings: some hand-written apps need them.
    The hard lint (strict apps, Forge builds, and apps still in quarantine)
    turns every way around ctx into an error: network and process modules,
    file access that skips ctx, private parts of ctx, and code under tests/.
    This catches mistakes in generated code. It is not a sandbox.
    """
    errors, warnings = [], []
    hard = hard or m.privacy == "strict"
    bypass = errors if hard else warnings
    entry_fn = m.entry.split(":")[1]
    for py in sorted(m.root.rglob("*.py")):
        rel = py.relative_to(m.root)
        if "tests" in rel.parts and not hard:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (SyntaxError, ValueError) as e:
            errors.append(f"{rel}: syntax error on line {getattr(e, 'lineno', '?')}")
            continue
        ctx_names = {"ctx"} | {n.args.args[0].arg for n in ast.walk(tree)
                               if isinstance(n, ast.FunctionDef) and n.name == entry_fn and n.args.args}
        for node in ast.walk(tree):
            at = f"{rel}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for n in names:
                    top = n.split(".")[0]
                    if top in DISCOURAGED_IMPORTS:
                        bypass.append(f"{at} imports {n}")
                    elif top in BYPASS_IMPORTS:
                        bypass.append(f"{at} imports {n}; use ctx.ai or ctx.fetch")
                    elif top in FILE_IMPORTS and hard:
                        errors.append(f"{at} imports {n}; use ctx.read_text, ctx.list and ctx.write_text")
                    elif top == "tests" and "tests" not in rel.parts:
                        bypass.append(f"{at} imports from tests/; app code must not depend on its tests")
                if isinstance(node, ast.ImportFrom) and node.module == "os" and hard:
                    for a in node.names:
                        if a.name not in SAFE_OS:
                            errors.append(f"{at} imports os.{a.name}; only os.path is allowed")
            elif isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id in ctx_names:
                    if node.attr == "cfg":
                        bypass.append(f"{at} reads ctx.cfg; use ctx.secret and ctx.path")
                    elif node.attr not in CTX_PUBLIC:
                        bypass.append(f"{at} touches ctx.{node.attr}, which is not part of the app contract")
                elif (isinstance(node.value, ast.Attribute) and node.value.attr == "ai"
                      and isinstance(node.value.value, ast.Name) and node.value.value.id in ctx_names):
                    bypass.append(f"{at} touches ctx.ai.{node.attr}; call ctx.ai(...) only")
                elif isinstance(node.value, ast.Name) and node.value.id == "os":
                    if hard and node.attr not in SAFE_OS:
                        errors.append(f"{at} uses os.{node.attr}; only os.path is allowed")
                    elif not hard and node.attr in DISCOURAGED_OS_CALLS:
                        warnings.append(f"{at} calls os.{node.attr}")
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id in BYPASS_CALLS:
                    bypass.append(f"{at} calls {f.id}()")
                elif isinstance(f, ast.Name) and f.id in REFLECT_CALLS and node.args \
                        and _root_name(node.args[0]) in ctx_names:
                    bypass.append(f"{at} calls {f.id}() on ctx")
                elif isinstance(f, ast.Name) and f.id == "open":
                    mode = node.args[1] if len(node.args) > 1 else next(
                        (k.value for k in node.keywords if k.arg == "mode"), None)
                    if hard:
                        errors.append(f"{at} calls open(); use ctx.read_text or ctx.write_text")
                    elif isinstance(mode, ast.Constant) and any(c in str(mode.value) for c in "wax+"):
                        warnings.append(f"{at} writes with open(); use ctx.write_text")
    return errors, warnings


def _mock_models(cfg, replies) -> dict:
    """Mock every capability. smoke.py may script replies: AI = {"fast": ["{...}", ...]}."""
    mock = {cap: {"provider": "mock"} for cap in cfg.models}
    for cap, reply in (replies or {}).items():
        items = list(reply) if isinstance(reply, (list, tuple)) else [reply]
        mock[cap] = {"provider": "mock", "reply": [r if isinstance(r, str) else json.dumps(r) for r in items]}
    return mock


def smoke(m, cfg, deps_dir):
    """Run the app with mock AI and no file writes. Returns an error string or None."""
    test_file = m.test.get("smoke")
    if not test_file:
        return None
    try:
        spec = importlib.util.spec_from_file_location(f"_smoke_{m.id.replace('-', '_')}", m.root / test_file)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        values, ask = resolve_inputs(m, getattr(mod, "INPUTS", {}), cfg, [m.root])
        if ask:
            return "smoke INPUTS is missing or wrong: " + ", ".join(
                a["name"] + (f" ({a['error']})" if a.get("error") else "") for a in ask)
        replies = getattr(mod, "AI", None)
        if replies is not None and not isinstance(replies, dict):
            return "smoke AI must be a dict of capability -> reply or list of replies"
        ctx = Ctx(m, cfg, values, "smoke", dry_run=True, models=_mock_models(cfg, replies),
                  fetch_mock=getattr(mod, "FETCH", None))
        result = invoke(m, deps_dir, ctx, values)
        response = route_outputs(m, result, ctx)
        check = getattr(mod, "check", None)
        if check:
            check(result, response)
    except Exception:
        return "smoke test failed:\n" + traceback.format_exc(limit=3)
    return None


def check_candidate(cfg, app_dir, hard: bool = False, secrets: bool = True) -> tuple:
    """Everything an app folder must pass: manifest, device config, lint, smoke test.
    Returns (manifest or None, errors, warnings). Used by sync, Forge and repair."""
    try:
        m = load(app_dir)
    except ManifestError as e:
        return None, e.errors, []
    lint_errors, warnings = lint(m, hard=hard)
    errors = check_against_config(m, cfg, secrets=secrets) + lint_errors
    if errors:
        return m, errors, warnings
    try:
        failure = smoke(m, cfg, deps.ensure(m, cfg))
    except deps.DepsError as e:
        failure = str(e)
    return m, [failure] if failure else [], warnings


def sync(cfg, force: bool = False, run_tests: bool = True) -> dict:
    report = {"ok": [], "failed": {}, "warnings": {}}
    if not cfg.apps_dir.is_dir():
        report["failed"]["(repo)"] = [f"No apps folder at {cfg.apps_dir}"]
        return report

    healthy = []
    for app_dir in sorted(p for p in cfg.apps_dir.iterdir() if (p / "app.toml").exists()):
        try:
            m = load(app_dir)
        except ManifestError as e:
            report["failed"][app_dir.name] = e.errors
            continue
        try:  # one broken app must not stop the others from syncing
            # Apps Forge built stay under the hard lint until they leave quarantine.
            lint_errors, warnings = lint(m, hard=quarantine.remaining(cfg, m.id) > 0)
            errors = check_against_config(m, cfg) + lint_errors
            if warnings:
                report["warnings"][m.id] = warnings
            if errors:
                report["failed"][m.id] = errors
                continue
            deps_dir = deps.ensure(m, cfg, force=force)
            if run_tests:
                err = smoke(m, cfg, deps_dir)
                if err:
                    report["failed"][m.id] = [err]
                    continue
        except Exception as e:
            report["failed"][m.id] = [f"{type(e).__name__}: {e}"]
            continue
        healthy.append(m)
        report["ok"].append(m.id)

    router.write_catalog(cfg, healthy)
    bridge.cleanup(cfg)
    return report
