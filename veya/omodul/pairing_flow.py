"""veya/omodul/pairing_flow — device pairing confirmation flow (NANO-W6 add J).

omodul contract: ``run_pairing_flow(config, input_data, output_dir) -> dict``;
never raises. ``_enabled_pillars`` declares ``decision_trail`` — both the code
request and the confirmation append a trail record (never the code itself).

Ops (``input_data["op"]``):
  * ``request`` — generate a pairing code for a device (rate-limited, TTL,
    single use). Returns the plaintext code once; it is never logged.
  * ``confirm`` — a signed-in user confirms a code: the code is consumed
    (constant-time), a device credential is issued through the injected
    ``issue_credential`` callable and the device registered through the
    injected ``register_device`` callable.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from veya.oprim.pairing import pairing_code_consume, pairing_code_create

STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"


class PairingFlowConfig:
    """Injection points for the pairing flow."""

    _enabled_pillars = {"decision_trail"}

    def __init__(
        self,
        *,
        store: Any = None,
        trail: Callable[[str, dict[str, Any]], None] | None = None,
        issue_credential: Callable[[str, str], dict[str, Any]] | None = None,
        register_device: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.trail = trail
        self.issue_credential = issue_credential
        self.register_device = register_device


def _trail(config: PairingFlowConfig, topic: str, payload: dict[str, Any]) -> None:
    if config.trail is None:
        return
    try:
        config.trail(topic, payload)
    except Exception:
        pass


async def run_pairing_flow(
    config: PairingFlowConfig,
    input_data: Any,
    output_dir: Path = Path("/tmp/veya"),
) -> dict[str, Any]:
    """Run one pairing-flow op. Never raises; errors return status=error."""

    try:
        data = dict(input_data or {})
        op = str(data.get("op") or "")
        if op == "request":
            return _op_request(config, data)
        if op == "confirm":
            return _op_confirm(config, data)
        return {"status": STATUS_ERROR, "error": f"unknown op: {op}"}
    except Exception as exc:  # omodul contract: never raises
        return {"status": STATUS_ERROR, "error": f"{type(exc).__name__}: {exc}"}


def _op_request(config: PairingFlowConfig, data: dict[str, Any]) -> dict[str, Any]:
    result = pairing_code_create(
        str(data.get("device_kind") or "browser"),
        device_label=str(data.get("device_label") or ""),
        ttl_s=float(data.get("ttl_s") or 600.0),
        store=config.store,
    )
    if "error" in result:
        _trail(
            config,
            "pairing.request_refused",
            {"device_kind": data.get("device_kind"), "reason": result["error"]},
        )
        return {"status": STATUS_ERROR, "error": result["error"]}
    _trail(
        config,
        "pairing.requested",
        {
            "device_kind": data.get("device_kind"),
            "device_label": data.get("device_label") or "",
            "expires_at": result["expires_at"],
        },
    )
    return {
        "status": STATUS_COMPLETED,
        "op": "request",
        "code": result["code"],
        "expires_at": result["expires_at"],
    }


def _op_confirm(config: PairingFlowConfig, data: dict[str, Any]) -> dict[str, Any]:
    code = str(data.get("code") or "")
    device_label = str(data.get("device_label") or "")
    consumed = pairing_code_consume(code, store=config.store)
    if consumed.get("status") != "ok":
        _trail(
            config,
            "pairing.confirm_failed",
            {"device_label": device_label, "reason": consumed.get("status")},
        )
        return {"status": STATUS_ERROR, "error": consumed.get("status", "invalid")}
    device_kind = str(consumed.get("device_kind") or "browser")
    credential: dict[str, Any] = {}
    if config.issue_credential is not None:
        try:
            credential = dict(config.issue_credential(device_kind, device_label))
        except Exception:
            credential = {}
    registration: dict[str, Any] = {}
    if config.register_device is not None:
        try:
            registration = dict(
                config.register_device(device_kind, device_label, credential)
            )
        except Exception:
            registration = {}
    _trail(
        config,
        "pairing.confirmed",
        {
            "device_kind": device_kind,
            "device_label": device_label,
            "credential_token_id": str(credential.get("token_id") or ""),
            "registered": bool(registration),
        },
    )
    return {
        "status": STATUS_COMPLETED,
        "op": "confirm",
        "device_kind": device_kind,
        "credential": credential,
        "registration": registration,
    }


__all__ = ["PairingFlowConfig", "run_pairing_flow", "STATUS_COMPLETED", "STATUS_ERROR"]
