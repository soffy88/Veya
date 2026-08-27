"""
veya/oskill/mobile.py — Mobile Device Bridge (Layer 2).

WebSocket-based mobile device control pipeline. Manages connections to
PWA shells running on user phones and executes atomic mobile actions.

No native SDK required — uses Web APIs available in modern mobile browsers.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from veya.oprim.mobile import (
    MobileAction,
    MobileActionResult,
    MobileActionSpec,
    MobileScreenshot,
)

# ---------------------------------------------------------------------------
# Device registry
# ---------------------------------------------------------------------------


@dataclass
class ConnectedDevice:
    """A connected mobile device."""

    device_id: str
    user_agent: str = ""
    screen_width: int = 0
    screen_height: int = 0
    device_pixel_ratio: float = 1.0
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


class MobileBridge:
    """WebSocket bridge for mobile device control.

    Manages connections to PWA shells and executes actions.

    Example:
        >>> bridge = MobileBridge()
        >>> await bridge.start_server(port=8766)
        >>> # User opens https://host:8766/mobile/shell on phone
        >>> screenshot = await bridge.screenshot("mobile-abc123")
    """

    def __init__(self):
        self._devices: dict[str, ConnectedDevice] = {}
        self._ws_connections: dict[str, Any] = {}  # device_id → WebSocket
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._server = None

    # ── Device management ──────────────────────────────────────────────

    def register_device(self, device_id: str, info: dict) -> ConnectedDevice:
        """Register a newly connected device."""
        device = ConnectedDevice(
            device_id=device_id,
            user_agent=info.get("ua", ""),
            screen_width=info.get("screen", {}).get("w", 0),
            screen_height=info.get("screen", {}).get("h", 0),
            device_pixel_ratio=info.get("screen", {}).get("dpr", 1.0),
        )
        self._devices[device_id] = device
        return device

    def get_device(self, device_id: str) -> ConnectedDevice | None:
        return self._devices.get(device_id)

    def list_devices(self) -> list[ConnectedDevice]:
        return list(self._devices.values())

    async def handle_message(self, device_id: str, raw_msg: str):
        """Handle an incoming WebSocket message from a device."""
        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type", msg.get("action", ""))

        if msg_type == "register":
            self.register_device(device_id, msg)

        elif msg_type in ("screenshot_result", "action_result"):
            request_id = msg.get("request_id")
            if request_id and request_id in self._pending_responses:
                future = self._pending_responses.pop(request_id)
                if not future.done():
                    future.set_result(msg)

        elif msg_type == "error":
            request_id = msg.get("request_id")
            if request_id and request_id in self._pending_responses:
                future = self._pending_responses.pop(request_id)
                if not future.done():
                    future.set_exception(RuntimeError(msg.get("msg", "Device error")))

    # ── Action execution ───────────────────────────────────────────────

    async def execute(
        self,
        device_id: str,
        action: MobileActionSpec,
        timeout_sec: float = 30.0,
    ) -> MobileActionResult:
        """Execute a mobile action on a connected device."""
        ws = self._ws_connections.get(device_id)
        if ws is None:
            return MobileActionResult(
                success=False,
                action=action.action.value,
                error=f"Device {device_id} not connected",
            )

        request_id = f"req_{int(time.time() * 1000)}_{device_id}"
        cmd = {
            "request_id": request_id,
            "action": action.action.value,
            "x": action.x,
            "y": action.y,
            "end_x": action.end_x,
            "end_y": action.end_y,
            "text": action.text,
            "selector": action.selector,
            "duration_ms": action.duration_ms,
        }

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_responses[request_id] = future

        try:
            await ws.send(json.dumps(cmd))
            response = await asyncio.wait_for(future, timeout=timeout_sec)
        except TimeoutError:
            self._pending_responses.pop(request_id, None)
            return MobileActionResult(
                success=False,
                action=action.action.value,
                error=f"Timeout after {timeout_sec}s",
            )
        except Exception as e:
            self._pending_responses.pop(request_id, None)
            return MobileActionResult(
                success=False,
                action=action.action.value,
                error=str(e),
            )

        screenshot = None
        if response.get("b64"):
            screenshot = MobileScreenshot(
                data_base64=response["b64"],
                width=response.get("w", 0),
                height=response.get("h", 0),
            )

        return MobileActionResult(
            success=True,
            action=action.action.value,
            screenshot=screenshot,
            text=response.get("text", ""),
            device_info={
                "screen": f"{response.get('w', 0)}x{response.get('h', 0)}",
            },
        )

    async def screenshot(self, device_id: str) -> MobileActionResult:
        """Capture a screenshot from the device."""
        return await self.execute(
            device_id,
            MobileActionSpec(action=MobileAction.SCREENSHOT),
        )

    async def tap(self, device_id: str, x: int, y: int) -> MobileActionResult:
        """Tap at coordinates on the device."""
        return await self.execute(
            device_id,
            MobileActionSpec(action=MobileAction.TAP, x=x, y=y),
        )

    async def type_text(self, device_id: str, text: str) -> MobileActionResult:
        """Type text on the device."""
        return await self.execute(
            device_id,
            MobileActionSpec(action=MobileAction.TYPE, text=text),
        )

    async def navigate(self, device_id: str, url: str) -> MobileActionResult:
        """Navigate to a URL on the device."""
        return await self.execute(
            device_id,
            MobileActionSpec(action=MobileAction.URL, text=url),
        )

    async def take_photo(self, device_id: str, facing: str = "back") -> MobileActionResult:
        """Take a photo with the device camera."""
        return await self.execute(
            device_id,
            MobileActionSpec(action=MobileAction.CAMERA, text=facing),
        )

    def disconnect(self, device_id: str):
        """Disconnect a device."""
        self._devices.pop(device_id, None)
        self._ws_connections.pop(device_id, None)
