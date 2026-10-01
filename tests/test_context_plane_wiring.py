"""上下文平面 + 能力发现的生产接线 (P0-06 / P0-07 / P1-06)。

三处接线的可观测契约:
- ``ContextGateway`` 在 GoalRun admission 处**自动** admit 投影 (不靠模型调工具);
- 上下文准入 fail-open —— 建不起投影绝不判死一个已持久化的 GoalRun;
- ``binding_applies()`` 真的在准入路径上按 scope 过滤候选上下文;
- 技能经 ``UnifiedCapabilityDiscovery`` (discover → eligibility → rank) surfaced,
  且排序权威 / 可用性 / 渐进加载逐位不变。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.context_gateway import ContextGateway, ContextScope
from server.explainability import context_explain
from server.goal_run import pre_admission
from server.goal_run.models import GoalStatus
from server.goal_run.pre_admission import create_pending_run
from server.goal_run.store import load_goal_run, read_events
from server.knowledge_binding import BindingScope, binding_applies, new_binding
from server.skill_eligibility import (
    CapabilityCandidate,
    CapabilityType,
    SkillEligibility,
    UnifiedCapabilityDiscovery,
)
from server.skill_hub import VeyaSkillHub, _oskill_mod

# ── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_gateway(monkeypatch: pytest.MonkeyPatch) -> ContextGateway:
    """共享 gateway 是进程级的 —— 每个用例换成干净实例, 状态不跨用例泄漏。"""
    gateway = ContextGateway()
    monkeypatch.setattr(pre_admission, "_CONTEXT_GATEWAY", gateway)
    return gateway


def _dispatch(project_root: Path, dispatch_id: str = "dispatch-1") -> pre_admission.PreAdmission:
    return create_pending_run(
        project_root=str(project_root),
        dispatch_id=dispatch_id,
        session_id="session-1",
        requested_executor="fake",
        tasks=[{"worker": "fake", "task": "ship the thing"}],
    )


def _write_skill(skills_dir: Path, name: str, *, description: str, skill_md: str | None = None):
    pkg = skills_dir / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": description,
                "type": "python",
                "entrypoint": "run.py",
                "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}},
            }
        ),
        encoding="utf-8",
    )
    (pkg / "run.py").write_text("def main(**kwargs):\n    return 'ok'\n", encoding="utf-8")
    if skill_md is not None:
        (pkg / "SKILL.md").write_text(skill_md, encoding="utf-8")


_SKILL_MD = """---
name: refactor_py
description: refactor python code
tags: [python, refactor]
---
# Refactor guide
Step 1: locate the long function.
Step 2: extract cohesive helpers.
"""


class _SpyDiscovery(UnifiedCapabilityDiscovery):
    """记录流水线每一段, 用来证明生产路径真的走 discover → eligibility → rank。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, Any]] = []
        self.registered: list[Any] = []

    def register(self, candidate: Any) -> None:
        self.calls.append(("register", candidate.name))
        self.registered.append(candidate)
        super().register(candidate)

    def discover(self, context: dict[str, Any]) -> list[Any]:
        self.calls.append(("discover", dict(context)))
        return super().discover(context)

    def rank(self, candidates: list[Any]) -> list[Any]:
        self.calls.append(("rank", [c.name for c in candidates]))
        return super().rank(candidates)


def _spy_pipeline(monkeypatch: pytest.MonkeyPatch) -> list[_SpyDiscovery]:
    spies: list[_SpyDiscovery] = []

    def _factory() -> _SpyDiscovery:
        spy = _SpyDiscovery()
        spies.append(spy)
        return spy

    monkeypatch.setattr("server.skill_hub.UnifiedCapabilityDiscovery", _factory)
    return spies


# ── P0-06: 上下文准自在 admission 自动发生 ─────────────────────────────


def test_context_admitted_automatically_at_admission(tmp_path: Path, isolated_gateway):
    """准入即 admit: 投影存在, 不需要任何工具调用 / retrieve。"""
    pre = _dispatch(tmp_path)

    items = pre_admission.context_gateway().iterate(goal_run_id=pre.goal_run_id)
    assert items, "admission must admit a context projection on its own"
    # 否定断言的对偶 (控制组): 同一 gateway 上未被准入的 run 没有投影,
    # 所以上面的 items 确实来自这次 admission, 不是 gateway 的默认内容。
    assert pre_admission.context_gateway().iterate(goal_run_id="goal_never_admitted") == []


