# 3O-IO-ALLOW — server/workspace_uri.py
# 理由：工作区虚拟 URI 与绝对路径互转及防越界校验（Path.resolve 路径规范化）；不产生副作用写操作。
"""server/workspace_uri — Clean-room virtual workspace URI implementation.

Flag: ``VEYA_WORKSPACE_URI`` (default off).

Translates between virtual ``veya://workspace/<rel_path>`` and host absolute
filesystem paths. Hides physical server layout from model outputs and prevents
directory traversal (``..`` escapes).
"""

from __future__ import annotations

import os
from pathlib import Path

from server.feature_flags import enabled

FLAG_NAME = "VEYA_WORKSPACE_URI"
URI_PREFIX = "veya://workspace/"


class PathTraversalError(ValueError):
    """Raised when a workspace URI attempts to escape the workspace root."""


def is_workspace_uri_enabled() -> bool:
    """Return True if virtual workspace URI translation is active."""
    try:
        return bool(enabled(FLAG_NAME))
    except KeyError:
        return False


def get_default_workspace_root() -> Path:
    """Return the active workspace root."""
    try:
        from server.tool_registry import _current_workspace_root

        bound = _current_workspace_root.get()
        if bound is not None:
            return bound
    except Exception:
        pass
    env_val = os.environ.get("VEYA_WORKSPACE")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def get_default_write_root() -> Path:
    """Return the active write root."""
    try:
        from server.tool_registry import _current_write_root

        bound = _current_write_root.get()
        if bound is not None:
            return bound
    except Exception:
        pass
    env_val = os.environ.get("VEYA_WRITE_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    return Path.home() / ".veya" / "work"


def to_virtual_uri(filepath: str | Path, root: Path | None = None) -> str:
    """Convert an absolute or relative filesystem path to a virtual veya://workspace/ URI."""
    raw = str(filepath or "").strip()
    if not raw:
        return URI_PREFIX
    if raw.startswith(URI_PREFIX):
        return raw

    ws_root = (root or get_default_workspace_root()).resolve()
    wr_root = get_default_write_root().resolve()
    p = Path(raw).expanduser().resolve()

    # If within workspace root
    try:
        rel = p.relative_to(ws_root)
        return f"{URI_PREFIX}{rel.as_posix()}"
    except ValueError:
        pass

    # If within write root
    try:
        rel = p.relative_to(wr_root)
        return f"{URI_PREFIX}{rel.as_posix()}"
    except ValueError:
        pass

    # Relative path without root prefix
    clean_p = Path(raw)
    if not clean_p.is_absolute():
        return f"{URI_PREFIX}{clean_p.as_posix()}"

    # Outside roots fallback (still hide server path)
    return f"{URI_PREFIX}{p.name}"


def to_real_path(
    uri_or_path: str,
    root: Path | None = None,
    *,
    must_exist: bool = False,
    is_write: bool = False,
) -> Path:
    """Resolve a virtual veya:// URI or raw path to a verified real filesystem Path.

    Guards against directory traversal (``..`` escapes).
    """
    raw = str(uri_or_path or "").strip()
    if not raw:
        raise ValueError("path cannot be empty")

    base_root = (
        root or (get_default_write_root() if is_write else get_default_workspace_root())
    ).resolve()

    if raw.startswith(URI_PREFIX):
        rel = raw[len(URI_PREFIX) :].strip()
        # Disallow leading slashes and explicit directory traversal sequences
        parts = Path(rel).parts
        if ".." in parts or any(part.startswith("/") or part.startswith("\\") for part in parts):
            raise PathTraversalError(f"path {raw!r} contains directory traversal (..)")
        target = (base_root / rel).resolve()
    else:
        p = Path(raw).expanduser()
        target = p.resolve() if p.is_absolute() else (base_root / p).resolve()

    # Verify target stays inside base_root or allowed roots
    try:
        target.relative_to(base_root)
    except ValueError:
        raise PathTraversalError(f"path {raw!r} escapes workspace root {base_root}") from None

    if must_exist and not target.exists():
        raise FileNotFoundError(f"file not found: {raw}")

    return target


def sanitize_text(text: str, root: Path | None = None) -> str:
    """Replace occurrences of real absolute workspace paths with virtual veya://workspace/ URIs."""
    if not text:
        return text

    ws_root = (root or get_default_workspace_root()).resolve()
    wr_root = get_default_write_root().resolve()

    out = text
    # Replace write root first (often longer or ~/.veya/work)
    wr_str = str(wr_root)
    if wr_str and wr_str in out:
        out = out.replace(wr_str + "/", URI_PREFIX)
        out = out.replace(wr_str, URI_PREFIX)

    # Replace workspace root
    ws_str = str(ws_root)
    if ws_str and ws_str in out:
        out = out.replace(ws_str + "/", URI_PREFIX)
        out = out.replace(ws_str, URI_PREFIX)

    return out
