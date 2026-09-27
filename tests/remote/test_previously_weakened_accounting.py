"""Accounting for every command the engine unification had weakened.

Before the fix, 31 of the 59 sampled commands that canonical main required
approval for were reported as safe.  This module pins each one to an explicit,
justified post-fix outcome so that a regression cannot re-open silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.coding.command_runner import command_requires_approval
from veya.remote.permission_engine import (
    CommandEffect,
    Decision,
    PermissionEngine,
    parse_command_context,
)

# (command, expected effect, expected decision, justification)
# "GATED" = the engine now requires approval.
# "OPEN_BY_POLICY" = canonical policy intentionally keeps it open; the reason is
# recorded so the decision is a documented choice, not a leftover.
ACCOUNTED: list[tuple[str, CommandEffect, Decision, str]] = [
    # ── destructive executables: all gated ──────────────────────────────
    (
        "rm -rf /",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.DENY,
        "GATED: host root is denied outright",
    ),
    ("rm file.txt", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("rm", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("rmdir d", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("shred s.bin", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("unlink f", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    (
        "dd if=/dev/zero of=/dev/sda",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
        "GATED",
    ),
    ("dd of=/dev/sda", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("mkfs.ext4 /dev/sda", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    (
        "chmod 777 /",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.DENY,
        "GATED: host root is denied outright",
    ),
    (
        "chmod -R 000 /",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.DENY,
        "GATED: host root is denied outright",
    ),
    (
        "chown -R root:root /",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.DENY,
        "GATED: host root is denied outright",
    ),
    # ── Git: destructive and remote gated ───────────────────────────────
    ("git clean -fd", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("git fetch --all", CommandEffect.REMOTE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("git gc --prune", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("git pull --rebase", CommandEffect.REMOTE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("git push origin main", CommandEffect.REMOTE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    (
        "git push --force",
        CommandEffect.REMOTE_IRREVERSIBLE_MUTATION,
        Decision.APPROVAL_REQUIRED,
        "GATED",
    ),
    ("git reset --hard", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("git restore f", CommandEffect.DESTRUCTIVE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    ("git clone url", CommandEffect.REMOTE_MUTATION, Decision.APPROVAL_REQUIRED, "GATED"),
    (
        "git branch -D x",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
        "GATED",
    ),
    (
        "git branch --force x",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
        "GATED",
    ),
    (
        "git checkout --orphan x",
        CommandEffect.DESTRUCTIVE_MUTATION,
        Decision.APPROVAL_REQUIRED,
        "GATED",
    ),
    # ── open by canonical policy, with the qualified gate that requires it ──
    (
        "git rebase main",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
        "OPEN_BY_POLICY: qualified Q1_L0/P0-C/D gate requires project Git to stay open",
    ),
    (
        "git checkout -b x",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
        "OPEN_BY_POLICY: branch creation is a reversible project mutation",
    ),
    (
        "pip install requests",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
        "OPEN_BY_POLICY: project-local dependency install is not host-privileged",
    ),
    (
        "npm install x",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
        "OPEN_BY_POLICY: project-local dependency install is not host-privileged",
    ),
    (
        "npm add x",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
        "OPEN_BY_POLICY: project-local dependency install is not host-privileged",
    ),
    (
        "yarn remove x",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
        "OPEN_BY_POLICY: project-local dependency removal is reversible",
    ),
    (
        "cargo update",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
        "OPEN_BY_POLICY: project-local lockfile update is not host-privileged",
    ),
    (
        "go get -u",
        CommandEffect.REVERSIBLE_MUTATION,
        Decision.ALLOW,
        "OPEN_BY_POLICY: project-local module update is not host-privileged",
    ),
]

GATED_COMMANDS = [row for row in ACCOUNTED if row[3].startswith("GATED")]
OPEN_COMMANDS = [row for row in ACCOUNTED if row[3].startswith("OPEN_BY_POLICY")]

# Every command main's old runner policy required approval for, minus the ones
# that were already gated before the unification. This is the closure's list of
# 31 previously weakened commands.
PREVIOUSLY_WEAKENED = (
    "rm -rf /",
    "rm file.txt",
    "rm",
    "rmdir d",
    "shred s.bin",
    "unlink f",
    "dd if=/dev/zero of=/dev/sda",
    "mkfs.ext4 /dev/sda",
    "chmod 777 /",
    "chmod -R 000 /",
    "chown -R root:root /",
    "git clean -fd",
    "git fetch --all",
    "git gc --prune",
    "git pull --rebase",
    "git push origin main",
    "git rebase main",
    "git reset --hard",
    "git restore f",
    "git clone url",
    "git checkout -b x",
    "git checkout --orphan x",
    "git branch -D x",
    "git branch --force x",
    "pip install requests",
    "npm install x",
    "npm add x",
    "yarn remove x",
    "cargo update",
    "go get -u",
    "dd of=/dev/sda",
)


def test_every_previously_weakened_command_is_accounted_for() -> None:
    assert len(PREVIOUSLY_WEAKENED) == 31
    accounted = {row[0] for row in ACCOUNTED}
    missing = sorted(set(PREVIOUSLY_WEAKENED) - accounted)
    assert not missing, f"unaccounted previously-weakened commands: {missing}"


@pytest.mark.parametrize(
    ("command", "effect", "decision", "why"),
    ACCOUNTED,
    ids=[row[0] for row in ACCOUNTED],
)
def test_previously_weakened_command_outcome_is_stable(
    tmp_path: Path, command: str, effect: CommandEffect, decision: Decision, why: str
) -> None:
    context = parse_command_context(command, cwd=tmp_path, workspace_root=tmp_path)
    got = PermissionEngine().evaluate(context)
    assert CommandEffect(context.command_effect) is effect, f"{command}: {why}"
    assert got.decision is decision, f"{command}: {why}"
    # the compatibility adapter must agree
    assert command_requires_approval(command.split()) is (decision is not Decision.ALLOW)


def test_no_unexpected_allows_among_gated_commands(tmp_path: Path) -> None:
    """Every command the closure classified as gated must still be gated."""
    unexpected: list[str] = []
    for command, _effect, decision, _why in GATED_COMMANDS:
        context = parse_command_context(command, cwd=tmp_path, workspace_root=tmp_path)
        got = PermissionEngine().evaluate(context)
        if got.decision is not decision:
            unexpected.append(f"{command}: {got.decision} != {decision}")
    assert not unexpected, unexpected
    assert len(GATED_COMMANDS) == 24
    assert len(OPEN_COMMANDS) == 8
    # every one of the 31 is covered; the matrix may also pin extra
    # commands (e.g. force-push) that main already gated.
    assert len(GATED_COMMANDS) + len(OPEN_COMMANDS) >= len(PREVIOUSLY_WEAKENED)
    assert {row[0] for row in ACCOUNTED} >= set(PREVIOUSLY_WEAKENED)


def test_open_by_policy_commands_have_a_written_justification() -> None:
    """An ALLOW must be a documented decision, never an unexplained leftover."""
    for _command, _effect, decision, why in OPEN_COMMANDS:
        assert decision is Decision.ALLOW
        assert why.startswith("OPEN_BY_POLICY: ")
        assert len(why) > len("OPEN_BY_POLICY: ")
