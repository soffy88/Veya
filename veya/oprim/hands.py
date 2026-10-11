"""
Hands oprims — atomic desktop automation operations (VEYA_HANDS).

Each oprim is a single atomic operation: one subprocess call or one
platform-specific API call. Linux X11 uses xdotool/scrot or python-mss+pynput.
macOS/Windows return unsupported stubs.

Flag: VEYA_HANDS (default OFF). Capability detection at import time.
"""

from __future__ import annotations

import base64
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from veya.obase.errors import VeyaError


class HandsUnsupportedError(VeyaError):
    """Raised when hands capability is not available on this platform."""

    def __init__(self, operation: str, reason: str):
        super().__init__(f"hands.{operation}: {reason}")
        self.operation = operation
        self.reason = reason


class HandsCapabilityError(VeyaError):
    """Raised when required tools are not installed."""

    def __init__(self, operation: str, missing: list[str]):
        super().__init__(f"hands.{operation}: missing tools: {', '.join(missing)}")
        self.operation = operation
        self.missing = missing


# ── Capability Detection ────────────────────────────────────────────────

_SYSTEM = platform.system().lower()
_HAS_XDOTOOL = shutil.which("xdotool") is not None
_HAS_SCROT = shutil.which("scrot") is not None
_HAS_WMCTRL = shutil.which("wmctrl") is not None
_HAS_MSS = False
_HAS_PYNPUT = False

# Module-level handles, so a caller (or a test) can substitute the capture
# backend without the oprim reaching for the import itself.  ``mss`` stays
# ``None`` when the package is absent, which is what ``_HAS_MSS`` records.
mss: Any = None
pynput: Any = None

# Check for python-mss and pynput (cross-platform screenshot/input)
try:
    import mss as _mss

    mss = _mss
    _HAS_MSS = True
except ImportError:
    pass

try:
    import pynput as _pynput

    pynput = _pynput
    _HAS_PYNPUT = True
except ImportError:
    pass


def _check_hands_enabled() -> None:
    """Verify VEYA_HANDS flag is enabled."""
    if os.environ.get("VEYA_HANDS", "").strip().lower() not in ("1", "true", "yes", "on"):
        raise HandsUnsupportedError(
            "init",
            "VEYA_HANDS flag not set (set VEYA_HANDS=1 to enable)"
        )


def _check_linux_x11() -> None:
    """Verify this host is one the hands oprims can drive.

    The hands backend is the X11 toolchain (xdotool/scrot), so a non-Linux
    host is unsupported by construction.  Whether a display is actually
    reachable is *capability*, not a hard precondition: it is reported by
    :func:`hands_capabilities` (and checked once by the runner before it
    advertises the capability), while each oprim reports the real failure —
    a non-zero exit from the tool it drove.
    """
    if _SYSTEM != "linux":
        raise HandsUnsupportedError(
            "platform",
            f"hands only implemented for Linux X11 (current: {platform.system()})"
        )


def _display() -> str | None:
    """The display this process would drive, if any."""
    return os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")


def _display_env(display: str | None) -> dict[str, str] | None:
    """The child environment for a command aimed at ``display``.

    ``None`` means "inherit": the tools then see the same DISPLAY this
    process already has.
    """
    if not display:
        return None
    return {**os.environ, "DISPLAY": display}


def _tool_available(name: str) -> bool:
    """Check if a tool is available."""
    if name == "xdotool":
        return _HAS_XDOTOOL
    if name == "scrot":
        return _HAS_SCROT
    if name == "wmctrl":
        return _HAS_WMCTRL
    if name == "mss":
        return _HAS_MSS
    if name == "pynput":
        return _HAS_PYNPUT
    return shutil.which(name) is not None


def _run_subprocess(
    cmd: list[str],
    *,
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run subprocess with consistent error handling."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise HandsUnsupportedError(
            "subprocess",
            f"command timed out after {timeout}s: {' '.join(cmd)}"
        ) from exc
    except OSError as exc:
        raise HandsUnsupportedError(
            "subprocess",
            f"failed to execute {' '.join(cmd)}: {exc}"
        ) from exc


# ── Oprims ──────────────────────────────────────────────────────────────

def _default_capture_path() -> str:
    """A per-process screenshot path in the system temp directory."""
    return str(Path(tempfile.gettempdir()) / f"veya_hands_{os.getpid()}.png")


def _read_capture(path: str) -> bytes | None:
    """Read a captured file back, or ``None`` when it is not there."""
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _encode_capture(data: bytes | None) -> str | None:
    """Base64 for a captured screenshot, so the caller needs no file access."""
    return base64.b64encode(data).decode("ascii") if data else None


