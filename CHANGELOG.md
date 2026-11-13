# Changelog

## 0.4.0

Shortcuts protocol 2. See "Needs a Shortcuts change" below before updating a device.

Fixed
- Output printed by an app, pip or a warning no longer corrupts the response
  JSON: it is captured and returned as `printed`. (`cli.py`)
- A broken `config.toml` no longer makes `run --request` exit 1 with a
  traceback; the Shortcut gets an `error` response, and `doctor` names the problem.
- Empty-text input keys from Shortcuts no longer overwrite what the router inferred.
- A router guess the app can't read (a bad date, an unknown choice) is
  dropped instead of failing the run. The router is told today's date.
- An optional input with `ask = "always"` can be skipped with an empty answer.
- Request ids and app ids are checked before they are used in file paths.
- `_open_url` passed any URL through, including `shortcuts://`. It now needs
  `[actions] open_url` (see Changed).
- Picking the suggested app from a `choose` menu keeps the inputs the router
  already found.
- Requests and attachments are deleted when a run is `ok`; leftovers are
  cleaned after every request, not only on `sync`.
- Forge: apps using `ctx.ai(..., as_json=True)` could not pass a smoke test.
  `smoke.py` can now script replies with `AI = {...}`.
- Forge and `sync`: a manifest with a wrong shape raised `AttributeError` and
  stopped everything. Manifest validation now reports every such case, and
  one bad app can't stop `sync`.
- Forge: the hard lint let through `subprocess`, `ctypes`, `shutil`, `open()`,
  `os.system`, `ctx.ai.cfg`, `ctx._net`, and anything under `tests/`. All are
  now errors.
- Forge: files from a rejected attempt stayed in staging and were installed.
- Forge: a manifest naming a secret the device lacks could never be drafted.
- Forge: a reused request id with an abandoned draft cancelled the new request.
- Forge: could not be approved from the terminal (`run --id`).
- Forge: redaction tokens such as `[[NAME_1]]` could be baked into the app.
- Forge: a failed final `sync` left the app folder and quarantine entry behind.
- `repair`: the path check was a string prefix test.

Added
- Responses carry `protocol`. New status `continue`; new keys `printed`,
  `speak`, `open_url_confirm`, `suggested`, `repairable`; `ask` items carry
  `required`, `default` and, after a bad answer, `error`.
- Input types `datetime`, `number`, `bool`; output target `speak`.
- Dates as Shortcuts writes them, and weekday names. (`harness/when.py`)
- Forge makes one model call per request, offers "Change it" with feedback,
  can revise an installed app (`inputs.target`), and shows inputs, reply
  targets, URL schemes, subdomain reach and missing secrets at approval.
- Apps in quarantine stay under the hard lint in `sync`.
- Built-in app `repair`: proposals are checked like any app and applied only
  on "Apply it" (`repair <id> --apply` in the terminal).
- The router and Forge redact your identifiers from what you said.
- `ack`, `shortcuts` commands; `[actions] single_shortcut` in `config.toml`.
- `shortcuts/README.md`: every Shortcut, action by action.
- 29 more tests (53 in all).

Changed (may need an edit to an app)
- `_open_url` requires its scheme under `[actions] open_url` in the manifest.
  Strict apps can't open URLs.
- Unknown top-level tables in `app.toml` are errors, as are output paths
  with placeholders that are not inputs.
- Strict apps are held to the wider hard lint (no `open()`, `pathlib`, `sys`,
  `os` beyond `os.path`).
- The app id `repair` is reserved.
- Request ids may contain only letters, digits, `-`, `_` and `.` (no spaces or colons).
- `repair` proposals are saved as `repairs/<id>-<time>/<id>/`.
- A weekday name or unreadable date sent as an input is now read or asked
  for again, rather than failing.

Needs a Shortcuts change
- Harness Run: check `protocol`; handle `continue`; on `ask`, start from the
  response's `inputs`, show an item's `error`, and send empty text for a
  skipped answer; handle `speak` and `open_url_confirm`; optionally offer
  repair when `repairable`. An older Harness Run still works for apps that
  only use `ok`, `ask`, `choose` and `error`, but can't finish a Forge build
  that needs a second attempt.
- Forge approval now has three choices.

## 0.3.0

Added
- Typed actions: apps return `_actions`; manifests declare `[actions] allow`. (`harness/actions.py`)
- Strict privacy mode: `[app] privacy = "strict"` turns on the egress gate, the
  audit log and no-retention handling. (`harness/redact.py`, `ai.py`, `bridge.py`)
- Redacted tracebacks and run logs for every app. (`runner.py`)
- Forge, the built-in app `forge`, with quarantine for what it installs.
  (`harness/forge.py`, `harness/quarantine.py`)
- `ctx.fetch` and `ctx.secret`, gated by `[permissions] net` and `secrets`.
- `python -m harness contract` and `promote`; the contract also feeds `repair`.
- `apps/remind-me` for testing actions; `tests/` with 24 offline tests.

Changed
- `secrets.toml` may hold an `[identifiers]` table. Top-level keys work as before.
- Menus from `choose` end with "Build a new app".
- `sync` warns about network imports and `ctx.cfg` in standard apps and
  rejects them in strict apps.
- The app id `forge` is reserved.

Needs a Shortcuts change
- Harness Run: loop over `actions` after `ok` (see README, Typed actions).
- One `Harness Act <type>` Shortcut per action type you use.

Existing apps and device configs run unchanged.
