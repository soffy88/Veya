"""Jev provider policy: OpenCode Zen, free-first, paid strictly opt-in (spec §15).

Provider = opencode-zen. The protocol is OpenCode Zen's ``state + questions``
fan-out on ``/zen/v1/systemone`` — never the OpenAI chat/completions shape.
"""

# 3O-IO-ALLOW: reads the operator's OpenCode credential file as the last-resort
# key source for the Jev decision provider (credential loading, not business I/O).

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROVIDER = "opencode-zen"
ENDPOINT = "https://opencode.ai/zen/v1/systemone"
MODELS_ENDPOINT = "https://opencode.ai/zen/v1/models"
PRIMARY_MODEL = "jev-1.13-free"
PAID_MODEL = "jev-1.13"
KEY_ENVS: tuple[str, ...] = ("OPENCODE_ZEN_API_KEY", "OPENCODE_API_KEY")
AUTH_JSON_ENV = "OPENCODE_AUTH_JSON"
DEFAULT_AUTH_JSON = "~/.local/share/opencode/auth.json"
PAID_FALLBACK_ENV = "JEV_ALLOW_PAID_FALLBACK"


@dataclass
class JevPolicy:
    enabled: bool = True
    endpoint: str = ENDPOINT
    models_endpoint: str = MODELS_ENDPOINT
    primary_model: str = PRIMARY_MODEL
    paid_model: str = PAID_MODEL
    allow_paid_fallback: bool = False
    timeout_s: float = 30.0
    low_confidence_threshold: float = 0.5
    max_calls: int | None = None
    calls: int = field(default=0)

    def models(self) -> list[str]:
        chain = [self.primary_model]
        if self.allow_paid_fallback:
            chain.append(self.paid_model)
        return chain

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": PROVIDER,
            "endpoint": self.endpoint,
            "primary_model": self.primary_model,
            "paid_model": self.paid_model,
            "allow_paid_fallback": self.allow_paid_fallback,
            "low_confidence_threshold": self.low_confidence_threshold,
            "max_calls": self.max_calls,
        }


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def policy_from_env(environ: dict[str, str] | None = None) -> JevPolicy:
    env = environ if environ is not None else os.environ
    return JevPolicy(
        enabled=not _truthy(env.get("JEV_DISABLED")),
        allow_paid_fallback=_truthy(env.get(PAID_FALLBACK_ENV)),
        timeout_s=float(env.get("JEV_TIMEOUT_S", "30")),
        low_confidence_threshold=float(env.get("JEV_LOW_CONFIDENCE", "0.5")),
    )


def _key_from_auth_json(path: Path) -> str | None:
    """Extract an OpenCode key without ever printing it."""

    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for key in ("key", "api_key", "token", "access_token"):
                    inner = value.get(key)
                    if isinstance(inner, str) and inner.strip():
                        return inner.strip()
    return None


def resolve_api_key(environ: dict[str, str] | None = None) -> tuple[str | None, str | None]:
    """Resolve the Zen key: env order first, then the OpenCode credential file.

    Returns ``(key, source)`` where source is an env name or "auth.json".
    """

    env = environ if environ is not None else os.environ
    for name in KEY_ENVS:
        value = (env.get(name) or "").strip()
        if value:
            return value, name
    path = Path(env.get(AUTH_JSON_ENV, DEFAULT_AUTH_JSON)).expanduser()
    key = _key_from_auth_json(path)
    return (key, "auth.json") if key else (None, None)


__all__ = [
    "AUTH_JSON_ENV",
    "DEFAULT_AUTH_JSON",
    "ENDPOINT",
    "KEY_ENVS",
    "MODELS_ENDPOINT",
    "PAID_FALLBACK_ENV",
    "PAID_MODEL",
    "PRIMARY_MODEL",
    "PROVIDER",
    "JevPolicy",
    "policy_from_env",
    "resolve_api_key",
]
