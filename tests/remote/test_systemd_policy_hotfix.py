from __future__ import annotations

from pathlib import Path

import pytest

from tests.remote.test_l0_capability_expansion import _session
from veya.remote import action_gateway, tool_adapter
from veya.remote.action_gateway import (
    ActionCategory,
    classify_action,
    parse_systemctl_command,
    systemctl_read_options_valid,
)


@pytest.mark.parametrize(
    "command",
    [
        "systemctl --user cat veya-remote-mcp.service",
        "systemctl --user show veya-remote-mcp.service -p ProtectSystem -p ReadWritePaths",
        "systemctl show --user veya-remote-mcp.service --no-pager",
        "systemctl --user status veya-remote-mcp.service --no-pager",
    ],
)
def test_systemctl_user_read_forms_auto_open(tmp_path: Path, command: str) -> None:
    invocation = parse_systemctl_command(command)
    assert invocation is not None
    assert invocation.scope == "user"
    assert systemctl_read_options_valid(invocation)
    classification = classify_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert classification.category == ActionCategory.AUTO_OPEN
    assert classification.capability_id == "service_control.user"


def test_systemctl_user_restart_managed_auto_open(tmp_path: Path) -> None:
    command = "systemctl --user restart veya-remote-mcp.service"
    classification = classify_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert classification.category == ActionCategory.AUTO_OPEN
    assert classification.capability_id == "service_control.user"


def test_systemctl_system_service_requires_approval(tmp_path: Path) -> None:
    command = "systemctl restart ssh.service"
    classification = classify_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert classification.category == ActionCategory.REQUIRE_APPROVAL
    assert classification.capability_id == "privileged.critical_service"


def test_systemctl_user_unknown_mutation_fails_closed(tmp_path: Path) -> None:
    command = "systemctl --user mask veya-remote-mcp.service"
    classification = classify_action(
        "shell.exec", {"command": command}, _session(tmp_path), str(tmp_path)
    )
    assert classification.category == ActionCategory.REQUIRE_APPROVAL
    assert classification.capability_id == "privileged.system_service_write"


def test_tool_adapter_reuses_action_gateway_systemctl_parser() -> None:
    assert tool_adapter.parse_systemctl_command is action_gateway.parse_systemctl_command
