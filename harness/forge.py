"""Forge: build a new app, or revise an existing one, from a spoken request.

Runs as the built-in app id "forge". Every request makes at most one model
call, so no single Shortcut run takes long:

1. Draft. A model writes only the manifest. The harness describes, in its own
   words, what that manifest lets the app touch, and asks: Build it, Change
   it, or Cancel. "Change it" takes spoken feedback and drafts again.
2. Build. A model writes main.py and tests/smoke.py against the approved
   manifest, which is frozen. The result must pass validation, the hard lint
   and its smoke test. If it doesn't, the reply is status "continue": the
   Shortcut resends the same request and the problems go back to the model.
3. Install. The app is copied to apps/<id>/ and starts in quarantine.

To revise an app, send inputs.target = "<app id>" with the request. The same
steps run against the existing files, and the old folder is kept in
forge/_backups for a week.

Forge writes only to its own staging folder and to apps/<id>/. The app's
code is run once, offline, by the smoke test before you have read it.
Review the diff in Working Copy before committing.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime

from . import quarantine
from .ai import AIClient, AIError
from .config import tomllib
from .contract import text as contract_text
from .manifest import ID_RE, RESERVED_IDS, ManifestError, load
from .redact import TOKEN_RE, Redactor
from .sync import check_against_config, check_candidate, missing_secrets, sync

BUILD, CHANGE, CANCEL = "Build it", "Change it", "Cancel"
STUB = "def run(ctx, **inputs):\n    return {}\n"
FILE_RE = re.compile(r"^(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py$")
BACKUPS = "_backups"  # not a valid request id, so it can't collide with a state folder
TOKEN_RULE = ("Tokens like [[NAME_1]] stand for personal details that were removed from the request. "
              "Never copy a token into the manifest or the code: make that value an input instead.")

DRAFT_PROMPT = """You design one small app for a Python harness on iOS. Write only its manifest.

{contract}
## This device

- Folder aliases: {aliases}
- AI capabilities: {capabilities}
- Allowed packages: {packages}
- App ids already taken: {taken}
- Shortcuts the shortcut.run action may start: {shortcuts}

## Rules

- Ask for the least access that does the job. Every permission is shown to the person for approval.
- Use privacy = "strict" if the app reads financial, medical or identity documents.
- Include [test] smoke = "tests/smoke.py" and at least two [router] examples.
- Prefer no dependencies.
- {token_rule}

## Request

{request}
{existing}{feedback}{problems}
Reply with JSON only: {{"id": "<kebab-case id>", "manifest": "<the full app.toml text>"}}"""

CODE_PROMPT = """Write the code for one app for a Python harness on iOS.
The manifest below is final: write code that fits it and do not return an app.toml.
Return every .py file the app needs; files you leave out will not exist.
{token_rule}

{contract}
## Request

{request}

## Manifest (app.toml)

{manifest}
{existing}
## An existing app, for style

{example}
{problems}
Reply with JSON only: {{"files": {{"main.py": "<full file>", "tests/smoke.py": "<full file>"}}}}"""


class ForgeError(Exception):
    pass


# ---------- what the person approves ----------

def describe(m, cfg=None) -> str:
    """The app's reach, read from the manifest by the harness, not by the model."""
    perms = m.permissions
    file_outputs = [s["path"] for s in m.outputs.values() if s.get("type") == "file"]
    targets = sorted({t for s in m.outputs.values() if s.get("type") == "text" for t in s.get("to", ["show"])})
    access = [
        ("Reads", perms.get("read", [])),
        ("Writes", sorted(set(perms.get("write", [])) | set(file_outputs))),
        ("Calls", [f"{host} and its subdomains" for host in perms.get("net", [])]),
        ("Secrets", perms.get("secrets", [])),
        ("Models", m.ai_uses),
        ("Actions", list(m.actions)),
        ("Opens URLs", [f"{scheme}://" for scheme in m.open_url]),
        ("Packages", m.dependencies),
    ]
    lines = [f"{m.name} ({m.id})", m.description, ""]
    if m.inputs:
        lines.append("Asks for: " + ", ".join(f"{name} ({spec.get('type')})" for name, spec in m.inputs.items()))
    if targets:
        lines.append("Replies by: " + ", ".join(targets))
    lines += [f"{label}: {', '.join(values)}" for label, values in access if values]
    if not any(values for _, values in access):
        lines.append("Touches nothing outside its own inputs.")
    lines.append("Privacy: strict, identifiers are redacted before any model call"
                 if m.privacy == "strict" else "Privacy: standard, inputs are sent to models as they are")
    if cfg is not None:
        missing = missing_secrets(m, cfg)
        if missing:
            lines.append(f"Before it can run, add to secrets.toml: {', '.join(missing)}")
    return "\n".join(lines)


