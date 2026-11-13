"""Harness tests. Run from the repo root: python -m unittest discover tests

Everything runs offline against a temporary HARNESS_HOME and a copy of apps/.
All personal details below are made up.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import contextlib  # noqa: E402
import datetime as dt  # noqa: E402
import io  # noqa: E402

from harness import PROTOCOL, actions, ai, bridge, cli, config, quarantine, repair, runner, when  # noqa: E402
from harness.manifest import load as load_manifest  # noqa: E402
from harness.redact import Redactor  # noqa: E402
from harness.sync import lint, sync  # noqa: E402

IDENTIFIERS = {"name": ["Jane Q. Doe", "Doe"], "account": ["000123456789"], "address": ["12 Elm Street"]}
CANARIES = ["jane", "doe", "000123456789", "0001 2345 6789", "elm street", "4111", "jane@example.com"]
STATEMENT = ("JANE Q DOE, 12 Elm Street. Account 0001 2345 6789. Card 4111 1111 1111 1111. "
             "Contact jane@example.com. 2026-11-03 COFFEE ROASTERS 4.50")

STRICT_TOML = '''schema = 1
[app]
id = "ledger"
name = "Ledger"
description = "Test app."
entry = "main:run"
privacy = "strict"
[inputs.text]
type = "text"
required = true
[inputs.mode]
type = "choice"
options = ["send", "crash", "pdf"]
default = "send"
[outputs.reply]
type = "text"
[ai]
uses = ["fast"]
[test]
smoke = "tests/smoke.py"
'''
STRICT_MAIN = '''def run(ctx, text, mode):
    if mode == "crash":
        raise ValueError("could not parse row: " + text)
    if mode == "pdf":
        return {"reply": ctx.ai("fast", "read this", file=ctx.app.root / "statement.pdf")}
    return {"reply": ctx.ai("fast", text)}
'''

DRAFT = {"id": "word-count", "manifest": '''schema = 1
[app]
id = "word-count"
name = "Word count"
description = "Count the words in dictated text."
entry = "main:run"
[inputs.text]
type = "dictation"
required = true
[outputs.reply]
type = "text"
[router]
examples = ["count these words", "how many words is this"]
[test]
smoke = "tests/smoke.py"
'''}
GOOD_CODE = {"files": {
    "main.py": 'def run(ctx, text):\n    return {"reply": f"{len(text.split())} words"}\n',
    "tests/smoke.py": 'INPUTS = {"text": "one two three"}\n\n\ndef check(result, response):\n'
                      '    assert response["show"] == "3 words"\n'}}
BAD_CODE = {"files": {**GOOD_CODE["files"],
                      "main.py": "import urllib.request\n\n" + GOOD_CODE["files"]["main.py"]}}


class HarnessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "apps", self.tmp / "repo" / "apps")
        (self.tmp / "vault").mkdir()
        os.environ["HARNESS_HOME"] = str(self.tmp / "home")
        os.environ["HARNESS_REPO"] = str(self.tmp / "repo")
        self.cfg = config.load()
        self.cfg.aliases["obsidian"] = self.tmp / "vault"
        self.cfg.identifiers = IDENTIFIERS
        self.cfg.models = {cap: {"provider": "mock"} for cap in ("fast", "reason", "transcribe")}
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def add_app(self, app_id, toml, main, smoke='INPUTS = {"text": "hi"}\n'):
        root = self.cfg.apps_dir / app_id
        (root / "tests").mkdir(parents=True)
        (root / "app.toml").write_text(toml)
        (root / "main.py").write_text(main)
        (root / "tests" / "smoke.py").write_text(smoke)

    def stored_text(self):
        """Everything the harness has written to its home folder."""
        return "\n".join(p.read_text(errors="replace") for p in self.cfg.home.rglob("*")
                         if p.is_file() and p.name not in ("secrets.toml", "config.toml")).lower()


class RedactorTests(unittest.TestCase):
    def test_known_identifiers_and_patterns(self):
        r = Redactor(IDENTIFIERS)
        out = r.redact(STATEMENT + " SSN 123-45-6789, phone (555) 123-4567, ref 9876543210.")
        for secret in ("JANE", "DOE", "Elm", "2345", "4111", "example.com", "123-45", "555", "9876543210"):
            self.assertNotIn(secret, out)
        self.assertIn("COFFEE ROASTERS 4.50", out)  # merchant, amount and date pass through
        self.assertIn("2026-11-03", out)
        self.assertEqual(r.leaks(out), [])
        self.assertEqual(r.restore(out).count("JANE Q DOE"), 1)

    def test_same_value_gets_same_token(self):
        r = Redactor(IDENTIFIERS)
        out = r.redact("000123456789 then 0001-2345-6789")
        self.assertEqual(out, "[[ACCOUNT_1]] then [[ACCOUNT_1]]")

    def test_leak_check_ignores_spacing_and_case(self):
        r = Redactor(IDENTIFIERS)
        self.assertEqual(r.leaks("holder:JANEQDOE acct 0.0.0.1.2.3.4.5.6.7.8.9"), ["ACCOUNT", "NAME"])

    def test_restore_walks_parsed_json(self):
        r = Redactor(IDENTIFIERS)
        token = r.redact("Jane Q. Doe")
        self.assertEqual(r.restore({"holder": [token]}), {"holder": ["Jane Q. Doe"]})

    def test_unrelated_numbers_survive(self):
        out = Redactor(IDENTIFIERS).redact("Paid 1234.56 on 2026-11-03, 12 items, check 1042")
        self.assertEqual(out, "Paid 1234.56 on 2026-11-03, 12 items, check 1042")


class PrivacyTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.add_app("ledger", STRICT_TOML, STRICT_MAIN)

    def run_request(self, mode):
        """Go through the same path Shortcuts uses, including the bridge."""
        req = {"app": "ledger", "inputs": {"text": STATEMENT, "mode": mode}}
        (self.cfg.inbox / "r1.json").write_text(json.dumps(req))
        (self.cfg.inbox / "r1").mkdir(exist_ok=True)
        (self.cfg.inbox / "r1" / "statement.txt").write_text(STATEMENT)
        resp = runner.handle(bridge.read_request(self.cfg, "r1"), self.cfg)
        bridge.write_response(self.cfg, "r1", resp)
        return resp

    def assert_nothing_stored(self):
        stored = self.stored_text()
        for canary in CANARIES:
            self.assertNotIn(canary, stored)

    def test_gate_redacts_then_restores(self):
        resp = self.run_request("send")
        self.assertEqual(resp["status"], "ok")
        self.assertIn("JANE Q DOE", resp["show"])  # restored for the person
        sent = (next(self.cfg.egress.glob("*.jsonl"))).read_text()
        self.assertIn("[[NAME_1]]", sent)
        self.assertIn("COFFEE ROASTERS", sent)
        self.assert_nothing_stored()
        self.assertFalse((self.cfg.inbox / "r1").exists())  # attachments purged

    def test_crash_leaves_no_data_behind(self):
        resp = self.run_request("crash")
        self.assertEqual(resp["status"], "error")
        self.assertIn("JANE Q DOE", resp["error"])  # shown to the person
        saved = (self.cfg.errors / "ledger.txt").read_text()
        self.assertIn("ValueError: <message withheld", saved)
        self.assertIn("main.py", saved)
        self.assert_nothing_stored()

    def test_binary_files_are_refused(self):
        resp = self.run_request("pdf")
        self.assertEqual(resp["status"], "error")
        self.assertIn("can't send a .pdf", resp["error"])

    def test_send_is_blocked_when_redaction_misses(self):
        self.cfg.identifiers = {"name": ["Jane Q. Doe"]}
        (self.cfg.apps_dir / "ledger" / "main.py").write_text(
            'def run(ctx, text, mode):\n    return {"reply": ctx.ai("fast", "ref XJANEQDOE7")}\n')
        resp = self.run_request("send")
        self.assertEqual(resp["status"], "error")
        self.assertIn("Not sent", resp["error"])
        self.assertEqual(list(self.cfg.egress.glob("*.jsonl")), [])

    def test_standard_app_errors_are_redacted_not_withheld(self):
        self.add_app("plain", STRICT_TOML.replace("ledger", "plain").replace('privacy = "strict"\n', ""),
                     STRICT_MAIN)
        resp = runner.handle({"app": "plain", "inputs": {"text": STATEMENT, "mode": "crash"}}, self.cfg)
        saved = (self.cfg.errors / "plain.txt").read_text()
        self.assertIn("could not parse row: [[NAME_1]]", saved)
        self.assertEqual(resp["status"], "error")
        self.assert_nothing_stored()

    def test_strict_lint_rejects_network_imports(self):
        (self.cfg.apps_dir / "ledger" / "main.py").write_text("import urllib.request\n" + STRICT_MAIN)
        report = sync(self.cfg)
        self.assertIn("imports urllib.request", report["failed"]["ledger"][0])


class ActionTests(HarnessCase):
    def test_remind_me_emits_a_reminder(self):
        resp = runner.handle({"app": "remind-me", "inputs": {"text": "call the bank", "day": "2027-01-15"}}, self.cfg)
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["actions"], [{
            "id": f"{resp['id']}-1", "type": "reminder.create", "tier": "write",
            "shortcut": "Harness Act reminder.create", "title": "call the bank", "due": "2027-01-15",
            "confirm": False, "summary": "Create reminder: call the bank (due 2027-01-15)"}])
        self.assertEqual(json.loads(self.cfg.runs_log.read_text().splitlines()[-1])["actions"], ["reminder.create"])

    def check(self, raw, allowed=("reminder.create", "mail.draft", "shortcut.run", "event.create")):
        return actions.validate(raw, allowed, self.cfg, "run")

    def test_rejections(self):
        for raw, message in [
            ([{"type": "note.create", "body": "x"}], "not declared"),
            ([{"type": "launch.missiles"}], "unknown type"),
            ([{"type": "reminder.create"}], "'title' is required"),
            ([{"type": "reminder.create", "title": "x", "when": "today"}], "unknown fields"),
            ([{"type": "event.create", "title": "x", "start": "next week"}], "not a date"),
            ([{"type": "shortcut.run", "name": "Wipe Phone"}], "not listed"),
        ]:
            with self.assertRaises(actions.ActionError) as caught:
                self.check(raw)
            self.assertIn(message, str(caught.exception))

    def test_outbound_always_confirms(self):
        out = self.check([{"type": "mail.draft", "to": "a@example.com", "subject": "Hi", "body": "x", "confirm": False}])
        self.assertTrue(out[0]["confirm"])
        self.assertEqual(out[0]["to"], ["a@example.com"])

    def test_bad_action_writes_no_files(self):
        self.add_app("half", '''schema = 1
[app]
id = "half"
name = "Half"
description = "Test."
entry = "main:run"
[outputs.note]
type = "file"
path = "@obsidian/half.md"
[permissions]
write = ["@obsidian"]
[actions]
allow = ["reminder.create"]
''', 'def run(ctx):\n    return {"note": "text", "_actions": [{"type": "reminder.create"}]}\n')
        resp = runner.handle({"app": "half"}, self.cfg)
        self.assertEqual(resp["status"], "error")
        self.assertFalse((self.tmp / "vault" / "half.md").exists())

    def test_quarantine_confirms_then_graduates(self):
        quarantine.add(self.cfg, "remind-me", 2)
        req = {"app": "remind-me", "inputs": {"text": "x"}}
        first = runner.handle(req, self.cfg)
        self.assertTrue(first["actions"][0]["confirm"])
        self.assertEqual(first["quarantine_runs_left"], 1)
        self.assertEqual(runner.handle(req, self.cfg)["quarantine_runs_left"], 0)
        self.assertFalse(runner.handle(req, self.cfg)["actions"][0]["confirm"])


class FetchTests(HarnessCase):
    TOML = '''schema = 1
[app]
id = "fetcher"
name = "Fetcher"
description = "Test."
entry = "main:run"
[inputs.url]
type = "url"
required = true
[outputs.reply]
type = "text"
[permissions]
net = ["api.example.com"]
'''

    def test_undeclared_host_is_refused(self):
        self.add_app("fetcher", self.TOML, 'def run(ctx, url):\n    return {"reply": ctx.fetch(url)["text"]}\n')
        resp = runner.handle({"app": "fetcher", "inputs": {"url": "https://evil.example.org/x"}}, self.cfg)
        self.assertIn("may not fetch", resp["error"])
        resp = runner.handle({"app": "fetcher", "inputs": {"url": "http://api.example.com/x"}}, self.cfg)
        self.assertIn("may not fetch", resp["error"])

    def test_strict_app_cannot_declare_net(self):
        self.add_app("fetcher", self.TOML.replace('entry = "main:run"', 'entry = "main:run"\nprivacy = "strict"'),
                     "def run(ctx, url):\n    return {}\n")
        self.assertIn("strict app may not declare", sync(self.cfg)["failed"]["fetcher"][0])


class ForgeTests(HarnessCase):
    def forge(self, replies, **req):
        """Send one Forge request, resending while it answers "continue" as Harness Run does."""
        self.cfg.models["reason"] = {"provider": "mock", "reply": [json.dumps(r) for r in replies]}
        self.steps = 1
        resp = runner.handle({"id": "f1", "app": "forge", **req}, self.cfg)
        while resp["status"] == "continue":
            self.steps += 1
            resp = runner.handle({"id": "f1", "app": "forge", "inputs": resp["inputs"]}, self.cfg)
        return resp

    def test_menus_offer_forge(self):
        sync(self.cfg)
        menu = runner.handle({}, self.cfg)["menu"]
        self.assertEqual(menu["Build a new app"], "forge")

    def test_draft_approve_build_install(self):
        resp = self.forge([DRAFT], utterance="count the words I dictate")
        self.assertEqual(resp["status"], "ask")
        prompt = resp["ask"][0]["prompt"]
        self.assertIn("Word count (word-count)", prompt)
        self.assertIn("Touches nothing outside its own inputs.", prompt)
        self.assertFalse((self.cfg.apps_dir / "word-count").exists())

        # First code attempt imports urllib and is rejected; the second passes.
        resp = self.forge([BAD_CODE, GOOD_CODE], inputs={**resp["inputs"], "approve": "Build it"})
        self.assertEqual(resp["status"], "ok", resp)
        self.assertEqual(self.steps, 2)  # one model call per request
        self.assertEqual(resp["built"], "word-count")
        self.assertEqual(quarantine.remaining(self.cfg, "word-count"), self.cfg.quarantine_runs)
        self.assertNotIn("urllib", (self.cfg.apps_dir / "word-count" / "main.py").read_text())
        self.assertFalse((self.cfg.forge / "f1").exists())

        run = runner.handle({"app": "word-count", "inputs": {"text": "a b"}}, self.cfg)
        self.assertEqual(run["show"], "2 words")

    def test_cancel_installs_nothing(self):
        resp = self.forge([DRAFT], utterance="count words")
        resp = self.forge([], inputs={**resp["inputs"], "approve": "Cancel"})
        self.assertIn("Cancelled", resp["show"])
        self.assertFalse((self.cfg.apps_dir / "word-count").exists())

    def test_manifest_is_frozen_and_paths_are_confined(self):
        resp = self.forge([DRAFT], utterance="count words")
        sneaky = {"files": {**GOOD_CODE["files"], "app.toml": "x", "../../evil.py": "x"}}
        self.cfg.forge_rounds = 0
        resp = self.forge([sneaky], inputs={**resp["inputs"], "approve": "Build it"})
        self.assertEqual(resp["status"], "error")
        self.assertIn("only .py files", resp["error"])
        self.assertFalse((self.cfg.forge / "evil.py").exists())

    def test_taken_and_reserved_ids_are_rejected(self):
        self.cfg.forge_rounds = 0
        for taken in ("hello", "forge"):
            resp = self.forge([{**DRAFT, "id": taken}], utterance="count words")
            self.assertEqual(resp["status"], "error")
            self.assertIn("already taken", resp["error"])

    def test_request_is_redacted_before_drafting(self):
        self.forge([DRAFT], utterance="track statements for Jane Q. Doe")
        state = json.loads((self.cfg.forge / "f1" / "state.json").read_text())
        self.assertEqual(state["request"], "track statements for [[NAME_1]]")

    def approve(self, resp, replies, choice="Build it", **extra):
        return self.forge(replies, inputs={**resp["inputs"], "approve": choice, **extra})

    def test_files_from_a_rejected_round_are_not_installed(self):
        resp = self.forge([DRAFT], utterance="count words")
        first = {"files": {**BAD_CODE["files"], "leftover.py": "import socket\n"}}
        resp = self.approve(resp, [first, GOOD_CODE])
        self.assertEqual(resp["status"], "ok", resp)
        self.assertFalse((self.cfg.apps_dir / "word-count" / "leftover.py").exists())

    def test_malformed_manifest_goes_back_to_the_model(self):
        broken = {**DRAFT, "manifest": DRAFT["manifest"].replace(
            '[inputs.text]\ntype = "dictation"\nrequired = true', '[inputs]\ntext = "dictation"')}
        resp = self.forge([broken, DRAFT], utterance="count words")
        self.assertEqual(resp["status"], "ask", resp)
        self.assertEqual(self.steps, 2)

    def test_missing_secret_is_a_note_not_a_dead_end(self):
        needs_key = {**DRAFT, "manifest": DRAFT["manifest"]
                     + '[permissions]\nnet = ["api.example.com"]\nsecrets = ["EXAMPLE_TOKEN_X"]\n'}
        resp = self.forge([needs_key], utterance="count words")
        prompt = resp["ask"][0]["prompt"]
        self.assertIn("add to secrets.toml: EXAMPLE_TOKEN_X", prompt)
        self.assertIn("api.example.com and its subdomains", prompt)
        self.assertIn("Asks for: text (dictation)", prompt)
        resp = self.approve(resp, [GOOD_CODE])
        self.assertEqual(resp["status"], "ok", resp)
        self.assertIn("not in the menu yet", resp["show"])
        self.assertTrue((self.cfg.apps_dir / "word-count").exists())

    def test_reused_id_with_abandoned_draft_starts_over(self):
        self.forge([DRAFT], utterance="count words")
        other = {"id": "tally", "manifest": DRAFT["manifest"].replace("word-count", "tally")}
        resp = self.forge([other], utterance="a completely different app")
        self.assertEqual(resp["status"], "ask")
        self.assertIn("(tally)", resp["ask"][0]["prompt"])

    def test_tokens_may_not_be_copied_into_the_app(self):
        self.cfg.forge_rounds = 0
        leaky = {**DRAFT, "manifest": DRAFT["manifest"].replace("Count the words", "Count [[NAME_1]]'s words")}
        resp = self.forge([leaky], utterance="count words for Jane Q. Doe")
        self.assertEqual(resp["status"], "error")
        self.assertIn("[[TOKEN]]", resp["error"])

    def test_change_it_redrafts_with_feedback(self):
        resp = self.forge([DRAFT], utterance="count words")
        self.assertEqual(resp["ask"][0]["options"], ["Build it", "Change it", "Cancel"])
        resp = self.approve(resp, [], choice="Change it")
        self.assertEqual(resp["ask"][0]["name"], "feedback")
        clip = {**DRAFT, "manifest": DRAFT["manifest"].replace('type = "text"\n[router]',
                                                               'type = "text"\nto = ["show", "clipboard"]\n[router]')}
        resp = self.forge([clip], inputs={**resp["inputs"], "feedback": "also copy the count"})
        self.assertEqual(resp["ask"][0]["name"], "approve")
        self.assertIn("Replies by: clipboard, show", resp["ask"][0]["prompt"])
        state = json.loads((self.cfg.forge / "f1" / "state.json").read_text())
        self.assertEqual(state["feedback"], ["also copy the count"])
        self.assertEqual(self.approve(resp, [GOOD_CODE])["status"], "ok")

    def test_revise_replaces_an_app_and_keeps_the_old_one(self):
        resp = self.approve(self.forge([DRAFT], utterance="count words"), [GOOD_CODE])
        self.assertEqual(resp["status"], "ok", resp)
        quarantine.promote(self.cfg, "word-count")
        new_code = {"files": {
            "main.py": 'def run(ctx, text):\n    return {"reply": f"{len(text.split())} words!"}\n',
            "tests/smoke.py": 'INPUTS = {"text": "a b"}\n'}}
        resp = self.forge([DRAFT], inputs={"target": "word-count", "request": "add an exclamation mark"})
        self.assertIn("replaces the installed app word-count", resp["ask"][0]["prompt"])
        resp = self.approve(resp, [new_code])
        self.assertEqual(resp["status"], "ok", resp)
        self.assertIn("Updated", resp["show"])
        run = runner.handle({"app": "word-count", "inputs": {"text": "a b"}}, self.cfg)
        self.assertEqual(run["show"], "2 words!")
        self.assertEqual(quarantine.remaining(self.cfg, "word-count"), self.cfg.quarantine_runs - 1)
        backups = list((self.cfg.forge / "_backups").iterdir())
        self.assertIn("3 words", (backups[0] / "tests" / "smoke.py").read_text())

    def test_rejected_install_is_rolled_back(self):
        from harness import forge
        real = forge.sync
        self.addCleanup(setattr, forge, "sync", real)
        forge.sync = lambda cfg: {**real(cfg), "ok": [], "failed": {"word-count": ["boom"]}}
        resp = self.approve(self.forge([DRAFT], utterance="count words"), [GOOD_CODE], choice="build it")
        self.assertEqual(resp["status"], "error")
        self.assertIn("was not installed because sync rejected it: boom", resp["error"])
        self.assertFalse((self.cfg.apps_dir / "word-count").exists())
        self.assertEqual(quarantine.remaining(self.cfg, "word-count"), 0)

    def test_revise_must_keep_the_id(self):
        self.cfg.forge_rounds = 0
        resp = self.forge([DRAFT], inputs={"target": "hello", "request": "shout"})
        self.assertIn("id must stay 'hello'", resp["error"])


JSON_TOML = STRICT_TOML.replace("ledger", "tasks").replace('privacy = "strict"\n', "")
JSON_MAIN = 'def run(ctx, text, mode):\n    return {"reply": ctx.ai("fast", text, as_json=True)["task"]}\n'


class LintAndSmokeTests(HarnessCase):
    def test_smoke_can_script_model_replies(self):
        self.add_app("tasks", JSON_TOML, JSON_MAIN)
        self.assertIn("did not return valid JSON", sync(self.cfg)["failed"]["tasks"][0])
        (self.cfg.apps_dir / "tasks" / "tests" / "smoke.py").write_text(
            'INPUTS = {"text": "hi"}\nAI = {"fast": {"task": "call"}}\n\n'
            'def check(result, response):\n    assert response["show"] == "call"\n')
        self.assertIn("tasks", sync(self.cfg)["ok"])

    def test_one_malformed_manifest_does_not_stop_sync(self):
        self.add_app("broken", JSON_TOML.replace("tasks", "broken").replace(
            '[inputs.text]\ntype = "text"\nrequired = true', '[inputs]\ntext = "dictation"'), JSON_MAIN)
        report = sync(self.cfg)
        self.assertIn("must be a table", " ".join(report["failed"]["broken"]))
        self.assertIn("hello", report["ok"])

    def test_manifest_rejects_typos_and_bad_placeholders(self):
        self.add_app("typo", JSON_TOML.replace("tasks", "typo") + '[permission]\nnet = ["a.example.com"]\n'
                     '[outputs.note]\ntype = "file"\npath = "@obsidian/{nope}.md"\n[permissions]\nwrite = ["@obsidian"]\n',
                     JSON_MAIN)
        errors = " ".join(sync(self.cfg)["failed"]["typo"])
        self.assertIn("unknown top-level keys ['permission']", errors)
        self.assertIn("['nope'], which are not declared inputs", errors)

    def test_hard_lint_closes_the_ways_around_ctx(self):
        self.add_app("gaps", JSON_TOML.replace("tasks", "gaps"), """import subprocess, ctypes, shutil, os, sys, pathlib
