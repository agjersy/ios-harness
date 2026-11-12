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

from harness import actions, bridge, config, quarantine, runner  # noqa: E402
from harness.redact import Redactor  # noqa: E402
from harness.sync import sync  # noqa: E402

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
        self.cfg.models["reason"] = {"provider": "mock", "reply": [json.dumps(r) for r in replies]}
        return runner.handle({"id": "f1", "app": "forge", **req}, self.cfg)

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


if __name__ == "__main__":
    unittest.main()
