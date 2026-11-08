"""Load and validate app manifests (app.toml, schema v1)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .config import tomllib

SCHEMA_VERSION = 1
INPUT_TYPES = {"file", "text", "dictation", "choice", "date", "url", "json"}
TEXT_TARGETS = {"show", "notification", "clipboard"}
WRITE_MODES = {"create", "append", "replace"}
ASK_MODES = {"never", "if_missing", "always"}
RUN_MODES = {"app", "extension"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ENTRY_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
ALIAS_RE = re.compile(r"^@[A-Za-z0-9_-]+(?:/|$)")


class ManifestError(Exception):
    def __init__(self, app: str, errors: list):
        self.app, self.errors = app, errors
        super().__init__(f"{app}: " + "; ".join(errors))


@dataclass
class Manifest:
    root: Path
    id: str
    name: str
    version: str
    description: str
    entry: str
    tags: list
    dependencies: list
    timeout_s: int
    mode: str
    inputs: dict
    outputs: dict
    ai_uses: list
    permissions: dict
    router: dict
    test: dict

    @property
    def deps_hash(self) -> str:
        blob = "\n".join(sorted(self.dependencies)).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def load(app_dir) -> Manifest:
    app_dir = Path(app_dir)
    path = app_dir / "app.toml"
    if not path.exists():
        raise ManifestError(app_dir.name, ["missing app.toml"])
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(app_dir.name, [f"TOML error: {e}"]) from None

    errors = validate(data, app_dir)
    if errors:
        raise ManifestError(app_dir.name, errors)

    app, rt = data["app"], data.get("runtime", {})
    return Manifest(
        root=app_dir.resolve(),
        id=app["id"],
        name=app["name"],
        version=str(app.get("version", "0.0.0")),
        description=app["description"],
        entry=app["entry"],
        tags=list(app.get("tags", [])),
        dependencies=list(rt.get("dependencies", [])),
        timeout_s=int(rt.get("timeout_s", 120)),
        mode=rt.get("mode", "app"),
        inputs=data.get("inputs", {}),
        outputs=data.get("outputs", {}),
        ai_uses=list(data.get("ai", {}).get("uses", [])),
        permissions=data.get("permissions", {}),
        router=data.get("router", {}),
        test=data.get("test", {}),
    )


def validate(data: dict, app_dir: Path) -> list:
    """Structural checks only. Device-specific checks live in sync.py."""
    e = []
    if data.get("schema") != SCHEMA_VERSION:
        e.append(f"schema must be {SCHEMA_VERSION}")
    app = data.get("app")
    if not isinstance(app, dict):
        return e + ["missing [app] table"]

    for key in ("id", "name", "description", "entry"):
        if not app.get(key):
            e.append(f"[app].{key} is required")
    app_id = app.get("id", "")
    if app_id and not ID_RE.match(app_id):
        e.append("[app].id must be kebab-case")
    if app_id and app_id != app_dir.name:
        e.append(f"[app].id '{app_id}' must match folder name '{app_dir.name}'")

    entry = app.get("entry", "")
    if entry:
        if not ENTRY_RE.match(entry):
            e.append("[app].entry must look like module:function")
        else:
            mod = entry.split(":")[0]
            if not (app_dir / (mod.replace(".", "/") + ".py")).exists():
                e.append(f"entry module {mod}.py not found")

    rt = data.get("runtime", {})
    if rt.get("mode", "app") not in RUN_MODES:
        e.append(f"[runtime].mode must be one of {sorted(RUN_MODES)}")
    if not isinstance(rt.get("dependencies", []), list):
        e.append("[runtime].dependencies must be a list")

    for name, spec in data.get("inputs", {}).items():
        where = f"[inputs.{name}]"
        if not name.isidentifier() or name == "ctx":
            e.append(f"{where} name must be a Python identifier other than ctx")
        t = spec.get("type")
        if t not in INPUT_TYPES:
            e.append(f"{where} type must be one of {sorted(INPUT_TYPES)}")
        if t == "choice" and not spec.get("options"):
            e.append(f"{where} choice needs options")
        if spec.get("ask", "if_missing") not in ASK_MODES:
            e.append(f"{where} ask must be one of {sorted(ASK_MODES)}")

    perms = data.get("permissions", {})
    for ref in perms.get("read", []) + perms.get("write", []):
        if not ALIAS_RE.match(ref):
            e.append(f"permission path must start with an @alias: {ref}")
    writes = [w.rstrip("/") for w in perms.get("write", [])]

    outputs = data.get("outputs", {})
    if not outputs:
        e.append("declare at least one [outputs.*]")
    for name, spec in outputs.items():
        where = f"[outputs.{name}]"
        t = spec.get("type")
        if t == "file":
            p = spec.get("path", "")
            if not ALIAS_RE.match(p):
                e.append(f"{where} path must start with an @alias")
            if spec.get("write", "replace") not in WRITE_MODES:
                e.append(f"{where} write must be one of {sorted(WRITE_MODES)}")
            static = p.split("{", 1)[0].rstrip("/")
            if not any(static == w or static.startswith(w + "/") for w in writes):
                e.append(f"{where} path is outside [permissions].write")
        elif t == "text":
            bad = set(spec.get("to", ["show"])) - TEXT_TARGETS
            if bad:
                e.append(f"{where} unknown targets {sorted(bad)}")
        else:
            e.append(f"{where} type must be file or text")
    return e
