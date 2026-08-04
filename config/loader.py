from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_DEFAULTS: dict[str, Any] = {
    "providers": {},
    "lsp": {},
    "llm": {"provider": "dashscope", "model": None},
    "max_turns": 50,
    "persona": "build",
}


def _load_dotenv(root: Path | None = None) -> None:
    """Load .env and .hicode.env from project root into os.environ.

    Priority (first-seen wins, os.environ already set is never overwritten):
      1. existing os.environ  — highest priority (shell export wins)
      2. .env                 — standard dotenv location (python-dotenv aware)
      3. .hicode.env          — legacy hicode-specific file

    Note: we use our own parser, NOT obase.config.Settings, so DASHSCOPE_API_KEY
    in .env is safe — pydantic-settings is never invoked here.
    """
    cwd = root or Path.cwd()
    for filename in (".env", ".hicode.env"):
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
    """Load config: .env → defaults → file (if given) → env overrides."""
    _load_dotenv()
    config: dict[str, Any] = dict(_DEFAULTS)

    if path:
        p = Path(path)
        if p.exists():
            with p.open() as f:
                file_cfg = json.load(f)
            config = _deep_merge(config, file_cfg)
    else:
        for candidate in _default_paths():
            if candidate.exists():
                with candidate.open() as f:
                    file_cfg = json.load(f)
                config = _deep_merge(config, file_cfg)
                break

    if api_key := os.environ.get("ANTHROPIC_API_KEY"):
        config.setdefault("providers", {})["anthropic"] = {"api_key": api_key}
    if api_key := os.environ.get("OPENAI_API_KEY"):
        config.setdefault("providers", {})["openai"] = {"api_key": api_key}
    if api_key := os.environ.get("DASHSCOPE_API_KEY"):
        config.setdefault("providers", {})["dashscope"] = {"api_key": api_key}

    if env_provider := os.environ.get("HICODE_LLM_PROVIDER"):
        config.setdefault("llm", {})["provider"] = env_provider
    if env_model := os.environ.get("HICODE_LLM_MODEL"):
        config.setdefault("llm", {})["model"] = env_model

    return config


def _default_paths() -> list[Path]:
    candidates = []
    project = Path.cwd() / ".hicode.json"
    candidates.append(project)
    home = Path.home() / ".hicode" / "config.json"
    candidates.append(home)
    return candidates


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result
