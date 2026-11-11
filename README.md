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
~/Documents/.harness/      <- per-device state, never committed
  config.toml              <- aliases, model mapping, package allowlist
  secrets.toml             <- API keys
  catalog.json             <- healthy apps, read by router and Shortcuts
  inbox/ outbox/           <- Shortcuts bridge
  deps/<id>-<hash>/        <- per-app packages (stand-in for venvs)
  errors/ repairs/         <- last traceback per app, proposed fixes
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

- `ok`: show `show`, post `notification`, copy `clipboard`, open `open_url`
- `ask`: collect each item in `ask` (its `type` picks the action), add the
  answers to `inputs`, resend with the same id and `app`
- `choose`: show the keys of `menu`, look up the picked key to get the app id,
  resend with the same id, that `app`, and the original `utterance`
- `error`: show `error`; optionally run `repair <app>`

Shortcuts: **Harness Run** (the generic loop above, called only by other
Shortcuts), **Harness Attach** (receives files, puts them in `inbox/<id>/`,
outputs `{id}`), and thin wrappers that build a dictionary and call Harness Run.

## Writing apps

`run(ctx, **inputs)` returns a dict keyed by output names. Use
`ctx.ai(capability, prompt, file=..., as_json=...)`, `ctx.read_text(ref)`,
`ctx.write_text(ref, text, mode)`, `ctx.list(ref)`, `ctx.path(ref)`, `ctx.log(msg)`.
Special keys `_show` and `_open_url` go straight to the Shortcut.
Smoke tests define `INPUTS` and an optional `check(result, response)`; they run
with mock models and no file writes.

## Device notes

- Confirmed on a-Shell: in-process pip into `--target` folders, HTTPS via
  `urllib`, and access to `pickFolder` folders from Python.
- Needs Python 3.11+ for `tomllib`, else `pip install tomli` (allowlisted).
- `timeout_s` is a budget that triggers a warning; runs can't be killed mid-way.
- `voice-to-vault/tests/sample.m4a` is a placeholder; replace it with real audio.
- Dates sent from Shortcuts must be formatted `yyyy-MM-dd` (or today,
  yesterday, tomorrow).
