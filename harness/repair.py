"""Ask a model to fix the last crash. Proposals are saved for review, never applied."""
from __future__ import annotations

import json
from datetime import datetime

from .ai import AIClient
from .contract import text as contract_text

PROMPT = """An app for a Python harness on iOS crashed. Propose a fix.

Change as little as possible and keep to the contract below. The traceback has
had personal data removed: tokens like [[NAME_1]] and withheld messages are expected.

{contract}

Manifest (app.toml):
{manifest}

Source files:
{sources}

Traceback:
{tb}

Reply with JSON only:
{{"explanation": "<one or two sentences>", "files": {{"<path relative to the app folder>": "<full new file content>"}}}}"""


def repair(app_id: str, cfg) -> dict:
    err_file = cfg.errors / f"{app_id}.txt"
    app_dir = cfg.apps_dir / app_id
    if not err_file.exists():
        return {"status": "error", "error": f"No recorded error for {app_id}"}

    tb = err_file.read_text(encoding="utf-8")
    sources = {str(p.relative_to(app_dir)): p.read_text(encoding="utf-8")
               for p in sorted(app_dir.rglob("*.py")) if "tests" not in p.relative_to(app_dir).parts}
    prompt = PROMPT.format(manifest=(app_dir / "app.toml").read_text(encoding="utf-8"),
                           sources=json.dumps(sources, indent=1), tb=tb,
                           contract=contract_text())
    out = AIClient(cfg)(cfg.repair_capability, prompt, max_tokens=8000, as_json=True)

    dest = cfg.repairs / f"{app_id}-{datetime.now():%Y%m%d-%H%M%S}"
    written = []
    for rel, text in (out.get("files") or {}).items():
        target = (dest / rel).resolve()
        if not str(target).startswith(str(dest.resolve())):
            continue  # ignore paths that try to escape the proposal folder
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(rel)

    explanation = out.get("explanation", "(no explanation)")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "EXPLANATION.md").write_text(f"# Repair for {app_id}\n\n{explanation}\n\n```\n{tb}\n```\n",
                                         encoding="utf-8")
    return {"status": "ok", "app": app_id, "repair_dir": str(dest), "files": written,
            "show": f"{explanation}\n\nProposed changes to {', '.join(written) or 'no files'} "
                    f"saved for review in {dest.name}."}
