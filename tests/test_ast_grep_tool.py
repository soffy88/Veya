"""ast-grep tools: honest missing CLI; mocked search/rewrite."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server import ast_grep_tool as ag
from server.tool_registry import ToolExecutionError, _tool_ast_grep_search


def test_missing_bin_is_honest(monkeypatch):
    monkeypatch.setattr(ag, "resolve_bin", lambda: None)
    rec = ag.search("print($A)", path=".")
    assert rec["ok"] is False
    assert "not installed" in rec["error"]


def test_search_parses_json(monkeypatch, tmp_path):
    monkeypatch.setattr(ag, "resolve_bin", lambda: "/usr/bin/ast-grep")

    def _fake(args, timeout=30):
        assert args[0] == "/usr/bin/ast-grep"
        assert "--pattern" in args
        return SimpleNamespace(returncode=0, stdout='[{"text":"print(1)","file":"a.py"}]', stderr="")

    monkeypatch.setattr(ag, "_run", _fake)
    rec = ag.search("print($A)", path=str(tmp_path), lang="python")
    assert rec["ok"] is True
    assert rec["n"] == 1


def test_tool_jails_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(ag, "resolve_bin", lambda: None)
    with pytest.raises(ToolExecutionError, match="not installed"):
        _tool_ast_grep_search("print($A)", path=".")
    with pytest.raises(ToolExecutionError, match="escapes"):
        _tool_ast_grep_search("print($A)", path="/etc/passwd")
