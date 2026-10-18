"""Measure the opt-in lazy tool surface against the canonical full surface.

Usage (from the repo root):

    VEYA_LAZY_TOOLS=1 VEYA_TOOL_SCHEMA_METRICS=1 \
        .venv/bin/python scripts/measure_lazy_tools.py
    .venv/bin/python scripts/measure_lazy_tools.py      # baseline

The script constructs one real ``MasterCoordinator`` per mode, binds a session,
and reports the exact JSON payload sent as ``tools`` to the model boundary.  It
does not call any LLM and does not mutate the user's real sessions.
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("VEYA_MEMORY", "0")


class _HistoryStore:
    async def load(self, *_args: object, **_kwargs: object) -> list[dict]:
        return []

    async def save(self, *_args: object, **_kwargs: object) -> None:
        return None


class _MemoryStore:
    async def retrieve(self, *_args: object, **_kwargs: object) -> list[dict]:
        return []

    async def add(self, *_args: object, **_kwargs: object) -> str:
        return ""


async def main() -> None:
    from server import lazy_tools
    from server.coordinator_master import MasterCoordinator
    from server.tool_registry import _current_master_session

    coord = MasterCoordinator(
        history_store=_HistoryStore(),
        memory_store=_MemoryStore(),
        llm_fn=lambda *_a, **_k: None,
    )
    lazy_tools.reset_sessions()
    out: dict[str, object] = {
        "lazy_enabled": coord._lazy_tools_enabled,
        "registry_tool_count": len(coord.tools),
        "resident_count": len(lazy_tools.resident_tool_names()),
        "full": lazy_tools.schema_metrics(coord._raw_get_all_tool_schemas()).as_dict(),
        "full_system_prompt_chars": len(coord._raw_get_system_prompt()),
    }

    token = _current_master_session.set("measure-session")
    try:
        initial = coord.get_all_tool_schemas()
        out["initial"] = lazy_tools.schema_metrics(initial).as_dict()
        if coord._lazy_tools_enabled:
            out["initial_names"] = [s["function"]["name"] for s in initial]
            out["lazy_system_prompt_chars"] = len(coord.get_system_prompt())
            loaded: list[str] = []
            for query in ("image crop OCR", "quantitative backtest market data"):
                payload = json.loads(
                    await coord.tools.execute("search_tools", {"query": query, "top_k": 5})
                )
                loaded.extend(payload["loaded"])
            out["search_loaded"] = loaded
            out["after_searches"] = lazy_tools.schema_metrics(
                coord.get_all_tool_schemas()
            ).as_dict()
        out["last_metrics"] = coord._lazy_schema_metrics
    finally:
        _current_master_session.reset(token)
        lazy_tools.reset_sessions()

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
