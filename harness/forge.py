"""Forge: build a new app from a spoken request.

Runs as the built-in app id "forge", one short step per request:

1. Draft. A model writes only the manifest. The harness describes, in its own
   words, what that manifest lets the app touch, and asks for a yes.
2. Build. A model writes main.py and tests/smoke.py against the approved
   manifest, which is frozen. The result must pass validation, the hard lint
   and its smoke test; failures go back to the model for a few rounds.
3. Install. The app is copied to apps/<id>/ and starts in quarantine.

Forge writes only to its own staging folder and to a new apps/<id>/ folder.
The app's code is run once, offline, by the smoke test before you have read
it. Review the diff in Working Copy before committing.
"""
from __future__ import annotations

import json
import re
import shutil

from . import deps, quarantine
from .ai import AIClient, AIError
from .config import tomllib
from .contract import text as contract_text
from .manifest import ID_RE, RESERVED_IDS, ManifestError, load
from .redact import Redactor
from .sync import check_against_config, lint, smoke, sync

BUILD, CANCEL = "Build it", "Cancel"
STUB = "def run(ctx, **inputs):\n    return {}\n"
FILE_RE = re.compile(r"^(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py$")

DRAFT_PROMPT = """You design one small app for a Python harness on iOS. Write only its manifest.

{contract}
## This device

- Folder aliases: {aliases}
- AI capabilities: {capabilities}
- Allowed packages: {packages}
- App ids already taken: {taken}

## Rules

- Ask for the least access that does the job. Every permission is shown to the person for approval.
- Use privacy = "strict" if the app reads financial, medical or identity documents.
- Include [test] smoke = "tests/smoke.py" and at least two [router] examples.
- Prefer no dependencies.

## Request

{request}
{problems}
Reply with JSON only: {{"id": "<kebab-case id>", "manifest": "<the full app.toml text>"}}"""

CODE_PROMPT = """Write the code for one app for a Python harness on iOS.
The manifest below is final: write code that fits it and do not return an app.toml.

{contract}
## Request

{request}

## Manifest (app.toml)

{manifest}

## An existing app, for style

{example}
{problems}
Reply with JSON only: {{"files": {{"main.py": "<full file>", "tests/smoke.py": "<full file>"}}}}"""


class ForgeError(Exception):
    pass


# ---------- what the person approves ----------

def describe(m) -> str:
    """The app's reach, read from the manifest by the harness, not by the model."""
    perms = m.permissions
    file_outputs = [s["path"] for s in m.outputs.values() if s.get("type") == "file"]
    rows = [
        ("Reads", perms.get("read", [])),
        ("Writes", sorted(set(perms.get("write", [])) | set(file_outputs))),
        ("Calls", perms.get("net", [])),
        ("Secrets", perms.get("secrets", [])),
        ("Models", m.ai_uses),
        ("Actions", list(m.actions)),
        ("Packages", m.dependencies),
    ]
    lines = [f"{m.name} ({m.id})", m.description, ""]
    lines += [f"{label}: {', '.join(values)}" for label, values in rows if values]
    if not any(values for _, values in rows):
        lines.append("Touches nothing outside its own inputs.")
    lines.append("Privacy: strict, identifiers are redacted before any model call"
                 if m.privacy == "strict" else "Privacy: standard, inputs are sent to models as they are")
    return "\n".join(lines)


# ---------- steps ----------

def _ask_model(cfg, prompt: str) -> dict:
    out = AIClient(cfg)(cfg.forge_capability, prompt, max_tokens=8000, as_json=True)
    if not isinstance(out, dict):
        raise ForgeError("The model did not return a JSON object")
    return out


def _problems(errors: list, previous=None) -> str:
    if not errors:
        return ""
    block = "\n## Your last attempt was rejected. Fix these problems.\n\n" + "\n".join(f"- {e}" for e in errors)
    if previous:
        block += "\n\nYour last attempt:\n" + json.dumps(previous, indent=1)
    return block + "\n"


def _check_manifest(cfg, app_dir) -> tuple:
    """Returns (manifest or None, errors)."""
    try:
        m = load(app_dir)
    except ManifestError as e:
        return None, e.errors
    errors = check_against_config(m, cfg)
    if m.test.get("smoke") != "tests/smoke.py":
        errors.append('[test] smoke must be "tests/smoke.py"')
    return m, errors


