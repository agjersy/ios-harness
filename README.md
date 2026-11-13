# iOS Python harness

Manifest-driven Python apps for iOS, run in a-Shell, triggered from Shortcuts,
deployed through Working Copy. Standard library only; nothing to install.

## Layout

```
repo/                      <- Working Copy repo, synced to a Files folder
  harness/                 <- this package (python -m harness ...)
  apps/<id>/app.toml       <- one folder per app
  apps/<id>/main.py
  apps/<id>/tests/smoke.py
  shortcuts/README.md      <- how to build each Shortcut, action by action
  tests/                   <- harness tests (python -m unittest discover tests)
~/Documents/.harness/      <- per-device state, never committed
  config.toml              <- aliases, model mapping, package allowlist
  secrets.toml             <- API keys, your [identifiers]
  catalog.json             <- healthy apps, read by router and Shortcuts
  inbox/ outbox/           <- Shortcuts bridge
  pending/                 <- router guesses waiting for a menu pick
  deps/<id>-<hash>/        <- per-app packages (stand-in for venvs)
  errors/ repairs/         <- last traceback per app, proposed fixes
  egress/                  <- what strict apps actually sent to models
  forge/                   <- Forge drafts awaiting approval, backups of revised apps
  quarantine.json          <- apps still on probation
  runs.jsonl actions.jsonl <- one line per run / per action the Shortcut reported
```

## Device setup (once)

