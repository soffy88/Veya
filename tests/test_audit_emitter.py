"""AuditEmitter 测试 — 决策审计统一写出口 + 双事务挂载。

门禁:
  1. 统一 Schema: 五类事件字段完整、JSONL 可回读;
  2. closed_loop_intervene: 一次事务写全 diagnose→decide→execute→learn,
     同一 trace_id, cpd_version 随轮次自增, 效用排序落审计;
  3. multi_step_plan: diagnose→plan→decide→execute→learn 五节点,
     graph_version 记录"当时用的哪版因果图";
  4. 事后可回答: 回放链路还原"为什么选这个动作/用的哪版模型/谁授权的"。
"""

from __future__ import annotations

import json

import pytest

from veya.platform import omodul as load_omodul
from veya.platform import oprim as load_oprim
from veya.platform import oskill as load_oskill

omodul = load_omodul()
oprim = load_oprim()
oskill = load_oskill()

networkx = pytest.importorskip("networkx")


# =========================================================================
# 一、Emitter 本体: 统一 Schema
# =========================================================================

def test_emitter_writes_schema_and_jsonl_roundtrip(tmp_path):
    sink = oprim.JsonlSink(str(tmp_path / "audit.jsonl"))
    em = oprim.AuditEmitter(sink=sink)

    aid = em.decide(
        inputs={"graph_version": 3, "cpd_version": 5, "threat_level": 0.12},
        decision={"chosen_strategy": "aggressive_repair",
                  "utilities": {"do(external_api=ok)": 0.21, "do(retry)": 0.05}},
        execution={"primitive": "circuit_break", "status": "ok",
                   "capability_nonce": "abc123"},
    )
    assert aid

    events = sink.read_all()
    assert len(events) == 1
    ev = events[0]
    # 统一 Schema 必需键
    assert {"audit_id", "trace_id", "ts", "event_type", "inputs"} <= set(ev)
    assert ev["event_type"] == "decide"
    assert ev["inputs"] == {"graph_version": 3, "cpd_version": 5, "threat_level": 0.12}
    assert ev["decision"]["chosen_strategy"] == "aggressive_repair"
    assert ev["execution"]["capability_nonce"] == "abc123"
    # 文件内容是逐行 JSON (JSONL 规范)
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["event_type"] == "decide"


def test_emitter_replay_by_trace_and_event_type_validation():
    sink = oprim.MemorySink()
    em = oprim.AuditEmitter(sink=sink, trace_id="trace-1")
    em.diagnose(inputs={"cpd_version": 1})
    em.decide(decision={"chosen_strategy": "x"})
    em.execute(execution={"primitive": "p", "status": "ok"})
    em.learn(learning={"cpd_version_after": 2})

    chain = em.replay()
    assert [e["event_type"] for e in chain] == ["diagnose", "decide", "execute", "learn"]
    assert all(e["trace_id"] == "trace-1" for e in chain)

    # 事件类型白名单
    with pytest.raises(ValueError, match="event_type"):
        oprim.AuditEvent(event_type="hack", trace_id="t")


# =========================================================================
# 二、closed_loop_intervene 挂载: 全链路审计
# =========================================================================

