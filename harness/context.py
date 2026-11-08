"""The ctx object every app receives: AI, guarded file I/O, logging.

The guards catch AI-written code wandering outside its declared folders.
They are guardrails, not a security sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path

from .ai import AIClient
from .paths import PathError, is_within, resolve


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
                 dry_run: bool = False, models: dict | None = None, extra_read=()):
        self.app = manifest
        self.cfg = cfg
        self.inputs = values
        self.run_id = run_id
        self.dry_run = dry_run
        self.calls: list = []
        self.messages: list = []
        self.writes: list = []
        self.ai = AIClient(cfg, allowed=set(manifest.ai_uses), on_call=self.calls.append, models=models)
        perms = manifest.permissions
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