def test_admitted_projection_is_persisted_and_citable(tmp_path: Path, isolated_gateway):
    """persist 的投影可 resolve / cite —— 落盘面是真投影, 不是一次性局部变量。"""
    pre = _dispatch(tmp_path)
    gateway = pre_admission.context_gateway()
    items = gateway.iterate(goal_run_id=pre.goal_run_id)

    resolved = gateway.resolve(
        goal_run_id=pre.goal_run_id,
        item_ids=[items[0].context_item_id],
    )
    assert [i.context_item_id for i in resolved] == [items[0].context_item_id]

    citation = gateway.cite(goal_run_id=pre.goal_run_id, item_id=items[0].context_item_id)
    assert citation and items[0].context_item_id in citation
    # 未知 id → 无引用 (证伪面: cite 不是恒返回字符串)。
    assert gateway.cite(goal_run_id=pre.goal_run_id, item_id="nope") is None


def test_shared_gateway_is_reused_and_admission_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """gateway 是模块级共享实例, 不随每次 dispatch 重建; 同 dispatch 只准一次。"""
    admitted: list[str] = []

    class _Recording(ContextGateway):
        def admit(self, **kwargs: Any):
            admitted.append(kwargs["goal_run_id"])
            return super().admit(**kwargs)

    gateway = _Recording()
    monkeypatch.setattr(pre_admission, "_CONTEXT_GATEWAY", gateway)

    first = _dispatch(tmp_path, dispatch_id="dispatch-rec")
    second = _dispatch(tmp_path, dispatch_id="dispatch-rec")

    assert first.goal_run_id == second.goal_run_id
    assert admitted == [first.goal_run_id]
    assert pre_admission.context_gateway() is gateway


# ── P0-06: fail-open ───────────────────────────────────────────────────


@pytest.mark.parametrize("failure", ["admit", "binding_applies"])
def test_context_admission_failure_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
):
    """上下文建不起来 → GoalRun 准入照常成功 (正例: 同调用不注入故障时成功, 见上)。"""

    def _boom(*args: Any, **kwargs: Any):
        raise RuntimeError("context plane unavailable")

    if failure == "admit":
        monkeypatch.setattr(pre_admission._CONTEXT_GATEWAY, "admit", _boom)
    else:
        monkeypatch.setattr(pre_admission, "binding_applies", _boom)

    pre = _dispatch(tmp_path)

    assert pre.state.status is GoalStatus.pending_execution
    assert pre.state.tasks and pre.goal_task_id in pre.state.tasks
    assert pre.state.last_stop_reason is None
    persisted = load_goal_run(str(tmp_path), pre.goal_run_id)
    assert persisted is not None and persisted.status is GoalStatus.pending_execution
    # 没有投影, 但 run 活着 —— fail-open 的另一半。
    assert pre_admission.context_gateway().iterate(goal_run_id=pre.goal_run_id) == []


def test_context_record_failure_does_not_fail_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_gateway: ContextGateway
):
    """落盘失败 → 准入仍成立: 上下文记录是旁路, 不是准入门。"""
    real_append = pre_admission.append_event

    def _append_fails(project_root: str, goal_id: str, event: dict[str, Any]) -> None:
        if event.get("type") == pre_admission._CONTEXT_ADMITTED_EVENT:
            raise RuntimeError("context record unavailable")
        real_append(project_root, goal_id, event)

    monkeypatch.setattr(pre_admission, "append_event", _append_fails)

    pre = _dispatch(tmp_path)

    assert pre.state.status is GoalStatus.pending_execution
    assert pre.state.last_stop_reason is None
    persisted = load_goal_run(str(tmp_path), pre.goal_run_id)
    assert persisted is not None and persisted.status is GoalStatus.pending_execution
    # 落盘是旁路: 内存里的投影照常成立, 已持久化的 run 不因此判死。
    assert isolated_gateway.iterate(goal_run_id=pre.goal_run_id)
    # 但 durable 记录确实没写成 —— 重启后就读不回来, 所以这条不能被当成"已落盘"。
    assert [
        event
        for event in read_events(str(tmp_path), pre.goal_run_id)
        if event.get("type") == pre_admission._CONTEXT_ADMITTED_EVENT
    ] == []


