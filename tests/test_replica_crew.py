"""三期 (KiroCrew 层) 门禁 — G7 教训结晶 / G8 触发器统一注册。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for lib in ("oprim", "omodul", "oservi", "obase", "oskill"):
    sys.path.insert(0, str(ROOT / "platform" / "3O" / lib))

from obase.event_bus import EventBus  # noqa: E402
from omodul.skill_crystallize import _lesson_signature, skill_crystallize  # noqa: E402
from oservi.trigger_register import TriggerRegistry, trigger_register  # noqa: E402

# =========================================================================
# G7 — 教训→技能结晶
# =========================================================================


def test_lesson_signature_stable():
    s1 = _lesson_signature("verify_failed", "mod_a", {"test": "x"})
    s2 = _lesson_signature("verify_failed", "mod_a", {"test": "x"})
    s3 = _lesson_signature("verify_failed", "mod_a", {"test": "y"})
    assert s1 == s2  # 同一模式签名稳定
    assert s1 != s3  # 证据不同 → 不同模式


def test_skill_crystallize_threshold(tmp_path, monkeypatch):
    """同一失败模式出现 3 次 → 自动结晶为技能包。"""
    import importlib

    sc = importlib.import_module("omodul.skill_crystallize")

    monkeypatch.setattr(sc, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(sc, "SKILLS_DIR", tmp_path / "skills")

    lesson = {
        "trigger_type": "verify_failed",
        "evidence": {"test": "test_login"},
        "subject_ref": "auth",
        "lesson": "登录测试失败: 会话过期未处理",
    }

    r1 = skill_crystallize(
        lesson, recurrence_threshold=3, lessons_file=str(tmp_path / "lessons.json")
    )
    assert r1["crystallized"] is False and r1["count"] == 1
    r2 = skill_crystallize(
        lesson, recurrence_threshold=3, lessons_file=str(tmp_path / "lessons.json")
    )
    assert r2["count"] == 2
    r3 = skill_crystallize(
        lesson, recurrence_threshold=3, lessons_file=str(tmp_path / "lessons.json")
    )
    assert r3["crystallized"] is True
    assert r3["ledger_registered"] is True
    assert r3["skill_module"] and Path(r3["skill_module"]).exists()

    # 技能包结构: manifest + run.py
    pkg = Path(r3["skill_module"]).parent
    assert (pkg / "manifest.json").exists()
    assert "login" in (pkg / "run.py").read_text() or "登录" in (pkg / "run.py").read_text()


def test_skill_crystallize_dedup(tmp_path, monkeypatch):
    """已存在同名技能 → dedup (不重复结晶)。"""
    import importlib

    sc = importlib.import_module("omodul.skill_crystallize")

    monkeypatch.setattr(sc, "LESSONS_FILE", tmp_path / "lessons.json")
    monkeypatch.setattr(sc, "SKILLS_DIR", tmp_path / "skills")

    # 预置同名技能 (一级技能库)
    (tmp_path / "skills" / "dupe_skill").mkdir(parents=True)

    lesson = {
        "trigger_type": "retrieval_miss",
        "evidence": {"q": "x"},
        "subject_ref": "kb",
        "lesson": "检索未命中",
    }
    # 先结晶 (生成技能)
    r = None
    for _ in range(3):
        r = skill_crystallize(
            lesson,
            recurrence_threshold=3,
            skill_name="dupe_skill",
            lessons_file=str(tmp_path / "lessons.json"),
        )
    assert r["dedup"] is True
    assert r["crystallized"] is False  # 查重后不重复生成


# =========================================================================
# G8 — 触发器统一注册
# =========================================================================


def test_trigger_kinds_and_webhook_endpoint(tmp_path):
    reg = TriggerRegistry(triggers_file=str(tmp_path / "triggers.json"))
    b = reg.register("webhook", "/hooks/ci", "wf_1")
    assert b["binding_id"].startswith("trg_")
    assert b["endpoint"] == f"/api/v1/trigger/{b['binding_id']}"
    assert b["active"] is True

    b2 = reg.register("cron", "0 9 * * *", "wf_2")
    assert b2["kind"] == "cron" and b2["endpoint"] == ""

    # 未知类型拒绝
    with pytest.raises(ValueError):
        reg.register("nonsense", "x", "wf")


def test_trigger_event_binding_fires_callback(tmp_path):
    """event 触发器: 事件到达 → 回调触发 + 计数 + 审计。"""
    bus = EventBus()
    reg = TriggerRegistry(event_bus=bus, triggers_file=str(tmp_path / "triggers.json"))
    fired: list[dict] = []
    b = reg.register("event", "agent.end", "wf_1", callback=lambda payload: fired.append(payload))
    assert b["kind"] == "event"

    bus.publish("agent.end", {"agent_id": "w1", "status": "done"})
    assert fired, "事件触发回调未执行"
    assert fired[0]["event"] == "agent.end"

    # 计数 + 持久化
    bindings = reg.list()
    assert bindings[0]["trigger_count"] == 1
    assert bindings[0]["last_triggered_at"] > 0
    # 重启后绑定仍在 (持久化)
    reg2 = TriggerRegistry(event_bus=bus, triggers_file=str(tmp_path / "triggers.json"))
    assert reg2.list()[0]["binding_id"] == b["binding_id"]


def test_trigger_manual_and_deactivate(tmp_path):
    reg = TriggerRegistry(triggers_file=str(tmp_path / "triggers.json"))
    b = reg.register("cron", "*/5 * * * *", "wf_1")
    out = reg.trigger(b["binding_id"], {"manual": True})
    assert out["ok"] is True and out["workflow_id"] == "wf_1"

    assert reg.deactivate(b["binding_id"]) is True
    out2 = reg.trigger(b["binding_id"], {})
    assert out2["ok"] is False


def test_trigger_register_entrypoint(tmp_path):
    from oservi.trigger_register import TriggerRegistry as TR

    reg = TR(triggers_file=str(tmp_path / "triggers.json"))
    out = trigger_register({"kind": "webhook", "spec": "/hooks/x"}, "wf_9", registry=reg)
    assert out["endpoint"].startswith("/api/v1/trigger/")


# =========================================================================
# 账本 (三期收尾)
# =========================================================================


def test_replica_ledger_all_registered():
    from server.operator_ledger import replica_ledger_summary

    summary = replica_ledger_summary()
    assert len(summary) == 8
    assert all(s["status"] == "registered" for s in summary), [
        s for s in summary if s["status"] != "registered"
    ]
