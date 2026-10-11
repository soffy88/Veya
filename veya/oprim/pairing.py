"""veya/oprim/pairing — device pairing code atoms (NANO-W6 addendum J).

Code generation/validation lives in obase (crypto boundary); these ops are the
atomic handle-based entry points. Codes are never logged by callers.
"""

from __future__ import annotations

from typing import Any


def _store_of(store: Any) -> Any:
    if store is not None:
        return store
    from veya.obase.pairing import PairingCodeStore

    return PairingCodeStore()


def pairing_code_create(
    device_kind: str,
    *,
    device_label: str = "",
    ttl_s: float = 600.0,
    store: Any = None,
) -> dict[str, Any]:
    """Generate one pairing code. Returns ``{code, expires_at}`` or a refusal.

    The plaintext code is returned once; only its digest is persisted.
    """

    return _store_of(store).generate(
        device_kind=device_kind, device_label=device_label, ttl_s=ttl_s
    )


def pairing_code_consume(code: str, *, store: Any = None) -> dict[str, Any]:
    """Consume one pairing code (constant-time, single use, attempt-capped)."""

    return _store_of(store).consume(code)


__all__ = ["pairing_code_create", "pairing_code_consume"]
