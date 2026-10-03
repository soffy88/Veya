"""Scope-model and permission-closure regression cover.

Two prior defects motivated this file, both of which were *under*-gating rather
than over-gating, and were found while adding the USER/HOST scopes:

1. ``tee`` was unclassified.  It fell through to the unknown-executable branch,
   which returns no target paths, so scope could not be resolved and
   ``tee /etc/systemd/system/x.service`` resolved to USER scope and was ALLOWed.
2. ``reboot`` / ``shutdown`` / ``poweroff`` / ``halt`` were also unclassified,
   so a bare ``reboot`` was an ALLOWed "reversible project mutation".
3. ``DENY_PATH_TRAVERSAL`` existed as a reason code but nothing raised it, so
   ``cat ../../../../etc/passwd`` was an ordinary ALLOWed read.

Plus the scope model itself: PROJECT / USER / HOST / REMOTE, with
``systemctl --user`` as USER scope, HOST administration executable but
APPROVAL_REQUIRED by default, and exactly one trusted-admin policy that never
downgrades destructive, credential, remote-irreversible, or any DENY outcome.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from veya.remote.permission_engine import (
    TRUSTED_ADMIN_ENV,
    Decision,
    PermissionEngine,
    ReasonCode,
    Scope,
    parse_command_context,
    trusted_admin_enabled,
)

WS = Path("/data/soffy/projects/veya")
HOME = Path.home()
USER_DROPIN = HOME / ".config/systemd/user/veya-remote-mcp.service.d/99-probe.conf"


def _decide(command: str, *, admin: bool = False, cwd: Path = WS):
    os.environ.pop(TRUSTED_ADMIN_ENV, None)
    if admin:
        os.environ[TRUSTED_ADMIN_ENV] = "enabled"
    try:
        context = parse_command_context(command, cwd=cwd, workspace_root=WS)
        return PermissionEngine().evaluate(context), context
    finally:
        os.environ.pop(TRUSTED_ADMIN_ENV, None)


# ── scope model ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "scope"),
    [
        ("cat README.md", Scope.PROJECT),
        ("touch newfile", Scope.PROJECT),
        (f"tee {USER_DROPIN}", Scope.USER),
        (f"cat {USER_DROPIN}", Scope.USER),
        ("systemctl --user daemon-reload", Scope.USER),
        ("systemctl --user restart veya-remote-mcp.service", Scope.USER),
        ("tee /etc/systemd/system/x.service", Scope.HOST),
        ("install -m755 x /usr/local/bin/x", Scope.HOST),
        ("install -m755 x /opt/veya/bin/x", Scope.HOST),
        ("tee /var/lib/veya/state.json", Scope.HOST),
        ("sudo systemctl daemon-reload", Scope.HOST),
        ("systemctl restart sshd", Scope.HOST),
        ("git push origin main", Scope.REMOTE),
    ],
)
def test_scope_model_is_project_user_host_remote(command: str, scope: Scope) -> None:
    decision, _ = _decide(command)
    assert decision.scope is scope, f"{command}: {decision.scope} {decision.reason}"


def test_user_systemd_dropin_is_not_scope_escape() -> None:
    """The reported bug: legitimate user runtime was DENY_SCOPE_ESCAPE."""
    decision, _ = _decide(f"tee {USER_DROPIN}")
    assert decision.reason is not ReasonCode.DENY_SCOPE_ESCAPE
    assert decision.decision is Decision.ALLOW
    assert decision.scope is Scope.USER


@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user daemon-reload",
        "systemctl --user restart veya-remote-mcp.service",
        "systemctl --user start veya-remote-mcp.service",
        "systemctl --user stop veya-remote-mcp.service",
        "systemctl --user status veya-remote-mcp.service",
        "systemctl --user show veya-remote-mcp.service",
    ],
)
def test_systemctl_user_is_user_scope_and_allowed(command: str) -> None:
    decision, _ = _decide(command)
    assert decision.scope is Scope.USER
    assert decision.decision is Decision.ALLOW


def test_systemctl_user_inspection_is_read_only() -> None:
    """status/show are inspection, not mutation."""
    for command in ("systemctl --user status x", "systemctl --user show x"):
        _, context = _decide(command)
        assert context.command_effect == "READ_ONLY", command
        assert context.filesystem_effect == "read", command


# ── boundaries that must stay gated ────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "decision"),
    [
        ("tee /etc/systemd/system/x.service", Decision.APPROVAL_REQUIRED),
        ("install -m755 x /usr/local/bin/x", Decision.APPROVAL_REQUIRED),
        ("install -m755 x /opt/veya/bin/x", Decision.APPROVAL_REQUIRED),
        ("sudo systemctl restart veya-remote-mcp", Decision.APPROVAL_REQUIRED),
        ("systemctl restart sshd", Decision.APPROVAL_REQUIRED),
        ("sudo systemctl daemon-reload", Decision.APPROVAL_REQUIRED),
        (f"cat {HOME / '.ssh' / 'id_rsa'}", Decision.DENY),
        (f"cat {HOME / '.aws' / 'credentials'}", Decision.DENY),
        ("cat /etc/shadow", Decision.APPROVAL_REQUIRED),
        ("cat /etc/sudoers", Decision.APPROVAL_REQUIRED),
    ],
)
def test_host_and_credential_boundaries_stay_gated(command: str, decision: Decision) -> None:
    got, _ = _decide(command)
    assert got.decision is decision, f"{command}: {got.decision} {got.reason}"


def test_workspace_dotdot_escape_is_denied() -> None:
    """DENY_PATH_TRAVERSAL was defined but never raised."""
    decision, _ = _decide("cat ../../../../etc/passwd")
    assert decision.decision is Decision.DENY
    assert decision.reason is ReasonCode.DENY_PATH_TRAVERSAL


def test_non_escaping_dotdot_stays_allowed() -> None:
    """Ordinary relative navigation inside the repo is normal work."""
    decision, _ = _decide("cat ../AGENTS.md", cwd=WS / "tests")
    assert decision.decision is Decision.ALLOW
    assert decision.scope is Scope.PROJECT


def test_power_state_is_gated_not_a_project_mutation() -> None:
    """A bare reboot used to be an ALLOWed reversible project mutation."""
    for command in ("reboot", "poweroff", "halt", "shutdown -h now", "systemctl poweroff"):
        decision, _ = _decide(command)
        assert decision.decision is not Decision.ALLOW, command


# ── trusted admin: exactly one policy ──────────────────────────────────


def test_trusted_admin_requires_exact_opt_in() -> None:
    os.environ.pop(TRUSTED_ADMIN_ENV, None)
    assert trusted_admin_enabled() is False
    for value in ("1", "true", "yes", "on", "enabled2", ""):
        os.environ[TRUSTED_ADMIN_ENV] = value
        try:
            assert trusted_admin_enabled() is False, value
        finally:
            os.environ.pop(TRUSTED_ADMIN_ENV, None)
    os.environ[TRUSTED_ADMIN_ENV] = "enabled"
    try:
        assert trusted_admin_enabled() is True
    finally:
        os.environ.pop(TRUSTED_ADMIN_ENV, None)


@pytest.mark.parametrize(
    "command",
    [
        "sudo systemctl daemon-reload",
        "sudo systemctl restart veya-remote-mcp",
        "sudo systemctl status x",
        "sudo install -m755 x /usr/local/bin/x",
        "tee /etc/systemd/system/veya-t.service.d/50-x.conf",
        "sudo mkdir -p /etc/veya",
    ],
)
def test_trusted_admin_allows_normal_host_administration(command: str) -> None:
    decision, _ = _decide(command, admin=True)
    assert decision.decision is Decision.ALLOW, command
    assert decision.reason is ReasonCode.ALLOW_TRUSTED_ADMIN


@pytest.mark.parametrize(
    "command",
    [
        "sudo rm -rf /",
        "sudo rm -rf /var",
        "mkfs.ext4 /dev/sda1",
        "sudo dd if=/dev/zero of=/dev/sda",
        "reboot",
        "sudo shutdown -h now",
        "sudo iptables -F",
        "cat /etc/shadow",
        "cat /etc/sudoers",
        f"cat {HOME / '.ssh' / 'id_rsa'}",
        f"cat {HOME / '.aws' / 'credentials'}",
    ],
)
def test_trusted_admin_never_downgrades_destructive_or_credentials(command: str) -> None:
    decision, _ = _decide(command, admin=True)
    assert decision.decision is not Decision.ALLOW, f"{command} -> {decision.reason}"
    assert decision.reason is not ReasonCode.ALLOW_TRUSTED_ADMIN


def test_host_admin_is_not_scope_escape() -> None:
    """HOST != DENY: administration is executable, it just needs approval."""
    for command in (
        "sudo systemctl daemon-reload",
        "sudo systemctl restart veya-remote-mcp",
        "tee /etc/systemd/system/veya-t.service.d/50-x.conf",
    ):
        decision, _ = _decide(command)
        assert decision.decision is Decision.APPROVAL_REQUIRED, command
        assert decision.reason is not ReasonCode.DENY_SCOPE_ESCAPE, command
        assert decision.scope is Scope.HOST, command


# ── the holes this work closed ─────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "tee /etc/systemd/system/x.service",
        "tee /etc/systemd/system/veya-t.service.d/50-x.conf",
        "tee /var/lib/veya/state.json",
        "tee /opt/veya/config.json",
    ],
)
def test_tee_resolves_targets_so_host_writes_are_gated(command: str) -> None:
    """`tee` used to return no targets, so scope never resolved to HOST."""
    _, context = _decide(command)
    assert context.target_paths, "tee must resolve its operands"
    decision, _ = _decide(command)
    assert decision.scope is Scope.HOST
    assert decision.decision is not Decision.ALLOW


def test_tee_in_project_and_user_scope_still_allowed() -> None:
    assert _decide("tee out.txt")[0].decision is Decision.ALLOW
    assert _decide(f"tee {USER_DROPIN}")[0].decision is Decision.ALLOW


# ── shell composition must not hide a target ──────────────────────────
#
# A classifier that reads only the first token sees nothing on the right of a
# pipe and nothing after `>`.  Every one of these was ALLOW before composition
# analysis existed, including `cat /etc/shadow | head -1`.


@pytest.mark.parametrize(
    "command",
    [
        "tee /etc/veya-probe.conf",
        "printf x | tee /etc/veya-probe.conf",
        "echo hi > /etc/veya-probe.conf",
        "echo hi >> /etc/veya-probe.conf",
        "cat a 2> /etc/veya-probe.conf",
        "mkdir -p /tmp/a && cat /tmp/a > /etc/veya-probe.conf",
        "true ; tee /etc/veya-probe.conf",
        "tee /etc/veya-probe.conf | cat",
    ],
)
def test_composed_command_cannot_hide_a_host_write(command: str) -> None:
    decision, _ = _decide(command)
    assert decision.decision is not Decision.ALLOW, f"{command}: {decision.reason}"
    assert decision.scope is Scope.HOST, f"{command}: {decision.scope}"


def test_pipe_cannot_exfiltrate_a_credential_file() -> None:
    decision, _ = _decide("cat /etc/shadow | head -1")
    assert decision.decision is Decision.APPROVAL_REQUIRED
    assert decision.reason is ReasonCode.APPROVAL_SECURITY_BOUNDARY


@pytest.mark.parametrize(
    "command",
    [
        "ls | grep foo",
        "cat README.md | head -3",
        "git status | cat",
        "echo hi > out.txt",
        "printf x > ~/.config/systemd/user/probe.conf",
        "tee ~/.config/systemd/user/probe.conf && systemctl --user daemon-reload",
        "mkdir -p out && cat README.md > out/copy.md",
    ],
)
def test_ordinary_composition_stays_allowed(command: str) -> None:
    """Composition analysis must not turn routine work into approvals."""
    decision, _ = _decide(command)
    assert decision.decision is Decision.ALLOW, f"{command}: {decision.reason}"
