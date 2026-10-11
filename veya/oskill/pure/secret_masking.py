"""3O-PURE — secret_masking: pure secret masking and env-name derivation.

Pure functions for NANO-W10 Addendum Q.
Replaces exact secret values with [secret:<NAME>], derives runner env names,
and recursively redacts payload structures without any store or IO access.
"""

from __future__ import annotations

import re
from typing import Any

_IDENT_CLEAN = re.compile(r"[^A-Za-z0-9_]")


def secret_env_name(name: str) -> str:
    """Derive runner shell env variable name for a bot secret.

    Example:
    'github-token' -> 'VEYA_SECRET_GITHUB_TOKEN'
    'OPENAI_KEY'   -> 'VEYA_SECRET_OPENAI_KEY'
    """
    clean = _IDENT_CLEAN.sub("_", (name or "").strip()).upper()
    while "__" in clean:
        clean = clean.replace("__", "_")
    clean = clean.strip("_")
    return f"VEYA_SECRET_{clean}" if clean else "VEYA_SECRET"


def mask_secret_values(text: str, secret_map: dict[str, str]) -> str:
    """Replace every exact copy of a secret value with [secret:<NAME>].

    Longer secret values are replaced first to prevent partial overlaps.
    Single-pass regex substitution prevents re-matching inside inserted placeholders.
    Empty secret values and values shorter than 3 characters are skipped to
    prevent accidental over-masking of common short tokens.
    """
    if not text or not secret_map:
        return text

    items = []
    for s_name, s_val in secret_map.items():
        val = str(s_val or "")
        if len(val) >= 3:
            items.append((s_name, val))

    if not items:
        return text

    # Sort items by value length descending
    items.sort(key=lambda pair: len(pair[1]), reverse=True)
    val_to_name: dict[str, str] = {}
    for name, val in items:
        if val not in val_to_name:
            val_to_name[val] = name

    pattern = re.compile("|".join(re.escape(val) for val in val_to_name))
    return pattern.sub(lambda m: f"[secret:{val_to_name[m.group(0)]}]", str(text))


def redact_secrets_payload(payload: Any, secret_map: dict[str, str]) -> Any:
    """Recursively mask secrets across dictionaries, lists, and strings."""
    if not secret_map:
        return payload

    if isinstance(payload, str):
        return mask_secret_values(payload, secret_map)

    if isinstance(payload, dict):
        return {key: redact_secrets_payload(value, secret_map) for key, value in payload.items()}

    if isinstance(payload, list):
        return [redact_secrets_payload(item, secret_map) for item in payload]

    if isinstance(payload, tuple):
        return tuple(redact_secrets_payload(item, secret_map) for item in payload)

    return payload


__all__ = [
    "mask_secret_values",
    "redact_secrets_payload",
    "secret_env_name",
]
