from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from config.authority import (  # noqa: F401 — re-exported canonical chain
    PRECEDENCE,
    deep_merge,
    default_paths,
    read_file_config,
)

_DEFAULTS: dict[str, Any] = {
    "providers": {},
    "lsp": {},
    "llm": {"provider": "dashscope", "model": None},
    "max_turns": 50,
    "persona": "build",
}


def _load_dotenv(root: Path | None = None) -> None:
    """Load .env and .veya.env from project root into os.environ.

    Priority (first-seen wins, os.environ already set is never overwritten):
      1. existing os.environ  — highest priority (shell export wins)
      2. .env                 — standard dotenv location (python-dotenv aware)
      3. .veya.env          — legacy veya-specific file

    Note: we use our own parser, NOT obase.config.Settings, so DASHSCOPE_API_KEY
    in .env is safe — pydantic-settings is never invoked here.
    """
    cwd = root or Path.cwd()
    for filename in (".env", ".veya.env"):
        env_file = cwd / filename
        if not env_file.exists():
            continue
        try:
            # Use python-dotenv when available for full .env syntax support
            from dotenv import dotenv_values

            for key, value in dotenv_values(env_file).items():
                if key and key not in os.environ and value is not None:
                    os.environ[key] = value
        except ImportError:
            # Fallback: simple line-by-line parser
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value


def load_config(path: str | None = None) -> dict[str, Any]:
    """Load config: .env → defaults → file (if given) → env overrides.

    The effective dict is built by :func:`config.adapters.effective_config`
    from :mod:`config.authority` resolutions — runtime > environment > file
    > default — the ONE canonical chain.  Unregistered keys keep the legacy
    deep-merge behaviour byte-identical.
    """
    from config.adapters import effective_config  # local import: adapters must not import loader

    _load_dotenv()
    file_cfg, _used = read_file_config(path)
    return effective_config(_DEFAULTS, file_cfg, os.environ)


def _default_paths() -> list[Path]:
    # Single implementation lives in config.authority; kept as a shim for
    # existing importers.
    return default_paths()


def _deep_merge(base: dict, override: dict) -> dict:
    # Single implementation lives in config.authority; kept as a shim for
    # existing importers.
    return deep_merge(base, override)
