"""Runtime CALLS overlay: traceback ingest + union with static blast_radius."""

from __future__ import annotations

from server.runtime_calls import ingest, merge_into_radius, parse_traceback, query


TB = """\
Traceback (most recent call last):
  File "app.py", line 10, in dispatch
    handler()
  File "app.py", line 4, in handler
    helper()
  File "util.py", line 2, in helper
    return 1
"""


def test_parse_python_traceback():
    edges = parse_traceback(TB)
    pairs = {(e["caller"], e["callee"]) for e in edges}
    assert ("dispatch", "handler") in pairs
    assert ("handler", "helper") in pairs


def test_ingest_and_query(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_RUNTIME_CALLS", str(tmp_path / "calls.jsonl"))
    rec = ingest(text=TB)
    assert rec["ok"] is True
    assert rec["ingested"] >= 2
    q = query("helper")
    assert "handler" in q["runtime_callers"]
    empty = ingest(text="no frames here")
    assert empty["ok"] is False


def test_json_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_RUNTIME_CALLS", str(tmp_path / "calls.jsonl"))
    rec = ingest(
        traces_json='[{"frames":[{"func":"a","file":"a.py","line":1},{"func":"b","file":"b.py","line":2}]}]'
    )
    assert rec["ok"]
    assert query("a")["runtime_callees"] == ["b"]


def test_merge_adds_runtime_only():
    radius = merge_into_radius(
        {"symbols": ["helper"], "callers": ["main"], "callees": [], "total_affected": 1},
        ["helper"],
    )
    # no store → runtime empty, static preserved
    assert radius["callers"] == ["main"]
    assert "runtime_callers" in radius