1. In Working Copy, clone the repo and sync it to a folder inside a-Shell.
2. In a-Shell, run `pickFolder` and choose your Obsidian vault, `cd` into it,
   and run `python -c "import os; print(os.getcwd())"` to get its real path.
   (`pwd` shows `~name`, which Python can't resolve.) Repeat for iWriter.
3. `cd` into the repo folder and run `python -m harness doctor`. This creates
   `~/Documents/.harness/` with default config and secrets files.
4. Edit `config.toml`: set the `[aliases]` paths from step 2.
   Edit `secrets.toml`: add your API keys.
5. Run `python -m harness sync`, then `python -m harness doctor` again.
   If `config.toml` has a mistake, `doctor` says what and where.
6. Build the Shortcuts: `python -m harness shortcuts` lists the ones this
   device needs, and `shortcuts/README.md` gives the steps for each.
7. Optional: set `HARNESS_REPO` if you run from outside the repo folder.

## Commands

| Command | What it does |
|---|---|
| `sync [--force] [--no-test]` | Validate, install packages, smoke-test, rebuild catalog |
| `run --request <id>` | Shortcuts entry point (always exits 0; status is in the response) |
| `run --app <id> -i name=value [--id <id>]` | Run from the terminal; reuse `--id` to answer an `ask` |
| `run --say "..."` / `route "..."` | Route free speech / preview the routing |
| `repair <id>` / `repair <id> --apply` | Propose and check a fix for the last crash / apply it |
| `promote <id>` | End quarantine early for an app Forge built |
| `ack <action id> done\|skipped\|failed [detail]` | Record what the Shortcut did with an action |
| `shortcuts` | List the Shortcuts this device needs |
| `contract` | Print the app-writing rules given to models |
| `doctor` / `list` | Check setup / list healthy apps |

## Shortcuts protocol

This is protocol **2**. Every response carries `"protocol": 2`; Harness Run
should compare it with the number it was built for and say so when they
differ, instead of misreading a response. `shortcuts/README.md` has the
build steps; this section is the reference.

A request is a JSON object with four optional keys, written with a-Shell's
Put File to `inbox/<id>.json`:

| Key | Meaning |
|---|---|
| `app` | App id to run. Omit to let the router or the menu decide. |
| `utterance` | What the user said. Used by the router when `app` is missing. |
| `inputs` | Values keyed by the app's input names. |
| `id` | Ignored; the file name is the id. Reuse the same id when resending. |

An id is letters, digits, `-`, `_` and `.` only (a UUID or a timestamp works).
Build the request with a Dictionary action, not by typing JSON into a Text
action: a Dictionary escapes quotes and line breaks in what was dictated.

Files go in `inbox/<id>/` and are picked up automatically as attachments,
then assigned to the app's file inputs in order. Shortcuts may flatten
`inputs` into a JSON string or leave keys as empty text; both are accepted
(an empty value never replaces one the router found), as are UTF-8 with a
BOM and UTF-16.

Run with Execute Command, using the absolute repo path:

```
cd /absolute/path/to/harness-repo
python -m harness run --request <id> 2>&1
```

The command prints the response JSON and nothing else (also saved to
`outbox/<id>.json`). Anything an app or pip prints is returned inside the
response as `printed`. Branch on `status`:

- `ok`: show `show`, speak `speak`, post `notification`, copy `clipboard`,
  open `open_url` (ask first if `open_url_confirm` is true), then perform
  each item in `actions` (see Typed actions). The request and its
  attachments are deleted from `inbox/` as soon as the run is `ok`.
- `ask`: collect each item in `ask` (its `type` picks the action; show its
  `error` first if it has one, and offer `default` if present), put the
  answers into the `inputs` **from the response**, resend with the same id
  and `app`. Send an empty answer as empty text: for an input that is not
  `required` it means "skip".
- `choose`: show the keys of `menu` (the last entry, "Build a new app", is
  Forge), look up the picked key to get the app id, resend with the same id,
  that `app`, and the original `utterance`. What the router already worked
  out from the utterance is kept for the app in `suggested`.
- `continue`: the harness has more to do (Forge retrying). Show `show` if you
  like, then resend at once with the same id, `app` and the `inputs` from the
  response.
- `error`: show `error`. If `repairable` is true, offer to send
  `{"app": "repair", "inputs": {"target": "<app>"}}` (see Repair).

Input types an `ask` can carry: `text`, `dictation`, `choice` (with
`options`), `date`, `datetime`, `number`, `bool`, `url`, `file`, `json`.
Dates may be sent as `yyyy-MM-dd`, ISO 8601, `today` / `tomorrow` /
`yesterday`, a weekday name, or the text Shortcuts shows for a date
("Nov 13, 2026 at 9:00 AM"). All-numeric dates such as `11/12/26` are
refused because their order depends on the region. A value the app can't
read comes back as an `ask` with an `error`, not as a failed run.

Shortcuts: **Harness Run** (the generic loop above, called only by other
Shortcuts), **Harness Attach** (receives files, puts them in `inbox/<id>/`,
outputs `{id}`), and thin wrappers that build a dictionary and call Harness Run.

## Typed actions

An app changes things on the device by returning `_actions`:

```python
return {"reply": "Done", "_actions": [
    {"type": "reminder.create", "title": "Pay Q4 estimate", "due": "2027-01-15"}]}
```

The app's manifest must list each type under `[actions] allow`. The harness
checks every action against its schema before any file is written, then adds
`id`, `tier`, `confirm`, `summary` and `shortcut` and puts the list in the
response. One bad action fails the whole run.

| Type | Tier | Fields (`?` = optional) |
|---|---|---|
| `reminder.create` | write | `title`, `due?`, `notes?`, `list?` |
| `event.create` | write | `title`, `start`, `end?`, `all_day?`, `location?`, `notes?`, `calendar?` |
| `note.create` | write | `body`, `title?`, `folder?` |
| `message.draft` | outbound | `to`, `body` |
| `mail.draft` | outbound | `to`, `subject`, `body` |
| `shortcut.run` | outbound | `name` (must be in `[actions] shortcuts` in config.toml), `input?` |

Dates are `yyyy-MM-dd` (no time of day) or ISO 8601 (`2027-01-15T09:00:00`).
`confirm` is true for every outbound action, for any action that asks for it,
and for every action of an app in quarantine.

In **Harness Run**, after handling `ok`: repeat with each item in `actions`;
if `confirm` is true, ask "`summary`. Go ahead?" and skip the item on no;
then Run Shortcut `shortcut` with the item as input. Each type gets one small
Shortcut named `Harness Act <type>` (for example `Harness Act reminder.create`)
that reads the fields from its input dictionary. To add a type, add an entry
to `harness/actions.py` and one such Shortcut. Test the loop with the
`remind-me` app, which needs no AI or network.

If Run Shortcut won't take a name from a variable on your iOS version, set
`single_shortcut = true` under `[actions]` in `config.toml`. Every action's
`shortcut` is then `Harness Act`, one Shortcut that branches on the item's
`type`.

Optionally, report what happened to each action so there is a record:
`python -m harness ack <action id> done|skipped|failed "<detail>"` appends a
line to `actions.jsonl`.

**Opening URLs.** `_open_url` is a way out of the device like any action, so
it is declared too: `[actions] open_url = ["https", "obsidian"]` lists the
URL schemes the app may open. Anything else fails the run. `shortcuts://`
can never be declared (use `shortcut.run`, which has an allowlist), strict
apps can't open URLs at all, and an app in quarantine sets
`open_url_confirm` so the Shortcut asks first.

## Privacy

Add your own details to `secrets.toml`:

```toml
[identifiers]
name = ["Jane Q. Doe", "Doe"]
address = ["12 Elm Street"]
account = ["000123456789"]
```

Set `privacy = "strict"` under `[app]` for apps that read financial, medical
or identity documents. For those apps:

- **Egress gate.** `ctx.ai` replaces your identifiers and pattern matches
  (emails, SSNs, card, routing and account numbers, phones, digit runs of 8+)
  with tokens like `[[ACCOUNT_1]]`, and restores them in the reply. If one of
  your identifiers is still readable after that, ignoring case, spacing and
  punctuation, nothing is sent and the run fails.
- **No binary files.** PDFs, images and audio can't be redacted, so `ctx.ai`
  refuses them. Extract the text on the device first (Shortcuts can do this)
  and pass that.
- **No other way out.** The manifest can't declare `net` or `open_url`, the
  app can't return `_open_url`, and `sync` rejects code that imports network
  or file modules, calls `open()`, or touches anything on `ctx` outside the
  app contract.
- **Audit.** The exact text sent is appended to `egress/<date>.jsonl`.
- **Nothing kept.** The response is printed for the Shortcut but not saved to
  `outbox/`; the request and its attachments are deleted when the run ends,
  whether it worked or failed; the router's guess is never saved for a strict app;
  `runs.jsonl` records the error type only; the saved traceback keeps file
  names and line numbers but no messages.

For all apps, saved tracebacks and logged errors are redacted the same way,
so `repair` never sends your identifiers to a model. What you say is redacted
before the router or Forge sends it to a model, whichever app it ends up in;
the router's answer has your details put back on the device.

Limits: redaction lowers exposure, it does not make a statement anonymous.
Merchant names, amounts and dates still go out. Identifiers shorter than four
characters are redacted but can't block a send. The lint catches mistakes in
generated code; it is not a sandbox.

## Forge

Choose "Build a new app" from any menu, or send `{"app": "forge", "utterance": "..."}`.
Every Forge request makes at most one model call, so no Shortcut run takes long.

1. **Draft.** A model writes only the manifest. The harness reads it and
   replies with `ask`: what the app asks for, how it replies, what it reads,
   writes, calls and opens, which actions, secrets and models it uses, with
   the choices "Build it", "Change it" and "Cancel". "Change it" asks what
   should change and drafts again with your feedback.
2. **Build.** A model writes `main.py` and `tests/smoke.py` against the
   approved manifest, which it can't change. The code must pass validation,
   the hard lint and its smoke test. If it doesn't, the reply is `continue`:
   Harness Run resends, and the problems go back to the model, for up to
   `[forge] rounds` more attempts. Each attempt starts from the manifest
   alone, so nothing from a rejected attempt is installed.
3. **Install.** The app is copied to `apps/<id>/` and enters quarantine: for
   its first `[forge] quarantine_runs` clean runs, every action and URL asks
   first, and `sync` keeps it under the hard lint. If the final `sync` rejects
   it, the folder is removed again.

**Revising an app.** Send `{"app": "forge", "inputs": {"target": "<app id>",
"request": "what to change"}}`. The model sees the current files, the id
can't change, and you approve the new manifest the same way. The old folder
is kept in `forge/_backups/` for a week and the app goes back into quarantine.

**Secrets.** If the manifest names a secret this device doesn't have, the
approval screen says which one to add. The app is built and installed, and
appears in the menu after you add the key to `secrets.toml` and run `sync`.

**Personal details.** Your identifiers are replaced with tokens before the
request reaches a model, and a draft or code that copies a token is rejected:
such values have to be inputs, not text baked into the app.

Forge can write only to its staging folder and `apps/<id>/`. The smoke test
runs the generated code once, offline, before you have read it, so review
the new folder in Working Copy before committing. The hard lint rejects the
usual ways around `ctx`; it is still not a sandbox.

From the terminal: `python -m harness run --app forge --id f1 -i request="..."`,
then the same command with `-i approve="Build it"`.

## Repair

When a run fails inside an app, the response has `"repairable": true`. Send
`{"app": "repair", "inputs": {"target": "<app id>"}}` (or run
`python -m harness repair <id>`). A model proposes changed `.py` files; the
harness lays them over a copy of the app in `repairs/<id>-<time>/` and runs
the same checks as `sync`. Only a proposal that passes is offered: the reply
is an `ask` with "Apply it" and "Discard" (`repair <id> --apply` from the
terminal). Applying keeps the replaced files in `repairs/<id>-<time>/before/`.
A mistyped input or an unknown app is not repairable and records no error.

## Writing apps

`run(ctx, **inputs)` returns a dict keyed by output names. Use
`ctx.ai(capability, prompt, file=..., as_json=...)`, `ctx.read_text(ref)`,
`ctx.write_text(ref, text, mode)`, `ctx.list(ref)`, `ctx.path(ref)`, `ctx.log(msg)`.
For other APIs use `ctx.fetch(url, method=, headers=, data=)`, which returns
`{"status", "text", "headers"}` and only calls HTTPS hosts listed under
`[permissions] net` (and their subdomains), and `ctx.secret(name)` for keys
listed under `[permissions] secrets`.
Input types: `text`, `dictation`, `choice`, `date`, `datetime`, `number`,
`bool`, `url`, `file`, `json`. Text output targets: `show`, `speak` (read
aloud; good for Siri, AirPods and CarPlay), `notification`, `clipboard`.
Special keys `_show`, `_open_url` and `_actions` go to the Shortcut after
the checks described under Typed actions.
Smoke tests define `INPUTS`, an optional `check(result, response)`, an
optional `FETCH = {"<url prefix>": "<body>"}` and an optional
`AI = {"<capability>": <reply or list of replies>}`; they run with mock
models, mock fetches and no file writes. An app that calls
`ctx.ai(..., as_json=True)` needs an `AI` reply in its smoke test.
A manifest with a misspelled table, a wrong value type or a path placeholder
that isn't an input is rejected with a message naming the problem, and one
bad app never stops `sync` for the others.
`python -m harness contract` prints the full rules.

## Device notes

- Confirmed on a-Shell: in-process pip into `--target` folders, HTTPS via
  `urllib`, and access to `pickFolder` folders from Python.
- Needs Python 3.11+ for `tomllib`, else `pip install tomli` (allowlisted).
- `timeout_s` is a budget that triggers a warning; runs can't be killed mid-way.
- `voice-to-vault/tests/sample.m4a` is a placeholder; replace it with real audio.
- Not yet tried on a device: Run Shortcut with the name taken from a
  variable, which the action loop depends on (`single_shortcut` is the way
  round it), and the Shortcut steps in `shortcuts/README.md` as a whole.
- `ctx.fetch` follows redirects, so a listed host can send it elsewhere.
- A weekday name means the next one after today: "friday" said on a Friday
  is a week away.
- Leftovers older than 7 days in `inbox/`, `outbox/`, `egress/`, `forge/`
  and `pending/` are removed after every Shortcuts request and every `sync`.
