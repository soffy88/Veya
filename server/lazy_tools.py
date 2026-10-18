"""Opt-in, session-scoped lazy tool loading.

The default Veya path keeps the canonical full tool surface.  When
``VEYA_LAZY_TOOLS=1`` is set, the MasterAgent receives a small resident set
plus tools that the model explicitly discovered (or directly called) in the
current session.  Discovery is always model-triggered; this module contains no
semantic routing or task classification.
"""

from __future__ import annotations

import contextvars
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_ENABLED_ENV = "VEYA_LAZY_TOOLS"
_RESIDENT_ENV = "VEYA_LAZY_RESIDENT_TOOLS"
_METRICS_ENV = "VEYA_TOOL_SCHEMA_METRICS"

# Initial resident set based on real tool-call frequency in local trajectories
# plus the canonical coding/execution path.  Operators can override it with a
# comma-separated VEYA_LAZY_RESIDENT_TOOLS value.
# Ordered by observed tool-call frequency (~/.veya/trajectories) and by the
# canonical coding path (docs/harness2-integration.md section 1.2).  The
# ``system_*`` tools are deliberately NOT here: the engine supplies them every
# round through its non-unloadable host surface (oservi get_system_schemas), so
# they stay visible in both modes and listing them as "resident" would
# overstate the reduction.
DEFAULT_RESIDENT_TOOLS: tuple[str, ...] = (
    "search_tools",
    "ask_user",
    "coding_task_run",
    "run_in_sandbox",
    "read_file_ast",
    "list_files",
    "grep",
    "read_hashline",
    "write_file",
    "fetch_url",
    "browser_run",
    "assemble_code_context",
    "agent_loop_run",
    "run_skill",
    "memory_search",
    "skill_search",
)


@dataclass(frozen=True)
class SchemaMetrics:
    tool_count: int
    chars: int
    estimated_tokens: int

    def as_dict(self) -> dict[str, int]:
        return {
            "tool_count": self.tool_count,
            "chars": self.chars,
            "estimated_tokens": self.estimated_tokens,
        }


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes"}


def enabled() -> bool:
    return _truthy(os.environ.get(_ENABLED_ENV))


def metrics_enabled() -> bool:
    return enabled() or _truthy(os.environ.get(_METRICS_ENV))


def resident_tool_names() -> tuple[str, ...]:
    configured = os.environ.get(_RESIDENT_ENV, "")
    if not configured.strip():
        return DEFAULT_RESIDENT_TOOLS
    names = tuple(dict.fromkeys(part.strip() for part in configured.split(",") if part.strip()))
    if "search_tools" not in names:
        names = ("search_tools", *names)
    return names


def schema_name(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    function = schema.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(schema.get("name") or "")


def estimate_tokens(text: str) -> int:
    """Match the repository's deterministic CJK-aware token estimate."""
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(1, cjk + (other + 3) // 4)


def schema_metrics(schemas: Iterable[dict] | None) -> SchemaMetrics:
    items = list(schemas or [])
    raw = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return SchemaMetrics(
        tool_count=len(items),
        chars=len(raw),
        estimated_tokens=estimate_tokens(raw),
    )


def catalog_names(schemas: Iterable[dict] | None) -> set[str]:
    return {name for schema in (schemas or []) if (name := schema_name(schema))}


_loaded_by_session: dict[str, list[str]] = {}


def loaded_tools(session_id: str | None) -> tuple[str, ...]:
    if not session_id:
        return ()
    return tuple(_loaded_by_session.get(session_id, ()))


def load_tools(session_id: str | None, names: Iterable[str]) -> tuple[str, ...]:
    """Append new tools to a session's stable loaded-tool order."""
    if not session_id:
        return ()
    loaded = _loaded_by_session.setdefault(session_id, [])
    known = set(loaded)
    for name in names:
        if name and name not in known:
            loaded.append(name)
            known.add(name)
    return tuple(loaded)


def clear_session(session_id: str | None) -> None:
    if session_id:
        _loaded_by_session.pop(session_id, None)


def reset_sessions() -> None:
    _loaded_by_session.clear()


def select_schemas(
    all_schemas: Iterable[dict],
    *,
    session_id: str | None,
    resident_names: Iterable[str] | None = None,
    always_names: Iterable[str] | None = None,
) -> list[dict]:
    """Build a stable surface: always-on, then resident, then discovery order.

    ``always_names`` holds the host-level tool names the engine marks as
    non-unloadable (oservi get_system_schemas); they stay visible in both modes
    so reload / swarm / automation / vault capabilities never disappear.
    The result is append-only and deterministic: loading a new tool appends to
    the tail without moving any earlier entry, so an already-sent prefix stays
    byte-identical (prompt-cache friendly).
    """
    by_name = {name: schema for schema in all_schemas if (name := schema_name(schema))}
    resident = tuple(resident_names or resident_tool_names())
    selected: list[dict] = []
    seen: set[str] = set()
    for name in tuple(always_names or ()) + resident:
        schema = by_name.get(name)
        if schema is not None and name not in seen:
            selected.append(schema)
            seen.add(name)
    for name in loaded_tools(session_id):
        schema = by_name.get(name)
        if schema is not None and name not in seen:
            selected.append(schema)
            seen.add(name)
    return selected


def compact_inventory(
    all_schemas: Iterable[dict],
    *,
    resident_names: Iterable[str] | None = None,
    always_names: Iterable[str] | None = None,
) -> str:
    """Return a stable name-only catalog instead of every tool description."""
    names = sorted(catalog_names(all_schemas))
    resident = tuple(resident_names or resident_tool_names())
    resident_present = [name for name in resident if name in names]
    always_present = [name for name in (always_names or ()) if name in names]
    lines = [
        "# AVAILABLE TOOLS (lazy loading enabled)",
        "Resident tools are directly callable: "
        + ", ".join(f"`{name}`" for name in resident_present)
        + ".",
    ]
    if always_present:
        lines.append(
            "Always-available host tools: "
            + ", ".join(f"`{name}`" for name in always_present)
            + "."
        )
    lines.append("All registered capabilities: " + ", ".join(f"`{name}`" for name in names) + ".")
    lines.append(
        "Call `search_tools(query)` to retrieve complete schemas and add matching "
        "tools to this session. A direct call to any registered tool also loads it."
    )
    return "\n".join(lines)


_catalog_ctx: contextvars.ContextVar[tuple[dict, ...] | None] = contextvars.ContextVar(
    "veya_lazy_tool_catalog", default=None
)


def bind_catalog(schemas: Iterable[dict]) -> contextvars.Token:
    return _catalog_ctx.set(tuple(schemas))


def reset_catalog(token: contextvars.Token) -> None:
    _catalog_ctx.reset(token)


def current_catalog() -> list[dict]:
    catalog = _catalog_ctx.get()
    return list(catalog or ())
