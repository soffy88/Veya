"""SF-001 follow-up + S12 follow-up."""

from __future__ import annotations

import os


def test_mutating_tools_without_declaration_get_one():
    from server.tool_registry import MasterToolRegistry, SideEffect

    reg = MasterToolRegistry()

    async def noop(**_kw):
        return "ok"

    schema = {"type": "object", "properties": {}}
    reg.register("wayfind_gh_add_ticket", "d", schema, noop)
    reg.register("write_file", "d", schema, noop)
    reg.register("team_delete", "d", schema, noop)
    reg.register("some_read_tool", "d", schema, noop)
    assert reg.side_effect_policy_for("wayfind_gh_add_ticket")["side_effect"] == SideEffect.EXTERNAL_MUTATION.value
    assert reg.side_effect_policy_for("write_file")["side_effect"] == SideEffect.LOCAL_WRITE.value
    assert reg.side_effect_policy_for("team_delete")["side_effect"] == SideEffect.PRIVILEGED.value
    assert reg.side_effect_policy_for("some_read_tool")["side_effect"] is None


def test_github_mutation_now_needs_approval():
    from server.action_gateway_adapter import _effect_for
    from server.tool_registry import SideEffect
    from veya.remote.permission_engine import PermissionEngine
    from veya.remote.policy_resolver import PolicyRequest, PolicyResolver

    eff = _effect_for("wayfind_gh_add_ticket", SideEffect.EXTERNAL_MUTATION, classify=lambda n: "unknown")
    decision = PolicyResolver(engine=PermissionEngine()).resolve(
        PolicyRequest(actor="t", tool="wayfind_gh_add_ticket", workspace=os.getcwd(), cwd=os.getcwd(), effect=eff),
        consume_approval=False,
    )
    assert decision.decision.value != "ALLOW"


def test_shim_text_never_becomes_the_reply():
    from server.bot_team.turns import _MODEL_UNAVAILABLE_REPLY, extract_reply
    from server.goal_run.bot_identity import BotSpec

    bot = BotSpec(bot_id="b", owner_id="u", name="B", runner_id="local")
    for raw in ("LLM provider not configured — this is a shim response.", "gpt-5.6-luna 兜底失败"):
        reply = extract_reply({"status": "completed", "reply": raw}, bot=bot)
        assert reply == _MODEL_UNAVAILABLE_REPLY
    assert extract_reply({"status": "completed", "reply": "5"}, bot=bot) == "5"
