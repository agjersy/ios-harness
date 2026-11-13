"""Load and validate app manifests (app.toml, schema v1)."""
from __future__ import annotations

import hashlib
import keyword
import re
import string
from dataclasses import dataclass
from pathlib import Path

from .actions import ACTIONS, URL_ALWAYS_REFUSED
from .config import tomllib

SCHEMA_VERSION = 1
INPUT_TYPES = {"file", "text", "dictation", "choice", "date", "datetime", "number", "bool", "url", "json"}
TEXT_TARGETS = {"show", "notification", "clipboard", "speak"}
WRITE_MODES = {"create", "append", "replace"}
ASK_MODES = {"never", "if_missing", "always"}
RUN_MODES = {"app", "extension"}
PRIVACY_MODES = {"standard", "strict"}
RESERVED_IDS = {"forge", "repair"}  # built into the harness
TABLES = {"schema", "app", "runtime", "inputs", "outputs", "ai", "permissions", "actions", "router", "test"}
SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*$")
PROVIDER_KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}  # only ctx.ai may use these
HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
SECRET_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
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
    privacy: str = "standard"
    actions: tuple = ()
    open_url: tuple = ()  # URL schemes _open_url may use

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

    try:
        errors = validate(data, app_dir)
    except Exception as e:  # a shape validate() didn't foresee is still a manifest error
        errors = [f"malformed manifest ({type(e).__name__}: {e})"]
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
        privacy=app.get("privacy", "standard"),
        actions=tuple(data.get("actions", {}).get("allow", [])),
        open_url=tuple(data.get("actions", {}).get("open_url", [])),
    )


def _table(data: dict, key: str, e: list, where: str | None = None) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        e.append(f"{where or '[' + key + ']'} must be a table")
        return {}
    return value


def _strings(table: dict, key: str, e: list, where: str) -> list:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        e.append(f"{where}.{key} must be a list of strings")
        return []
    return value


