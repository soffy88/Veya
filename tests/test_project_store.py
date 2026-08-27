"""server.project_store 测试 — 状态映射表 + M1 .veya-project/ Store。"""

from __future__ import annotations

from pathlib import Path

from server.project_store import (
    ProjectAskRequest,
    ProjectAskResponse,
    ProjectStore,
    to_project_status,
)

# ── 状态映射表 ───────────────────────────────────────────────────────


def test_to_project_status_done_is_completed():
    assert to_project_status("done") == ("completed", None)


def test_to_project_status_failed_uses_error_as_reason():
    assert to_project_status("failed", "boom") == ("blocked", "boom")


def test_to_project_status_failed_without_error_has_default_reason():
    status, reason = to_project_status("failed", "")
    assert status == "blocked"
    assert reason  # 永不为空


def test_to_project_status_cancelled_is_blocked():
    status, reason = to_project_status("cancelled", "user stop")
    assert status == "blocked"
    assert reason == "user stop"


def test_to_project_status_non_terminal_passthrough():
    assert to_project_status("queued") == ("queued", None)
    assert to_project_status("running") == ("running", None)


# ── project_ask 契约草案 ─────────────────────────────────────────────


def test_project_ask_request_defaults():
    req = ProjectAskRequest(project_root="/tmp/x", request="do the thing")
    assert req.assignee_hint is None


def test_project_ask_response_terminal_only_in_practice():
    resp = ProjectAskResponse(task_id="t1", status="blocked", block_reason="worker failed")
    assert resp.status == "blocked"
    assert resp.artifacts == []


# ── M1 Store ─────────────────────────────────────────────────────────


def test_ensure_layout_creates_expected_tree(tmp_path: Path):
    store = ProjectStore(tmp_path)
    store.ensure_layout()
    veya_dir = tmp_path / ".veya-project"
    assert veya_dir.is_dir()
    assert (veya_dir / "workers").is_dir()
    assert (veya_dir / "output").is_dir()
    assert (veya_dir / "runs").is_dir()
    assert (veya_dir / "PROJECT_STATE.md").exists()
    assert (veya_dir / "DECISIONS.md").exists()
    assert (veya_dir / "LESSONS.md").exists()


def test_ensure_layout_does_not_clobber_existing_state(tmp_path: Path):
    store = ProjectStore(tmp_path)
    store.ensure_layout()
    store.write_state("# custom state\n")
    store.ensure_layout()  # 再次调用不得覆盖
    assert store.read_state() == "# custom state\n"


def test_write_read_state_roundtrip(tmp_path: Path):
    store = ProjectStore(tmp_path)
    store.write_state("# Project State\n\nhello\n")
    assert "hello" in store.read_state()


def test_append_decision_and_lesson(tmp_path: Path):
    store = ProjectStore(tmp_path)
    store.append_decision("## 2026-08-15 — chose X\n- Reason: because Y")
    store.append_decision("## 2026-08-16 — chose Z\n- Reason: because W")
    text = store.read_decisions()
    assert "chose X" in text and "chose Z" in text
    # 追加式: 顺序保留
    assert text.index("chose X") < text.index("chose Z")

    store.append_lesson("## 2026-08-15 — learned something")
    assert "learned something" in store.read_lessons()


def test_queue_mirror_roundtrip(tmp_path: Path):
    store = ProjectStore(tmp_path)
    assert store.load_queue_mirror() == {"tasks": []}
    snapshot = {"tasks": [{"id": "t1", "status": "completed"}]}
    store.save_queue_mirror(snapshot)
    assert store.load_queue_mirror() == snapshot


def test_run_dir_creates_and_returns_directory(tmp_path: Path):
    store = ProjectStore(tmp_path)
    d = store.run_dir("tsk_001")
    assert d.is_dir()
    assert d == tmp_path / ".veya-project" / "runs" / "tsk_001"


def test_project_root_scoping_is_independent_of_home_veya(tmp_path: Path):
    """.veya-project/ 必须是项目内相对目录, 不解析到 ~/.veya/ (作用域不同)。"""
    store = ProjectStore(tmp_path)
    assert store.dir == tmp_path / ".veya-project"
    assert ".veya-project" != ".veya"


# ── Understand 门禁 artifact: runs/<task_id>/understand.json ────────────


def test_write_read_understand_roundtrip(tmp_path: Path):
    store = ProjectStore(tmp_path)
    data = {"task_id": "tsk_1", "decision": "ask", "questions": ["q1"]}
    store.write_understand("tsk_1", data)
    assert store.read_understand("tsk_1") == data


def test_read_understand_missing_task_returns_none(tmp_path: Path):
    store = ProjectStore(tmp_path)
    assert store.read_understand("does_not_exist") is None
