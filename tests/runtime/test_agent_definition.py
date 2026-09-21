"""D8 gates: agent definition / deployment / run-identity separation."""

from __future__ import annotations

import pytest

from server.agent_definition import (
    AgentDefinition,
    AgentDefinitionStore,
    AgentDeployment,
    AgentDeploymentStatus,
    AgentRunIdentity,
    activate_deployment,
    create_definition,
    create_deployment,
    disable_deployment,
    resolve_agent_deployment,
    retire_deployment,
    stamp_run_identity,
)


@pytest.fixture()
def store(tmp_path):
    return AgentDefinitionStore(tmp_path / "agents")


@pytest.fixture()
def events():
    return []


def _emit(events):
    def _record(topic, payload):
        events.append((topic, payload))

    return _record


def _register_provider(name="rt-test", available=True):
    from veya.platform import load

    obase = load("obase")
    spec = obase.ProviderSpec(name=name, available=available)
    try:
        obase.ProviderRegistry.get().register_spec(spec, replace=True)
    except TypeError:
        obase.ProviderRegistry.get().register_spec(spec)
    return spec


@pytest.fixture(autouse=True)
def _clean_registry():
    import contextlib

    from veya.platform import load

    obase = load("obase")
    with contextlib.suppress(Exception):
        obase.ProviderRegistry.clear()
    yield
    with contextlib.suppress(Exception):
        obase.ProviderRegistry.clear()


# -- definition -------------------------------------------------------


def test_definition_create_and_immutable(store, events):
    d1 = create_definition(store, definition_id="a", name="A", version=1, emit=_emit(events))
    assert d1.version == 1
    # same content twice -> idempotent, single event set
    d1b = create_definition(store, definition_id="a", name="A", version=1, emit=_emit(events))
    assert d1b.key() == d1.key()
    # new content same version -> conflict
    with pytest.raises(ValueError, match="conflict"):
        create_definition(store, definition_id="a", name="A-CHANGED", version=1)
    # new version ok
    d2 = create_definition(store, definition_id="a", name="A", version=2, emit=_emit(events))
    assert d2.version == 2
    topics = [t for t, _ in events]
    assert "agent.definition_created" in topics
    assert "agent.definition_version_created" in topics


def test_definition_rejects_bad_refs(store):
    with pytest.raises(ValueError):
        create_definition(store, definition_id="a", name="A", skill_refs=["bad ref!"])


def test_plaintext_secret_denied_in_refs(store):
    # refs are validated; a value with spaces/secrets shape is rejected
    with pytest.raises(ValueError):
        AgentDeployment(
            deployment_id="d",
            definition_id="a",
            definition_version=1,
            revision=1,
            runtime_id="r",
            secret_refs=["sk-live-abc 123"],
        )
    # value-like payloads never accepted as refs
    with pytest.raises(ValueError):
        AgentDefinition(definition_id="a", version=1, name="A", tool_refs=["not a ref!!"])


# -- deployment -------------------------------------------------------


def test_deployment_pin_and_activate(store, events):
    _register_provider("rt-1", available=True)
    create_definition(store, definition_id="a", name="A", version=1)
    create_definition(store, definition_id="a", name="A", version=2)
    dep = create_deployment(
        store,
        deployment_id="dep",
        definition_id="a",
        definition_version=1,
        runtime_id="rt-1",
        emit=_emit(events),
    )
    assert dep.status is AgentDeploymentStatus.DRAFT
    active = activate_deployment(store, "dep", emit=_emit(events))
    assert active.status is AgentDeploymentStatus.ACTIVE
    assert active.definition_version == 1
    # old deployment does not float
    assert store.get_deployment("dep").definition_version == 1
    # activate twice harmless
    again = activate_deployment(store, "dep", emit=_emit(events))
    assert again.status is AgentDeploymentStatus.ACTIVE


def test_deployment_missing_definition_denied(store):
    with pytest.raises(ValueError, match="missing definition"):
        create_deployment(
            store,
            deployment_id="d",
            definition_id="ghost",
            definition_version=1,
            runtime_id="rt-x",
        )


def test_invalid_runtime_denied(store):
    create_definition(store, definition_id="a", name="A", version=1)
    create_deployment(
        store,
        deployment_id="d",
        definition_id="a",
        definition_version=1,
        runtime_id="ghost-rt",
    )
    with pytest.raises(ValueError, match="runtime unavailable"):
        activate_deployment(store, "d")


def test_unavailable_runtime_denied(store):
    _register_provider("rt-down", available=False)
    create_definition(store, definition_id="a", name="A", version=1)
    create_deployment(
        store,
        deployment_id="d",
        definition_id="a",
        definition_version=1,
        runtime_id="rt-down",
    )
    with pytest.raises(ValueError, match="runtime unavailable"):
        activate_deployment(store, "d")