# ── P0-06 收口: Admission / Resolve / Cite / Explain / Audit 同一 authority ──


def _count_gateway_constructions(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """把 ContextGateway 的构造次数变成可断言的量 (第二个实例一旦出现就会被看见)。"""
    import server.context_gateway as context_gateway_module

    constructed: list[Any] = []
    real = context_gateway_module.ContextGateway

    def _counted(*args: Any, **kwargs: Any):
        constructed.append(kwargs.get("goal_run_id"))
        return real(*args, **kwargs)

    monkeypatch.setattr(context_gateway_module, "ContextGateway", _counted)
    return constructed


def test_explain_never_constructs_its_own_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_gateway: ContextGateway
):
    """A: explain 走 context_gateway() 那个实例 —— 临时 ContextGateway 不许出现。"""
    pre = _dispatch(tmp_path)
    admitted = isolated_gateway.iterate(goal_run_id=pre.goal_run_id)
    assert admitted, "正例: 准入确实产生了投影, 否则下面的 item_count 是空转"

    constructed = _count_gateway_constructions(monkeypatch)
    explained = context_explain(pre.goal_run_id, project_root=str(tmp_path))

    assert constructed == [], "explain 必须复用唯一权威, 不得新建 ContextGateway"
    assert explained["item_count"] == len(admitted)
    assert pre_admission.context_gateway() is isolated_gateway


def test_explain_round_trip_returns_the_admitted_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_gateway: ContextGateway
):
    """B: create_pending_run → admit → 落盘 → explain 拿回同一份投影/证据。"""
    monkeypatch.setenv("VEYA_PROJECT_ROOT", str(tmp_path))
    pre = _dispatch(tmp_path)
    projection = pre_admission.context_projection(
        goal_run_id=pre.goal_run_id, project_root=str(tmp_path)
    )
    assert projection is not None

    explained = context_explain(pre.goal_run_id)

    assert explained["goal_run_id"] == pre.goal_run_id
    assert explained["projection_id"] == projection.projection_id
    assert explained["budget_tokens"] == projection.budget_tokens
    assert explained["item_count"] == len(projection.items)
    assert explained["items"] == [
        {
            "id": item.context_item_id,
            "scope": item.scope.value,
            "provenance": item.provenance,
            "authority": item.authority,
        }
        for item in projection.items
    ]
    # 同一份证据仍能被这个 authority 引用 —— explain 不是另起一份投影。
    first_id = explained["items"][0]["id"]
    assert first_id in isolated_gateway.cite(goal_run_id=pre.goal_run_id, item_id=first_id)


def test_explain_survives_process_restart(tmp_path: Path, isolated_gateway: ContextGateway):
    """C: 落盘 → 换进程 (gateway 全空) → 回读仍得到同一份 evidence。"""
    pre = _dispatch(tmp_path)
    before = context_explain(pre.goal_run_id, project_root=str(tmp_path))
    assert before["items"], "正例: 准入确实落了投影"

    # 进程重启: 模块级 gateway 换成全新实例, 内存里的投影全部消失。
    fresh = ContextGateway()
    pre_admission._CONTEXT_GATEWAY = fresh
    assert fresh.iterate(goal_run_id=pre.goal_run_id) == []

    # 控制组: 换一个 project_root 就读不到 —— 所以下面的 after 确实来自这次运行
    # 自己的 durable 记录, 而不是某条进程内旁路。
    assert context_explain(pre.goal_run_id, project_root=str(tmp_path / "elsewhere")) == {
        "goal_run_id": pre.goal_run_id,
        "explanation": "no projection found",
    }

    after = context_explain(pre.goal_run_id, project_root=str(tmp_path))

    assert after["projection_id"] == before["projection_id"], "重启前后是同一个投影"
    assert after["items"] == before["items"]
    assert after["item_count"] == before["item_count"]
    # 还原后的状态属于 authority 自己 —— 回读不是绕过 gateway 的旁路。
    assert fresh.cite(goal_run_id=pre.goal_run_id, item_id=before["items"][0]["id"])


