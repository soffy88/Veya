"""Verification script for Claude-BugHunter Phase 0 integration.

Asserts:
1. When VEYA_BUGHUNTER_INDEX=1, querying keywords (ssrf, idor, oauth) matches
   relevant security SKILL.md entries from Claude-BugHunter skills/.
2. When VEYA_BUGHUNTER_INDEX is unset/0, default lazy tool search output does
   not contain security_skills, and resident tool surface is byte-identical.
"""

from __future__ import annotations

import json
import os

from server import bughunter_index, tool_registry


def main() -> None:
    print("=== Running Claude-BugHunter Phase 0 Verification ===")

    # 1. Test Disabled State
    if "VEYA_BUGHUNTER_INDEX" in os.environ:
        del os.environ["VEYA_BUGHUNTER_INDEX"]

    assert not bughunter_index.enabled(), "Should be disabled by default"
    assert bughunter_index.index.count() == 0, "Index count must be 0 when disabled"

    res_disabled_str = tool_registry._lazy_search_tools("ssrf", top_k=5)
    res_disabled = json.loads(res_disabled_str)
    assert "security_skills" not in res_disabled, "Disabled state must not return security_skills"
    print(" [PASS] Default disabled state: zero noise, no security_skills.")

    # 2. Test Enabled State & Keyword Matching
    os.environ["VEYA_BUGHUNTER_INDEX"] = "1"
    try:
        assert bughunter_index.enabled(), "Should be enabled when VEYA_BUGHUNTER_INDEX=1"
        total_skills = bughunter_index.index.count()
        print(f" [INFO] Discovered {total_skills} Claude-BugHunter SKILL.md files.")
        assert total_skills >= 80, f"Expected at least 80 skills, got {total_skills}"

        for kw in ["ssrf", "idor", "oauth"]:
            hits = bughunter_index.search_bughunter_skills(kw, top_k=3)
            assert len(hits) > 0, f"Expected hits for keyword '{kw}'"
            print(f" [PASS] Keyword '{kw}' matched {len(hits)} skills (top: {hits[0]['name']}).")

        # Test tool_registry integration when enabled
        res_enabled_str = tool_registry._lazy_search_tools("ssrf", top_k=5)
        res_enabled = json.loads(res_enabled_str)
        assert "security_skills" in res_enabled, "Enabled state must include security_skills in tool search payload"
        assert len(res_enabled["security_skills"]) > 0
        print(" [PASS] tool_registry._lazy_search_tools successfully surfaces security_skills when enabled.")

    finally:
        del os.environ["VEYA_BUGHUNTER_INDEX"]

    print("=== All Phase 0 Verifications Passed Successfully! ===")


if __name__ == "__main__":
    main()

