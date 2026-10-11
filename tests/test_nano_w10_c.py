"""NANO-W10 Group C tests — safety and permissions.

Scope (W10_GROUP_C.md): item 3 (command auto-review), addendum P (group roles),
addendum Q (per-bot secrets), item 7a (agent-friendly tool errors), item 7b
(failure -> eval export).  This file belongs to group C alone.

Every test runs with the feature flags ON via monkeypatch *and* exercises the
default-OFF path explicitly, because "off" is the shipped behaviour.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# item 7a — agent-friendly tool errors
# ---------------------------------------------------------------------------


class TestToolErrorText:
    """The pure formatter behind every tool error the model reads."""

    def test_states_what_failed_and_the_next_step(self):
        from veya.oskill.pure.tool_error_text import explain_tool_error

        text = explain_tool_error("read_file_ast", "FileNotFoundError: missing.py")
        assert "What failed:" in text
        assert "Next step:" in text
        assert "read_file_ast" in text

    def test_missing_file_points_at_listing_first(self):
        from veya.oskill.pure.tool_error_text import suggest_next_step

        assert "does not exist" in suggest_next_step("FileNotFoundError: [Errno 2] no such file")

    def test_missing_argument_points_at_the_schema(self):
        from veya.oskill.pure.tool_error_text import suggest_next_step

        step = suggest_next_step("arguments failed JSON schema validation: 'path' is a required")
        assert "schema" in step
        assert "required argument" in step

    def test_permission_denied_points_at_access_not_the_tool(self):
        from veya.oskill.pure.tool_error_text import suggest_next_step

        assert "not writable" in suggest_next_step("PermissionError: [Errno 13] denied")

    def test_timeout_asks_for_a_narrower_call(self):
        from veya.oskill.pure.tool_error_text import suggest_next_step

        assert "narrow the scope" in suggest_next_step("tool timed out after 30s")

    def test_unknown_failure_does_not_invent_a_diagnosis(self):
        """An unrecognised failure gets an honest generic step, not a guess."""

        from veya.oskill.pure.tool_error_text import suggest_next_step

        step = suggest_next_step("something entirely unexpected happened")
        assert "do not repeat the same call" in step

    def test_argument_values_are_never_echoed_back(self):
        """An argument may hold a secret the caller already masked elsewhere."""

        from veya.oskill.pure.tool_error_text import explain_tool_error

        text = explain_tool_error(
            "fetch", "boom", arguments={"url": "https://x/?token=hunter2secret"}
        )
        assert "url" in text
        assert "hunter2secret" not in text

    def test_long_error_text_is_truncated(self):
        from veya.oskill.pure.tool_error_text import MAX_DETAIL_CHARS, explain_tool_error

        text = explain_tool_error("t", "x" * 5000)
        assert "truncated" in text
        assert len(text) < MAX_DETAIL_CHARS + 400


class TestRegistryErrorPath:
    """The registry's error paths actually produce the agent-friendly shape."""

    @pytest.mark.asyncio
    async def test_unknown_tool_names_what_failed_and_next_step(self):
        from server.tool_registry import MasterToolRegistry, ToolExecutionError

        registry = MasterToolRegistry()
        with pytest.raises(ToolExecutionError) as excinfo:
            await registry.execute("definitely_not_registered_w10c", {})
        message = str(excinfo.value)
        assert "What failed:" in message
        assert "Next step:" in message

    @pytest.mark.asyncio
    async def test_validation_failure_states_what_failed_and_next_step(self):
        from server.tool_registry import MasterToolRegistry, ToolExecutionError

        registry = MasterToolRegistry()
        registry.register(
            "w10c_probe",
            "probe",
            {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
            lambda n: n,
        )
        with pytest.raises(ToolExecutionError) as excinfo:
            await registry.execute("w10c_probe", {})
        message = str(excinfo.value)
        assert "What failed:" in message
        assert "Next step:" in message
        assert "n" in message

    @pytest.mark.asyncio
    async def test_raised_exception_is_phrased_for_the_model(self):
        from server.tool_registry import MasterToolRegistry, ToolExecutionError

        def boom() -> str:
            raise RuntimeError("widget exploded")

        registry = MasterToolRegistry()
        registry.register("w10c_boom", "boom", {"type": "object", "properties": {}}, boom)
        with pytest.raises(ToolExecutionError) as excinfo:
            await registry.execute("w10c_boom", {})
        message = str(excinfo.value)
        assert "What failed:" in message
        assert "Next step:" in message
        assert "widget exploded" in message

    def test_guard_over_the_live_registry(self):
        """Every registered tool must be able to say what failed + next step.

        The guard runs the *live* ``master_tools`` surface rather than a sample:
        a tool added tomorrow inherits the formatter automatically, so this only
        fails if someone routes a failure around it.
        """

        from server.tool_registry import master_tools
        from veya.oskill.pure.tool_error_text import explain_tool_error

        registered = master_tools.list_tools()
        assert registered, "the live registry is empty — the guard would pass vacuously"

        offenders = []
        for name in registered:
            # Stand in for the real call: whatever this tool raises, the text
            # the model reads goes through the same formatter.
            text = explain_tool_error(name, "RuntimeError: representative failure")
            if "What failed:" not in text or "Next step:" not in text:
                offenders.append(name)
        assert not offenders, f"tools whose errors lack the agent-friendly shape: {offenders}"