@pytest.mark.asyncio
async def test_closed_loop_emits_full_chain(tmp_path):
    cpd = oskill.CategoricalCPD(
        child_states=["success", "fault"],
        counts={"degraded": [4.0, 6.0], "healthy": [8.0, 2.0]},
        parents=["mode"], version=5,
    )
    interventions = [
        {"action_id": "do_mode=healthy", "target_value": "healthy", "cost": 0.1, "risk": 0.0},
    ]
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    audit_path = str(tmp_path / "audit.jsonl")

    inp = omodul.ClosedLoopInput(
        cpd, interventions=interventions,
        threat_level=0.12, capability_nonce="cap-001",
        graph_version=3, notes="生产故障演练 #1",
    )
    cfg = omodul.ClosedLoopConfig(simulate=True, rounds=3, seed=0,
                                  baseline_config="degraded", audit_path=audit_path)
    result = await omodul.closed_loop_intervene(cfg, inp, out)

    # 事务返回 trace_id, 事后可回放
    trace_id = result["audit_trace_id"]
    assert trace_id

    events = oprim.JsonlSink(audit_path).read_trace(trace_id)
    types = [e["event_type"] for e in events]
    # 一次事务: 1×diagnose + 3×decide + 3×execute + 3×learn (rounds=3)
    assert types == ["diagnose"] + ["decide", "execute", "learn"] * 3

    # 事后回答①: 用的哪版模型 / 威胁水平
    diag = events[0]
    assert diag["inputs"]["cpd_version"] == 5
    assert diag["inputs"]["graph_version"] == 3
    assert diag["inputs"]["threat_level"] == 0.12
    assert diag["context"]["notes"] == "生产故障演练 #1"

    # 事后回答②: 为什么选这个动作 (效用排序落审计)
    decide0 = events[1]
    assert decide0["decision"]["chosen_strategy"] == "do_mode=healthy"
    assert "utilities" in decide0["decision"]

    # 事后回答③: 谁授权执行的 + 执行了什么 primitive
    exec0 = events[2]
    assert exec0["execution"]["primitive"] == "do_mode=healthy"
    assert exec0["execution"]["capability_nonce"] == "cap-001"
    assert exec0["execution"]["status"] in ("ok", "failed")

    # 事后回答④: cpd_version 随轮次自增 (3 轮 → 5 → 8)
    cpd_versions = [e["inputs"]["cpd_version"] for e in events if e["event_type"] == "decide"]
    assert cpd_versions == [5, 6, 7]
    learn_after = [e["learning"]["cpd_version_after"] for e in events
                   if e["event_type"] == "learn"]
    assert learn_after == [6, 7, 8]
    assert result["cpd_after"]["version"] == 8


@pytest.mark.asyncio
async def test_closed_loop_no_audit_when_path_missing(tmp_path):
    """audit_path=None → 不写审计, 事务正常。"""
    cpd = oskill.CategoricalCPD(
        child_states=["success", "fault"],
        counts={"degraded": [4.0, 6.0], "healthy": [8.0, 2.0]}, parents=["mode"])
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    inp = omodul.ClosedLoopInput(cpd, interventions=[
        {"action_id": "do_mode=healthy", "target_value": "healthy", "cost": 0.1}])
    cfg = omodul.ClosedLoopConfig(simulate=True, rounds=2)
    result = await omodul.closed_loop_intervene(cfg, inp, out)
    assert result["status"] == "executed"
    assert result["audit_trace_id"] is None


# =========================================================================
# 三、multi_step_plan 挂载: 五节点审计 + 因果图版本
# =========================================================================

def _build_store():
    store = omodul.CausalGraphStore() if hasattr(omodul, "CausalGraphStore") else None
    if store is None:
        from obase.causal_graph_store import CausalGraphStore
        store = CausalGraphStore()
    store.add_node("api_gateway", p_fail=0.3)
    store.add_node("db", p_fail=0.2)
    store.add_node("task_outcome")
    store.add_edge("api_gateway", "task_outcome")
    store.add_edge("db", "task_outcome")
    return store