def draft(cfg, state_dir, request: str) -> dict:
    taken = sorted(p.name for p in cfg.apps_dir.iterdir() if p.is_dir()) + sorted(RESERVED_IDS)
    errors, previous = [], None
    for _ in range(cfg.forge_rounds + 1):
        out = _ask_model(cfg, DRAFT_PROMPT.format(
            contract=contract_text(), aliases=", ".join(f"@{a}" for a in sorted(cfg.aliases)),
            capabilities=", ".join(sorted(cfg.models)), packages=", ".join(sorted(cfg.allowlist)) or "none",
            taken=", ".join(taken), request=request, problems=_problems(errors, previous)))
        app_id, toml_text = str(out.get("id", "")), str(out.get("manifest", ""))
        previous = {"id": app_id, "manifest": toml_text}
        if not ID_RE.match(app_id):
            errors = ["id must be kebab-case"]
        elif app_id in taken:
            errors = [f"id '{app_id}' is already taken"]
        else:
            app_dir = state_dir / "staging" / app_id
            shutil.rmtree(state_dir / "staging", ignore_errors=True)
            app_dir.mkdir(parents=True)
            (app_dir / "app.toml").write_text(toml_text, encoding="utf-8")
            (app_dir / "main.py").write_text(STUB, encoding="utf-8")  # so the entry check passes
            m, errors = _check_manifest(cfg, app_dir)
            if not errors:
                state = {"request": request, "app": app_id}
                (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
                return {"status": "ask", "app": "forge", "inputs": {"request": request},
                        "ask": [{"name": "approve", "type": "choice", "options": [BUILD, CANCEL],
                                 "prompt": describe(m) + "\n\nBuild this app?"}]}
    raise ForgeError("Could not draft a valid manifest: " + "; ".join(errors))


def _write_files(app_dir, files) -> list:
    if not isinstance(files, dict) or "main.py" not in files or "tests/smoke.py" not in files:
        return ['reply must include "main.py" and "tests/smoke.py" under "files"']
    errors = []
    for rel, body in files.items():
        if not FILE_RE.match(str(rel)) or not isinstance(body, str):
            errors.append(f"{rel}: only .py files inside the app folder are accepted")
            continue
        target = app_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return errors


def build(cfg, state_dir, state: dict) -> dict:
    app_id = state["app"]
    app_dir = state_dir / "staging" / app_id
    frozen = (app_dir / "app.toml").read_text(encoding="utf-8")
    example = cfg.apps_dir / "voice-to-vault"
    example_text = "\n\n".join(f"--- {name}\n{(example / name).read_text(encoding='utf-8')}"
                               for name in ("app.toml", "main.py", "tests/smoke.py")
                               if (example / name).exists()) or "(none available)"
    errors, previous, warnings = [], None, []
    for _ in range(cfg.forge_rounds + 1):
        out = _ask_model(cfg, CODE_PROMPT.format(
            contract=contract_text(), request=state["request"], manifest=frozen,
            example=example_text, problems=_problems(errors, previous)))
        previous = out.get("files")
        errors = _write_files(app_dir, previous)
        if errors:
            continue
        m, errors = _check_manifest(cfg, app_dir)
        if errors:
            continue
        errors, warnings = lint(m, hard=True)
        if errors:
            continue
        failure = smoke(m, cfg, deps.ensure(m, cfg))
        if not failure:
            return install(cfg, state_dir, m, warnings)
        errors = [failure]
    raise ForgeError(f"The code for {app_id} did not pass after {cfg.forge_rounds + 1} attempts. "
                     f"Last problems: " + "; ".join(errors)[:1500])


def install(cfg, state_dir, m, warnings: list) -> dict:
    target = cfg.apps_dir / m.id
    if target.exists():
        raise ForgeError(f"apps/{m.id} already exists")
    shutil.copytree(m.root, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    quarantine.add(cfg, m.id, cfg.quarantine_runs)
    report = sync(cfg)
    shutil.rmtree(state_dir, ignore_errors=True)
    if m.id not in report["ok"]:
        raise ForgeError(f"Installed apps/{m.id} but sync rejected it: "
                         + "; ".join(report["failed"].get(m.id, ["unknown reason"])))
    lines = [f"Installed {m.name} as apps/{m.id}.",
             f"For its first {cfg.quarantine_runs} runs every action will ask before it happens.",
             "Review the new folder in Working Copy before you commit it."]
    lines += [f"Note: {w}" for w in warnings]
    return {"status": "ok", "app": "forge", "built": m.id, "show": "\n".join(lines)}


# ---------- entry point ----------

def handle(req: dict, cfg) -> dict:
    """Called by runner.handle for app id 'forge'. Returns a normal response dict."""
    inputs = dict(req.get("inputs") or {})
    state_dir = cfg.forge / req["id"]
    state_file = state_dir / "state.json"
    try:
        if state_file.exists():
            choice = inputs.get("approve")
            if choice == BUILD:
                return build(cfg, state_dir, json.loads(state_file.read_text(encoding="utf-8")))
            shutil.rmtree(state_dir, ignore_errors=True)
            return {"status": "ok", "app": "forge", "show": "Cancelled. Nothing was installed."}

        request = str(inputs.get("request") or req.get("utterance") or "").strip()
        if not request:
            return {"status": "ask", "app": "forge", "inputs": inputs,
                    "ask": [{"name": "request", "type": "dictation",
                             "prompt": "What should the new app do?"}]}
        state_dir.mkdir(parents=True, exist_ok=True)
        # The request goes to a model, so take your identifiers out of it first.
        return draft(cfg, state_dir, Redactor(cfg.identifiers).redact(request))
    except (ForgeError, AIError, KeyError, tomllib.TOMLDecodeError) as e:
        return {"status": "error", "app": "forge", "error": str(e)}