def validate(data: dict, app_dir: Path) -> list:
    """Structural checks only. Device-specific checks live in sync.py."""
    e = []
    if data.get("schema") != SCHEMA_VERSION:
        e.append(f"schema must be {SCHEMA_VERSION}")
    unknown = sorted(set(data) - TABLES)
    if unknown:
        e.append(f"unknown top-level keys {unknown}; known: {', '.join(sorted(TABLES))}")
    app = data.get("app")
    if not isinstance(app, dict):
        return e + ["missing [app] table"]

    for key in ("id", "name", "description", "entry"):
        if not isinstance(app.get(key), str) or not app[key].strip():
            e.append(f"[app].{key} is required and must be text")
    app_id = app.get("id") if isinstance(app.get("id"), str) else ""
    if app_id and not ID_RE.match(app_id):
        e.append("[app].id must be kebab-case")
    if app_id and app_id != app_dir.name:
        e.append(f"[app].id '{app_id}' must match folder name '{app_dir.name}'")
    if app_id in RESERVED_IDS:
        e.append(f"[app].id '{app_id}' is reserved")
    for key in ("name", "description"):
        if isinstance(app.get(key), str) and ("\n" in app[key] or "\r" in app[key]):
            e.append(f"[app].{key} must be a single line")
    strict = app.get("privacy", "standard") == "strict"
    if app.get("privacy", "standard") not in PRIVACY_MODES:
        e.append(f"[app].privacy must be one of {sorted(PRIVACY_MODES)}")
    _strings(app, "tags", e, "[app]")

    entry = app.get("entry") if isinstance(app.get("entry"), str) else ""
    if entry:
        if not ENTRY_RE.match(entry):
            e.append("[app].entry must look like module:function")
        else:
            mod = entry.split(":")[0]
            if not (app_dir / (mod.replace(".", "/") + ".py")).exists():
                e.append(f"entry module {mod}.py not found")

    rt = _table(data, "runtime", e)
    if rt.get("mode", "app") not in RUN_MODES:
        e.append(f"[runtime].mode must be one of {sorted(RUN_MODES)}")
    _strings(rt, "dependencies", e, "[runtime]")
    timeout = rt.get("timeout_s", 120)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        e.append("[runtime].timeout_s must be a whole number of seconds")

    inputs = _table(data, "inputs", e)
    for name, spec in inputs.items():
        where = f"[inputs.{name}]"
        if not isinstance(spec, dict):
            e.append(f"{where} must be a table with a type, like [inputs.{name}] type = \"text\"")
            continue
        if not name.isidentifier() or keyword.iskeyword(name) or name == "ctx" or name.startswith("_"):
            e.append(f"{where} name must be a Python identifier, not a keyword, not ctx, not starting with _")
        t = spec.get("type")
        if t not in INPUT_TYPES:
            e.append(f"{where} type must be one of {sorted(INPUT_TYPES)}")
        if t == "choice":
            options = spec.get("options")
            if not isinstance(options, list) or not options:
                e.append(f"{where} choice needs options = [...]")
            elif "default" in spec and spec["default"] not in options:
                e.append(f"{where} default must be one of its options")
        if "accept" in spec:
            _strings(spec, "accept", e, where)
        if spec.get("ask", "if_missing") not in ASK_MODES:
            e.append(f"{where} ask must be one of {sorted(ASK_MODES)}")
        if not isinstance(spec.get("required", False), bool):
            e.append(f"{where} required must be true or false")
        if not isinstance(spec.get("prompt", ""), str):
            e.append(f"{where} prompt must be text")

    perms = _table(data, "permissions", e)
    for ref in _strings(perms, "read", e, "[permissions]") + _strings(perms, "write", e, "[permissions]"):
        if not ALIAS_RE.match(ref):
            e.append(f"permission path must start with an @alias: {ref}")
    writes = [w.rstrip("/") for w in perms.get("write", []) if isinstance(w, str)] \
        if isinstance(perms.get("write", []), list) else []
    net = perms.get("net", [])
    if not isinstance(net, list):
        e.append("[permissions].net must be a list")
        net = []
    for host in net:
        if not isinstance(host, str) or not HOST_RE.match(host):
            e.append(f"[permissions].net entries are bare host names like api.github.com: {host}")
    if strict and net:
        e.append("a strict app may not declare [permissions].net; its only way out is ctx.ai")
    secrets = perms.get("secrets", [])
    if not isinstance(secrets, list):
        e.append("[permissions].secrets must be a list")
        secrets = []
    for name in secrets:
        if not isinstance(name, str) or not SECRET_RE.match(name):
            e.append(f"[permissions].secrets entries are names like GITHUB_TOKEN: {name}")
        elif name in PROVIDER_KEYS:
            e.append(f"[permissions].secrets may not include {name}; use ctx.ai")

    _strings(_table(data, "ai", e), "uses", e, "[ai]")
    _strings(_table(data, "router", e), "examples", e, "[router]")
    _table(data, "test", e)

    acts = _table(data, "actions", e)
    for kind in _strings(acts, "allow", e, "[actions]"):
        if kind not in ACTIONS:
            e.append(f"[actions].allow has unknown type '{kind}'; known: {', '.join(sorted(ACTIONS))}")
    schemes = _strings(acts, "open_url", e, "[actions]")
    for scheme in schemes:
        if not SCHEME_RE.match(scheme):
            e.append(f"[actions].open_url entries are URL schemes like https or obsidian: {scheme}")
        elif scheme in URL_ALWAYS_REFUSED:
            e.append(f"[actions].open_url may not include '{scheme}'"
                     + ("; use the shortcut.run action" if scheme == "shortcuts" else ""))
    if strict and schemes:
        e.append("a strict app may not declare [actions].open_url; opening a URL can carry data out")

    outputs = _table(data, "outputs", e)
    if not outputs:
        e.append("declare at least one [outputs.*]")
    for name, spec in outputs.items():
        where = f"[outputs.{name}]"
        if not isinstance(spec, dict):
            e.append(f"{where} must be a table with a type")
            continue
        t = spec.get("type")
        if t == "file":
            p = spec.get("path", "")
            if not isinstance(p, str) or not ALIAS_RE.match(p):
                e.append(f"{where} path must start with an @alias")
                continue
            if spec.get("write", "replace") not in WRITE_MODES:
                e.append(f"{where} write must be one of {sorted(WRITE_MODES)}")
            static = p.split("{", 1)[0].rstrip("/")
            if not any(static == w or static.startswith(w + "/") for w in writes):
                e.append(f"{where} path is outside [permissions].write")
            try:
                fields = {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(p) if f}
            except ValueError:
                e.append(f"{where} path has unbalanced braces")
                fields = set()
            missing = sorted(fields - set(inputs))
            if missing:
                e.append(f"{where} path uses {missing}, which are not declared inputs")
        elif t == "text":
            to = spec.get("to", ["show"])
            if not isinstance(to, list) or not all(isinstance(x, str) for x in to):
                e.append(f"{where} to must be a list such as [\"show\"]")
            elif set(to) - TEXT_TARGETS:
                e.append(f"{where} unknown targets {sorted(set(to) - TEXT_TARGETS)}")
        else:
            e.append(f"{where} type must be file or text")
    return e