def test_multi_step_plan_emits_full_chain(tmp_path):
    store = _build_store()
    version_before = store.version      # 2 节点 + 2 边 = 版本 5
    audit_path = str(tmp_path / "audit.jsonl")

    report = omodul.multi_step_plan(
        "task failed: db timeout after api gateway 5xx",
        store=store,
        threat_level=0.12,
        execute=True,
        repair_callback=lambda node: 0.6,
        capability_nonce="cap-007",
        notes="故障演练 #2",
        audit_path=audit_path,
    )
    assert report.audit_trace_id

    events = oprim.JsonlSink(audit_path).read_trace(report.audit_trace_id)
    types = [e["event_type"] for e in events]
    assert types == ["diagnose", "plan", "decide", "execute", "learn"]

    # 用的哪版因果图
    for e in events:
        assert e["inputs"]["graph_version"] == version_before
    # 威胁水平
    assert events[0]["inputs"]["threat_level"] == 0.12
    # 策略选择 + 效用
    assert events[2]["decision"]["chosen_strategy"] == report.strategy
    assert "utilities" in events[2]["decision"]
    # 执行 primitive + 授权 nonce
    assert events[3]["execution"]["capability_nonce"] == "cap-007"
    assert events[3]["execution"]["primitive"].startswith("do(")
    # 学习: 更新了哪些节点 CPD
    assert events[4]["learning"]["cpd_updated"] == report.cpd_updated


def test_graph_store_version_bumps_on_mutation():
    store = _build_store()
    v0 = store.version
    store.add_node("cache", p_fail=0.1)
    assert store.version == v0 + 1
    store.add_edge("cache", "task_outcome")
    assert store.version == v0 + 2


def test_cpd_version_bumps_on_update():
    cpd = oskill.CategoricalCPD(child_states=["success", "fault"], parents=["mode"])
    cpd2 = oskill.dirichlet_update(cpd, "m", "success")
    assert cpd2.version == 2
    cpd3 = oskill.ema_update(cpd2, "m", "fault", alpha=0.1)
    assert cpd3.version == 3
    # JSON 往返保留版本
    assert oskill.CategoricalCPD.from_dict(cpd3.to_dict()).version == 3


# =========================================================================
# 四、HTTP 回放层
# =========================================================================

def test_audit_route_replay(tmp_path, monkeypatch):
    # 先写一条真实链路 (走 closed_loop)
    import asyncio

    from fastapi.testclient import TestClient

    from server.app import app

    cpd = oskill.CategoricalCPD(
        child_states=["success", "fault"],
        counts={"degraded": [4.0, 6.0], "healthy": [8.0, 2.0]}, parents=["mode"])
    out = tmp_path / "reports"
    out.mkdir(parents=True, exist_ok=True)
    audit_path = str(tmp_path / "audit.jsonl")
    inp = omodul.ClosedLoopInput(cpd, interventions=[
        {"action_id": "do_mode=healthy", "target_value": "healthy", "cost": 0.1}],
        capability_nonce="cap-route")
    cfg = omodul.ClosedLoopConfig(simulate=True, rounds=2, audit_path=audit_path)
    result = asyncio.run(omodul.closed_loop_intervene(cfg, inp, out))
    trace_id = result["audit_trace_id"]

    # 用同一审计目录的适配器 (monkeypatch 路由级目录到 tmp)
    import server.routes.audit as audit_route_mod
    audit_route_mod._audit = __import__("server.audit", fromlist=["VeyaAudit"]).VeyaAudit(
        audit_dir=str(tmp_path))

    # audit 路由已挂 require_user (强制登录) → 注册测试用户拿 token
    import server.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_DB_PATH", tmp_path / "auth.db")
    auth_mod._init_db()  # monkeypatch 换库后需手动建表

    c = TestClient(app)
    reg = c.post("/api/v1/auth/register", json={"username": "audit_test", "password": "secret123"})
    assert reg.status_code == 200, reg.text
    token = reg.json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}

    r = c.get(f"/audit/{trace_id}", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["trace_id"] == trace_id
    assert [e["event_type"] for e in body["events"]] == \
        ["diagnose", "decide", "execute", "learn"] * 1 + ["decide", "execute", "learn"]
    # 取证字段
    assert body["events"][0]["inputs"]["cpd_version"] == 1
    assert body["events"][2]["execution"]["capability_nonce"] == "cap-route"

    r2 = c.get("/audit/traces", headers=hdr)
    assert r2.status_code == 200
    assert any(t["trace_id"] == trace_id for t in r2.json()["traces"])

    r3 = c.get("/audit/ghost-trace", headers=hdr)
    assert r3.status_code == 404
