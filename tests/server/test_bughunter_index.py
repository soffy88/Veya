"""Unit tests for Claude-BugHunter Phase 0 read-only knowledge integration."""

from __future__ import annotations

import json
import os

from server import bughunter_index, tool_registry


def test_bughunter_index_disabled_by_default() -> None:
    # Ensure default state (env unset or 0)
    if "VEYA_BUGHUNTER_INDEX" in os.environ:
        del os.environ["VEYA_BUGHUNTER_INDEX"]

    assert not bughunter_index.enabled()
    assert bughunter_index.index.count() == 0
    assert bughunter_index.search_bughunter_skills("ssrf") == []


def test_bughunter_index_enabled_and_search() -> None:
    os.environ["VEYA_BUGHUNTER_INDEX"] = "1"
    try:
        assert bughunter_index.enabled()
        count = bughunter_index.index.count()
        assert count > 50  # We know there are ~83 skills

        hits = bughunter_index.search_bughunter_skills("ssrf")
        assert len(hits) > 0
        # Verify hit structure
        hit = hits[0]
        assert "name" in hit
        assert "path" in hit
        assert "description" in hit

        # Test on-demand reading
        body = bughunter_index.index.read(hit["name"])
        assert body is not None
        assert len(body) > 0
    finally:
        del os.environ["VEYA_BUGHUNTER_INDEX"]


def test_tool_registry_lazy_search_behavior() -> None:
    # When VEYA_BUGHUNTER_INDEX=0, lazy_search_tools should NOT include security_skills
    if "VEYA_BUGHUNTER_INDEX" in os.environ:
        del os.environ["VEYA_BUGHUNTER_INDEX"]

    res_off_str = tool_registry._lazy_search_tools("ssrf", top_k=5)
    res_off = json.loads(res_off_str)
    assert "security_skills" not in res_off

    # When VEYA_BUGHUNTER_INDEX=1, lazy_search_tools should include security_skills
    os.environ["VEYA_BUGHUNTER_INDEX"] = "1"
    try:
        res_on_str = tool_registry._lazy_search_tools("ssrf", top_k=5)
        res_on = json.loads(res_on_str)
        assert "security_skills" in res_on
        assert isinstance(res_on["security_skills"], list)
        assert len(res_on["security_skills"]) > 0
    finally:
        del os.environ["VEYA_BUGHUNTER_INDEX"]

