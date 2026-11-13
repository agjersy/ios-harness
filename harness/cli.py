"""Command line: python -m harness <command>."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json

from . import PROTOCOL, actions, bridge, config, quarantine, repair, runner
from .contract import text as contract_text
from .doctor import doctor
from .router import load_catalog
from .sync import sync


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


ACK_RESULTS = ("done", "skipped", "failed")


def _from_shortcuts(req_id: str) -> dict:
    """The Shortcuts path. Nothing but the response JSON may reach the command
    output, so anything an app, pip or a warning prints is captured and
    returned under `printed` instead. Never raises."""
    noise, cfg = io.StringIO(), None
    resp = {"id": req_id, "protocol": PROTOCOL}
    with contextlib.redirect_stdout(noise), contextlib.redirect_stderr(noise):
        try:
            cfg = config.load()
            resp = runner.handle(bridge.read_request(cfg, req_id), cfg)
            resp["id"] = resp.get("id") or req_id
        except Exception as e:
            resp.update(status="error", error=f"{type(e).__name__}: {e}")
        printed = noise.getvalue().strip()
        if printed:
            resp["printed"] = printed[-2000:]
        if cfg is not None:
            try:
                bridge.write_response(cfg, req_id, resp)
                bridge.cleanup(cfg)
            except Exception as e:
                resp.setdefault("warning", f"Response not saved to outbox: {type(e).__name__}: {e}")
    return resp


def _ack(cfg, args) -> dict:
    """Record what the Shortcut did with an action, so runs can be audited."""
    entry = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "action": args.action,
             "result": args.result, "detail": (args.detail or "")[:500]}
    try:
        bridge.check_id(args.action)
        if args.result not in ACK_RESULTS:
            raise ValueError(f"result must be one of {', '.join(ACK_RESULTS)}")
        with open(cfg.actions_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        return {"protocol": PROTOCOL, "status": "error", "error": f"{type(e).__name__}: {e}"}
    return {"protocol": PROTOCOL, "status": "ok", **entry}


def _shortcuts(cfg) -> list:
    """The Shortcuts this device needs, worked out from the installed apps."""
    lines = ["Harness Run        the request loop; every wrapper Shortcut calls it",
             "Harness Attach     saves shared files to inbox/<id>/"]
    try:
        catalog = runner.router.load_catalog(cfg)
    except Exception:
        catalog = []
    used = {}
    for app in catalog:
        for kind in app.get("actions", []):
            used.setdefault(kind, []).append(app["id"])
    if cfg.single_action_shortcut:
        lines.append(f"{actions.DISPATCHER:18} one Shortcut that branches on the action's type "
                     f"({', '.join(sorted(used)) or 'no types in use yet'})")
    else:
        for kind in sorted(used):
            lines.append(f"{actions.SHORTCUT_PREFIX + kind:32} used by {', '.join(used[kind])}")
    for name in sorted(cfg.action_shortcuts):
        lines.append(f"{name:32} allowed for shortcut.run")
    lines.append("")
    lines.append("Build steps for each are in shortcuts/README.md.")
    return lines


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness", description="iOS Python harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="validate apps, install packages, smoke-test, rebuild catalog")
    s.add_argument("--force", action="store_true", help="reinstall all packages")
    s.add_argument("--no-test", action="store_true", help="skip smoke tests")

    r = sub.add_parser("run", help="run an app")
    r.add_argument("--request", help="request id in the inbox (used by Shortcuts)")
    r.add_argument("--json", help="inline request JSON")
    r.add_argument("--app", help="app id")
    r.add_argument("--say", help="free text to route")
    r.add_argument("--id", help="request id; reuse it to answer an `ask` (Forge approval, for example)")
    r.add_argument("-i", "--input", action="append", default=[], metavar="NAME=VALUE")

    ro = sub.add_parser("route", help="show how a request would be routed")
    ro.add_argument("text")
    rp = sub.add_parser("repair", help="propose a fix for an app's last crash, or apply one")
    rp.add_argument("app")
    rp.add_argument("--apply", action="store_true", help="apply the latest proposal that passed its checks")
    pr = sub.add_parser("promote", help="end quarantine for an app Forge built")
    pr.add_argument("app")
    ak = sub.add_parser("ack", help="record what the Shortcut did with an action (always exits 0)")
    ak.add_argument("action", help="the action's id from the response")
    ak.add_argument("result", help="done, skipped or failed")
    ak.add_argument("detail", nargs="?", default="")
    sub.add_parser("shortcuts", help="list the Shortcuts this device needs")
    sub.add_parser("contract", help="print the app-writing rules given to models")
    sub.add_parser("doctor", help="check device setup")
    sub.add_parser("list", help="list apps in the catalog")

    args = ap.parse_args(argv)

    if args.cmd == "run" and args.request:  # always exit 0, status lives in the response
        _print(_from_shortcuts(args.request))
        return 0

    try:
        cfg = config.load()
    except Exception as e:  # a broken config.toml must be readable, not a traceback
        message = f"{type(e).__name__}: {e}"
        if args.cmd == "ack":
            _print({"protocol": PROTOCOL, "status": "error", "error": message})
            return 0
        print(f"XX  config: {message}")
        print("    Fix config.toml or secrets.toml in your harness folder "
              "(default ~/Documents/.harness), then run doctor again.")
        return 1

    if args.cmd == "sync":
        report = sync(cfg, force=args.force, run_tests=not args.no_test)
        for app_id in report["ok"]:
            print(f"ok      {app_id}")
        for app_id, errs in report["failed"].items():
            print(f"FAILED  {app_id}")
            for e in errs:
                print(f"        {e}")
        for app_id, warns in report["warnings"].items():
            for w in warns:
                print(f"warn    {app_id}: {w}")
        return 0 if not report["failed"] else 1

    if args.cmd == "run":
        req = json.loads(args.json) if args.json else {}
        if not isinstance(req, dict):
            raise ValueError("--json must be a JSON object")
        if args.app:
            req["app"] = args.app
        if args.say:
            req["utterance"] = args.say
        if args.id:
            req["id"] = args.id
        inputs = req.setdefault("inputs", {})
        for pair in args.input:
            name, _, value = pair.partition("=")
            inputs[name] = value
        resp = runner.handle(req, cfg)
        while resp.get("status") == "continue":  # Forge retrying: resend, as Harness Run does
            print(resp.get("show", "Working..."))
            resp = runner.handle({**req, "id": resp["id"], "app": resp["app"], "inputs": resp["inputs"]}, cfg)
        _print(resp)
        if resp.get("status") == "ask" and not args.id:
            print(f"\nTo answer, run again with --id {resp['id']} and -i <name>=<answer>.")
        return 0

    if args.cmd == "route":
        _print(runner.route_request({"utterance": args.text}, cfg))
        return 0

    if args.cmd == "repair":
        _print(repair.apply(args.app, cfg) if args.apply else repair.repair(args.app, cfg))
        return 0

    if args.cmd == "promote":
        found = quarantine.promote(cfg, args.app)
        print(f"{args.app} is out of quarantine" if found else f"{args.app} was not in quarantine")
        return 0

    if args.cmd == "ack":
        _print(_ack(cfg, args))
        return 0

    if args.cmd == "shortcuts":
        print("\n".join(_shortcuts(cfg)))
        return 0

    if args.cmd == "contract":
        print(contract_text())
        return 0

    if args.cmd == "doctor":
        checks = doctor(cfg)
        for name, ok, detail in checks:
            print(f"{'ok ' if ok else 'XX '} {name}: {detail}")
        return 0 if all(ok for _, ok, _ in checks) else 1

    if args.cmd == "list":
        for a in load_catalog(cfg):
            left = quarantine.remaining(cfg, a["id"])
            flags = [a.get("privacy")] if a.get("privacy") == "strict" else []
            flags += [f"quarantine: {left} runs left"] if left else []
            print(f"{a['id']:24} {a['name']}" + (f"  [{', '.join(flags)}]" if flags else ""))
        return 0
    return 1


def main(argv=None) -> int:
    try:
        return _main(argv)
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