def test_disable_retire_idempotent(store):
    _register_provider("rt-1", available=True)
    create_definition(store, definition_id="a", name="A", version=1)
    create_deployment(
        store,
        deployment_id="d",
        definition_id="a",
        definition_version=1,
        runtime_id="rt-1",
    )
    activate_deployment(store, "d")
    assert disable_deployment(store, "d").status is AgentDeploymentStatus.DISABLED
    assert disable_deployment(store, "d").status is AgentDeploymentStatus.DISABLED
    assert retire_deployment(store, "d").status is AgentDeploymentStatus.RETIRED
    assert retire_deployment(store, "d").status is AgentDeploymentStatus.RETIRED


# -- resolution -------------------------------------------------------


def test_resolution_deterministic(store):
    _register_provider("rt-1", available=True)
    create_definition(store, definition_id="a", name="A", version=1)
    create_deployment(
        store,
        deployment_id="d",
        definition_id="a",
        definition_version=1,
        runtime_id="rt-1",
    )
    activate_deployment(store, "d")
    res = resolve_agent_deployment(store, deployment_id="d")
    assert res.runtime_available is True
    assert res.definition_version == 1
    assert res.deployment_revision == 1
    ident = stamp_run_identity(res, session_id="sess-1")
    assert isinstance(ident, AgentRunIdentity)
    assert ident.session_id == "sess-1"
    assert ident.agent_instance_id is None


def test_resolution_by_definition_picks_latest_active(store):
    _register_provider("rt-1", available=True)
    create_definition(store, definition_id="a", name="A", version=1)
    create_deployment(
        store,
        deployment_id="d1",
        definition_id="a",
        definition_version=1,
        runtime_id="rt-1",
    )
    activate_deployment(store, "d1")
    res = resolve_agent_deployment(store, definition_id="a")
    assert res.deployment_id == "d1"


# -- goalrun snapshot -------------------------------------------------


def test_goalrun_snapshot_roundtrip_and_immutable():
    from server.goal_run.models import GoalRunState

    state = GoalRunState(goal_id="g", goal_text="t")
    state.agent_definition_id = "a"
    state.agent_definition_version = 2
    state.agent_deployment_id = "d"
    state.agent_deployment_revision = 3
    state.agent_runtime_id = "rt-1"
    state.agent_session_id = "sess-1"
    data = state.to_taskgraph_json()
    assert data["agent_definition_version"] == 2
    assert data["agent_deployment_revision"] == 3
    restored = GoalRunState.from_taskgraph_json(data, "t")
    assert restored.agent_definition_id == "a"
    assert restored.agent_session_id == "sess-1"


def test_session_run_separated():
    from server.goal_run.models import GoalRunState

    state = GoalRunState(goal_id="g", goal_text="t")
    state.agent_session_id = "sess-1"
    assert state.goal_id != state.agent_session_id
    assert state.agent_definition_id is None


def test_d3_resume_unchanged():
    from runtime.execution.resume import decide_resume_disposition

    decision = decide_resume_disposition(trigger="infra_retry", session_id="s", resume_capable=True)
    assert decision.disposition.value == "resume_session"


async def test_revoked_dependency_blocks_run_start(store):
    _register_provider("rt-1", available=True)
    create_definition(
        store,
        definition_id="a",
        name="A",
        version=1,
        skill_refs=["ghost-skill"],
    )
    create_deployment(
        store,
        deployment_id="d",
        definition_id="a",
        definition_version=1,
        runtime_id="rt-1",
    )
    activate_deployment(store, "d")
    from server.agent_definition import resolve_agent_deployment as _resolve
    from server.goal_run.runner import _validate_run_dependencies

    res = _resolve(store, deployment_id="d")
    reason = await _validate_run_dependencies(store, res)
    assert reason is not None and "ghost-skill" in reason


def test_restart_persistence(tmp_path):
    first = AgentDefinitionStore(tmp_path / "ag")
    create_definition(first, definition_id="a", name="A", version=1)
    second = AgentDefinitionStore(tmp_path / "ag")
    assert second.get_definition("a", 1) is not None
    assert second.get_definition("a", 1).name == "A"


def test_d12_boundary_optional_ref_only():
    ident = AgentRunIdentity(
        definition_id="a",
        definition_version=1,
        deployment_id="d",
        deployment_revision=1,
        runtime_id="r",
    )
    assert ident.agent_instance_id is None
    assert "agent_instance_id" in ident.to_dict()


def test_d9_boundary_no_ranking():
    import pathlib

    src = pathlib.Path("server/agent_definition.py").read_text().lower()
    for token in ("top_k", "topk", "token_budget", "capabilityprojection", "semantic rout"):
        assert token not in src
