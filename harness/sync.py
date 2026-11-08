"""Validate every app, install its packages, smoke-test it, rebuild the catalog.

Only apps that pass every step reach the catalog, so Shortcuts menus and
the router never offer a broken app.
"""
from __future__ import annotations

import ast
import importlib.util
import traceback

from . import bridge, deps, router
from .config import pkg_name
from .context import Ctx
from .manifest import ManifestError, load
from .paths import ALIAS_REF
from .runner import invoke, resolve_inputs, route_outputs

DISCOURAGED_IMPORTS = {"subprocess", "socket", "shutil", "ctypes", "multiprocessing"}
DISCOURAGED_OS_CALLS = {"remove", "unlink", "rename", "replace", "rmdir", "removedirs", "makedirs"}


def check_against_config(m, cfg) -> list:
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
    return errors


def lint(m) -> tuple:
    """Flag code that bypasses ctx. Warnings, not errors: some apps need these."""
    errors, warnings = [], []
    for py in m.root.rglob("*.py"):
        rel = py.relative_to(m.root)
        if "tests" in rel.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as e:
            errors.append(f"{rel}: syntax error on line {e.lineno}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for n in names:
                    if n.split(".")[0] in DISCOURAGED_IMPORTS:
                        warnings.append(f"{rel}:{node.lineno} imports {n}")
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name) and f.id == "open":
                    mode = node.args[1] if len(node.args) > 1 else next(
                        (k.value for k in node.keywords if k.arg == "mode"), None)
                    if isinstance(mode, ast.Constant) and any(c in str(mode.value) for c in "wax+"):
                        warnings.append(f"{rel}:{node.lineno} writes with open(); use ctx.write_text")
                elif (isinstance(f, ast.Attribute) and f.attr in DISCOURAGED_OS_CALLS
                      and isinstance(f.value, ast.Name) and f.value.id == "os"):
                    warnings.append(f"{rel}:{node.lineno} calls os.{f.attr}")
    return errors, warnings


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
            return "smoke INPUTS is missing: " + ", ".join(a["name"] for a in ask)
        mock = {cap: {"provider": "mock"} for cap in cfg.models}
        ctx = Ctx(m, cfg, values, "smoke", dry_run=True, models=mock)
        result = invoke(m, deps_dir, ctx, values)
        response = route_outputs(m, result, ctx)
        check = getattr(mod, "check", None)
        if check:
            check(result, response)
    except Exception:
        return "smoke test failed:\n" + traceback.format_exc(limit=3)
    return None


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
        lint_errors, warnings = lint(m)
        errors = check_against_config(m, cfg) + lint_errors
        if warnings:
            report["warnings"][m.id] = warnings
        if errors:
            report["failed"][m.id] = errors
            continue
        try:
            deps_dir = deps.ensure(m, cfg, force=force)
        except deps.DepsError as e:
            report["failed"][m.id] = [str(e)]
            continue
        if run_tests:
            err = smoke(m, cfg, deps_dir)
            if err:
                report["failed"][m.id] = [err]
                continue
        healthy.append(m)
        report["ok"].append(m.id)

    router.write_catalog(cfg, healthy)
    bridge.cleanup(cfg)
    return report
