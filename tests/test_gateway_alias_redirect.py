"""AII 2026-10-14: VEYA_GATEWAY_ALIAS_REDIRECT serves an alias from another entry.

The gateway module fetches remote catalogs at import time, so the helper is
loaded on its own from the source instead of importing the whole module.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "veya_llm_gateway.py"


def _alias_redirect():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_alias_redirect"
    )
    ns: dict = {"os": os}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(_SRC), "exec"), ns)
    return ns["_alias_redirect"]


def test_redirect_parses(monkeypatch):
    fn = _alias_redirect()
    monkeypatch.setenv(
        "VEYA_GATEWAY_ALIAS_REDIRECT", "veya-free:veya-fllm; Veya1.2-Free : veya-fllm"
    )
    assert fn("veya-free") == "veya-fllm"
    assert fn("VEYA1.2-free") == "veya-fllm"
    assert fn("veya1.2") is None


def test_redirect_unset_or_self(monkeypatch):
    fn = _alias_redirect()
    monkeypatch.delenv("VEYA_GATEWAY_ALIAS_REDIRECT", raising=False)
    assert fn("veya-free") is None
    monkeypatch.setenv("VEYA_GATEWAY_ALIAS_REDIRECT", "veya-free:veya-free")
    assert fn("veya-free") is None


def test_chat_completions_uses_redirect():
    src = _SRC.read_text(encoding="utf-8")
    assert "redirect = _alias_redirect(requested)" in src
