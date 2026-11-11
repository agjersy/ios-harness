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
    for sub in ("inbox", "outbox", "deps", "repairs", "errors"):
        (home / sub).mkdir(exist_ok=True)

    cfg_file, sec_file = home / "config.toml", home / "secrets.toml"
    if not cfg_file.exists():
        cfg_file.write_text(DEFAULT_CONFIG, encoding="utf-8")
    if not sec_file.exists():
        sec_file.write_text(DEFAULT_SECRETS, encoding="utf-8")

    data = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
    secrets = {k: str(v) for k, v in tomllib.loads(sec_file.read_text(encoding="utf-8")).items()}
    aliases = {k: _expand(v) for k, v in data.get("aliases", {}).items()}
    aliases.setdefault("inbox", home / "inbox")
    router = data.get("router", {})

    return Config(
        home=home,
        repo=Path(os.environ.get("HARNESS_REPO", REPO)),
        aliases=aliases,
        models=data.get("models", {}),
        allowlist={norm_pkg(p) for p in data.get("packages", {}).get("allow", [])},
        secrets=secrets,
        router_capability=router.get("capability", "fast"),
        router_threshold=float(router.get("threshold", 0.6)),
        repair_capability=data.get("repair", {}).get("capability", "reason"),
    )
