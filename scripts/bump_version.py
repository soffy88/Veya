#!/usr/bin/env python3
"""G15 — bump Veya version across pyproject + runtime version strings.

Usage:
    python scripts/bump_version.py patch|minor|major [--dry-run]

Edits:
- pyproject.toml  [project] version
- cli/main.py     argparse --version string
- cli/simple_cli.py argparse --version string
- server/app.py   FastAPI version + /health payload

Versions are parsed from the pyproject section as the single source of truth.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

VERSION_FILES = [
    "cli/main.py",
    "cli/simple_cli.py",
    "server/app.py",
]


def read_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text, re.M)
    if not m:
        raise SystemExit("cannot find version in pyproject.toml")
    return ".".join(m.groups())


def bump(current: str, part: str) -> str:
    major, minor, patch = (int(x) for x in current.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"unknown part: {part} (use patch|minor|major)")


def apply(new_version: str) -> None:
    old_version = read_version()

    # pyproject (single source of truth)
    p = ROOT / "pyproject.toml"
    s = p.read_text(encoding="utf-8")
    s = re.sub(r'^version\s*=\s*"[\d.]+"', f'version = "{new_version}"', s, count=1, flags=re.M)
    p.write_text(s, encoding="utf-8")

    # runtime version strings (match against the pre-bump version)
    for rel in VERSION_FILES:
        f = ROOT / rel
        s = f.read_text(encoding="utf-8")
        s = s.replace(f"veya {old_version}", f"veya {new_version}")
        s = s.replace(f'version="{old_version}"', f'version="{new_version}"')
        s = s.replace(f'"version": "{old_version}"', f'"version": "{new_version}"')
        f.write_text(s, encoding="utf-8")


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    if not args:
        print(__doc__)
        return 1
    part = args[0]
    dry = "--dry-run" in sys.argv
    current = read_version()
    new_version = bump(current, part)
    if dry:
        print(f"[dry-run] {current} -> {new_version}")
        return 0
    apply(new_version)
    print(f"bumped {current} -> {new_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
