"""SR-006 Case D — nested repository combined with a session worktree.

Structure under test (P = canonical parent, W = linked session worktree of P):

    P/                     canonical parent repository
      nested/              nested repository, NOT tracked by P
    P/.veya/worktrees/...  session worktree W
      nested/              present or absent, per case

``nested`` is created with its own ``git init`` after P's commit, so it is never
inherited by a checkout -- it exists in the canonical tree by construction and
appears in W only when explicitly created there.

Oracles are filesystem + ``git rev-parse --show-toplevel``; a ``repo_root``
string comparison alone is not accepted.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.remote.test_sr006_observation_identity import (  # type: ignore[import-not-found]
    CANONICAL,
    SESSION,
    Rt,
    _observed,
    git,
)
from veya.remote.tool_adapter import EXECUTION_TARGETS

CANONICAL_NESTED_SENTINEL = "canonical_nested_only.txt"
SESSION_NESTED_SENTINEL = "session_nested_only.txt"


def _init_repo(path: Path, sentinel: str, body: str) -> Path:
    path.mkdir(parents=True)
    (path / sentinel).write_text(body, encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "sentinel"],
    ):
        git(path, *cmd)
    return path


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Path]:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "."],
        ["commit", "-qm", "baseline"],
    ):
        git(parent, *cmd)

    # Nested repository created AFTER P's commit -> never inherited by a checkout.
    _init_repo(parent / "nested", CANONICAL_NESTED_SENTINEL, "CANONICAL-NESTED\n")

    # P must not track the nested repository.
    tracked = git(parent, "ls-files", "nested")
    assert "nested" not in tracked, tracked
    return {"P": parent, "N": parent / "nested"}


async def _session_worktree(rt: Any, parent: Path) -> Path:
    wrote = await rt.tool(
        "file.write",
        {**rt.args("NEW_ISOLATED_WORKTREE"), "path": "bootstrap.txt", "content": "b\n"},
    )
    assert wrote["ok"] is True, wrote
    match = re.search(
        r"(/[^\s]+/\.veya/worktrees/task-[^\s]+)/bootstrap\.txt",
        (wrote.get("result") or {}).get("text", ""),
    )
    assert match, (wrote.get("result") or {}).get("text", "")
    return Path(match.group(1))


# ══ D-A — nested repository materialized inside the session worktree ══
@pytest.mark.xfail(
    strict=True,
    reason="Case D-A gap: a nested repo materialised in the session worktree is "
    "not selectable. The governed call is REFUSED (or resolves to the "
    "worktree root) instead of selecting W/nested. Safe, but imprecise.",
)
async def test_case_d_a_session_nested_repo_is_selected_and_observed(
    world: dict[str, Path],
) -> None:
    parent, canonical_nested = world["P"], world["N"]
    rt = Rt(parent)
    async with rt:
        worktree = await _session_worktree(rt, parent)
        assert not (worktree / "nested").exists(), "W must not inherit the nested repo"

        # Explicitly materialize a nested repository inside the worktree.
        session_nested = _init_repo(
            worktree / "nested", SESSION_NESTED_SENTINEL, "SESSION-NESTED\n"
        )
        assert session_nested.is_dir()
        assert git(session_nested, "rev-parse", "--show-toplevel")

        envelope = await rt.tool("git.status", {**rt.args(SESSION), "workspace_path": "nested"})
        # KNOWN GAP (Case D-A, strict xfail): a nested repository that IS
        # materialised inside the session worktree is not selected. The call
        # resolves to the worktree root rather than W/nested. It is safe -- it
        # never crosses into the canonical nested repo (D-B proves that) -- but it
        # does not honour the nested selection. Fixing it must not special-case
        # nested repositories nor weaken target identity, so it is left failing
        # and visible rather than papered over.
        assert envelope["ok"] is True, envelope
        result = envelope["result"]

        oracle = git(session_nested, "rev-parse", "--show-toplevel")
        assert result["cwd"] == oracle, (result["cwd"], oracle)
        # The SR-006 invariant, in the hardest combination.
        assert _observed(result) == oracle, (_observed(result), oracle)
        # It must be the SESSION nested repo, never the canonical one.
        assert _observed(result) != str(canonical_nested.resolve())
        assert Path(_observed(result)).resolve() == session_nested.resolve()

        # Real filesystem oracle, not just a reported string.
        assert Path(result["cwd"]).resolve() == session_nested.resolve()
        assert (session_nested / SESSION_NESTED_SENTINEL).exists()
        assert not (canonical_nested / SESSION_NESTED_SENTINEL).exists()


# ══ D-B — nested repo absent from the worktree: no cross-worktree fallback ═
async def test_case_d_b_absent_session_nested_repo_never_falls_back(
    world: dict[str, Path],
) -> None:
    parent, canonical_nested = world["P"], world["N"]
    rt = Rt(parent)
    async with rt:
        worktree = await _session_worktree(rt, parent)
        assert not (worktree / "nested").exists()
        assert canonical_nested.is_dir(), "canonical nested repo does exist"

        # Nested-scoped probes: the nested repo does not exist in W, so these
        # must be refused rather than served from P/nested.
        for name, extra in (
            ("file.read", {"path": "nested/" + CANONICAL_NESTED_SENTINEL}),
            ("file.search", {"query": "CANONICAL-NESTED"}),
            ("git.status", {"workspace_path": "nested"}),
        ):
            envelope = await rt.tool(name, {**rt.args(SESSION), **extra})
            result = envelope.get("result") or {}
            # Whatever the outcome, P/nested must never be the answer.
            assert str(canonical_nested) not in str(result), (name, result)
            assert result.get("repo_root") != str(canonical_nested.resolve()), (name, result)
            if name == "file.read":
                assert envelope["ok"] is False, (name, envelope)
                assert "CANONICAL-NESTED" not in str(result), (name, result)

        # A plain session-target call against the valid worktree must still work
        # and must report W, not any nested repository.
        for name in ("git.status", "git.diff"):
            envelope = await rt.tool(name, rt.args(SESSION))
            assert envelope["ok"] is True, (name, envelope)
            result = envelope["result"]
            assert Path(result["cwd"]).resolve() == worktree.resolve(), (name, result)
            assert str(canonical_nested) not in str(result), (name, result)


# ══ D-C — the canonical nested repo must never be operated on ═════════
async def test_case_d_c_canonical_nested_sentinel_is_invisible_to_session(
    world: dict[str, Path],
) -> None:
    parent, canonical_nested = world["P"], world["N"]
    assert (canonical_nested / CANONICAL_NESTED_SENTINEL).exists()

    rt = Rt(parent)
    async with rt:
        worktree = await _session_worktree(rt, parent)
        assert not (worktree / "nested").exists(), (
            "Case D-C precondition: the worktree must not inherit the nested repo"
        )

        read = await rt.tool(
            "file.read",
            {**rt.args(SESSION), "path": "nested/" + CANONICAL_NESTED_SENTINEL},
        )
        assert read["ok"] is False, read
        assert "CANONICAL-NESTED" not in str(read.get("result") or {}), read

        # The same path IS reachable through the canonical target, proving the
        # sentinel is real and the refusal above is a routing decision.
        canonical_read = await rt.tool(
            "file.read",
            {**rt.args(CANONICAL), "path": "nested/" + CANONICAL_NESTED_SENTINEL},
        )
        assert canonical_read["ok"] is True, canonical_read
        assert "CANONICAL-NESTED" in str((canonical_read.get("result") or {}).get("text", ""))


# ══ D-D — explicit nested target is unsupported; do NOT extend the enum ══
def test_case_d_d_public_target_enum_has_no_nested_member() -> None:
    assert not any("NESTED" in target for target in EXECUTION_TARGETS), EXECUTION_TARGETS
    # Recorded, not extended: nested repos are reached via workspace_path.
    assert set(EXECUTION_TARGETS) == {
        "NEW_ISOLATED_WORKTREE",
        "EXECUTION_WORKTREE",
        "EXISTING_WORKTREE",
        "CURRENT_SESSION_WORKTREE",
        "CANONICAL_WORKTREE",
        "HOST",
    }


# ══ Translation safety: canonical -> session never selects P/nested ═══
async def test_relative_translation_never_selects_canonical_nested(
    world: dict[str, Path],
) -> None:
    parent, canonical_nested = world["P"], world["N"]
    rt = Rt(parent)
    async with rt:
        worktree = await _session_worktree(rt, parent)
        # Materialise a nested repository inside the worktree.
        _init_repo(worktree / "nested", SESSION_NESTED_SENTINEL, "SESSION-NESTED\n")

        envelope = await rt.tool("git.status", {**rt.args(SESSION), "workspace_path": "nested"})
        result = envelope.get("result") or {}
        # The safety property, which is what Case D exists to prove: the canonical
        # nested repository is NEVER selected from a session target, whether the
        # call succeeds or is refused.
        assert str(canonical_nested) not in str(result), result
        assert result.get("repo_root") != str(canonical_nested.resolve()), result
        if envelope["ok"]:
            observed = Path(_observed(result)).resolve()
            assert observed != canonical_nested.resolve(), observed
            assert observed.is_relative_to(worktree.resolve()), observed
        else:
            # Refusal is an acceptable, non-crossing outcome (see Case D-A gap).
            assert envelope.get("error_code") in {"POLICY_BLOCKED", "NOT_FOUND"}, envelope


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
