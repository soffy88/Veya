"""Runtime CALLS overlay: observed stacks → caller/callee edges.

MCP ``ingest_traces`` is still a stub (binary returns
"Runtime edge creation from traces not yet implemented"). This module stores
edges locally so blast_radius can union static graph + observed calls
(interface dispatch / tests / reflection that tree-sitter cannot see).
Best-effort dual-write to the MCP tool so we light up when the binary lands.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_TB_FILE = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.+)\s*$'
)
_PYTEST = re.compile(r"^(?P<file>\S+):(?P<line>\d+): in (?P<func>.+)\s*$")


def store_path() -> Path:
    override = os.environ.get("VEYA_RUNTIME_CALLS", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".veya" / "codebase-traces" / "calls.jsonl"


def _norm_func(name: str) -> str:
    return (name or "").strip().split(".")[-1]


def parse_traceback(text: str) -> list[dict[str, Any]]:
    """Python traceback or pytest --tb=short → ordered frames then CALLS pairs."""
    frames: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        m = _TB_FILE.match(raw) or _PYTEST.match(raw)
        if not m:
            continue
        frames.append(
            {
                "func": _norm_func(m.group("func")),
                "file": m.group("file"),
                "line": int(m.group("line")),
            }
        )
    return _pairs_from_frames(frames)


def parse_json_traces(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    edges: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        payload = payload.get("traces") or payload.get("stacks") or payload.get("edges") or [payload]
    if not isinstance(payload, list):
        return []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if "caller" in item and "callee" in item:
            edges.append(
                {
                    "caller": _norm_func(str(item["caller"])),
                    "callee": _norm_func(str(item["callee"])),
                    "file": str(item.get("file") or item.get("path") or ""),
                    "line": int(item.get("line") or 0),
                }
            )
            continue
        frames = item.get("frames") or item.get("stack") or []
        if isinstance(frames, list):
            norm = []
            for fr in frames:
                if not isinstance(fr, dict):
                    continue
                name = fr.get("func") or fr.get("function") or fr.get("name") or ""
                if not name:
                    continue
                norm.append(
                    {
                        "func": _norm_func(str(name)),
                        "file": str(fr.get("file") or fr.get("filename") or ""),
                        "line": int(fr.get("line") or fr.get("lineno") or 0),
                    }
                )
            edges.extend(_pairs_from_frames(norm))
    return edges


def _pairs_from_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(len(frames) - 1):
        a, b = frames[i], frames[i + 1]
        if not a["func"] or not b["func"]:
            continue
        if a["func"] == b["func"] and a.get("file") == b.get("file"):
            continue
        out.append(
            {
                "caller": a["func"],
                "callee": b["func"],
                "file": b.get("file") or a.get("file") or "",
                "line": int(b.get("line") or a.get("line") or 0),
            }
        )
    return out


def ingest(
    *,
    text: str = "",
    traces_json: str = "",
    extra: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    edges = []
    if text.strip():
        edges.extend(parse_traceback(text))
    if (traces_json or "").strip():
        try:
            edges.extend(parse_json_traces(traces_json))
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"traces_json: {exc}", "ingested": 0}
    if extra:
        edges.extend(parse_json_traces(extra))
    edges = [e for e in edges if e.get("caller") and e.get("callee")]
    if not edges:
        return {"ok": False, "error": "no CALLS frames parsed", "ingested": 0}
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    with _LOCK, path.open("a", encoding="utf-8") as fh:
        for e in edges:
            rec = {**e, "ts": now}
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "ingested": len(edges), "sample": edges[:8]}


def load_edges() -> list[dict[str, Any]]:
    path = store_path()
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("caller") and rec.get("callee"):
                out.append(rec)
    return out


def query(symbol: str, *, direction: str = "both") -> dict[str, Any]:
    name = _norm_func(symbol)
    callers: dict[str, int] = defaultdict(int)
    callees: dict[str, int] = defaultdict(int)
    for e in load_edges():
        if e["callee"] == name and direction in {"both", "callers"}:
            callers[e["caller"]] += 1
        if e["caller"] == name and direction in {"both", "callees"}:
            callees[e["callee"]] += 1
    return {
        "symbol": name,
        "runtime_callers": sorted(callers, key=callers.get, reverse=True),
        "runtime_callees": sorted(callees, key=callees.get, reverse=True),
        "counts": {
            "callers": dict(callers),
            "callees": dict(callees),
        },
    }


def merge_into_radius(radius: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
    """Union static blast_radius with observed CALLS (does not drop static fields)."""
    rt_callers: dict[str, int] = defaultdict(int)
    rt_callees: dict[str, int] = defaultdict(int)
    names = {_norm_func(s) for s in symbols}
    for e in load_edges():
        if e["callee"] in names:
            rt_callers[e["caller"]] += 1
        if e["caller"] in names:
            rt_callees[e["callee"]] += 1
    extra_callers = [c for c in rt_callers if c not in (radius.get("callers") or [])]
    extra_callees = [c for c in rt_callees if c not in (radius.get("callees") or [])]
    radius = dict(radius)
    radius["runtime_callers"] = sorted(rt_callers, key=rt_callers.get, reverse=True)
    radius["runtime_callees"] = sorted(rt_callees, key=rt_callees.get, reverse=True)
    radius["runtime_only_callers"] = extra_callers
    radius["runtime_only_callees"] = extra_callees
    radius["total_affected"] = int(radius.get("total_affected") or 0) + len(
        set(extra_callers) | set(extra_callees)
    )
    return radius