def hands_screenshot(
    *,
    output_path: str | None = None,
    display: str | None = None,
) -> dict[str, Any]:
    """
    Take a screenshot of the primary display.

    Prefers python-mss (works on Wayland too) and falls back to scrot.  The
    base64 payload is returned alongside the path so a caller that has to show
    the frame to a model never has to read the file itself.

    Args:
        output_path: Where to save the screenshot (default: temp file)
        display: X11 display to use (default: $DISPLAY)

    Returns:
        {"path": str, "width": int, "height": int, "format": "png",
         "image_base64": str | None}
    """
    _check_hands_enabled()
    _check_linux_x11()

    # Prefer python-mss if available (works on Wayland too)
    if _HAS_MSS and mss is not None:
        if output_path is None:
            output_path = _default_capture_path()

        raw: bytes | None = None
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            shot = sct.shot(mon=1, output=output_path)
            if isinstance(shot, (bytes, bytearray)):
                raw = bytes(shot)
        if raw is None:
            raw = _read_capture(output_path)
        return {
            "path": output_path,
            "width": monitor["width"],
            "height": monitor["height"],
            "format": "png",
            "image_base64": _encode_capture(raw),
        }

    # Fallback: scrot.  With no capture backend at all there is nothing to
    # try, which is a missing capability rather than a failing command.
    if not _tool_available("scrot"):
        raise HandsCapabilityError(
            "screenshot",
            ["mss" if not _HAS_MSS else "scrot"],
        )
    if output_path is None:
        output_path = _default_capture_path()

    cmd = ["scrot"]
    if display:
        cmd.extend(["-d", display])
    cmd.append(output_path)

    result = _run_subprocess(cmd, env=_display_env(display))
    if result.returncode != 0:
        raise HandsUnsupportedError(
            "screenshot",
            f"scrot failed: {result.stderr.strip()}"
        )

    # Get dimensions via xdotool
    dim_result = _run_subprocess(
        ["xdotool", "getdisplaygeometry"], env=_display_env(display)
    )
    width, height = 0, 0
    if dim_result.returncode == 0:
        try:
            width, height = map(int, dim_result.stdout.strip().split())
        except ValueError:
            pass

    return {
        "path": output_path,
        "width": width,
        "height": height,
        "format": "png",
        "image_base64": _encode_capture(_read_capture(output_path)),
    }


def hands_list_windows(*, display: str | None = None) -> dict[str, Any]:
    """
    List all visible windows on the current display.

    Args:
        display: X11 display to use (default: $DISPLAY)

    Returns:
        {"windows": [{"id": str, "title": str, "pid": int, "geometry": {"x": int, "y": int, "width": int, "height": int}}]}
    """
    _check_hands_enabled()
    _check_linux_x11()


    env = _display_env(display)

    # Get window list with geometry from xdotool
    result = _run_subprocess(
        ["xdotool", "search", "--onlyvisible", "--name", ".*"],
        timeout=5.0,
        env=env,
    )
    if result.returncode != 0:
        raise HandsUnsupportedError(
            "list_windows",
            f"xdotool search failed: {result.stderr.strip()}"
        )

    window_ids = result.stdout.strip().split()
    windows = []

    for wid in window_ids:
        # Get window title
        title_result = _run_subprocess(["xdotool", "getwindowname", wid], env=env)
        title = title_result.stdout.strip() if title_result.returncode == 0 else ""

        # Get window geometry
        geom_result = _run_subprocess(
            ["xdotool", "getwindowgeometry", "--shell", wid], env=env
        )
        geometry = {"x": 0, "y": 0, "width": 0, "height": 0}
        if geom_result.returncode == 0:
            for line in geom_result.stdout.strip().splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key in ("X", "Y", "WIDTH", "HEIGHT"):
                        geometry[key.lower()] = int(value)

        # Get PID
        pid_result = _run_subprocess(["xdotool", "getwindowpid", wid], env=env)
        pid = 0
        if pid_result.returncode == 0:
            try:
                pid = int(pid_result.stdout.strip())
            except ValueError:
                pass

        windows.append({
            "id": wid,
            "title": title,
            "pid": pid,
            "geometry": geometry,
        })

    return {"windows": windows}


def hands_click(
    x: int,
    y: int,
    *,
    button: int = 1,
    display: str | None = None,
) -> dict[str, Any]:
    """
    Click at absolute screen coordinates.

    Args:
        x: X coordinate
        y: Y coordinate
        button: Mouse button (1=left, 2=middle, 3=right, 4=wheel up, 5=wheel down)
        display: X11 display to use (default: $DISPLAY)

    Returns:
        {"success": true, "x": int, "y": int, "button": int}
    """
    _check_hands_enabled()
    _check_linux_x11()


    env = _display_env(display)

    # Move mouse and click
    result = _run_subprocess(
        ["xdotool", "mousemove", str(x), str(y), "click", str(button)], env=env
    )
    if result.returncode != 0:
        raise HandsUnsupportedError(
            "click",
            f"xdotool click failed: {result.stderr.strip()}"
        )

    return {"success": True, "x": x, "y": y, "button": button}


