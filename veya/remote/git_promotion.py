"""Deprecated compatibility marker for the removed file-copy path.

Canonical promotion is implemented only by
``veya.remote.execution_worktree.CanonicalPromotionService``.  This module is
kept import-stable so old imports fail closed instead of mutating a checkout.
"""

from __future__ import annotations

from typing import Any


class PromotionError(RuntimeError):
    """Compatibility exception for callers migrating to execution promotion."""


def _removed(*args: Any, **kwargs: Any) -> None:
    raise PromotionError(
        "legacy file-copy promotion was removed; use git.promote with execution_id"
    )


preflight_promotion = _removed
apply_promotion = _removed
rollback_promotion = _removed

__all__ = ["PromotionError", "apply_promotion", "preflight_promotion", "rollback_promotion"]
