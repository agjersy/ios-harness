"""Check the device setup: Python, pip, folders, aliases, keys, catalog."""
from __future__ import annotations

import os
import sys

KEY_FOR_PROVIDER = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def doctor(cfg) -> list:
    checks = [("Python", sys.version_info >= (3, 9), sys.version.split()[0])]
    try:
        import pip._internal.cli.main  # noqa: F401
        checks.append(("pip (in-process)", True, "available"))
    except ImportError:
        checks.append(("pip (in-process)", False, "not importable"))

    checks.append(("Apps folder", cfg.apps_dir.is_dir(), str(cfg.apps_dir)))
    for alias, path in sorted(cfg.aliases.items()):
        ok = path.is_dir() and os.access(path, os.W_OK)
        checks.append((f"@{alias}", ok, str(path) if ok else f"{path} missing or not writable"))

    for provider in sorted({s.get("provider") for s in cfg.models.values()} - {"mock"}):
        key = KEY_FOR_PROVIDER.get(provider)
        try:
            ok = bool(key) and bool(cfg.secret(key))
        except KeyError:
            ok = False
        checks.append((f"{provider} key", ok, key or "unknown provider"))

    checks.append(("Catalog", cfg.catalog_path.exists(), "run sync" if not cfg.catalog_path.exists() else "present"))
    return checks
