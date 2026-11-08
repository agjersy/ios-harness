"""Resolve @alias paths and keep them inside their alias folder."""
from __future__ import annotations

import re
from pathlib import Path

ALIAS_REF = re.compile(r"^@([A-Za-z0-9_-]+)(?:/(.*))?$")


class PathError(Exception):
    pass


def is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def resolve(ref: str, cfg, values: dict | None = None) -> Path:
    """'@obsidian/Daily/{day:%Y-%m-%d}.md' -> absolute Path."""
    m = ALIAS_REF.match(ref)
    if not m:
        raise PathError(f"Paths must start with an @alias: {ref}")
    alias, rest = m.group(1), m.group(2) or ""
    if alias not in cfg.aliases:
        raise PathError(f"Unknown alias @{alias}; add it to config.toml")
    base = cfg.aliases[alias].resolve()
    if values:
        try:
            rest = rest.format_map(values)
        except (KeyError, ValueError) as e:
            raise PathError(f"Cannot fill {ref}: {e}") from None
    path = (base / rest).resolve()
    if not is_within(path, base):
        raise PathError(f"{ref} escapes @{alias}")
    return path
