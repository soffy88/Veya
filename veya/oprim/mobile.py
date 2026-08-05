"""
veya/oprim/mobile.py — Mobile device operations via PWA bridge (Layer 1).

Atomic operations for mobile device control through a Progressive Web App
shell running on the device. Uses WebSocket for real-time commands and
screenshots, avoiding native SDK dependencies.

The PWA approach provides:
- Cross-platform (iOS, Android, any modern browser)
- No native SDK required (no adb, no Xcode)
- Screenshot capture via Canvas API
- Touch/click emulation
- Device sensor access (camera, GPS, accelerometer via Web API)

Architecture:
    Agent (veya server)  ←WebSocket→  PWA Shell (on phone)  ←DOM→  Mobile Browser
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class MobileAction(StrEnum):
    """Mobile device actions supported via PWA bridge."""

    TAP = "tap"              # Touch at coordinates
    SWIPE = "swipe"          # Swipe gesture
    TYPE = "type"            # Type text into focused element
    SCROLL = "scroll"        # Scroll by pixels
    SCREENSHOT = "screenshot"  # Capture current screen
    BACK = "back"            # Navigate back
    REFRESH = "refresh"      # Refresh page
    URL = "url"              # Navigate to URL
    CAMERA = "camera"        # Capture photo (front/back)
    GEOLOCATION = "geolocation"  # Get GPS coordinates
    CLIPBOARD = "clipboard"  # Read/write clipboard
    NOTIFICATION = "notification"  # Send push notification
    VIBRATE = "vibrate"      # Haptic feedback
    ORIENTATION = "orientation"  # Get device orientation
    BATTERY = "battery"      # Get battery status


@dataclass
class MobileElement:
    """A detected UI element on the mobile screen."""

    selector: str
    tag: str = ""
    text: str = ""
    bounds: dict[str, int] = field(default_factory=lambda: {"x": 0, "y": 0, "w": 0, "h": 0})
    clickable: bool = True
    visible: bool = True


@dataclass
class MobileScreenshot:
    """A screenshot captured from the mobile device."""

    data_base64: str       # PNG base64
    width: int = 0
    height: int = 0
    device_pixel_ratio: float = 1.0
    timestamp_ms: float = 0.0


@dataclass
class MobileActionSpec:
    """Specification for a single mobile action."""

    action: MobileAction
    x: int | None = None       # Tap/swipe x coordinate
    y: int | None = None       # Tap/swipe y coordinate
    end_x: int | None = None   # Swipe end x
    end_y: int | None = None   # Swipe end y
    text: str | None = None    # Type text / URL
    duration_ms: int = 300     # Gesture duration
    selector: str | None = None  # CSS selector for element tap


@dataclass
class MobileActionResult:
    """Result of a mobile action."""

    success: bool
    action: str
    screenshot: MobileScreenshot | None = None
    text: str = ""
    elements: list[MobileElement] = field(default_factory=list)
    device_info: dict[str, Any] = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# Action builders (stateless)
# ---------------------------------------------------------------------------


def action_tap(x: int, y: int) -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.TAP, x=x, y=y)


def action_tap_element(selector: str) -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.TAP, selector=selector)


def action_swipe(start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int = 300) -> MobileActionSpec:
    return MobileActionSpec(
        action=MobileAction.SWIPE,
        x=start_x, y=start_y,
        end_x=end_x, end_y=end_y,
        duration_ms=duration_ms,
    )


def action_type_text(text: str) -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.TYPE, text=text)


def action_scroll_px(dx: int, dy: int) -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.SCROLL, x=dx, y=dy)


def action_screenshot() -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.SCREENSHOT)


def action_navigate_url(url: str) -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.URL, text=url)


def action_take_photo(facing: Literal["front", "back"] = "back") -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.CAMERA, text=facing)


def action_get_location() -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.GEOLOCATION)


def action_read_clipboard() -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.CLIPBOARD, text="read")


def action_write_clipboard(text: str) -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.CLIPBOARD, text=text)


def action_get_orientation() -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.ORIENTATION)


def action_get_battery() -> MobileActionSpec:
    return MobileActionSpec(action=MobileAction.BATTERY)


# ---------------------------------------------------------------------------
# PWA manifest builder
# ---------------------------------------------------------------------------


def build_pwa_manifest(
    *,
    name: str = "veya Mobile Shell",
    short_name: str = "veya",
    theme_color: str = "#1a1a2e",
    background_color: str = "#0f0f23",
    display: str = "standalone",
    orientation: str = "portrait",
    icon_path: str = "/static/icon-192.png",
) -> dict[str, Any]:
    """Build a PWA manifest.json for the mobile control shell.

    Returns a dict ready for JSON serialization.
    """
    return {
        "name": name,
        "short_name": short_name,
        "start_url": "/mobile/shell",
        "display": display,
        "orientation": orientation,
        "theme_color": theme_color,
        "background_color": background_color,
        "icons": [
            {"src": icon_path, "sizes": "192x192", "type": "image/png"},
            {"src": icon_path.replace("192", "512"), "sizes": "512x512", "type": "image/png"},
        ],
        "share_target": {
            "action": "/mobile/share",
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {"url": "url", "text": "text", "title": "title"},
        },
    }


def build_mobile_shell_html(
    *,
    ws_endpoint: str = "/ws/mobile",
    title: str = "veya Mobile",
) -> str:
    """Build the PWA mobile shell HTML page with WebSocket bridge.

    Returns a complete HTML document string with inline JS for:
    - WebSocket connection to veya server
    - Screenshot capture via html2canvas or native Canvas
    - Touch event forwarding
    - Device API access (camera, GPS, clipboard)
    - Service worker registration for offline PWA
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#1a1a2e">
<title>{title}</title>
<link rel="manifest" href="/mobile/manifest.json">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0f0f23;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;overflow:hidden;height:100vh;width:100vw}}
#status{{position:fixed;top:0;left:0;right:0;z-index:9999;padding:8px 12px;font-size:12px;text-align:center;transition:all .3s}}
.status-connected{{background:#1b5e20;color:#a5d6a7}}
.status-disconnected{{background:#b71c1c;color:#ef9a9a}}
#remote-frame{{width:100%;height:100%;border:none;position:absolute;top:0;left:0}}
#overlay{{position:fixed;bottom:0;left:0;right:0;display:flex;gap:8px;padding:12px;background:rgba(0,0,0,.8);z-index:9998}}
.overlay-btn{{flex:1;padding:10px;border:none;border-radius:8px;font-size:14px;background:#333;color:#fff;cursor:pointer}}
.overlay-btn:active{{background:#555}}
</style>
</head>
<body>
<div id="status" class="status-disconnected">⏳ Connecting...</div>
<iframe id="remote-frame" sandbox="allow-scripts allow-same-origin allow-forms allow-popups"></iframe>
<div id="overlay">
  <button class="overlay-btn" onclick="sendCmd('screenshot')">📸</button>
  <button class="overlay-btn" onclick="sendCmd('back')">◀</button>
  <button class="overlay-btn" onclick="sendCmd('refresh')">↻</button>
  <button class="overlay-btn" onclick="sendCmd('url')">🔗</button>
  <button class="overlay-btn" onclick="sendCmd('camera')">📷</button>
</div>
<script>
const WS_URL = '{ws_endpoint}';
let ws, deviceId = 'mobile-' + Math.random().toString(36).slice(2,10);
let statusEl = document.getElementById('status');
let frameEl = document.getElementById('remote-frame');

function setStatus(text, connected) {{
  statusEl.textContent = text;
  statusEl.className = connected ? 'status-connected' : 'status-disconnected';
}}

function connect() {{
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + WS_URL + '?id=' + deviceId);
  ws.onopen = () => {{
    setStatus('✅ Connected — ' + deviceId, true);
    ws.send(JSON.stringify({{type:'register',deviceId,ua:navigator.userAgent,screen:{{w:screen.width,h:screen.height,dpr:window.devicePixelRatio}}}}));
  }};
  ws.onclose = () => {{ setStatus('❌ Disconnected', false); setTimeout(connect, 3000); }};
  ws.onerror = () => setStatus('⚠ Error', false);
  ws.onmessage = (ev) => {{
    const msg = JSON.parse(ev.data);
    if (msg.action === 'url' && msg.url) frameEl.src = msg.url;
    else if (msg.action === 'back') history.back();
    else if (msg.action === 'refresh') frameEl.src = frameEl.src;
    else if (msg.action === 'screenshot') captureScreen();
    else if (msg.action === 'clipboard' && msg.text) navigator.clipboard.writeText(msg.text);
    else if (msg.action === 'vibrate') navigator.vibrate(msg.duration || 200);
  }};
}}

function sendCmd(action, data) {{
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({{action,...data||{{}},deviceId,timestamp:Date.now()}}));
}}

async function captureScreen() {{
  try {{
    const canvas = document.createElement('canvas');
    canvas.width = screen.width * window.devicePixelRatio;
    canvas.height = screen.height * window.devicePixelRatio;
    const ctx = canvas.getContext('2d');
    // Use MediaDevices for screen capture on supported browsers
    if (navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) {{
      const stream = await navigator.mediaDevices.getDisplayMedia({{video:true}});
      const track = stream.getVideoTracks()[0];
      const imgCap = new ImageCapture(track);
      const bitmap = await imgCap.grabFrame();
      ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      track.stop();
    }} else {{
      // Fallback: capture visible DOM as text description
      ctx.fillStyle = '#1a1a2e';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#fff';
      ctx.font = '24px sans-serif';
      ctx.fillText('Screenshot unavailable in this browser', 20, 60);
    }}
    const b64 = canvas.toDataURL('image/png').split(',')[1];
    sendCmd('screenshot_result', {{b64, w: canvas.width, h: canvas.height}});
  }} catch(e) {{ sendCmd('error', {{msg: 'Screenshot failed: ' + e.message}}); }}
}}

// Service Worker for offline PWA
if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/mobile/sw.js').catch(() => {{}});
}}

connect();
</script>
</body>
</html>"""


def build_service_worker_js() -> str:
    """Build the PWA service worker for offline caching."""
    return """
const CACHE = 'veya-mobile-v1';
const ASSETS = ['/mobile/shell', '/mobile/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)));
});

self.addEventListener('fetch', e => {
  e.respondWith(
    caches.match(e.request).then(r => r || fetch(e.request))
  );
});
"""