def test_admission_resolve_cite_explain_audit_share_one_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_gateway: ContextGateway
):
    """D: 五个面共用一个实例, 一份投影只留一条 durable 记录 (SECOND_CONTEXT_STATE=0)。"""
    constructed = _count_gateway_constructions(monkeypatch)

    pre = _dispatch(tmp_path)
    gateway = pre_admission.context_gateway()
    items = gateway.iterate(goal_run_id=pre.goal_run_id)
    gateway.resolve(goal_run_id=pre.goal_run_id, item_ids=[items[0].context_item_id])
    assert gateway.cite(goal_run_id=pre.goal_run_id, item_id=items[0].context_item_id)
    context_explain(pre.goal_run_id, project_root=str(tmp_path))

    assert constructed == []
    assert gateway is isolated_gateway, "唯一权威就是共享的那个实例"
    admitted = [
        event
        for event in read_events(str(tmp_path), pre.goal_run_id)
        if event.get("type") == pre_admission._CONTEXT_ADMITTED_EVENT
    ]
    assert len(admitted) == 1, "一份投影 = 一条 durable 记录, 不得分叉"


def test_explain_without_admission_reports_no_projection(
    tmp_path: Path, isolated_gateway: ContextGateway
):
    """未准入的运行 → 无投影 (否定断言; 让它失败的正例见上面的 round trip)。"""
    assert context_explain("goal_never_admitted", project_root=str(tmp_path)) == {
        "goal_run_id": "goal_never_admitted",
        "explanation": "no projection found",
    }


# ── P1-06: binding_applies 按 scope 过滤 ───────────────────────────────


def test_binding_applies_filters_by_scope():
    goal = new_binding("k-goal", BindingScope.GOAL)
    # 正例: 同 scope 适用。
    assert binding_applies(goal, context_scope=BindingScope.GOAL) is True
    # 否定: scope 不匹配一律不适用 (语义相似再高也没用)。
    assert binding_applies(goal, context_scope=BindingScope.PROJECT) is False
    assert binding_applies(goal, context_scope=BindingScope.SESSION) is False


def test_binding_applies_honours_every_discriminator():
    binding = new_binding(
        "k-proj",
        BindingScope.PROJECT,
        project_id="p1",
        repository_id="r1",
        goal_type="refactor",
        skill_id="s1",
    )
    full = {
        "project_id": "p1",
        "repository_id": "r1",
        "goal_type": "refactor",
        "skill_id": "s1",
    }
    assert binding_applies(binding, context_scope=BindingScope.PROJECT, **full) is True

    for key in full:
        mismatch = {**full, key: "other"}
        assert binding_applies(binding, context_scope=BindingScope.PROJECT, **mismatch) is False, (
            f"mismatched {key} must not apply"
        )

    # 调用方没传判别项时, 绑定带约束 → 不放行 (不得因"没传"而默认通过)。
    assert binding_applies(binding, context_scope=BindingScope.PROJECT) is False
    # scope 不匹配时, 判别项全对也不适用。
    assert binding_applies(binding, context_scope=BindingScope.REPOSITORY, **full) is False


def test_admission_projection_keeps_only_binding_applicable_scopes(
    tmp_path: Path, isolated_gateway
):
    """被发现的 scope 里, 只有绑定适用的才进投影 —— 过滤非空转。"""
    assert ContextScope.SESSION in pre_admission._ADMISSION_SCOPES, "过滤若为空转则本用例无意义"

    pre = _dispatch(tmp_path)
    scopes = [
        item.scope for item in pre_admission.context_gateway().iterate(goal_run_id=pre.goal_run_id)
    ]
    assert scopes == [ContextScope.GOAL, ContextScope.PROJECT]
    assert ContextScope.SESSION not in scopes


