"""The ctx object every app receives: AI, guarded file and network I/O, logging.

The guards catch AI-written code wandering outside its declared folders.
They are guardrails, not a security sandbox.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .ai import AIClient
from .paths import PathError, is_within, resolve
from .redact import Redactor


class PermissionDenied(Exception):
    pass


def write_file(path: Path, text: str, mode: str = "replace") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "create":
        if path.exists():
            raise FileExistsError(f"{path} already exists")
        path.write_text(text, encoding="utf-8")
    elif mode == "append":
        sep = ""
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    sep = "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(sep + text + ("" if text.endswith("\n") else "\n"))
    else:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


class Ctx:
    def __init__(self, manifest, cfg, values: dict, run_id: str, *,
                 dry_run: bool = False, models: dict | None = None, extra_read=(),
                 fetch_mock: dict | None = None):
        self.app = manifest
        self.cfg = cfg
        self.inputs = values
        self.run_id = run_id
        self.dry_run = dry_run
        self.calls: list = []
        self.messages: list = []
        self.writes: list = []
        self.strict = manifest.privacy == "strict"
        self.redactor = Redactor(cfg.identifiers)  # one token map per run
        self.ai = AIClient(cfg, allowed=set(manifest.ai_uses), on_call=self.calls.append, models=models,
                           gate=self.redactor if self.strict else None,
                           audit=cfg.egress if self.strict and not dry_run else None, app_id=manifest.id)
        perms = manifest.permissions
        self._net = [h.lower() for h in perms.get("net", [])]
        self._secrets = set(perms.get("secrets", []))
        self._fetch_mock = fetch_mock or {}
        self._write = [resolve(p, cfg) for p in perms.get("write", [])]
        self._read = [resolve(p, cfg) for p in perms.get("read", [])] + self._write
        self._read += [manifest.root] + [Path(p).resolve() for p in extra_read]

    def path(self, ref: str, **extra) -> Path:
        return resolve(ref, self.cfg, {**self.inputs, **extra})

    def _as_path(self, ref) -> Path:
        if isinstance(ref, Path):
            return ref.resolve()
        if isinstance(ref, str) and ref.startswith("@"):
            return self.path(ref)
        raise PathError("Use an @alias path or a Path from inputs")

    def _check(self, path: Path, roots: list, verb: str) -> None:
        if not any(is_within(path, r) for r in roots):
            raise PermissionDenied(f"{self.app.id} may not {verb} {path}; declare it under [permissions]")

    def read_text(self, ref) -> str:
        p = self._as_path(ref)
        self._check(p, self._read, "read")
        return p.read_text(encoding="utf-8")

    def read_bytes(self, ref) -> bytes:
        p = self._as_path(ref)
        self._check(p, self._read, "read")
        return p.read_bytes()

    def list(self, ref, pattern: str = "*") -> list:
        p = self._as_path(ref)
        self._check(p, self._read, "read")
        return sorted(p.glob(pattern))

    def write_text(self, ref, text: str, mode: str = "replace") -> Path:
        p = self._as_path(ref)
        self._check(p, self._write, "write")
        if self.dry_run:
            self.writes.append({"path": str(p), "mode": mode, "chars": len(text)})
        else:
            write_file(p, text, mode)
        return p

    def log(self, msg) -> None:
        self.messages.append(str(msg))

    def secret(self, name: str) -> str:
        """A key from secrets.toml, if the manifest lists it under [permissions].secrets."""
        if name not in self._secrets:
            raise PermissionDenied(f"{self.app.id} may not read secret {name}; declare it under [permissions]")
        return "mock-secret" if self.dry_run else self.cfg.secret(name)

    def fetch(self, url: str, *, method: str = "GET", headers: dict | None = None,
              data=None, timeout: int = 60) -> dict:
        """HTTPS request to a host listed under [permissions].net.

        Returns {"status", "text", "headers"}; HTTP errors come back as a
        status, not an exception. Pass a dict or list as data to send JSON.
        """
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not any(host == h or host.endswith("." + h) for h in self._net):
            raise PermissionDenied(f"{self.app.id} may not fetch {parts.scheme}://{host}; "
                                   "declare the host under [permissions].net")
        if self.dry_run:  # smoke tests: FETCH = {"<url prefix>": "<body>"} in smoke.py
            body = next((b for prefix, b in self._fetch_mock.items() if url.startswith(prefix)), "{}")
            return {"status": 200, "text": body if isinstance(body, str) else json.dumps(body), "headers": {}}

        headers = dict(headers or {})
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            data = data.encode()
        start = time.monotonic()
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = {"status": r.status, "text": r.read().decode("utf-8", "replace"), "headers": dict(r.headers)}
        except urllib.error.HTTPError as e:
            out = {"status": e.code, "text": e.read().decode("utf-8", "replace"), "headers": dict(e.headers)}
        self.calls.append({"fetch": host, "status": out["status"], "ms": int((time.monotonic() - start) * 1000)})
        return out