# ---------- helpers ----------

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


def _sources(app_dir) -> str:
    names = ["app.toml"] + sorted(str(p.relative_to(app_dir)) for p in app_dir.rglob("*.py"))
    return "\n\n".join(f"--- {name}\n{(app_dir / name).read_text(encoding='utf-8')}"
                       for name in names if (app_dir / name).exists())


def _check_manifest(cfg, app_dir) -> tuple:
    """Returns (manifest or None, errors). A secret that isn't on the device
    yet is not an error: the person is told to add it."""
    try:
        m = load(app_dir)
    except ManifestError as e:
        return None, e.errors
    errors = check_against_config(m, cfg, secrets=False)
    if m.test.get("smoke") != "tests/smoke.py":
        errors.append('[test] smoke must be "tests/smoke.py"')
    return m, errors


def _save(state_dir, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def _echo(state: dict, **extra) -> dict:
    """The inputs the Shortcut sends back with its next request."""
    out = {"request": state["request"], **extra}
    if state.get("target"):
        out["target"] = state["target"]
    return out


def _again(cfg, state_dir, state: dict, what: str, errors: list, previous) -> dict:
    """Record a failed round. Returns a "continue" reply, or raises when rounds run out."""
    state["round"] += 1
    total = cfg.forge_rounds + 1
    if state["round"] >= total:
        shutil.rmtree(state_dir, ignore_errors=True)
        raise ForgeError(f"The {what} for {state.get('app') or 'the app'} did not pass after {total} "
                         f"attempt{'s' if total != 1 else ''}. Last problems: " + "; ".join(errors)[:1500])
    state.update(errors=errors, previous=previous)
    _save(state_dir, state)
    extra = {"approve": BUILD} if state["stage"] == "build" else {}
    return {"status": "continue", "app": "forge", "inputs": _echo(state, **extra),
            "show": f"Attempt {state['round']} of {total} at the {what} had problems. Trying again."}


# ---------- steps: one model call each ----------

def draft(cfg, state_dir, state: dict) -> dict:
    target = state.get("target")
    taken = sorted(p.name for p in cfg.apps_dir.iterdir() if p.is_dir()) + sorted(RESERVED_IDS)
    existing = feedback = ""
    if target:
        existing = (f"\n## You are revising this existing app\n\nKeep its id, \"{target}\". Change only what "
                    f"the request needs.\n\n{_sources(cfg.apps_dir / target)}\n")
    if state.get("feedback"):
        feedback = ("\n## The person saw this draft and asked for changes\n\n"
                    + "\n".join(f"- {f}" for f in state["feedback"])
                    + f"\n\nThe draft they saw:\n{state.get('base', '')}\n")
    out = _ask_model(cfg, DRAFT_PROMPT.format(
        contract=contract_text(), aliases=", ".join(f"@{a}" for a in sorted(cfg.aliases)),
        capabilities=", ".join(sorted(cfg.models)), packages=", ".join(sorted(cfg.allowlist)) or "none",
        taken=", ".join(taken), shortcuts=", ".join(sorted(cfg.action_shortcuts)) or "none",
        token_rule=TOKEN_RULE, request=state["request"], existing=existing, feedback=feedback,
        problems=_problems(state.get("errors") or [], state.get("previous"))))
    app_id, toml_text = str(out.get("id", "")), str(out.get("manifest", ""))
    m = None
    if not ID_RE.match(app_id):
        errors = ["id must be kebab-case"]
    elif target and app_id != target:
        errors = [f"id must stay '{target}'"]
    elif not target and app_id in taken:
        errors = [f"id '{app_id}' is already taken"]
    elif TOKEN_RE.search(toml_text):
        errors = ["the manifest contains a [[TOKEN]]; " + TOKEN_RULE]
    else:
        app_dir = state_dir / "staging" / app_id
        shutil.rmtree(state_dir / "staging", ignore_errors=True)
        app_dir.mkdir(parents=True)
        (app_dir / "app.toml").write_text(toml_text, encoding="utf-8")
        (app_dir / "main.py").write_text(STUB, encoding="utf-8")  # so the entry check passes
        m, errors = _check_manifest(cfg, app_dir)
    if errors:
        state["app"] = app_id if ID_RE.match(app_id) else None
        return _again(cfg, state_dir, state, "manifest", errors, {"id": app_id, "manifest": toml_text})

    state.update(stage="approve", app=app_id, round=0, errors=[], previous=None, base=toml_text)
    _save(state_dir, state)
    prompt = describe(m, cfg)
    if target:
        prompt += f"\n\nThis replaces the installed app {target}. The old version is kept for a week."
    return {"status": "ask", "app": "forge", "inputs": _echo(state),
            "ask": [{"name": "approve", "type": "choice", "options": [BUILD, CHANGE, CANCEL],
                     "required": True, "prompt": prompt + "\n\nBuild this app?"}]}


def _reset_staging(cfg, app_dir, target) -> None:
    """Start every round from the manifest alone, so nothing from a rejected
    attempt is installed. A revised app keeps its non-code files (fixtures)."""
    for item in list(app_dir.iterdir()):
        if item.name != "app.toml":
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    if target:
        source = cfg.apps_dir / target
        for path in sorted(source.rglob("*")):
            rel = path.relative_to(source)
            if path.is_file() and path.suffix not in (".py", ".pyc") and str(rel) != "app.toml" \
                    and "__pycache__" not in rel.parts:
                (app_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, app_dir / rel)


def _write_files(app_dir, files) -> list:
    if not isinstance(files, dict) or "main.py" not in files or "tests/smoke.py" not in files:
        return ['reply must include "main.py" and "tests/smoke.py" under "files"']
    errors = []
    for rel, body in files.items():
        if not FILE_RE.match(str(rel)) or not isinstance(body, str):
            errors.append(f"{rel}: only .py files inside the app folder are accepted")
            continue
        if TOKEN_RE.search(body):
            errors.append(f"{rel} contains a [[TOKEN]]; " + TOKEN_RULE)
        target = app_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return errors


def build(cfg, state_dir, state: dict) -> dict:
    app_id, target = state["app"], state.get("target")
    app_dir = state_dir / "staging" / app_id
    frozen = (app_dir / "app.toml").read_text(encoding="utf-8")
    examples = ["voice-to-vault"] + (["remind-me"] if "[actions]" in frozen else [])
    example_text = "\n\n".join(_sources(cfg.apps_dir / name) for name in examples
                               if name != target and (cfg.apps_dir / name / "app.toml").exists()) \
        or "(none available)"
    existing = f"\n## The app's current code, which you are revising\n\n{_sources(cfg.apps_dir / target)}\n" \
        if target else ""
    out = _ask_model(cfg, CODE_PROMPT.format(
        contract=contract_text(), token_rule=TOKEN_RULE, request=state["request"], manifest=frozen,
        existing=existing, example=example_text,
        problems=_problems(state.get("errors") or [], state.get("previous"))))
    files = out.get("files")
    _reset_staging(cfg, app_dir, target)
    errors, warnings, m = _write_files(app_dir, files), [], None
    if not errors:
        m, errors, warnings = check_candidate(cfg, app_dir, hard=True, secrets=False)
    if errors:
        return _again(cfg, state_dir, state, "code", errors, files if isinstance(files, dict) else None)
    return install(cfg, state_dir, m, warnings, replace=bool(target))


def install(cfg, state_dir, m, warnings: list, replace: bool = False) -> dict:
    target = cfg.apps_dir / m.id
    backup, was_quarantined = None, quarantine.remaining(cfg, m.id)
    if target.exists():
        if not replace:
            raise ForgeError(f"apps/{m.id} already exists")
        backup = cfg.forge / BACKUPS / f"{m.id}-{datetime.now():%Y%m%d-%H%M%S}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(backup))
    shutil.copytree(m.root, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    quarantine.add(cfg, m.id, cfg.quarantine_runs)
    missing = missing_secrets(m, cfg)
    report = sync(cfg)
    shutil.rmtree(state_dir, ignore_errors=True)
    if m.id not in report["ok"] and not missing:
        # Put things back the way they were: no half-installed app is left runnable.
        shutil.rmtree(target, ignore_errors=True)
        quarantine.promote(cfg, m.id)
        if backup:
            shutil.move(str(backup), str(target))
            if was_quarantined:
                quarantine.add(cfg, m.id, was_quarantined)
        sync(cfg)
        raise ForgeError(f"apps/{m.id} was not installed because sync rejected it: "
                         + "; ".join(report["failed"].get(m.id, ["unknown reason"])))
    lines = [f"{'Updated' if replace else 'Installed'} {m.name} as apps/{m.id}.",
             f"For its next {cfg.quarantine_runs} runs every action will ask before it happens.",
             "Review the new folder in Working Copy before you commit it."]
    if backup:
        lines.append(f"The old version is in forge/{BACKUPS}/{backup.name} for a week.")
    if missing:
        lines.append(f"It is not in the menu yet: add {', '.join(missing)} to secrets.toml, "
                     "then run `python -m harness sync`.")
    lines += [f"Note: {w}" for w in warnings]
    return {"status": "ok", "app": "forge", "built": m.id, "show": "\n".join(lines)}


# ---------- entry point ----------

def _cancel(state_dir) -> dict:
    shutil.rmtree(state_dir, ignore_errors=True)
    return {"status": "ok", "app": "forge", "show": "Cancelled. Nothing was installed."}


def handle(req: dict, cfg) -> dict:
    """Called by runner.handle for app id 'forge'. Returns a normal response dict."""
    inputs = {k: v for k, v in (req.get("inputs") or {}).items() if v not in (None, "")} \
        if isinstance(req.get("inputs"), dict) else {}
    state_dir = cfg.forge / req["id"]
    state_file = state_dir / "state.json"
    redactor = Redactor(cfg.identifiers)  # everything here goes to a model
    request = redactor.redact(str(inputs.get("request") or req.get("utterance") or "").strip())
    approve = inputs.get("approve")
    if approve:  # a dictated or retyped "build it" still counts
        approve = {c.lower(): c for c in (BUILD, CHANGE, CANCEL)}.get(str(approve).strip().lower(), approve)
    try:
        state = None
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except ValueError:
                state = None
        # An abandoned draft under a reused id must not swallow a new request.
        if state and not approve and request and request != state.get("request"):
            state = None
        if state is None:
            shutil.rmtree(state_dir, ignore_errors=True)

        if state:
            stage = state.get("stage")
            if approve == CANCEL:
                return _cancel(state_dir)
            if stage == "draft":
                return draft(cfg, state_dir, state)
            if stage == "build":
                return build(cfg, state_dir, state)
            if approve == BUILD:
                state.update(stage="build", round=0, errors=[], previous=None)
                _save(state_dir, state)
                return build(cfg, state_dir, state)
            if approve == CHANGE:
                feedback = redactor.redact(str(inputs.get("feedback") or "").strip())
                if not feedback:
                    return {"status": "ask", "app": "forge", "inputs": _echo(state, approve=CHANGE),
                            "ask": [{"name": "feedback", "type": "dictation", "required": True,
                                     "prompt": "What should change?"}]}
                state.update(stage="draft", round=0, errors=[], previous=None,
                             feedback=[*(state.get("feedback") or []), feedback])
                _save(state_dir, state)
                return draft(cfg, state_dir, state)
            return _cancel(state_dir)

        target = str(inputs.get("target") or "").strip() or None
        if target and (not ID_RE.match(target) or target in RESERVED_IDS
                       or not (cfg.apps_dir / target / "app.toml").exists()):
            raise ForgeError(f"No app named '{target}' to revise")
        if not request:
            return {"status": "ask", "app": "forge", "inputs": inputs,
                    "ask": [{"name": "request", "type": "dictation", "required": True,
                             "prompt": f"What should change in {target}?" if target
                             else "What should the new app do?"}]}
        state = {"stage": "draft", "request": request, "target": target, "app": target, "round": 0}
        return draft(cfg, state_dir, state)
    except (ForgeError, AIError, KeyError, OSError, tomllib.TOMLDecodeError) as e:
        return {"status": "error", "app": "forge", "error": str(e)}
