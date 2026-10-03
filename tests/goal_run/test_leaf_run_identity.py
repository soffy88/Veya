"""A leaf's run identity is reproducible and collision-free.

Both are needed. Reproducible, so a retried or resumed leaf finds its own brief,
artifacts and understand chain. Collision-free, so two different instructions
never share a run directory.

The identity used to be the instruction truncated to 30 characters, which is
neither: templated work shares prefixes constantly, so the second instruction
overwrote the first's brief.md and inherited its artifacts. The disk carried a
single ``leaf_Implement_directly_in_the_cano`` entry standing for every
instruction that began that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.goal_run.leaf import _run_identity, execute_leaf

# Each pair shares a 30+ character prefix, which is exactly what the old
# identity truncated to. Built from a shared base so the precondition cannot
# drift when the strings are edited.
_BASE = "Add a regression test that proves the executor cannot bypass"
assert len(_BASE) >= 30

PREFIX_SHARING_PAIRS = [
    (_BASE + " admission", _BASE + " the registry"),
    (
        "Investigate and fix the failing flaky test in the runtime suite now",
        "Investigate and fix the failing flaky test in the goal_run suite later",
    ),
    (
        "修复该缺陷并补充针对该模块的回归测试验证全部通过，其路径位于 server 目录下",
        "修复该缺陷并补充针对该模块的回归测试验证全部通过，其路径位于 veya 目录下",
    ),
]


@pytest.mark.parametrize(("first", "second"), PREFIX_SHARING_PAIRS)
def test_run_identity_is_collision_free_for_shared_prefixes(first: str, second: str):
    # The precondition that made this a real defect, stated rather than assumed:
    # these instructions are indistinguishable under the old 30-char identity.
    assert first[:30] == second[:30]
    assert first != second

    assert _run_identity(first) != _run_identity(second)


def test_run_identity_is_reproducible():
    instruction = _BASE + " admission"
    assert _run_identity(instruction) == _run_identity(instruction)
    # A trailing space is a different instruction, so it gets a different run.
    assert _run_identity(instruction) != _run_identity(instruction + " ")


def test_run_identity_is_ascii_safe_and_bounded():
    for instruction in ("a", "x" * 400, "中文" * 200, "weird/../path\tand\nnewlines", ""):
        identity = _run_identity(instruction)
        assert identity.isascii(), identity
        assert len(identity) <= 80, identity
        # No separators or traversal left in a name that becomes a directory.
        assert not set("/\\ \t\n") & set(identity), identity


@pytest.mark.asyncio
async def test_prefix_sharing_instructions_keep_separate_run_dirs(tmp_path: Path):
    """End to end: the second brief must not overwrite the first."""

    first, second = PREFIX_SHARING_PAIRS[0]
    for instruction in (first, second):
        # assignee="" stops at the admission gate, which is *after* the brief is
        # persisted — which is exactly the write that used to collide.
        result = await execute_leaf(str(tmp_path), instruction, assignee="")
        assert result.status == "blocked"
        assert "no executor requested" in (result.block_reason or "")

    runs = tmp_path / ".veya-project" / "runs"
    briefs = {
        p.name: (p / "brief.md").read_text(encoding="utf-8") for p in runs.iterdir() if p.is_dir()
    }
    assert len(briefs) == 2, f"expected two run directories, got {sorted(briefs)}"
    joined = "\n".join(briefs.values())
    assert first in joined
    assert second in joined


@pytest.mark.asyncio
async def test_the_same_instruction_reuses_its_run_dir(tmp_path: Path):
    instruction = "Add a regression test that proves the executor cannot bypass admission"
    for _ in range(2):
        await execute_leaf(str(tmp_path), instruction, assignee="")

    runs = tmp_path / ".veya-project" / "runs"
    assert len([p for p in runs.iterdir() if p.is_dir()]) == 1
