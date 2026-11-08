"""Per-app package folders standing in for virtual environments.

a-Shell runs everything in one process, so real venvs are unreliable.
Each app gets deps/<id>-<hash>/ installed with pip --target, and the
runner puts that folder first on sys.path. A changed dependency list
gets a new hash, a fresh install, and the old folder is removed.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path


class DepsError(Exception):
    pass


def _pip(args: list) -> None:
    try:
        from pip._internal.cli.main import main as pip_main  # in-process: no subprocess on iOS
    except ImportError:
        raise DepsError("pip is not available in this Python") from None
    code = pip_main(args)
    if code:
        raise DepsError(f"pip exited with code {code}")


def ensure(manifest, cfg, force: bool = False) -> Path:
    target = cfg.deps_dir / f"{manifest.id}-{manifest.deps_hash}"
    stamp = target / ".installed"
    if stamp.exists() and not force:
        return target

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    if manifest.dependencies:
        _pip(["install", "--target", str(target), "--upgrade", "--no-input",
              "--disable-pip-version-check", *manifest.dependencies])
    stamp.write_text(datetime.now().isoformat() + "\n" + "\n".join(manifest.dependencies))

    for old in cfg.deps_dir.glob(f"{manifest.id}-*"):
        if old != target and old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
    return target
