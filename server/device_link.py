"""Device link: multi-device continuity for veya.

Covers what veya lacked next to Cindy's device-link: a device directory
with presence, provider-share scopes, and shared-task scopes, all carried
in a versioned envelope the relay routes by header only (payload opaque).

Session handoff itself already exists and is NOT duplicated here:
``server/routes/session_handoff.py`` (handoff + fork) and the remote-control
handoff endpoint. This module adds the surrounding device layer: which
devices exist, which device may use another device's provider, and which
tasks are shared between two devices.

Authorization split, stated plainly: :func:`parse_envelope` only checks
structure (version + routing header). Ownership is enforced by the route:
a caller may only name devices it owns, for both ``src`` and ``dst``.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from server.bot_team.conversations import atomic_write_json

#: Protocol version: integer, only ever increases; incompatible change +1.
PROTOCOL_VERSION = 1

#: A frame over this size is refused gracefully, never truncated silently.
MAX_FRAME_BYTES = 2 * 1024 * 1024

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

SCOPE_KINDS = ("provider-share", "shared-task")


@dataclass
class Device:
    device_id: str
    owner_id: str
    label: str = ""
    platform: str = ""
    created_at: float = 0.0
    last_seen_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Scope:
    scope_id: str
    kind: str
    host_device: str
    guest_device: str
    ref: str
    owner_id: str
    status: str = "active"
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_path() -> Path:
    return Path(
        os.environ.get(
            "VEYA_DEVICE_LINK_PATH",
            str(Path.home() / ".veya" / "device_links.json"),
        )
    ).expanduser()


class DeviceLinkStore:
    """Device directory + cross-device scopes, one JSON file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else _default_path()
        self._devices: dict[str, Device] = {}
        self._scopes: dict[str, Scope] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for item in raw.get("devices", []):
            if isinstance(item, dict) and item.get("device_id"):
                self._devices[str(item["device_id"])] = Device(**{k: v for k, v in item.items() if k in Device.__dataclass_fields__})
        for item in raw.get("scopes", []):
            if isinstance(item, dict) and item.get("scope_id"):
                self._scopes[str(item["scope_id"])] = Scope(**{k: v for k, v in item.items() if k in Scope.__dataclass_fields__})

    def _save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "devices": [d.to_dict() for d in self._devices.values()],
                "scopes": [s.to_dict() for s in self._scopes.values()],
            },
        )

    def _owned_device(self, device_id: str, owner_id: str) -> Device:
        device = self._devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device: {device_id!r}")
        if device.owner_id != owner_id:
            raise PermissionError(f"device {device_id!r} belongs to someone else")
        return device

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def require_owned(self, device_id: str, owner_id: str) -> Device:
        """Return the device, or raise KeyError/PermissionError.

        Ownership is the relay's source of truth for envelope routing: a
        caller may only name devices it owns, for both src and dst.
        """

        return self._owned_device(device_id, owner_id)

    def register_device(
        self, device_id: str, owner_id: str, *, label: str = "", platform: str = ""
    ) -> Device:
        value = str(device_id or "").strip()
        if not _DEVICE_ID_RE.match(value):
            raise ValueError(f"invalid device_id: {device_id!r}")
        if value in self._devices:
            raise ValueError(f"device {value!r} already exists")
        now = time.time()
        device = Device(
            device_id=value,
            owner_id=owner_id,
            label=label.strip(),
            platform=platform.strip(),
            created_at=now,
            last_seen_at=now,
        )
        self._devices[value] = device
        self._save()
        return device

    def touch(self, device_id: str, owner_id: str) -> Device:
        device = self._owned_device(device_id, owner_id)
        device.last_seen_at = time.time()
        self._save()
        return device

    def list_devices(self, owner_id: str) -> list[Device]:
        return [d for d in self._devices.values() if d.owner_id == owner_id]

    def create_scope(
        self,
        kind: str,
        owner_id: str,
        *,
        host_device: str,
        guest_device: str,
        ref: str,
    ) -> Scope:
        if kind not in SCOPE_KINDS:
            raise ValueError(f"unknown scope kind: {kind!r}")
        if host_device == guest_device:
            raise ValueError("host_device and guest_device must differ")
        self._owned_device(host_device, owner_id)
        self._owned_device(guest_device, owner_id)
        scope = Scope(
            scope_id=uuid.uuid4().hex,
            kind=kind,
            host_device=host_device,
            guest_device=guest_device,
            ref=str(ref or ""),
            owner_id=owner_id,
            created_at=time.time(),
        )
        self._scopes[scope.scope_id] = scope
        self._save()
        return scope

    def revoke_scope(self, scope_id: str, owner_id: str) -> bool:
        scope = self._scopes.get(scope_id)
        if scope is None:
            return False
        if scope.owner_id != owner_id:
            raise PermissionError(f"scope {scope_id!r} belongs to someone else")
        scope.status = "revoked"
        self._save()
        return True

    def list_scopes(self, owner_id: str) -> list[Scope]:
        return [s for s in self._scopes.values() if s.owner_id == owner_id]


def build_envelope(*, kind: str, src: str, dst: str, payload: Any) -> dict[str, Any]:
    """Build a versioned relay envelope. The payload is never inspected."""

    if not str(kind or "").strip():
        raise ValueError("envelope kind is required")
    if not str(src or "").strip() or not str(dst or "").strip():
        raise ValueError("envelope src and dst are required")
    try:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"envelope payload is not JSON-serializable: {exc}") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError(f"envelope payload exceeds {MAX_FRAME_BYTES} bytes")
    return {
        "v": PROTOCOL_VERSION,
        "kind": str(kind),
        "id": uuid.uuid4().hex,
        "src": str(src),
        "dst": str(dst),
        "payload": payload,
    }


def parse_envelope(value: Any) -> dict[str, Any]:
    """Validate an envelope's structure only. The payload stays opaque."""

    if not isinstance(value, dict):
        raise ValueError("envelope must be an object")
    if value.get("v") != PROTOCOL_VERSION:
        raise ValueError(f"unsupported envelope version: {value.get('v')!r}")
    for field in ("kind", "id", "src", "dst"):
        if not str(value.get(field) or "").strip():
            raise ValueError(f"envelope {field} is required")
    if "payload" not in value:
        raise ValueError("envelope payload is required")
    return value


__all__ = [
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "SCOPE_KINDS",
    "Device",
    "DeviceLinkStore",
    "Scope",
    "build_envelope",
    "parse_envelope",
]