def test_admission_drops_bindings_whose_constraints_are_unmet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """绑定带约束而本次运行不满足 → 该 scope 不投影, 但准入仍成功。"""
    monkeypatch.setattr(
        pre_admission,
        "_ADMISSION_BINDINGS",
        (new_binding("project_state", BindingScope.PROJECT, project_id="other-project"),),
    )
    pre = _dispatch(tmp_path)

    assert pre_admission.context_gateway().iterate(goal_run_id=pre.goal_run_id) == []
    assert pre.state.status is GoalStatus.pending_execution


# ── P0-07: 技能经统一能力发现 surfaced ────────────────────────────────


async def test_skill_discovery_goes_through_unified_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _write_skill(tmp_path, "refactor_py", description="refactor python code", skill_md=_SKILL_MD)
    _write_skill(tmp_path, "weather", description="get the weather")
    hub = VeyaSkillHub(skills_dir=tmp_path)
    spies = _spy_pipeline(monkeypatch)

    task = "refactor this python function"
    out = json.loads(await hub.execute("list_skills", {"task": task}))

    assert spies, "skills must be surfaced through UnifiedCapabilityDiscovery"
    stages = [call[0] for call in spies[0].calls]
    assert stages == ["register"] * len(spies[0].registered) + ["discover", "rank"]
    assert ("discover", {"intent": task}) in spies[0].calls
    assert {c.capability_type for c in spies[0].registered} == {CapabilityType.SKILL}

    # 流水线没有篡改排序权威: 与 3O select_skill 的路由顺序逐位一致。
    osk = _oskill_mod()
    assert osk is not None, "3O 单源缺失时本用例的顺序断言无意义"
    expected = [
        meta["name"] for meta in osk.select_skill(task, skill_index=hub._skill_index(), top_k=3)
    ]
    assert [entry["name"] for entry in out] == expected
    assert expected[0] == "refactor_py"

    # 渐进加载不变: 命中的带 body, 未命中的不带。
    assert "Step 1" in out[0]["body"]
    assert all("body" not in entry for entry in out[1:])


async def test_skill_discovery_applies_eligibility_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """eligibility 是真闸门: intent 不匹配 → 不 surfaced (正例在末尾)。"""
    _write_skill(tmp_path, "refactor_py", description="refactor python code")
    _write_skill(tmp_path, "weather", description="get the weather")
    hub = VeyaSkillHub(skills_dir=tmp_path)
    _spy_pipeline(monkeypatch)

    task = "refactor this python function"
    monkeypatch.setattr(
        "server.skill_hub.CapabilityCandidate",
        lambda **kwargs: CapabilityCandidate(
            **kwargs, eligibility=SkillEligibility(intent="something else entirely")
        ),
    )
    assert json.loads(await hub.execute("list_skills", {"task": task})) == []

    # 正例: intent 匹配 → 照常 surfaced, 顺序不变。
    monkeypatch.setattr(
        "server.skill_hub.CapabilityCandidate",
        lambda **kwargs: CapabilityCandidate(**kwargs, eligibility=SkillEligibility(intent=task)),
    )
    out = json.loads(await hub.execute("list_skills", {"task": task}))
    assert [entry["name"] for entry in out] == ["refactor_py", "weather"]


async def test_skill_availability_and_catalog_unchanged(tmp_path: Path):
    """接线不动可用性: dispatcher 目录、按需 list、命中才拉 body 都保持原样。"""
    _write_skill(tmp_path, "refactor_py", description="refactor python code", skill_md=_SKILL_MD)
    _write_skill(tmp_path, "weather", description="get the weather")
    hub = VeyaSkillHub(skills_dir=tmp_path)

    assert [s["function"]["name"] for s in hub.get_all_schemas()] == [
        "list_skills",
        "run_skill",
    ]
    assert hub._all_skill_names() == ["refactor_py", "weather"]
    assert hub.has("refactor_py") and not hub.has("nope")
    # dispatcher 模式: 不带 task 的 list_skills 列全名 (含 body-free 摘要)。
    listed = json.loads(await hub.execute("list_skills", {}))
    assert [entry["name"] for entry in listed] == ["refactor_py", "weather"]
    assert all("body" not in entry for entry in listed)
    assert (
        await hub.execute("run_skill", {"skill_name": "refactor_py", "args": {"goal": "x"}}) == "ok"
    )
