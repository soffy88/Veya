"""Hashline: content-hash tags reject stale edits. Not task routing."""

from __future__ import annotations

import pytest

from server.hashline import HashlineError, apply, line_hash, render
from server.tool_registry import ToolExecutionError, _tool_edit_hashline, _tool_read_hashline


def test_render_includes_stable_content_hash():
    src = "alpha\nbeta\n"
    out = render(src)
    assert f"LINE#{line_hash('alpha')}|alpha" in out
    assert f"LINE#{line_hash('beta')}|beta" in out
    assert "1|" in out and "2|" in out


def test_apply_replaces_unique_span():
    src = "keep\nchange me\nkeep2\n"
    tag = f"LINE#{line_hash('change me')}"
    rec = apply(src, start_tag=tag, new_text="changed")
    assert rec["content"] == "keep\nchanged\nkeep2\n"
    assert rec["replaced_lines"] == 1


def test_stale_tag_rejected():
    src = "keep\nchange me\n"
    with pytest.raises(HashlineError, match="not found"):
        apply(src, start_tag="LINE#deadbeef", new_text="x")


def test_ambiguous_duplicate_lines():
    src = "same\nsame\n"
    tag = f"LINE#{line_hash('same')}"
    with pytest.raises(HashlineError, match="ambiguous"):
        apply(src, start_tag=tag, new_text="x")


def test_range_edit_and_tool_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    p = tmp_path / "a.py"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    listed = _tool_read_hashline("a.py")
    ha = line_hash("a")
    hb = line_hash("b")
    assert f"LINE#{ha}" in listed
    msg = _tool_edit_hashline("a.py", start_tag=f"LINE#{ha}", end_tag=f"LINE#{hb}", new_text="X\nY")
    assert "hashline-edited" in msg
    assert p.read_text(encoding="utf-8") == "X\nY\nc\n"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    with pytest.raises(ToolExecutionError, match="not found"):
        _tool_edit_hashline("a.py", start_tag="LINE#ffffffff", new_text="nope")
