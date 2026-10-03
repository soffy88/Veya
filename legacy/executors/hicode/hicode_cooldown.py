"""Retired: Hicode model-identity authority.

The generic upstream quota-cooldown primitives this module used to own now live
in :mod:`veya.provider_cooldown` (extracted verbatim), because
``runtime/provider_reliability.py`` depends on them and must not import from a
retired executor. Only the Hicode-specific model mapping remains here, and
nothing in the product calls it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from veya.provider_cooldown import (
    FAILURE_CLASS,
    ModelCooldown,
    UpstreamQuotaFailure,
    classify_upstream_failure,
    cooldown_snapshot,
    effective_cooldown_until,
    record_cooldown,
)

_AUTHORITY = Path(__file__).resolve().parents[3] / "config" / "hicode_model_mapping.json"

__all__ = [
    "FAILURE_CLASS",
    "ModelCooldown",
    "UpstreamQuotaFailure",
    "classify_upstream_failure",
    "cooldown_snapshot",
    "effective_cooldown_until",
    "hicode_model_mapping",
    "load_hicode_mapping_authority",
    "record_cooldown",
]


def load_hicode_mapping_authority() -> dict[str, dict[str, Any]]:
    raw = json.loads(_AUTHORITY.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or len(raw) != 1:
        raise RuntimeError("HICODE mapping authority must contain exactly one mapping")
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict) or value.get("requested_model") != key:
            raise RuntimeError("HICODE mapping authority has an invalid requested_model")
        required = ("internal_provider", "upstream_model", "source", "active", "updated_at")
        if any(name not in value for name in required):
            raise RuntimeError("HICODE mapping authority is incomplete")
        out[key] = dict(value)
    return out


def hicode_model_mapping(requested_model: str) -> dict[str, Any]:
    mapping = load_hicode_mapping_authority().get(requested_model)
    if not mapping or not mapping.get("active"):
        raise RuntimeError(f"inactive or unknown HICODE model: {requested_model}")
    return mapping
