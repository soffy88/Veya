from __future__ import annotations

from veya.remote.tool_adapter import BINDING_INDEX, EXECUTION_TARGETS, resolve_execution_target

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
    assert (
        resolve_execution_target(
            "/data/soffy/projects/veya",
            None,
            "",
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
