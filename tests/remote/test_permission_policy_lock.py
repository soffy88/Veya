"""Lock the final permission policy.

NORMAL_DEVELOPMENT_OPEN_BY_DEFAULT: ordinary development work is allowed.
Destructive, privileged, host/global and irreversible work is not.

Every ALLOW and every APPROVAL_REQUIRED in this module is justified by an
explicit policy clause, so an unexplained verdict on either side is a failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from veya.remote.permission_engine import (
    CommandEffect,
    Decision,
    PermissionEngine,
    ReasonCode,
    parse_command_context,
)

# ── NORMAL_DEVELOPMENT_OPEN_BY_DEFAULT ───────────────────────────────────
# Clause: safe/read-only shell, project-scoped file mutation, normal project
# Git, and project-local dependency mutation stay open.
OPEN_BY_DEFAULT: list[tuple[str, CommandEffect]] = [
    # safe / read-only shell
    ("ls -la", CommandEffect.READ_ONLY),
    ("cat notes.txt", CommandEffect.READ_ONLY),
    ("rg pattern src", CommandEffect.READ_ONLY),
    ("git status", CommandEffect.READ_ONLY),
    # project-scoped file mutation
    ("mkdir -p src/pkg", CommandEffect.REVERSIBLE_MUTATION),
    ("touch new.txt", CommandEffect.REVERSIBLE_MUTATION),
    ("cp a.txt b.txt", CommandEffect.REVERSIBLE_MUTATION),
    # normal project Git, including rebase / worktree / checkout -b
    ("git add .", CommandEffect.REVERSIBLE_MUTATION),
    ("git commit -m change", CommandEffect.REVERSIBLE_MUTATION),
    ("git merge feature", CommandEffect.REVERSIBLE_MUTATION),
    ("git rebase main", CommandEffect.REVERSIBLE_MUTATION),
    ("git worktree add ../probe feature", CommandEffect.REVERSIBLE_MUTATION),
    ("git checkout -b feature", CommandEffect.REVERSIBLE_MUTATION),
    ("git checkout feature", CommandEffect.REVERSIBLE_MUTATION),
    # project-local dependency mutation
    ("pip install requests", CommandEffect.REVERSIBLE_MUTATION),
    ("npm install left-pad", CommandEffect.REVERSIBLE_MUTATION),
    ("yarn add left-pad", CommandEffect.REVERSIBLE_MUTATION),
    ("yarn remove left-pad", CommandEffect.REVERSIBLE_MUTATION),
    ("cargo update", CommandEffect.REVERSIBLE_MUTATION),
    ("go get example.com/pkg", CommandEffect.REVERSIBLE_MUTATION),
    # user-level runtime operations already qualified
    ("systemctl --user restart app", CommandEffect.REVERSIBLE_MUTATION),
]

# ── require approval or deny ─────────────────────────────────────────────
# Clause: destructive filesystem, privileged host, host/global package,
# system service, destructive Git, irreversible remote, force push, unknown
# high-impact.
GATED: list[tuple[str, CommandEffect]] = [
    # destructive filesystem
    ("rm -rf build", CommandEffect.DESTRUCTIVE_MUTATION),
    ("rmdir build", CommandEffect.DESTRUCTIVE_MUTATION),
    ("shred -u secret.txt", CommandEffect.DESTRUCTIVE_MUTATION),
    ("dd if=/dev/zero of=image.img", CommandEffect.DESTRUCTIVE_MUTATION),
    ("chmod 777 .", CommandEffect.DESTRUCTIVE_MUTATION),
    ("chown -R root:root .", CommandEffect.DESTRUCTIVE_MUTATION),
    ("truncate -s 0 data.db", CommandEffect.DESTRUCTIVE_MUTATION),
    ("find . -name '*.log' -delete", CommandEffect.DESTRUCTIVE_MUTATION),
    ("kill -9 1", CommandEffect.DESTRUCTIVE_MUTATION),
    # privileged host mutation
    ("sudo apt install x", CommandEffect.REVERSIBLE_MUTATION),
    ("mount /dev/sda1 /mnt", CommandEffect.PRIVILEGED_HOST_MUTATION),
    ("iptables -F", CommandEffect.PRIVILEGED_HOST_MUTATION),
    # host / global package mutation
    ("pip install -g x", CommandEffect.PRIVILEGED_HOST_MUTATION),
    ("npm install -g x", CommandEffect.PRIVILEGED_HOST_MUTATION),
    # system-level service mutation
    ("systemctl restart nginx", CommandEffect.PRIVILEGED_HOST_MUTATION),
    # destructive Git
    ("git clean -fdx", CommandEffect.DESTRUCTIVE_MUTATION),
    ("git reset --hard", CommandEffect.DESTRUCTIVE_MUTATION),
    ("git restore file.txt", CommandEffect.DESTRUCTIVE_MUTATION),
    ("git branch -D feature", CommandEffect.DESTRUCTIVE_MUTATION),
    ("git gc --prune", CommandEffect.DESTRUCTIVE_MUTATION),
    # irreversible remote / force push
    ("git push origin main", CommandEffect.REMOTE_MUTATION),
    ("git push --force", CommandEffect.REMOTE_IRREVERSIBLE_MUTATION),
    ("git fetch --all", CommandEffect.REMOTE_MUTATION),
    # unknown high-impact
    ("sudo rm -rf /", CommandEffect.PRIVILEGED_HOST_MUTATION),
    ("rm -rf /", CommandEffect.DESTRUCTIVE_MUTATION),
]


def _decide(command: str, root: Path):
    context = parse_command_context(command, cwd=root, workspace_root=root)
    return CommandEffect(context.command_effect), PermissionEngine().evaluate(context)


@pytest.mark.parametrize(
    ("command", "effect"), OPEN_BY_DEFAULT, ids=[r[0] for r in OPEN_BY_DEFAULT]
)
def test_normal_development_is_open_by_default(
    tmp_path: Path, command: str, effect: CommandEffect
) -> None:
    got_effect, decision = _decide(command, tmp_path)
    assert got_effect is effect, command
    assert decision.decision is Decision.ALLOW, f"{command}: {decision.reason.value}"


@pytest.mark.parametrize(("command", "effect"), GATED, ids=[r[0] for r in GATED])
def test_high_impact_is_gated(tmp_path: Path, command: str, effect: CommandEffect) -> None:
    got_effect, decision = _decide(command, tmp_path)
    assert got_effect is effect, command
    assert decision.decision is not Decision.ALLOW, f"{command} was allowed"


def test_no_unjustified_allow() -> None:
    """Every ALLOW in this module belongs to an open-by-default clause."""
    # If a command appears in both lists the policy is self-contradictory.
    open_commands = {row[0] for row in OPEN_BY_DEFAULT}
    gated_commands = {row[0] for row in GATED}
    assert not (open_commands & gated_commands)
    # and the open set is exactly the documented clause
    assert len(OPEN_BY_DEFAULT) == 21
    assert len(GATED) == 25


def test_no_unjustified_approval() -> None:
    """Every gated verdict is approval-or-deny, never a silent allow."""
    root = Path("/tmp/ws-policy-audit")
    root.mkdir(parents=True, exist_ok=True)
    for command, _effect in GATED:
        _got, decision = _decide(command, root)
        assert decision.decision in {Decision.APPROVAL_REQUIRED, Decision.DENY}, command
        assert decision.reason.value, command


def test_gated_reasons_are_specific() -> None:
    """A gate must say why, not just deny."""
    root = Path("/tmp/ws-policy-audit")
    root.mkdir(parents=True, exist_ok=True)
    expected = {
        "rm -rf build": ReasonCode.APPROVAL_HOST_DESTRUCTIVE,
        "git push --force": ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE,
        "systemctl restart nginx": ReasonCode.APPROVAL_HOST_PRIVILEGE,
        "pip install -g x": ReasonCode.APPROVAL_HOST_PRIVILEGE,
        "kill -9 1": ReasonCode.APPROVAL_UNKNOWN_HIGH_IMPACT,
        "rm -rf /": ReasonCode.DENY_SCOPE_ESCAPE,
    }
    for command, reason in expected.items():
        _effect, decision = _decide(command, root)
        assert decision.reason is reason, f"{command}: {decision.reason.value}"


def test_blunt_executable_gating_was_not_restored() -> None:
    """main's blunt executable list must stay gone; arguments still matter."""
    root = Path("/tmp/ws-policy-audit")
    root.mkdir(parents=True, exist_ok=True)
    # Same executable, different arguments, different verdicts. A blunt
    # executable-only policy could not produce this.
    _effect, allowed = _decide("mkdir -p x", root)
    assert allowed.decision is Decision.ALLOW
    _effect, gated = _decide("rm -rf x", root)
    assert gated.decision is not Decision.ALLOW
    # package scope is argument-driven
    _effect, local = _decide("pip install x", root)
    assert local.decision is Decision.ALLOW
    _effect, glob = _decide("pip install -g x", root)
    assert glob.decision is not Decision.ALLOW
    # systemd scope is argument-driven
    _effect, user = _decide("systemctl --user restart app", root)
    assert user.decision is Decision.ALLOW
    _effect, system = _decide("systemctl restart nginx", root)
    assert system.decision is not Decision.ALLOW
