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
  tests/                   <- harness tests (python -m unittest discover tests)
~/Documents/.harness/      <- per-device state, never committed
  config.toml              <- aliases, model mapping, package allowlist
  secrets.toml             <- API keys, your [identifiers]
  catalog.json             <- healthy apps, read by router and Shortcuts
  inbox/ outbox/           <- Shortcuts bridge
  deps/<id>-<hash>/        <- per-app packages (stand-in for venvs)
  errors/ repairs/         <- last traceback per app, proposed fixes
  egress/                  <- what strict apps actually sent to models
  forge/                   <- Forge drafts awaiting approval
  quarantine.json          <- apps still on probation
  runs.jsonl               <- one line per run
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
6. Optional: set `HARNESS_REPO` if you run from outside the repo folder.

## Commands

| Command | What it does |
|---|---|
| `sync [--force] [--no-test]` | Validate, install packages, smoke-test, rebuild catalog |
| `run --request <id>` | Shortcuts entry point (always exits 0; status is in the response) |
| `run --app <id> -i name=value` | Run from the terminal |
| `run --say "..."` / `route "..."` | Route free speech / preview the routing |
| `repair <id>` | Propose a fix for the app's last crash (saved, not applied) |
| `promote <id>` | End quarantine early for an app Forge built |
| `contract` | Print the app-writing rules given to models |
| `doctor` / `list` | Check setup / list healthy apps |

## Shortcuts protocol

A request is a JSON object with four optional keys, written with a-Shell's
Put File to `inbox/<id>.json`:

| Key | Meaning |
|---|---|
| `app` | App id to run. Omit to let the router or the menu decide. |
| `utterance` | What the user said. Used by the router when `app` is missing. |
| `inputs` | Values keyed by the app's input names. |
| `id` | Ignored; the file name is the id. Reuse the same id when resending. |

Files go in `inbox/<id>/` and are picked up automatically as attachments,
then assigned to the app's file inputs in order. Shortcuts may flatten
`inputs` into a JSON string or leave keys as empty text; both are accepted,
as are UTF-8 with a BOM and UTF-16.

Run with Execute Command, using the absolute repo path:

```
cd /absolute/path/to/harness-repo
python -m harness run --request <id> 2>&1
```

The command prints the full response JSON (also saved to `outbox/<id>.json`).
Branch on `status`:

- `ok`: show `show`, post `notification`, copy `clipboard`, open `open_url`,
  then perform each item in `actions` (see Typed actions)
- `ask`: collect each item in `ask` (its `type` picks the action), add the
  answers to `inputs`, resend with the same id and `app`
- `choose`: show the keys of `menu` (the last entry, "Build a new app", is Forge), look up the picked key to get the app id,
  resend with the same id, that `app`, and the original `utterance`
- `error`: show `error`; optionally run `repair <app>`

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
- **No other way out.** The manifest can't declare `net`, and `sync` rejects
  code that imports network modules or reads `ctx.cfg`.
- **Audit.** The exact text sent is appended to `egress/<date>.jsonl`.
- **Nothing kept.** The response is printed for the Shortcut but not saved to
  `outbox/`; the request and its attachments are deleted when the run ends;
  `runs.jsonl` records the error type only; the saved traceback keeps file
  names and line numbers but no messages.

For all apps, saved tracebacks and logged errors are redacted the same way,
so `repair` never sends your identifiers to a model.

Limits: redaction lowers exposure, it does not make a statement anonymous.
Merchant names, amounts and dates still go out. Identifiers shorter than four
characters are redacted but can't block a send. The lint catches mistakes in
generated code; it is not a sandbox.

## Forge

Choose "Build a new app" from any menu, or send `{"app": "forge", "utterance": "..."}`.

1. **Draft.** A model writes only the manifest. The harness reads it and
   replies with `ask`: what the app reads, writes, calls, and which actions
   and models it uses, with the choices "Build it" and "Cancel".
2. **Build.** A model writes `main.py` and `tests/smoke.py` against the
   approved manifest, which it can't change. The code must pass validation,
   the strict lint and its smoke test; failures go back to the model for
   `[forge] rounds` more attempts.
3. **Install.** The app is copied to `apps/<id>/` and enters quarantine: for
   its first `[forge] quarantine_runs` clean runs, every action asks first.

Forge can write only to its staging folder and a new `apps/<id>/`. The smoke
test runs the generated code once, offline, before you have read it, so
review the new folder in Working Copy before committing. The build step makes
up to three model calls in one run and can take a minute or more.

## Writing apps

`run(ctx, **inputs)` returns a dict keyed by output names. Use
`ctx.ai(capability, prompt, file=..., as_json=...)`, `ctx.read_text(ref)`,
`ctx.write_text(ref, text, mode)`, `ctx.list(ref)`, `ctx.path(ref)`, `ctx.log(msg)`.
For other APIs use `ctx.fetch(url, method=, headers=, data=)`, which returns
`{"status", "text", "headers"}` and only calls HTTPS hosts listed under
`[permissions] net`, and `ctx.secret(name)` for keys listed under
`[permissions] secrets`.
Special keys `_show`, `_open_url` and `_actions` go straight to the Shortcut.
Smoke tests define `INPUTS`, an optional `check(result, response)`, and an
optional `FETCH = {"<url prefix>": "<body>"}`; they run with mock models, mock
fetches and no file writes. `python -m harness contract` prints the full rules.

## Device notes

- Confirmed on a-Shell: in-process pip into `--target` folders, HTTPS via
  `urllib`, and access to `pickFolder` folders from Python.
- Needs Python 3.11+ for `tomllib`, else `pip install tomli` (allowlisted).
- `timeout_s` is a budget that triggers a warning; runs can't be killed mid-way.
- `voice-to-vault/tests/sample.m4a` is a placeholder; replace it with real audio.
- Not yet tried on a device: Run Shortcut with the name taken from a
  variable, which the action loop depends on.
- `ctx.fetch` follows redirects, so a listed host can send it elsewhere.
- Dates sent from Shortcuts must be formatted `yyyy-MM-dd` (or today,
  yesterday, tomorrow).
