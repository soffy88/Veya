"""Managed-Python bootstrap for Hicode's 3O import boundary.

# 3O-IO-ALLOW: managed-runtime bootstrap boundary must inspect sys.path and
# module provenance before handing control to the isolated Hicode runtime.

The Veya host process starts this module with the canonical managed Python
interpreter and sends one JSON request over stdin.  This entrypoint performs
only the managed-runtime bootstrap/provenance check; Reasonix remains the
managed Hicode executor started by the host adapter.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

_PACKAGES = ("obase", "oprim", "omodul", "oskill", "oservi")


def _owner_3o_in_sys_path() -> bool:
    for raw in sys.path:
        if not raw:
            continue
        try:
            candidate = Path(raw).resolve()
        except OSError:
            continue
        if candidate.name in _PACKAGES and candidate.parent.name == "3O":
            return True
        if candidate.name == "3O":
            return True
    return False


def bootstrap(request: dict[str, Any]) -> dict[str, Any]:
    """Import the clean managed 3O surface and return secret-free evidence."""

    managed_root = (
        Path(str(request.get("managed_runtime_root") or os.environ["HICODE_MANAGED_RUNTIME_ROOT"]))
        .expanduser()
        .resolve()
    )
    imported: dict[str, str] = {}
    for name in _PACKAGES:
        module = importlib.import_module(name)
        module_path = Path(str(module.__file__)).resolve()
        if managed_root not in module_path.parents:
            raise RuntimeError(f"{name} escaped managed runtime: {module_path}")
        imported[name] = str(module_path)

    from oprim._task_tier import TIER_FLAGSHIP

    if TIER_FLAGSHIP != "FLAGSHIP":
        raise RuntimeError("managed oprim TIER_FLAGSHIP contract is invalid")
    importlib.import_module("omodul.model_router")

    owner_3o = _owner_3o_in_sys_path()
    if owner_3o:
        raise RuntimeError("owner platform/3O is present in managed sys.path")
    return {
        "status": "READY",
        "runtime_fingerprint": str(request.get("runtime_fingerprint") or ""),
        "python": sys.executable,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "packages": imported,
        "oprim_imported_from": imported["oprim"],
        "omodul_imported_from": imported["omodul"],
        "owner_platform_3o_in_sys_path": False,
        "owner_3o_import_detected": False,
        "model_requests": 0,
        "tool_calls": 0,
    }


def main() -> int:
    try:
        raw = sys.stdin.readline()
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("bootstrap request must be a JSON object")
        result = bootstrap(request)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:  # fail closed, with bounded non-secret detail
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "failure_class": "HICODE_RUNTIME_INCOMPATIBLE",
                    "failure_detail": f"{type(exc).__name__}: {exc}"[:2000],
                    "model_requests": 0,
                    "tool_calls": 0,
                    "owner_3o_import_detected": False,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["bootstrap", "main"]
