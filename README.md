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

1. In Working Copy, clone the repo and sync it to a folder in Files.
2. In a-Shell, run `pickFolder` and choose that folder. Do the same for your
   Obsidian vault and iWriter folder. After each, run `pwd` to see its path.
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

Request, written with a-Shell's Put File to `inbox/<id>.json`
(attachments go in `inbox/<id>/`):

```json
{"app": "voice-to-vault", "inputs": {"audio": "memo.m4a"}}
{"utterance": "log this to today's note", "attachments": ["memo.m4a"]}
```

Then Execute Command: `cd <repo> ; python -m harness run --request <id>`.
Read `outbox/<id>.json` with Get File and branch on `status`:

- `ok`: show `show`, post `notification`, copy `clipboard`, open `open_url`
- `ask`: collect each item in `ask` (type tells you which action), resend with the same id
- `choose`: show `choices` as a menu, resend with `app` set
- `error`: show `error`; optionally run `repair <app>`

## Writing apps

`run(ctx, **inputs)` returns a dict keyed by output names. Use
`ctx.ai(capability, prompt, file=..., as_json=...)`, `ctx.read_text(ref)`,
`ctx.write_text(ref, text, mode)`, `ctx.list(ref)`, `ctx.path(ref)`, `ctx.log(msg)`.
Special keys `_show` and `_open_url` go straight to the Shortcut.
Smoke tests define `INPUTS` and an optional `check(result, response)`; they run
with mock models and no file writes.

## Verify on device

These depend on a-Shell behaviour that could not be tested off-device:

- Python version: needs 3.11+ for `tomllib`, else `pip install tomli` (allowlisted).
- In-process pip (`pip._internal`) installing into `--target` folders.
- HTTPS certificates for `urllib`; if requests fail with SSL errors, install
  `certifi` and point `SSL_CERT_FILE` at it.
- Access to `pickFolder` folders from Python, and from Shortcuts-launched runs.
- Whether a-Shell reuses the interpreter between commands (the runner purges
  app modules either way).
- `timeout_s` is a budget that triggers a warning; runs can't be killed mid-way.
- `voice-to-vault/tests/sample.m4a` is a placeholder; replace it with real audio.
