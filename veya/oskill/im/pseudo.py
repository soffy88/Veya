"""veya.im.pseudo — pseudo-anonymizer (SPEC §5.5.1).

Stable, non-reversible reference tokens for user identifiers.  A given raw
``user_id`` always maps to the same pseudonym (stable), the token contains no
PII (non-PII), and the mapping is one-way (HMAC-SHA256 with a project secret),
so decision trails and memory stores never hold personal data.

Resolution order:
1. ``obase.pseudo_anonymizer`` — the 3O main-library element, when mounted.
2. Layer-4 ``PseudoAnonymizer`` (this module) — HMAC-SHA256 based.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable
from typing import Any, cast

# Development/test-only fallback.  Production startup rejects its use and
# requires VEYA_PSEUDO_SECRET to be supplied by the deployment secret layer.
_DEV_FALLBACK_SECRET = "veya-layer4-pseudo-anonymizer"


def _production_mode() -> bool:
    return os.environ.get("VEYA_EXECUTION_PRODUCTION", "").strip().lower() not in {
        "",
        "0",
        "false",
        "off",
        "no",
    }


def _resolve_obase_anonymizer() -> Any | None:
    """Return the obase pseudo_anonymizer element if mounted, else None."""
    try:
        from veya.platform import load

        mod = load("obase")
        return getattr(mod, "pseudo_anonymizer", None)
    except Exception:
        return None


class PseudoAnonymizer:
    """HMAC-SHA256 based stable pseudo-anonymizer (SPEC §5.5.1).

    >>> anon = PseudoAnonymizer(secret="test-secret")
    >>> a1 = anon.anonymize("student-123")
    >>> a2 = anon.anonymize("student-123")
    >>> a1 == a2
    True
    >>> "student" in a1  # no PII leaked
    False
    """

    _PREFIX = "u_"

    def __init__(self, secret: str | None = None) -> None:
        resolved = (secret or os.environ.get("VEYA_PSEUDO_SECRET", "")).strip()
        if not resolved and _production_mode():
            raise RuntimeError("VEYA_PSEUDO_SECRET is required in production")
        self._secret = (resolved or _DEV_FALLBACK_SECRET).encode("utf-8")

    def anonymize(self, user_id: str) -> str:
        """Return a stable non-PII reference token for *user_id*."""
        raw = user_id.encode("utf-8")
        digest = hmac.new(self._secret, raw, hashlib.sha256).hexdigest()[:24]
        return f"{self._PREFIX}{digest}"

    def __call__(self, user_id: str) -> str:
        return self.anonymize(user_id)


class _ObasedAnonymizer:
    """Adapter exposing obase's pseudo_anonymizer through the same surface."""

    def __init__(self, element: Any) -> None:
        self._element = element

    def anonymize(self, user_id: str) -> str:
        fn = getattr(self._element, "anonymize", None)
        if callable(fn):
            return cast(str, fn(user_id))
        # some 3O elements expose a plain callable instead
        return cast(Callable[[str], str], self._element)(user_id)


def resolve_anonymizer() -> Any:
    """Return the best available anonymizer (obase first, Layer-4 fallback)."""
    obase_anon = _resolve_obase_anonymizer()
    if obase_anon is not None:
        return _ObasedAnonymizer(obase_anon)
    return PseudoAnonymizer()


def anonymize_user_id(user_id: str, *, secret: str | None = None) -> str:
    """One-shot pseudo-anonymization: stable, non-PII reference token."""
    if secret is not None:
        return PseudoAnonymizer(secret=secret).anonymize(user_id)
    return cast(str, resolve_anonymizer().anonymize(user_id))


__all__ = ["PseudoAnonymizer", "anonymize_user_id", "resolve_anonymizer"]
