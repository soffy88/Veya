"""The backend facade answers in one shape, and says when it cut something.

``BackendRegistry.run`` had three branches and three different response key sets.
Output and error were bounded by bare ``[:4000]`` / ``[:2000]`` slices repeated
per branch, so a clipped answer was indistinguishable from a short one: there
was no marker in the text and no flag in the payload. A caller could not tell
that it was reading a prefix.

The remote adapter's ``_limit`` already answered this with a marker plus a
``truncated`` flag. This is the same contract for the backend facade, and every
branch now emits the same keys.
"""

from __future__ import annotations

import shutil
import sys

import pytest

from server.backends import (
    ERROR_LIMIT,
    OUTPUT_LIMIT,
    TRUNCATION_MARKER,
    BackendRegistry,
    _capped,
    _result,
)

SHAPE = {"ok", "backend", "output", "output_truncated", "error", "error_truncated", "duration_s"}


def _stub_coordinator(monkeypatch, output: str) -> None:
    """Swap the coordinator for a stub without touching the shared singleton.

    ``_run_builtin`` does a function-local ``from server.coordinator_master
    import master_coordinator``, so replacing the module attribute is enough and
    the real coordinator's own ``chat_stream`` is never overwritten. Mutating
    the singleton instead leaked into a later suite member:
    test_stream_pump_mirrors_events_for_logged_in_user passed alone and failed
    whenever this module ran first.
    """

    class _Stub:
        async def chat_stream(self, prompt: str, model: str | None = None):
            return {"status": "success", "output": output}

    import server.coordinator_master as coordinator_module

    monkeypatch.setattr(coordinator_module, "master_coordinator", _Stub())


def _available_command() -> list[str]:
    assert shutil.which(sys.executable)
    return [sys.executable]


def test_result_carries_the_full_shape():
    body = _result(ok=True, backend="x", output="hi", error="")
    assert set(body) >= SHAPE, sorted(SHAPE - set(body))


def test_capped_marks_and_reports_its_own_truncation():
    text, truncated = _capped("A" * (OUTPUT_LIMIT + 100), OUTPUT_LIMIT)
    assert truncated is True
    assert TRUNCATION_MARKER.strip() in text
    assert len(text) > OUTPUT_LIMIT  # the limit is on content, not on the marker

    short, not_truncated = _capped("small", OUTPUT_LIMIT)
    assert not_truncated is False
    assert short == "small"
    assert TRUNCATION_MARKER not in short


def test_capped_handles_empty_and_none_without_inventing_content():
    assert _capped(None, OUTPUT_LIMIT) == ("", False)
    assert _capped("", OUTPUT_LIMIT) == ("", False)


def test_the_two_caps_are_distinct_and_named():
    # They used to be two magic numbers repeated in three branches.
    assert OUTPUT_LIMIT != ERROR_LIMIT


@pytest.mark.asyncio
async def test_every_run_branch_emits_the_same_keys():
    """Driven through run() so a branch cannot quietly reintroduce its own shape."""

    registry = BackendRegistry()

    # cli refusal branches
    for name, expected_code in (
        ("claude", "EXECUTOR_NOT_ADMITTED"),
        ("opencode", "EXECUTION_FACADE_CLOSED"),
        ("hicode", "EXECUTOR_RETIRED"),
    ):
        registry.register(name, "cli", command=_available_command())
        result = await registry.run(name, "hi", timeout_s=10)
        assert set(result) >= SHAPE, f"{name}: missing {sorted(SHAPE - set(result))}"
        assert result["error_code"] == expected_code
        assert result["output_truncated"] is False


@pytest.mark.asyncio
async def test_a_long_answer_is_marked_rather_than_silently_clipped(monkeypatch):
    """Driven through run() so a branch cannot bypass the shared capping."""

    _stub_coordinator(monkeypatch, "A" * (OUTPUT_LIMIT + 500))
    registry = BackendRegistry()

    result = await registry.run("master", "hi", timeout_s=10)

    assert result["ok"] is True
    assert result["output_truncated"] is True
    assert TRUNCATION_MARKER.strip() in result["output"]
    # The prefix is still the real answer, not a placeholder.
    assert result["output"].startswith("A" * 100)


@pytest.mark.asyncio
async def test_a_short_answer_is_not_flagged(monkeypatch):
    _stub_coordinator(monkeypatch, "all good")
    result = await BackendRegistry().run("master", "hi", timeout_s=10)

    assert result["output_truncated"] is False
    assert TRUNCATION_MARKER not in result["output"]
    assert result["output"] == "all good"
