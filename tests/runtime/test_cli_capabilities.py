"""The frozen CLI capability inventory: aliases, order, and honesty of values.

The inventory exists so routing/UI code and the process supervisor can ask what
an engine can do without importing the argv builder. These tests pin the two
things that make it safe to rely on: lookup behaviour, and the absence of
optimistic claims (nothing may claim a wider scope or a shorter deadline than
the argv table supports).
"""

from __future__ import annotations

import dataclasses

import pytest

from server.cli_capabilities import (
    CANONICAL_NAMES,
    CliCapability,
    find_capability,
    get_capability,
    list_capabilities,
    normalize_name,
)


def test_records_are_frozen():
    cap = get_capability("codex")
    with pytest.raises(dataclasses.FrozenInstanceError):
        cap.supports_resume = True  # type: ignore[misc]


def test_aliases_resolve_to_the_canonical_record():
    assert get_capability("claude") is get_capability("claude_code")
    assert get_capability("claude-code") is get_capability("claude_code")
    assert get_capability("  Claude  ") is get_capability("claude_code")
    assert get_capability("open-code") is get_capability("opencode")
    assert get_capability("opencode_go") is get_capability("opencode")
    # unknown ids pass through normalisation untouched (only lookup rejects)
    assert normalize_name("no-such-binary") == "no-such-binary"


def test_unknown_engine_raises_but_find_returns_none():
    with pytest.raises(ValueError, match="no-such-cli-binary"):
        get_capability("no-such-cli-binary")
    assert find_capability("no-such-cli-binary") is None
    assert find_capability("claude") is get_capability("claude_code")


def test_list_is_complete_and_in_canonical_order():
    listed = list_capabilities()
    assert tuple(cap.name for cap in listed) == CANONICAL_NAMES
    assert len({cap.name for cap in listed}) == len(listed)
    assert all(isinstance(cap, CliCapability) for cap in listed)
    # every record is reachable by its own name
    for cap in listed:
        assert get_capability(cap.name) is cap


def test_timeouts_are_internally_consistent():
    for cap in list_capabilities():
        assert cap.default_idle_timeout_s > 0, cap.name
        assert cap.default_wall_timeout_s > 0, cap.name
        # the idle guard must be able to fire first, otherwise every stall is
        # reported as a wall timeout and the distinction is worthless
        assert cap.default_idle_timeout_s < cap.default_wall_timeout_s, cap.name


def test_claims_stay_conservative():
    for cap in list_capabilities():
        assert cap.binary and " " not in cap.binary, cap.name
        assert cap.output_format in {"text", "json", "stream-json"}
        assert cap.permission_scope in {"read_only", "workspace_write", "full"}
        # no engine is launched with a flag that widens it to full access
        assert cap.permission_scope != "full", cap.name


def test_only_codex_is_recorded_as_writing_the_workspace():
    """codex is the one argv with an explicit ``--sandbox workspace-write``."""
    assert get_capability("codex").permission_scope == "workspace_write"
    for cap in list_capabilities():
        if cap.name != "codex":
            assert cap.permission_scope == "read_only", cap.name


def test_streaming_flag_matches_the_output_format():
    for cap in list_capabilities():
        # structured stream formats are meaningless without streaming support
        if cap.output_format == "stream-json":
            assert cap.supports_streaming, cap.name
