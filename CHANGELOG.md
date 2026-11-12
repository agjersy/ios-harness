# Changelog

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
