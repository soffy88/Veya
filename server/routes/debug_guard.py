"""Production guard for legacy interactive-debug endpoints."""

from __future__ import annotations

import os

from fastapi import HTTPException


def require_nonproduction_debug() -> None:
    """Disable interactive debugging when the service is in production mode."""

    production = os.environ.get("VEYA_EXECUTION_PRODUCTION", "0").strip().lower()
    if production not in {"", "0", "false", "off", "no"}:
        raise HTTPException(status_code=404, detail="debug endpoints are disabled")


__all__ = ["require_nonproduction_debug"]