from tests import helper
def run(context, text, mode):
    context.ai.gate = None
    context._net.append("evil.example")
    key = getattr(context, "cfg")
    data = open(os.path.expanduser("~/x")).read()
    os.system("true")
    return {"reply": "ok"}
""")
        (self.cfg.apps_dir / "gaps" / "tests" / "helper.py").write_text("import urllib.request\n")
        m = load_manifest(self.cfg.apps_dir / "gaps")
        errors, _ = lint(m, hard=True)
        found = " ".join(errors)
        for expected in ("imports subprocess", "imports ctypes", "imports shutil", "imports sys", "imports pathlib",
                         "imports from tests/", "ctx.ai.gate", "ctx._net", "getattr() on ctx", "calls open()",
                         "uses os.system", "tests/helper.py:1 imports urllib.request"):
            self.assertIn(expected, found)
        self.assertNotIn("uses os.path", found)
        self.assertEqual(lint(m)[0], [])  # ordinary apps still get warnings only

    def test_quarantined_apps_stay_under_the_hard_lint(self):
        self.add_app("plain", JSON_TOML.replace("tasks", "plain"),
                     "import shutil\n\ndef run(ctx, text, mode):\n    return {'reply': 'ok'}\n")
        self.assertIn("plain", sync(self.cfg)["ok"])
        quarantine.add(self.cfg, "plain", 2)
        self.assertIn("imports shutil", sync(self.cfg)["failed"]["plain"][0])


class RepairTests(HarnessCase):
    FIX = 'def run(ctx, text, mode):\n    return {"reply": "fixed " + text}\n'

    def setUp(self):
        super().setUp()
        self.add_app("plain", STRICT_TOML.replace("ledger", "plain").replace('privacy = "strict"\n', ""), STRICT_MAIN)
        resp = runner.handle({"app": "plain", "inputs": {"text": "x", "mode": "crash"}}, self.cfg)
        self.assertTrue(resp["repairable"])

    def propose(self, files):
        self.cfg.models["reason"] = {"provider": "mock", "reply": json.dumps({"explanation": "Why.", "files": files})}
        return runner.handle({"id": "fix1", "app": "repair", "inputs": {"target": "plain"}}, self.cfg)

    def test_passing_fix_is_offered_then_applied(self):
        resp = self.propose({"main.py": self.FIX, "../../evil.py": "x", "app.toml": "x"})
        self.assertEqual(resp["status"], "ok")  # a proposal with stray paths is not offered
        self.assertFalse((self.cfg.home / "evil.py").exists())
        resp = self.propose({"main.py": self.FIX})
        self.assertEqual(resp["ask"][0]["options"], ["Apply it", "Discard"])
        self.assertIn("crash", (self.cfg.apps_dir / "plain" / "main.py").read_text())  # nothing applied yet
        resp = runner.handle({"id": "fix1", "app": "repair", "inputs": {**resp["inputs"], "approve": "Apply it"}}, self.cfg)
        self.assertEqual(resp["status"], "ok", resp)
        run = runner.handle({"app": "plain", "inputs": {"text": "x", "mode": "crash"}}, self.cfg)
        self.assertEqual(run["show"], "fixed x")
        self.assertFalse((self.cfg.errors / "plain.txt").exists())
        self.assertIn("crash", next(self.cfg.repairs.glob("plain-*/before/main.py")).read_text())

    def test_failing_fix_is_not_offered(self):
        resp = self.propose({"main.py": "def run(ctx, text, mode):\n    return 1 / 0\n"})
        self.assertEqual(resp["status"], "ok")
        self.assertIn("does NOT pass", resp["show"])
        self.assertEqual(repair.apply("plain", self.cfg)["status"], "error")

    def test_input_mistakes_are_not_repairable(self):
        (self.cfg.errors / "plain.txt").unlink()
        resp = runner.handle({"app": "nope"}, self.cfg)
        self.assertIn("No app named", resp["error"])
        self.assertNotIn("repairable", resp)
        self.assertEqual(list(self.cfg.errors.iterdir()), [])


OPT_TOML = '''schema = 1
[app]
id = "opt"
name = "Opt"
description = "Test."
entry = "main:run"
[inputs.note]
type = "text"
ask = "always"
[inputs.day]
type = "date"
[inputs.count]
type = "number"
[inputs.loud]
type = "bool"
[inputs.at]
type = "datetime"
[outputs.reply]
type = "text"
to = ["show", "speak"]
[actions]
open_url = ["https", "obsidian"]
'''
OPT_MAIN = '''def run(ctx, note, day, count, loud, at):
    if note == "hi":
        print("debug noise")
    out = {"reply": repr((note, day, count, loud, at))}
    if note and ":" in note:
        out["_open_url"] = note
    return out
'''


class ShortcutsTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.add_app("opt", OPT_TOML, OPT_MAIN, 'INPUTS = {"note": "x"}\n')

    def route_reply(self, **reply):
        self.cfg.models["fast"] = {"provider": "mock", "reply": json.dumps(
            {"confidence": 0.9, "alternatives": [], "inputs": {}, **reply})}

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_command_output_is_only_the_response(self):
        (self.cfg.inbox / "r1.json").write_text(json.dumps({"app": "opt", "inputs": {"note": "hi"}}))
        (self.cfg.inbox / "r1").mkdir()
        code, out = self.cli("run", "--request", "r1")
        resp = json.loads(out)  # print() in the app did not get in the way
        self.assertEqual((code, resp["status"], resp["protocol"]), (0, "ok", PROTOCOL))
        self.assertEqual(resp["printed"], "debug noise")
        self.assertEqual(resp["speak"], resp["show"])
        self.assertFalse((self.cfg.inbox / "r1.json").exists())  # finished requests are cleared at once
        self.assertFalse((self.cfg.inbox / "r1").exists())
        self.assertTrue((self.cfg.outbox / "r1.json").exists())

    def test_broken_config_still_answers_in_json(self):
        (self.cfg.home / "config.toml").write_text('[aliases]\nobsidian = "~vault"\n')
        code, out = self.cli("run", "--request", "r1")
        self.assertEqual((code, json.loads(out)["status"]), (0, "error"))
        self.assertIn("a-Shell bookmark", json.loads(out)["error"])
        code, out = self.cli("doctor")
        self.assertEqual(code, 1)
        self.assertIn("XX  config:", out)

    def test_request_ids_cannot_leave_the_inbox(self):
        (self.cfg.home / "evil.json").write_text(json.dumps({"app": "hello", "inputs": {"text": "x"}}))
        code, out = self.cli("run", "--request", "../evil")
        self.assertEqual(json.loads(out)["status"], "error")
        self.assertEqual(runner.handle({"id": "../x", "app": "hello"}, self.cfg)["status"], "error")
        self.assertEqual(runner.handle({"app": "../hello"}, self.cfg)["status"], "error")
        self.assertEqual(sorted(p.name for p in self.cfg.home.glob("*evil*")), ["evil.json"])

    def test_empty_keys_do_not_wipe_what_the_router_found(self):
        sync(self.cfg)
        self.route_reply(app="hello", inputs={"text": "from router"})
        resp = runner.handle({"utterance": "say hello", "inputs": {"text": ""}}, self.cfg)
        self.assertEqual(resp["show"], "Harness heard: from router")

    def test_router_guesses_the_app_cannot_read_are_dropped(self):
        sync(self.cfg)
        self.route_reply(app="remind-me", inputs={"text": "call the bank", "day": "when I get round to it"})
        resp = runner.handle({"utterance": "remind me to call the bank"}, self.cfg)
        self.assertEqual(resp["status"], "ok", resp)
        self.assertEqual(resp["actions"][0]["due"], (dt.date.today() + dt.timedelta(days=1)).isoformat())

    def test_router_sees_today_and_no_identifiers(self):
        sync(self.cfg)
        seen = []

        def spy(cfg, spec, *, prompt, **_):
            seen.append(prompt)
            return json.dumps({"app": "hello", "inputs": {"text": "[[NAME_1]] rang"}, "confidence": 0.9})
        self.addCleanup(ai.PROVIDERS.__setitem__, "mock", ai.PROVIDERS["mock"])
        ai.PROVIDERS["mock"] = spy
        resp = runner.handle({"utterance": "say that Jane Q. Doe rang"}, self.cfg)
        self.assertIn(f"{dt.date.today():%A %Y-%m-%d}", seen[0])
        self.assertNotIn("Jane", seen[0])
        self.assertEqual(resp["show"], "Harness heard: Jane Q. Doe rang")  # restored on the device

    def test_picking_from_the_menu_keeps_what_was_said(self):
        sync(self.cfg)
        self.route_reply(app="hello", inputs={"text": "good morning"}, confidence=0.2)
        resp = runner.handle({"id": "m1", "utterance": "morning"}, self.cfg)
        self.assertEqual((resp["status"], resp["suggested"]), ("choose", "hello"))
        resp = runner.handle({"id": "m1", "app": "hello", "utterance": "morning", "inputs": {"text": ""}}, self.cfg)
        self.assertEqual(resp["show"], "Harness heard: good morning")
        self.assertEqual(list(self.cfg.pending.iterdir()), [])

    def test_optional_always_input_can_be_skipped(self):
        first = runner.handle({"app": "opt"}, self.cfg)
        self.assertEqual([a["name"] for a in first["ask"]], ["note"])
        second = runner.handle({"app": "opt", "inputs": {**first["inputs"], "note": ""}}, self.cfg)
        self.assertEqual(second["status"], "ok", second)

    def test_unreadable_answer_is_asked_again_not_fatal(self):
        resp = runner.handle({"app": "opt", "inputs": {"note": "x", "day": "32nd of Octember", "count": "seven"}}, self.cfg)
        self.assertEqual(resp["status"], "ask")
        self.assertEqual({a["name"]: a["error"] for a in resp["ask"]},
                         {"day": "Not a date: 32nd of Octember", "count": "'count' must be a number"})
        self.assertNotIn("day", resp["inputs"])
        resp = runner.handle({"app": "opt", "inputs": {**resp["inputs"], "day": "Nov 13, 2026 at 9:00\u202fAM",
                                                       "count": "7", "loud": "Yes", "at": "2026-11-13T09:30"}}, self.cfg)
        self.assertEqual(resp["show"], repr(("x", dt.date(2026, 11, 13), 7, True, dt.datetime(2026, 11, 13, 9, 30))))

    def test_dates_as_shortcuts_and_people_write_them(self):
        friday = dt.date(2026, 11, 13)
        for text, expected in [("13 November 2026", friday), ("Fri, Nov 13, 2026", friday),
                               ("next friday", dt.date(2026, 11, 20)), ("monday", dt.date(2026, 11, 16)),
                               ("Nov 13, 2026 at 5:05 PM", dt.datetime(2026, 11, 13, 17, 5))]:
            self.assertEqual(when.parse(text, today=friday), expected)
        for text in ("11/12/26", "soon"):
            with self.assertRaises(ValueError):
                when.parse(text)

    def test_open_url_must_be_declared_and_never_runs_shortcuts(self):
        ok = runner.handle({"app": "opt", "inputs": {"note": "obsidian://open?vault=v"}}, self.cfg)
        self.assertEqual(ok["open_url"], "obsidian://open?vault=v")
        for url in ("shortcuts://run-shortcut?name=Wipe", "tel:5551234567"):
            resp = runner.handle({"app": "opt", "inputs": {"note": url}}, self.cfg)
            self.assertEqual(resp["status"], "error")
            self.assertIn("ActionError", resp["error"])
        quarantine.add(self.cfg, "opt", 1)
        self.assertTrue(runner.handle({"app": "opt", "inputs": {"note": "https://example.com"}}, self.cfg)["open_url_confirm"])
        for bad, message in (('open_url = ["shortcuts"]', "use the shortcut.run action"),
                             ('open_url = ["https"]\n[app2]', "unknown top-level")):
            (self.cfg.apps_dir / "opt" / "app.toml").write_text(OPT_TOML.replace('open_url = ["https", "obsidian"]', bad))
            self.assertIn(message, " ".join(sync(self.cfg)["failed"]["opt"]))
        strict = STRICT_TOML + '[actions]\nopen_url = ["https"]\n'
        self.add_app("ledger", strict, STRICT_MAIN)
        self.assertIn("strict app may not declare [actions].open_url", " ".join(sync(self.cfg)["failed"]["ledger"]))

    def test_ack_and_single_shortcut(self):
        code, out = self.cli("ack", "r1-1", "done", "created")
        self.assertEqual((code, json.loads(out)["status"]), (0, "ok"))
        self.assertEqual(json.loads(self.cfg.actions_log.read_text())["result"], "done")
        code, out = self.cli("ack", "r1-1", "exploded")
        self.assertEqual((code, json.loads(out)["status"]), (0, "error"))
        self.cfg.single_action_shortcut = True
        resp = runner.handle({"app": "remind-me", "inputs": {"text": "x"}}, self.cfg)
        self.assertEqual(resp["actions"][0]["shortcut"], "Harness Act")


if __name__ == "__main__":
    unittest.main()
