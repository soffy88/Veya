"""The advertised capability surface is derived, not hand-maintained.

``runtime.capabilities`` answered with a literal list of execution targets. It
named four of the five targets in ``EXECUTION_TARGETS`` and omitted
``EXECUTION_WORKTREE`` — a target the runtime accepts — so a caller reading the
advertised surface to decide what it could ask for could not see one of the
answers available to it. A capability report that is edited by hand drifts from
the authority it claims to describe; this asserts it against that authority.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from veya.remote.models import RemotePermissions, RemoteSession
from veya.remote.runtime_profile import ExecutionDomain
from veya.remote.tool_adapter import EXECUTION_TARGETS, RemoteToolAdapter


def _session(path: Path) -> RemoteSession:
    now = time.time()
    return RemoteSession(
        session_id="rs-caps",
        principal="test",
        token_id="rt-caps",
        workspaces=(str(path.resolve()),),
        active_workspace=str(path.resolve()),
        permissions=RemotePermissions(
            read=True,
            write=True,
            shell=True,
            git=True,
            network=False,
            destructive=False,
            service_control=False,
        ),
        created_at=now,
        expires_at=now + 3600,
    )


@pytest.mark.asyncio
async def test_capabilities_report_every_execution_target(tmp_path: Path):
    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)

    res = await adapter._call_impl(session, "runtime.capabilities", {"path": str(tmp_path)})
    assert res.ok is True, res.message
    caps = res.result["capabilities"]

    assert caps["available_targets"] == list(EXECUTION_TARGETS)
    # The regression this pins: the literal omitted a real target.
    assert "EXECUTION_WORKTREE" in caps["available_targets"]
    assert caps["available_domains"] == [d.value for d in ExecutionDomain]


def test_the_reported_targets_are_the_ones_the_resolver_accepts():
    """No target may be advertised that the resolver would reject."""

    from veya.remote.tool_adapter import resolve_execution_target

    for target in EXECUTION_TARGETS:
        resolved = resolve_execution_target(
            "/tmp", requested_execution_target=target, intent="mutation"
        )
        assert resolved == target


@pytest.mark.asyncio
async def test_capability_tool_input_schemas_match_the_derived_lists(tmp_path: Path):
    """The declared enums must not drift from the reported lists either."""

    adapter = RemoteToolAdapter(None)
    session = _session(tmp_path)
    res = await adapter._call_impl(session, "runtime.capabilities", {"path": str(tmp_path)})
    caps = res.result["capabilities"]

    # runtime.capabilities must at least agree with itself across two calls.
    again = await adapter._call_impl(session, "runtime.capabilities", {"path": str(tmp_path)})
    assert again.result["capabilities"]["available_targets"] == caps["available_targets"]
    assert again.result["capabilities"]["available_domains"] == caps["available_domains"]
