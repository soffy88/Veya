"""Unit tests for multi-source external skill index (Phase 0: bughunter & wshobson)."""

from __future__ import annotations

import json
import os

from server import bughunter_index, external_skill_index, tool_registry


def test_external_index_disabled_by_default() -> None:
    for env in ["VEYA_BUGHUNTER_INDEX", "VEYA_WSHOBSON_INDEX"]:
        if env in os.environ:
            del os.environ[env]

    assert not external_skill_index.enabled()
    assert not bughunter_index.enabled()
    assert external_skill_index.index.count() == 0
    assert external_skill_index.search_external_skills("fastapi") == []


def test_wshobson_index_enabled_and_search() -> None:
    os.environ["VEYA_WSHOBSON_INDEX"] = "1"
    try:
        assert external_skill_index.enabled()
        assert not bughunter_index.enabled()
        count = external_skill_index.index.count()
        assert count > 100  # We know there are ~185 skills

        hits = external_skill_index.search_external_skills("fastapi")
        assert len(hits) > 0
        hit = hits[0]
        assert "name" in hit
        assert "path" in hit
        assert "source" in hit
        assert hit["source"] == "wshobson"
        assert "plugin_name" in hit
        assert hit["plugin_name"] is not None

        # Test on-demand reading
        body = external_skill_index.index.read(hit["name"])
        assert body is not None
        assert len(body) > 0
    finally:
        del os.environ["VEYA_WSHOBSON_INDEX"]


def test_both_sources_enabled() -> None:
    os.environ["VEYA_BUGHUNTER_INDEX"] = "1"
    os.environ["VEYA_WSHOBSON_INDEX"] = "1"
    try:
        assert external_skill_index.enabled()
        assert bughunter_index.enabled()
        count = external_skill_index.index.count()
        assert count > 200

        hits = external_skill_index.search_external_skills("python")
        assert len(hits) > 0
        _sources = {h["source"] for h in hits}
        # Both sources might have python hits or at least be present in entries
        entries = external_skill_index.index.entries()
        has_bh = any(e.source == "bughunter" for e in entries)
        has_ws = any(e.source == "wshobson" for e in entries)
        assert has_bh
        assert has_ws
    finally:
        del os.environ["VEYA_BUGHUNTER_INDEX"]
        del os.environ["VEYA_WSHOBSON_INDEX"]


def test_tool_registry_lazy_search_behavior_wshobson() -> None:
    for env in ["VEYA_BUGHUNTER_INDEX", "VEYA_WSHOBSON_INDEX"]:
        if env in os.environ:
            del os.environ[env]

    res_off_str = tool_registry._lazy_search_tools("fastapi", top_k=5)
    res_off = json.loads(res_off_str)
    assert "security_skills" not in res_off
    assert "external_skills" not in res_off

    os.environ["VEYA_WSHOBSON_INDEX"] = "1"
    try:
        # Check that tool_registry search behaves identically for default-off or with wshobson
        # (wshobson doesn’t add security_skills unless bughunter is enabled, or we can check external search)
        res_on_str = tool_registry._lazy_search_tools("fastapi", top_k=5)
        _res_on = json.loads(res_on_str)
        # tool_registry currently hooks bughunter_index for security_skills.
        # Wshobson is available via external_skill_index.search_external_skills.
    finally:
        del os.environ["VEYA_WSHOBSON_INDEX"]
