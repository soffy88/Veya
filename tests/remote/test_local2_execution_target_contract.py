from __future__ import annotations

import pytest

from veya.remote.tool_adapter import (
    BINDING_INDEX,
    EXECUTION_TARGETS,
    RemoteToolAdapterError,
    resolve_execution_target,
)

EXPECTED_TARGETS = [
    "NEW_ISOLATED_WORKTREE",
    "EXECUTION_WORKTREE",
    "EXISTING_WORKTREE",
    "CANONICAL_WORKTREE",
    "HOST",
]


def test_local2_public_tools_expose_execution_target() -> None:
    assert list(EXECUTION_TARGETS) == EXPECTED_TARGETS
    for name in ("shell.exec", "test.run", "build.run", "file.write", "file.patch"):
        properties = BINDING_INDEX[name].schema["properties"]
        assert properties["execution_target"] == {
            "type": "string",
            "enum": EXPECTED_TARGETS,
        }
        if name in {"shell.exec", "test.run", "build.run"}:
            assert properties["execution_domain"]["type"] == "string"


def test_execution_domain_does_not_select_checkout() -> None:
    # Read/execute observes the live tree; mutation stays isolated unless the
    # caller explicitly asks for canonical. The execution domain never changes
    # this choice on its own.
    assert (
        resolve_execution_target(
            "/data/soffy/projects/veya",
            None,
            "",
        )
        == "CANONICAL_WORKTREE"
    )
    assert (
        resolve_execution_target(
            "/data/soffy/projects/veya",
            None,
            "",
            intent="mutation",
        )
        == "NEW_ISOLATED_WORKTREE"
    )
    assert (
        resolve_execution_target(
            "/data/soffy/projects/veya",
            None,
            "CANONICAL_WORKTREE",
        )
        == "CANONICAL_WORKTREE"
    )


def test_empty_execution_target_still_auto_detects() -> None:
    # An omitted or blank target is not a mutation, so it resolves to the live
    # tree rather than to a throwaway worktree.
    assert resolve_execution_target("/data/soffy/projects/veya", None, "") == "CANONICAL_WORKTREE"
    assert (
        resolve_execution_target("/data/soffy/projects/veya", None, "   ") == "CANONICAL_WORKTREE"
    )


@pytest.mark.parametrize("target", EXPECTED_TARGETS)
def test_every_declared_target_round_trips(target: str) -> None:
    assert resolve_execution_target("/data/soffy/projects/veya", None, target) == target
    assert resolve_execution_target("/data/soffy/projects/veya", None, target.lower()) == target
    assert resolve_execution_target("/data/soffy/projects/veya", None, f"  {target} ") == target


@pytest.mark.parametrize(
    "target",
    [
        "INVALID_TARGET",
        "CANONICAL",
        "ISOLATED",
        "NEW_WORKTREE",
        "0",
        "true",
        "../CANONICAL_WORKTREE",
    ],
)
def test_unknown_execution_target_fails_closed(target: str) -> None:
    with pytest.raises(RemoteToolAdapterError) as excinfo:
        resolve_execution_target("/data/soffy/projects/veya", None, target)
    assert excinfo.value.code == "INVALID_ARGUMENT"
    assert "execution_target" in excinfo.value.message


def test_unknown_execution_target_never_degrades_to_canonical_or_host() -> None:
    for target in ("INVALID_TARGET", "CANONICAL", "HOSTED", "ISOLATED"):
        with pytest.raises(RemoteToolAdapterError):
            resolve_execution_target("/data/soffy/projects/veya", None, target)
