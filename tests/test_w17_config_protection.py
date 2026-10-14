"""W17 clean-room: configuration self-tampering protection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.approvals import get_approval_service
from server.config_protection import (
    is_protected_config_path,
)
from server.privileged_action_gate import (
    STATUS_APPROVAL_REQUIRED,
    STATUS_APPROVED,
    request_privileged_action,
)
from server.tool_registry import (
    ToolExecutionError,
    _tool_edit_hashline,
    _tool_read_hashline,
    _tool_write_file,
)


def test_is_protected_config_path():
    home = Path.home()

    # ~/.veya/*.json
    assert is_protected_config_path("~/.veya/hosts.json")
    assert is_protected_config_path("~/.veya/settings.json")
    assert is_protected_config_path(str(home / ".veya" / "tokens.json"))
    # Non-json or subdirs in .veya should not trigger the ~/.veya/*.json rule
    assert not is_protected_config_path("~/.veya/work/result.json")
    assert not is_protected_config_path("~/.veya/ssh/cm/socket")

    # deploy/*flags*.env
    assert is_protected_config_path("deploy/dev.flags.env")
    assert is_protected_config_path("deploy/flags.env")
    assert is_protected_config_path("/repo/deploy/production_flags.env")
    assert not is_protected_config_path("deploy/docker-compose.yml")
    assert not is_protected_config_path("deploy/env.template")

    # ~/.config/veya/*
    assert is_protected_config_path("~/.config/veya/config.yaml")
    assert is_protected_config_path(str(home / ".config" / "veya" / "credentials"))
    assert not is_protected_config_path("~/.config/other/config.yaml")
    assert not is_protected_config_path("src/server/app.py")


def test_write_file_protected_config_requires_human_approval(tmp_path, monkeypatch):
    target = tmp_path / ".veya" / "test_target.json"
    target_str = str(target)
    # Monkeypatch home to tmp_path so target is under home / .veya / test_target.json
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert is_protected_config_path(target_str)

    # 1. Unapproved write is refused
    with pytest.raises(ToolExecutionError, match="配置防自篡改保护"):
        _tool_write_file(target_str, '{"evil": true}', approved=False)

    # 2. Approved=True without approval_id is refused
    with pytest.raises(ToolExecutionError, match="携带经批准的 approval_id"):
        _tool_write_file(target_str, '{"evil": true}', approved=True, approval_id="")

    # 3. Approval by agent is refused
    class FakeStore:
        def __init__(self):
            self.rec = {
                "request_id": "app_agent_1",
                "status": "completed",
                "decision": "allow",
                "principal": "agent",
            }

        def get(self, aid):
            return self.rec

    service = get_approval_service()
    monkeypatch.setattr(service, "get", lambda aid: FakeStore().get(aid))

    with pytest.raises(ToolExecutionError, match="必须由人类审批者批准"):
        _tool_write_file(target_str, '{"evil": true}', approved=True, approval_id="app_agent_1")

    # 4. Approval by human succeeds
    class FakeHumanStore:
        def get(self, aid):
            return {
                "request_id": "app_human_1",
                "status": "completed",
                "decision": "allow",
                "principal": "human",
            }

    monkeypatch.setattr(service, "get", lambda aid: FakeHumanStore().get(aid))
    out = _tool_write_file(target_str, '{"safe": true}', approved=True, approval_id="app_human_1")
    assert "已写入受保护配置" in out
    assert target.read_text(encoding="utf-8") == '{"safe": true}'


def test_edit_hashline_protected_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".config" / "veya" / "settings.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("setting: old_value\n", encoding="utf-8")

    # Read hashline tags
    tags = _tool_read_hashline(str(target))
    assert "setting: old_value" in tags
    line_tag = next(ln for ln in tags.splitlines() if "LINE#" in ln)
    tag = line_tag.split("LINE#")[1].split()[0]

    # Unapproved edit refused
    with pytest.raises(ToolExecutionError, match="配置防自篡改保护"):
        _tool_edit_hashline(
            str(target), start_tag=f"1#{tag}", new_text="setting: new_value\n", approved=False
        )

    # Approved edit succeeds
    service = get_approval_service()
    monkeypatch.setattr(
        service,
        "get",
        lambda aid: {
            "request_id": aid,
            "status": "completed",
            "decision": "allow",
            "principal": "human",
        },
    )
    res = _tool_edit_hashline(
        str(target),
        start_tag=f"1#{tag}",
        new_text="setting: new_value\n",
        approved=True,
        approval_id="app_h1",
    )
    assert "hashline-edited" in res
    assert target.read_text(encoding="utf-8") == "setting: new_value\n"


def test_privileged_action_gate_never_auto_approves_protected_config(monkeypatch):
    class AllowDecision:
        verdict = "ALLOW"
        reason = "policy allows"

    class MockGateway:
        def __init__(self, **_):
            pass

        def evaluate_policy(self, req):
            return AllowDecision()

    monkeypatch.setattr("server.privileged_action_gate.ActionGatewayAdapter", MockGateway)

    # Normal target can be approved if policy allows
    outcome_normal = request_privileged_action(
        action="write",
        arguments={"path": "src/normal.py"},
        effect="write",
    )
    assert outcome_normal.status == STATUS_APPROVED

    # Protected target must NEVER be auto-approved
    outcome_protected = request_privileged_action(
        action="write",
        arguments={"path": "deploy/dev.flags.env"},
        effect="write",
    )
    assert outcome_protected.status == STATUS_APPROVAL_REQUIRED
    assert "protected configuration path" in outcome_protected.reason


def test_approval_service_rejects_agent_resolving_protected_config(monkeypatch):
    service = get_approval_service()
    approval = {
        "approval_id": "a1",
        "tool": "write_file",
        "tool_args": {"filepath": "deploy/flags.env"},
        "delegate_policy": "allow:agent:reviewer",
    }
    # Human can resolve
    assert service.check_resolver_principal(approval, "human") is None
    # Agent cannot resolve protected config
    refusal = service.check_resolver_principal(approval, "agent:reviewer")
    assert refusal == "protected configuration changes are human-only"
