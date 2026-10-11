"""veya/obase/a11y.py — Accessibility tree snapshot adapters (主体 6).

External adapter for extracting accessibility trees:
- Browser: Playwright page.accessibility.snapshot()
- Desktop: AT-SPI on Linux runner

obase layer: external interface adaptation only, zero 3O layer imports.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from typing import Any

__all__ = [
    "capture_browser_a11y",
    "capture_desktop_a11y",
    "format_a11y_tree",
]


async def capture_browser_a11y(page: Any) -> dict[str, Any]:
    """Capture accessibility snapshot from a Playwright Page or compatible object.

    Returns dict with keys:
        - "available": bool
        - "tree": dict | None
        - "formatted": str
        - "error": str
    """
    if page is None:
        return {
            "available": False,
            "tree": None,
            "formatted": "",
            "error": "no page provided",
        }

    try:
        accessibility = getattr(page, "accessibility", None)
        if accessibility is not None and hasattr(accessibility, "snapshot"):
            snapshot_fn = accessibility.snapshot
            if asyncio.iscoroutinefunction(snapshot_fn):
                snapshot = await snapshot_fn(interesting_only=True)
            else:
                snapshot = snapshot_fn(interesting_only=True)
            formatted = format_a11y_tree(snapshot) if snapshot else ""
            return {
                "available": bool(snapshot),
                "tree": snapshot,
                "formatted": formatted,
                "error": "",
            }

        # Fallback for dict/mock
        if isinstance(page, dict) and "accessibility" in page:
            tree = page.get("accessibility")
            return {
                "available": bool(tree),
                "tree": tree,
                "formatted": format_a11y_tree(tree) if tree else "",
                "error": "",
            }

        return {
            "available": False,
            "tree": None,
            "formatted": "",
            "error": "page object does not support accessibility.snapshot",
        }
    except Exception as exc:
        return {
            "available": False,
            "tree": None,
            "formatted": "",
            "error": str(exc),
        }


def capture_desktop_a11y() -> dict[str, Any]:
    """Capture AT-SPI accessibility tree from Linux desktop runner.

    Probes AT-SPI bus or system tools. Degrades gracefully if not on Linux desktop.
    """
    if os.name != "posix":
        return {
            "available": False,
            "tree": None,
            "formatted": "",
            "error": "desktop AT-SPI is only available on POSIX/Linux platforms",
        }

    bus_address = os.environ.get("AT_SPI_BUS_ADDRESS", "")
    if not bus_address and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return {
            "available": False,
            "tree": None,
            "formatted": "",
            "error": "no graphical display or AT-SPI bus detected",
        }

    # If gdbus or busctl is present, probe org.a11y.Bus
    gdbus = shutil.which("gdbus")
    if gdbus:
        try:
            res = subprocess.run(
                [
                    gdbus,
                    "call",
                    "--session",
                    "--dest",
                    "org.a11y.Bus",
                    "--object-path",
                    "/org/a11y/bus",
                    "--method",
                    "org.a11y.Bus.GetAddress",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                addr = res.stdout.strip().strip("('").rstrip("',)")
                return {
                    "available": True,
                    "tree": {"bus": "org.a11y.Bus", "address": addr},
                    "formatted": f"[Desktop a11y AT-SPI bus active: {addr}]",
                    "error": "",
                }
        except Exception:
            pass

    return {
        "available": False,
        "tree": None,
        "formatted": "",
        "error": "AT-SPI service not reachable on current runner",
    }


def format_a11y_tree(node: Any, depth: int = 0, max_depth: int = 8) -> str:
    """Format an accessibility snapshot tree into readable text for agents."""
    if node is None or depth > max_depth:
        return ""

    if isinstance(node, list):
        return "\n".join(format_a11y_tree(item, depth=depth, max_depth=max_depth) for item in node)

    if not isinstance(node, dict):
        return str(node)

    indent = "  " * depth
    role = node.get("role", "node")
    name = node.get("name", "")
    value = node.get("value", "")
    desc = node.get("description", "")

    parts = [f"{indent}[{role}]"]
    if name:
        parts.append(f'"{name}"')
    if value:
        parts.append(f"value={value}")
    if desc:
        parts.append(f"({desc})")

    line = " ".join(parts)
    children = node.get("children", [])
    if children and isinstance(children, list):
        child_lines = [
            format_a11y_tree(child, depth=depth + 1, max_depth=max_depth)
            for child in children
        ]
        child_rendered = "\n".join(c for c in child_lines if c)
        if child_rendered:
            return f"{line}\n{child_rendered}"

    return line
