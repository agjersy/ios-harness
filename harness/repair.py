"""Ask a model to fix the last crash, check the fix, and apply it only on a yes.

A proposal is a full copy of the app with the model's files laid over it,
saved under repairs/<id>-<time>/<id>/. It goes through the same checks as
any app (manifest, lint, smoke test). Only a proposal that passes is offered
for applying; the files it replaces are kept beside it in before/.

From Shortcuts this is the built-in app "repair":
{"app": "repair", "inputs": {"target": "<app id>"}}. It answers with an `ask`
(Apply it / Discard). From the terminal: `repair <id>`, then `repair <id> --apply`.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime

from . import quarantine
from .ai import AIClient, AIError
from .contract import text as contract_text
from .forge import FILE_RE
from .manifest import ID_RE, RESERVED_IDS
from .sync import check_candidate, sync

APPLY, DISCARD = "Apply it", "Discard"

PROMPT = """An app for a Python harness on iOS crashed. Propose a fix.

Change as little as possible and keep to the contract below. The traceback has
had personal data removed: tokens like [[NAME_1]] and withheld messages are expected.
You may change .py files only; the manifest stays as it is.

{contract}

Manifest (app.toml):
{manifest}

Source files:
{sources}

Traceback:
{tb}

Reply with JSON only:
{{"explanation": "<one or two sentences>", "files": {{"<path relative to the app folder>": "<full new file content>"}}}}"""


class RepairError(Exception):
    pass


def _app_dir(cfg, app_id):
    if not isinstance(app_id, str) or not ID_RE.match(app_id) or app_id in RESERVED_IDS \
            or not (cfg.apps_dir / app_id / "app.toml").exists():
        raise RepairError(f"No app named '{app_id}'")
    return cfg.apps_dir / app_id


def repair(app_id: str, cfg) -> dict:
    """Propose a fix (one model call) and check it. Nothing in apps/ changes."""
    try:
        app_dir = _app_dir(cfg, app_id)
    except RepairError as e:
        return {"status": "error", "error": str(e)}
    err_file = cfg.errors / f"{app_id}.txt"
    if not err_file.exists():
        return {"status": "error", "error": f"No recorded error for {app_id}"}

    tb = err_file.read_text(encoding="utf-8")
    sources = {str(p.relative_to(app_dir)): p.read_text(encoding="utf-8")
               for p in sorted(app_dir.rglob("*.py"))}
    prompt = PROMPT.format(manifest=(app_dir / "app.toml").read_text(encoding="utf-8"),
                           sources=json.dumps(sources, indent=1), tb=tb,
                           contract=contract_text())
    out = AIClient(cfg)(cfg.repair_capability, prompt, max_tokens=8000, as_json=True)
    if not isinstance(out, dict):
        return {"status": "error", "error": "The model did not return a JSON object"}

    dest = cfg.repairs / f"{app_id}-{datetime.now():%Y%m%d-%H%M%S}"
    candidate = dest / app_id
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(app_dir, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    written, problems = [], []
    files = out.get("files") if isinstance(out.get("files"), dict) else {}
    for rel, text in files.items():
        if not FILE_RE.match(str(rel)) or not isinstance(text, str):
            problems.append(f"{rel}: only .py files inside the app folder are accepted")
            continue
        target = candidate / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(rel)
    if not written:
        problems.append("the model proposed no usable files")
    if not problems:
        _, problems, _ = check_candidate(cfg, candidate, hard=quarantine.remaining(cfg, app_id) > 0)

    explanation = str(out.get("explanation") or "(no explanation)")
    passed = not problems
    (dest / "EXPLANATION.md").write_text(
        f"# Repair for {app_id}\n\n{explanation}\n\nChecks: {'passed' if passed else 'FAILED'}\n"
        + "".join(f"- {p}\n" for p in problems) + f"\n```\n{tb}\n```\n", encoding="utf-8")
    (dest / "result.json").write_text(json.dumps({"app": app_id, "passed": passed, "files": written}),
                                      encoding="utf-8")
    verdict = ("It passes the app's checks. Apply it with `python -m harness repair "
               f"{app_id} --apply`." if passed else
               "It does NOT pass the app's checks, so it can't be applied:\n" + "\n".join(problems)[:1200])
    return {"status": "ok", "app": app_id, "repair_dir": str(dest), "proposal": dest.name,
            "files": written, "passed": passed, "problems": problems, "explanation": explanation,
            "show": f"{explanation}\n\nProposed changes to {', '.join(written) or 'no files'} "
                    f"saved for review in {dest.name}.\n{verdict}"}


def _result(folder) -> dict:
    try:
        return json.loads((folder / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply(app_id: str, cfg, proposal: str | None = None) -> dict:
    """Copy a passing proposal's files into apps/<id>/, keeping the old ones in before/."""
    try:
        app_dir = _app_dir(cfg, app_id)
        folders = sorted(p for p in cfg.repairs.glob(f"{app_id}-*") if p.is_dir())
        if proposal is not None:
            folders = [p for p in folders if p.name == proposal]
        folders = [p for p in folders if _result(p).get("app") == app_id]
        if not folders:
            raise RepairError(f"No repair proposal for {app_id}; run `repair {app_id}` first")
        dest = folders[-1]
        result, candidate = _result(dest), dest / app_id
        if not result.get("passed"):
            raise RepairError(f"{dest.name} did not pass its checks, so it can't be applied")
        # The app may have changed since the proposal was written: check again.
        _, problems, _ = check_candidate(cfg, candidate, hard=quarantine.remaining(cfg, app_id) > 0)
        if problems:
            raise RepairError(f"{dest.name} no longer passes: " + "; ".join(problems)[:800])
    except RepairError as e:
        return {"status": "error", "app": "repair", "error": str(e)}

    for rel in result.get("files", []):
        if not FILE_RE.match(str(rel)) or not (candidate / rel).is_file():
            continue
        if (app_dir / rel).is_file():
            (dest / "before" / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(app_dir / rel, dest / "before" / rel)
        (app_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate / rel, app_dir / rel)
    (cfg.errors / f"{app_id}.txt").unlink(missing_ok=True)
    report = sync(cfg)
    note = "" if app_id in report["ok"] else \
        "\nWarning: sync now rejects it: " + "; ".join(report["failed"].get(app_id, ["unknown reason"]))[:600]
    return {"status": "ok", "app": "repair", "repaired": app_id, "files": result.get("files", []),
            "show": f"Applied {dest.name} to apps/{app_id}. The replaced files are in "
                    f"repairs/{dest.name}/before. Review the change in Working Copy before you commit it.{note}"}


def handle(req: dict, cfg) -> dict:
    """Called by runner.handle for app id 'repair'."""
    inputs = req.get("inputs") if isinstance(req.get("inputs"), dict) else {}
    target, approve = str(inputs.get("target") or "").strip(), inputs.get("approve")
    if approve and str(approve).strip().lower() == APPLY.lower():
        approve = APPLY
    try:
        if not target:
            crashed = sorted(p.stem for p in cfg.errors.glob("*.txt")
                             if (cfg.apps_dir / p.stem / "app.toml").exists())
            if not crashed:
                return {"status": "ok", "app": "repair", "show": "No app has a recorded crash."}
            return {"status": "ask", "app": "repair", "inputs": {},
                    "ask": [{"name": "target", "type": "choice", "options": crashed, "required": True,
                             "prompt": "Which app should be repaired?"}]}
        if approve == APPLY:
            return apply(target, cfg, str(inputs.get("proposal") or "") or None)
        if approve:
            return {"status": "ok", "app": "repair", "show": "Left as it is. The proposal is still saved."}
        out = repair(target, cfg)
        if out["status"] != "ok":
            return {"status": "error", "app": "repair", "error": out["error"]}
        if not out["passed"]:
            return {"status": "ok", "app": "repair", "show": out["show"]}
        return {"status": "ask", "app": "repair", "inputs": {"target": target, "proposal": out["proposal"]},
                "ask": [{"name": "approve", "type": "choice", "options": [APPLY, DISCARD], "required": True,
                         "prompt": f"{out['explanation']}\n\nChanges {', '.join(out['files'])} in {target}. "
                                   "The fix passes the app's checks.\n\nApply it?"}]}
    except (AIError, KeyError, OSError) as e:
        return {"status": "error", "app": "repair", "error": str(e)}