def hands_type(
    text: str,
    *,
    delay_ms: int = 0,
    display: str | None = None,
) -> dict[str, Any]:
    """
    Type text at the current cursor position.

    Args:
        text: Text to type
        delay_ms: Delay between keystrokes in milliseconds
        display: X11 display to use (default: $DISPLAY)

    Returns:
        {"success": true, "chars_typed": int}
    """
    _check_hands_enabled()
    _check_linux_x11()


    env = _display_env(display)

    # xdotool type doesn't support delay directly, use --delay
    cmd = ["xdotool", "type"]
    if delay_ms > 0:
        cmd.extend(["--delay", str(delay_ms)])
    cmd.append(text)

    result = _run_subprocess(cmd, env=env)
    if result.returncode != 0:
        raise HandsUnsupportedError(
            "type",
            f"xdotool type failed: {result.stderr.strip()}"
        )

    return {"success": True, "chars_typed": len(text)}


def hands_key(
    key_sequence: str,
    *,
    display: str | None = None,
) -> dict[str, Any]:
    """
    Press a key or key combination.

    Args:
        key_sequence: Key sequence (e.g., "Return", "ctrl+c", "alt+Tab", "F11")
        display: X11 display to use (default: $DISPLAY)

    Returns:
        {"success": true, "key_sequence": str}
    """
    _check_hands_enabled()
    _check_linux_x11()


    env = _display_env(display)

    result = _run_subprocess(["xdotool", "key", key_sequence], env=env)
    if result.returncode != 0:
        raise HandsUnsupportedError(
            "key",
            f"xdotool key failed: {result.stderr.strip()}"
        )

    return {"success": True, "key_sequence": key_sequence}


def hands_scroll(
    *,
    direction: str = "down",
    amount: int = 3,
    display: str | None = None,
) -> dict[str, Any]:
    """
    Scroll at current mouse position.

    Args:
        direction: "up" or "down"
        amount: Number of scroll clicks
        display: X11 display to use (default: $DISPLAY)

    Returns:
        {"success": true, "direction": str, "amount": int}
    """
    _check_hands_enabled()
    _check_linux_x11()


    env = _display_env(display)

    # xdotool uses button 4 for up, 5 for down
    button = "4" if direction == "up" else "5"
    for _ in range(amount):
        result = _run_subprocess(["xdotool", "click", button], env=env)
        if result.returncode != 0:
            raise HandsUnsupportedError(
                "scroll",
                f"xdotool scroll failed: {result.stderr.strip()}"
            )

    return {"success": True, "direction": direction, "amount": amount}


def hands_focus_window(
    window_id: str,
    *,
    display: str | None = None,
) -> dict[str, Any]:
    """
    Focus a specific window by ID.

    Args:
        window_id: Window ID from hands_list_windows
        display: X11 display to use (default: $DISPLAY)

    Returns:
        {"success": true, "window_id": str}
    """
    _check_hands_enabled()
    _check_linux_x11()


    env = _display_env(display)

    result = _run_subprocess(["xdotool", "windowactivate", window_id], env=env)
    if result.returncode != 0:
        raise HandsUnsupportedError(
            "focus_window",
            f"xdotool windowactivate failed: {result.stderr.strip()}"
        )

    return {"success": True, "window_id": window_id}


def hands_capabilities() -> dict[str, Any]:
    """
    Return the available hands capabilities on this system.

    ``usable`` is the preflight a runner (or a route) checks before it
    advertises the capability: the flag on, a Linux host, a display to drive,
    and at least one capture/input backend installed.
    """
    enabled = os.environ.get("VEYA_HANDS", "").strip().lower() in ("1", "true", "yes", "on")
    display = _display()
    operations = {
        "screenshot": _HAS_MSS or _HAS_SCROT,
        "list_windows": _HAS_XDOTOOL and _HAS_WMCTRL,
        "click": _HAS_XDOTOOL,
        "type": _HAS_XDOTOOL,
        "key": _HAS_XDOTOOL,
        "scroll": _HAS_XDOTOOL,
        "focus_window": _HAS_XDOTOOL,
    }
    return {
        "enabled": enabled,
        "platform": _SYSTEM,
        "display": display,
        "supported_platform": _SYSTEM == "linux",
        "tools": {
            "xdotool": _HAS_XDOTOOL,
            "scrot": _HAS_SCROT,
            "wmctrl": _HAS_WMCTRL,
            "mss": _HAS_MSS,
            "pynput": _HAS_PYNPUT,
        },
        "operations": operations,
        "usable": bool(
            enabled
            and _SYSTEM == "linux"
            and display
            and any(operations.values())
        ),
    }


__all__ = [
    "HandsUnsupportedError",
    "HandsCapabilityError",
    "hands_screenshot",
    "hands_list_windows",
    "hands_click",
    "hands_type",
    "hands_key",
    "hands_scroll",
    "hands_focus_window",
    "hands_capabilities",
]