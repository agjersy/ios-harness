"""The rules for writing an app, as text for a model.

Built from the same tables the harness enforces, so the prompt that Forge
and repair send can't drift from what sync will accept.
Print it with `python -m harness contract`.
"""
from __future__ import annotations

from .actions import ACTIONS
from .manifest import ASK_MODES, INPUT_TYPES, TEXT_TARGETS, WRITE_MODES
from .sync import BYPASS_IMPORTS, CTX_PUBLIC, DISCOURAGED_IMPORTS, FILE_IMPORTS

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
open_url = ["https"]            # URL schemes "_open_url" may use; leave out if unused

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
run() receives: file -> pathlib.Path, date -> datetime.date, datetime ->
datetime.datetime, number -> int or float, bool -> bool, json -> the parsed
value, everything else -> str. An optional input that was not given is None.
An input may set default = ... (for dates: "today", "tomorrow") and, for a
file, accept = ["m4a", "wav"].
Input ask modes: {", ".join(sorted(ASK_MODES))}. "if_missing" asks only for a required input
with no value; "always" asks unless the request already has a value; "never"
uses the default or fails.
Text output targets: {", ".join(sorted(TEXT_TARGETS))} ("speak" is read aloud, so keep it to a
sentence or two). File write modes: {", ".join(sorted(WRITE_MODES))}.
A file output's path may use {{input_name}} placeholders for declared inputs only.
Unknown tables or keys at the top level are rejected.

### Code

main.py defines run(ctx, **inputs) with one parameter per declared input.
It returns a dict keyed by output names. Optional extra keys: "_show" (text),
"_open_url" (a URL whose scheme is listed in [actions].open_url; never
shortcuts://, and never in a strict app), "_actions" (a list, see Actions).

Everything outside the function's own computation goes through ctx:
- ctx.ai(capability, prompt, file=None, system=None, max_tokens=1024, as_json=False)
- ctx.read_text(ref), ctx.read_bytes(ref), ctx.list(ref, pattern="*")
- ctx.write_text(ref, text, mode="replace")  (file outputs are written for you)
- ctx.fetch(url, method="GET", headers=None, data=None) -> {{"status", "text", "headers"}}
- ctx.secret(name), ctx.path(ref), ctx.log(message)
A ref is an "@alias/..." string or a Path received as a file input.

Standard library plus declared dependencies only. No compiled packages, no
subprocesses, no threads left running. These rules are checked, and code
that breaks one is rejected:
- Do not import {", ".join(sorted(BYPASS_IMPORTS | DISCOURAGED_IMPORTS | FILE_IMPORTS))}.
- From os use only os.path. Do not call open(): read and write through ctx.
- Do not call eval, exec, compile, globals or __import__.
- Use only these parts of ctx: {", ".join(sorted(CTX_PUBLIC))}. Call ctx.ai(...) but do not
  read or set anything on it. No ctx.cfg, no names starting with "_", no
  getattr or setattr on ctx.
- main.py must not import from tests/, and the same rules apply to tests/.
Use ctx.log(message) rather than print().

### Actions

To change something on the device, return "_actions": a list of dicts with
"type" plus that type's fields ("?" marks optional). Each type used must be in
[actions].allow. Outbound actions always ask the person first.
{_action_lines()}
A "when" is "YYYY-MM-DD" (no time of day) or ISO 8601 like "2027-01-15T09:00:00";
a datetime.date or datetime.datetime object works too.

### Privacy

privacy = "strict" is for apps that handle financial, medical or identity
documents. In a strict app, ctx.ai replaces personal identifiers with tokens
such as [[ACCOUNT_1]] before sending and restores them in the reply, so treat
tokens as opaque values. A strict app can't pass PDFs, images or audio to
ctx.ai, can't declare net or open_url, and must not put document contents in
exception messages or ctx.log.

### Smoke test

tests/smoke.py defines INPUTS (a dict of input values) and optionally
check(result, response), which uses assert. It may define
FETCH = {{"<url prefix>": "<response body>"}} and
AI = {{"<capability>": "<reply>"}} or {{"<capability>": ["<first reply>", "<second>", ...]}}
(a dict or list reply is sent as JSON; the last reply repeats).
The test runs offline: ctx.ai returns the AI reply for that capability, or
"[mock <capability>] <start of prompt>" if none is given; ctx.fetch returns the
FETCH body or "{{}}"; ctx.secret returns "mock-secret"; nothing is written.
If the app calls ctx.ai(..., as_json=True), the smoke test must give a JSON
reply in AI. Do not wrap model calls in try/except just to pass the test.
"""
