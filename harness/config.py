"""Device config: folder aliases, model mapping, package allowlist, secrets.

Lives in HARNESS_HOME (default ~/Documents/.harness), outside the repo,
so each device keeps its own paths and keys.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: pip install tomli
    import tomli as tomllib

REPO = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = '''\
# Harness device config. One per device; never committed.

[aliases]
# @name -> folder. For folders outside a-Shell (Obsidian vault, iWriter),
# run pickFolder, cd into it, then run:
#   python -c "import os; print(os.getcwd())"
# and paste the printed path here (pwd shows ~name, which won't work).
obsidian = "~/Documents/vault"
iwriter = "~/Documents/iwriter"

# Capabilities apps ask for, mapped to real models. Change models here only.
[models.reason]
provider = "anthropic"
model = "claude-sonnet-5-5"

[models.fast]
provider = "anthropic"
model = "claude-haiku-5-5"

[models.transcribe]
provider = "openai"
model = "whisper-1"

[router]
capability = "fast"
threshold = 0.6

[repair]
capability = "reason"

[forge]
# Forge writes new apps from a spoken request. New apps start in quarantine:
# for their first runs every action asks before it happens.
capability = "reason"
rounds = 2
quarantine_runs = 3

[actions]
# Shortcuts that apps may start with the shortcut.run action.
shortcuts = []
# true: every action names the one Shortcut "Harness Act", which branches on
# the action's type. Use it if Run Shortcut can't take a name from a variable.
single_shortcut = false

[packages]
# Top-level packages apps may declare. Pure-Python only.
allow = [
  "python-frontmatter", "pyyaml", "markdown", "python-dateutil",
  "beautifulsoup4", "requests", "feedparser", "tomli",
]
'''

DEFAULT_SECRETS = '''\
# API keys. Stays on this device. Environment variables override these.
ANTHROPIC_API_KEY = ""
OPENAI_API_KEY = ""

# Your own details. Apps marked privacy = "strict" replace these (and
# pattern matches such as card numbers and emails) with tokens before any
# text is sent to a model, and refuse to send if one slips through.
# Spacing, case and punctuation are ignored when matching.
[identifiers]
# name = ["Jane Q. Doe", "Doe"]
# address = ["12 Elm Street"]
# account = ["000123456789"]
'''


def norm_pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pkg_name(spec: str) -> str:
    """'requests==2.32.*' -> 'requests'"""
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return norm_pkg(m.group(1)) if m else ""


@dataclass
class Config:
    home: Path
    repo: Path
    aliases: dict
    models: dict
    allowlist: set
    secrets: dict
    identifiers: dict
    action_shortcuts: set
    single_action_shortcut: bool = False
    forge_capability: str = "reason"
    forge_rounds: int = 2
    quarantine_runs: int = 3
    router_capability: str = "fast"
    router_threshold: float = 0.6
    repair_capability: str = "reason"

    @property
    def apps_dir(self) -> Path:
        return self.repo / "apps"

    @property
    def inbox(self) -> Path:
        return self.home / "inbox"

    @property
    def outbox(self) -> Path:
        return self.home / "outbox"

    @property
    def deps_dir(self) -> Path:
        return self.home / "deps"

    @property
    def repairs(self) -> Path:
        return self.home / "repairs"

    @property
    def errors(self) -> Path:
        return self.home / "errors"

    @property
    def egress(self) -> Path:
        return self.home / "egress"

    @property
    def forge(self) -> Path:
        return self.home / "forge"

    @property
    def pending(self) -> Path:
        """Router guesses kept between a `choose` reply and the pick."""
        return self.home / "pending"

    @property
    def actions_log(self) -> Path:
        return self.home / "actions.jsonl"

    @property
    def quarantine_path(self) -> Path:
        return self.home / "quarantine.json"

    @property
    def catalog_path(self) -> Path:
        return self.home / "catalog.json"

    @property
    def runs_log(self) -> Path:
        return self.home / "runs.jsonl"

    def secret(self, name: str) -> str:
        value = os.environ.get(name) or self.secrets.get(name, "")
        if not value:
            raise KeyError(f"Missing secret {name}: add it to {self.home / 'secrets.toml'}")
        return value


def _expand(value) -> Path:
    """Expand ~ and ~/ only. a-Shell shows bookmarks as ~name, which Python can't resolve."""
    value = str(value)
    if value == "~" or value.startswith("~/"):
        return Path(os.path.expanduser(value))
    if value.startswith("~"):
        raise ValueError(
            f"Path {value!r} is an a-Shell bookmark name. Run "
            "python -c \"import os; print(os.getcwd())\" inside that folder "
            "and use the printed path instead.")
    return Path(value)


def _default_home() -> Path:
    """~/Documents/.harness, or beside the repo when HOME is unavailable."""
    expanded = os.path.expanduser("~")
    if expanded and expanded != "~":
        return Path(expanded) / "Documents" / ".harness"
    return REPO.parent / ".harness"


def load(home: Path | None = None) -> Config:
    home = _expand(home or os.environ.get("HARNESS_HOME") or _default_home())
    home.mkdir(parents=True, exist_ok=True)
    for sub in ("inbox", "outbox", "deps", "repairs", "errors", "egress", "forge", "pending"):
        (home / sub).mkdir(exist_ok=True)

    cfg_file, sec_file = home / "config.toml", home / "secrets.toml"
    if not cfg_file.exists():
        cfg_file.write_text(DEFAULT_CONFIG, encoding="utf-8")
    if not sec_file.exists():
        sec_file.write_text(DEFAULT_SECRETS, encoding="utf-8")

    data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    sec_data = tomllib.loads(sec_file.read_text(encoding="utf-8"))
    secrets = {k: str(v) for k, v in sec_data.items() if not isinstance(v, (dict, list))}
    identifiers = {label: [values] if isinstance(values, str) else [str(v) for v in values]
                   for label, values in sec_data.get("identifiers", {}).items()}
    aliases = {k: _expand(v) for k, v in data.get("aliases", {}).items()}
    aliases.setdefault("inbox", home / "inbox")
    router = data.get("router", {})
    forge = data.get("forge", {})

    return Config(
        home=home,
        repo=Path(os.environ.get("HARNESS_REPO", REPO)),
        aliases=aliases,
        models=data.get("models", {}),
        allowlist={norm_pkg(p) for p in data.get("packages", {}).get("allow", [])},
        secrets=secrets,
        identifiers=identifiers,
        action_shortcuts=set(data.get("actions", {}).get("shortcuts", [])),
        single_action_shortcut=bool(data.get("actions", {}).get("single_shortcut", False)),
        forge_capability=forge.get("capability", "reason"),
        forge_rounds=int(forge.get("rounds", 2)),
        quarantine_runs=int(forge.get("quarantine_runs", 3)),
        router_capability=router.get("capability", "fast"),
        router_threshold=float(router.get("threshold", 0.6)),
        repair_capability=data.get("repair", {}).get("capability", "reason"),
    )
