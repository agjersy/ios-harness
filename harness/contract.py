"""The rules for writing an app, as text for a model.

Built from the same tables the harness enforces, so the prompt that Forge
and repair send can't drift from what sync will accept.
Print it with `python -m harness contract`.
"""
from __future__ import annotations

from .actions import ACTIONS
from .manifest import ASK_MODES, INPUT_TYPES, TEXT_TARGETS, WRITE_MODES
from .sync import BYPASS_IMPORTS, DISCOURAGED_IMPORTS

MANIFEST_EXAMPLE = '''\
schema = 1

[app]
id = "kebab-case-id"            # must match the folder name
name = "Short name for menus"
version = "0.1.0"
description = "One sentence the router uses to pick this app."
entry = "main:run"
privacy = "standard"            # or "strict" (see Privacy)

[runtime]
dependencies = []               # pure-Python packages from the device allowlist
timeout_s = 120

[inputs.example]                # one table per argument of run()
type = "text"
prompt = "Question shown if the value is missing"
required = true

[outputs.result]                # one table per key that run() returns
type = "text"
to = ["show"]

[outputs.note]
type = "file"
path = "@alias/Folder/{day:%Y-%m-%d}.md"   # {input} placeholders are filled in
write = "append"

[ai]
uses = ["fast"]                 # capabilities passed to ctx.ai

[permissions]
read = ["@alias/Folder"]
write = ["@alias/Folder"]       # every file output must sit under one of these
net = ["api.example.com"]       # hosts ctx.fetch may call
secrets = ["EXAMPLE_TOKEN"]     # names ctx.secret may read

[actions]
allow = ["reminder.create"]

[router]
examples = ["what a person would say", "another phrasing"]

[test]
smoke = "tests/smoke.py"
'''


def _action_lines() -> str:
    lines = []
    for kind, spec in ACTIONS.items():
        fields = ", ".join(f"{name}{'' if required else '?'}: {field_kind}"
                           for name, (field_kind, required) in spec["fields"].items())
        lines.append(f"- {kind} ({spec['tier']}): {fields}")
    return "\n".join(lines)


def text() -> str:
    return f"""## App contract

An app is a folder: app.toml (the manifest), main.py, tests/smoke.py.

### Manifest (TOML). Leave out tables the app doesn't need.

{MANIFEST_EXAMPLE}
Input types: {", ".join(sorted(INPUT_TYPES))}. A choice input needs options = [...].
A file input arrives as a pathlib.Path, a date input as a datetime.date.
Input ask modes: {", ".join(sorted(ASK_MODES))}.
Text output targets: {", ".join(sorted(TEXT_TARGETS))}. File write modes: {", ".join(sorted(WRITE_MODES))}.

### Code

main.py defines run(ctx, **inputs) with one parameter per declared input.
It returns a dict keyed by output names. Optional extra keys: "_show" (text),
"_open_url" (a URL), "_actions" (a list, see Actions).

Everything outside the function's own computation goes through ctx:
- ctx.ai(capability, prompt, file=None, system=None, max_tokens=1024, as_json=False)
- ctx.read_text(ref), ctx.read_bytes(ref), ctx.list(ref, pattern="*")
- ctx.write_text(ref, text, mode="replace")  (file outputs are written for you)
- ctx.fetch(url, method="GET", headers=None, data=None) -> {{"status", "text", "headers"}}
- ctx.secret(name), ctx.path(ref), ctx.log(message)
A ref is an "@alias/..." string or a Path received as a file input.

Standard library plus declared dependencies only. No compiled packages, no
subprocesses, no threads left running. Do not import {", ".join(sorted(BYPASS_IMPORTS | DISCOURAGED_IMPORTS))},
do not call eval, exec or __import__, do not touch ctx.cfg, and do not write
files with open().

### Actions

To change something on the device, return "_actions": a list of dicts with
"type" plus that type's fields ("?" marks optional). Each type used must be in
[actions].allow. Outbound actions always ask the person first.
{_action_lines()}
A "when" is "YYYY-MM-DD" (no time of day) or ISO 8601 like "2027-01-15T09:00:00".

### Privacy

privacy = "strict" is for apps that handle financial, medical or identity
documents. In a strict app, ctx.ai replaces personal identifiers with tokens
such as [[ACCOUNT_1]] before sending and restores them in the reply, so treat
tokens as opaque values. A strict app can't pass PDFs, images or audio to
ctx.ai, can't declare net, and must not put document contents in exception
messages or ctx.log.

### Smoke test

tests/smoke.py defines INPUTS (a dict of input values) and optionally
check(result, response), which uses assert. It may define
FETCH = {{"<url prefix>": "<response body>"}}. The test runs offline: ctx.ai
returns "[mock <capability>] <start of prompt>", ctx.fetch returns the FETCH
body or "{{}}", ctx.secret returns "mock-secret", and nothing is written. The
app must not crash on those stand-in values.
"""
