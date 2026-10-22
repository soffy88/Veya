"""One shape for "this feature is switched off" across HTTP surfaces.

A route that exists but whose feature flag is off must not be confused with a
route that does not exist at all: clients and monitors need to tell "feature
disabled" from "wrong URL" without string-matching a prose message.

The status stays 404 so a disabled surface keeps the "invisible rather than
merely empty" property the remote-control router already relies on, and the
body carries the machine-readable discriminator::

    {"detail": {"code": "feature_disabled",
                "feature": "VEYA_REMOTE_CONTROL",
                "message": "..."}}

``detail`` is a JSON object rather than a string so it cannot collide with a
genuine not-found message; ``message`` keeps the previous human text.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

FEATURE_DISABLED_CODE = "feature_disabled"


def feature_disabled(
    feature: str,
    *,
    message: str = "",
    status_code: int = status.HTTP_404_NOT_FOUND,
) -> HTTPException:
    """Build the canonical "feature disabled" HTTP error."""
    return HTTPException(
        status_code=status_code,
        detail={
            "code": FEATURE_DISABLED_CODE,
            "feature": feature,
            "message": message or f"{feature} is disabled",
        },
    )


def is_feature_disabled(detail: Any) -> bool:
    """Whether an error body's ``detail`` marks a disabled feature."""
    return isinstance(detail, dict) and detail.get("code") == FEATURE_DISABLED_CODE


__all__ = ["FEATURE_DISABLED_CODE", "feature_disabled", "is_feature_disabled"]
