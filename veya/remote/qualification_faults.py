"""Opt-in, out-of-band fault hooks for Local2 qualification only.

This module is deliberately not part of the MCP argument surface.  Hooks are
no-ops unless a qualification process supplies both the explicit enable flag
and a run-scoped control directory.  The harness observes checkpoint files and
controls release/restart externally; the product never restarts itself.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class QualificationFault(RuntimeError):
    """A deterministic test-only failure at a canonical lifecycle boundary."""

    def __init__(self, checkpoint: str) -> None:
        super().__init__(f"qualification fault injected at {checkpoint}")
        self.checkpoint = checkpoint


def _enabled() -> bool:
    return (
        os.environ.get("VEYA_QUALIFICATION_FAULT_INJECTION", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and bool(os.environ.get("VEYA_QUALIFICATION_RUN_ID", "").strip())
        and bool(os.environ.get("VEYA_QUALIFICATION_CONTROL_DIR", "").strip())
    )


def enabled() -> bool:
    """Expose only the activation state; never expose a public MCP setting."""

    return _enabled()


def checkpoint(name: str, **state: Any) -> None:
    """Publish a selected checkpoint and optionally fail or pause there."""

    if not _enabled():
        return
    selected = os.environ.get("VEYA_QUALIFICATION_FAULT_CHECKPOINT", "").strip()
    if selected != name:
        return
    control = Path(os.environ["VEYA_QUALIFICATION_CONTROL_DIR"])
    control.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": os.environ["VEYA_QUALIFICATION_RUN_ID"],
        "checkpoint": name,
        "action": os.environ.get("VEYA_QUALIFICATION_FAULT_ACTION", "PAUSE").upper(),
        "observed_at": time.time(),
        "state": {key: value for key, value in state.items() if value is not None},
    }
    marker = control / "checkpoint.json"
    temporary = control / "checkpoint.json.tmp"
    temporary.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
    temporary.replace(marker)
    action = str(payload["action"])
    if action == "FAIL":
        raise QualificationFault(name)
    if action != "PAUSE":
        raise RuntimeError(f"unsupported qualification fault action: {action}")
    release = control / "release"
    while not release.exists():
        time.sleep(0.05)


__all__ = ["QualificationFault", "checkpoint", "enabled"]
