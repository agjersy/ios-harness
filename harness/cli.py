"""Command line: python -m harness <command>."""
from __future__ import annotations

import argparse
import json

from . import bridge, config, quarantine, runner
from .contract import text as contract_text
from .doctor import doctor
from .repair import repair
from .router import load_catalog
from .sync import sync


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


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
    r.add_argument("-i", "--input", action="append", default=[], metavar="NAME=VALUE")

    ro = sub.add_parser("route", help="show how a request would be routed")
    ro.add_argument("text")
    rp = sub.add_parser("repair", help="propose a fix for an app's last crash")
    rp.add_argument("app")
    pr = sub.add_parser("promote", help="end quarantine for an app Forge built")
    pr.add_argument("app")
    sub.add_parser("contract", help="print the app-writing rules given to models")
    sub.add_parser("doctor", help="check device setup")
    sub.add_parser("list", help="list apps in the catalog")

    args = ap.parse_args(argv)
    cfg = config.load()

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
        if args.request:  # Shortcuts path: always exit 0, status lives in the response
            try:
                req = bridge.read_request(cfg, args.request)
                resp = runner.handle(req, cfg)
            except Exception as e:
                resp = {"id": args.request, "status": "error", "error": f"{type(e).__name__}: {e}"}
            bridge.write_response(cfg, args.request, resp)
            _print(resp)  # Shortcuts reads this from the command output
            return 0
        req = json.loads(args.json) if args.json else {}
        if args.app:
            req["app"] = args.app
        if args.say:
            req["utterance"] = args.say
        inputs = req.setdefault("inputs", {})
        for pair in args.input:
            name, _, value = pair.partition("=")
            inputs[name] = value
        _print(runner.handle(req, cfg))
        return 0

    if args.cmd == "route":
        _print(runner.route_request({"utterance": args.text}, cfg))
        return 0

    if args.cmd == "repair":
        _print(repair(args.app, cfg))
        return 0

    if args.cmd == "promote":
        found = quarantine.promote(cfg, args.app)
        print(f"{args.app} is out of quarantine" if found else f"{args.app} was not in quarantine")
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
