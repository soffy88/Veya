"""Tests for W16 Project Brain: Tasks store and projection (Group A)."""

from pathlib import Path

from server.project_brain.brief import load_brief
from server.project_brain.tasks import (
    TASK_STATUS_ACCEPTED,
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    ProjectTask,
    get_task,
    is_conclusion_sent,
    list_tasks,
    load_tasks,
    mark_conclusion_sent,
    record_receipt,
    render_tasks_markdown,
    sync_tasks_to_brief,
    upsert_task,
)


def test_task_creation_and_serialization():
    task = ProjectTask(
        task_id="task-100",
        objective="完成数据处理脚本编写",
        goal_run_id="goal-200",
        binding_id="bind-300",
        routine_id="rout-400",
        status=TASK_STATUS_ACCEPTED,
    )
    d = task.to_dict()
    assert d["task_id"] == "task-100"
    assert d["status"] == TASK_STATUS_ACCEPTED
    assert d["created_at"] != ""
    assert d["updated_at"] != ""

    restored = ProjectTask.from_dict(d)
    assert restored.task_id == task.task_id
    assert restored.objective == task.objective
    assert restored.binding_id == task.binding_id


def test_tasks_load_save_roundtrip(tmp_path: Path):
    # Missing tasks.json returns empty dict
    tasks = load_tasks(tmp_path)
    assert tasks == {}

    task1 = ProjectTask(task_id="t1", objective="第一个任务")
    task2 = ProjectTask(task_id="t2", objective="第二个任务")
    upsert_task(tmp_path, task1)
    upsert_task(tmp_path, task2)

    loaded = load_tasks(tmp_path)
    assert len(loaded) == 2
    assert "t1" in loaded
    assert "t2" in loaded
    assert get_task(tmp_path, "t1").objective == "第一个任务"

    tasks_list = list_tasks(tmp_path)
    assert len(tasks_list) == 2


def test_task_status_updates(tmp_path: Path):
    task = ProjectTask(task_id="t1", objective="生命周期测试任务")
    upsert_task(tmp_path, task)

    task.status = TASK_STATUS_RUNNING
    upsert_task(tmp_path, task)
    assert get_task(tmp_path, "t1").status == TASK_STATUS_RUNNING

    task.status = TASK_STATUS_DONE
    upsert_task(tmp_path, task)
    assert get_task(tmp_path, "t1").status == TASK_STATUS_DONE


def test_receipts_and_deduplication(tmp_path: Path):
    task = ProjectTask(task_id="t1", objective="回执与结论测试")
    upsert_task(tmp_path, task)

    # Initial receipt recording
    record_receipt(tmp_path, "t1", "msg_001", receipt_sent=True, conclusion_sent=False)
    t = get_task(tmp_path, "t1")
    assert t.receipts["msg_001"]["receipt_sent"] is True
    assert t.receipts["msg_001"]["conclusion_sent"] is False
    assert is_conclusion_sent(t, "msg_001") is False

    # First conclusion send should be allowed
    assert mark_conclusion_sent(tmp_path, "t1", "msg_001") is True

    # Second conclusion send for the same message must be suppressed (deduplication)
    assert mark_conclusion_sent(tmp_path, "t1", "msg_001") is False


def test_render_tasks_markdown_and_sync(tmp_path: Path):
    # Empty case
    assert render_tasks_markdown([]) == "暂无进行中任务。"

    t_running = ProjectTask(task_id="t1", objective="开发新功能", status=TASK_STATUS_RUNNING)
    t_failed = ProjectTask(
        task_id="t2",
        objective="修复单元测试",
        status=TASK_STATUS_FAILED,
        fix_notes=["需要调整依赖版本"],
    )

    rendered = render_tasks_markdown([t_running, t_failed])
    assert "### 进行中" in rendered
    assert "[running] 开发新功能" in rendered
    assert "### 阻塞 / 失败" in rendered
    assert "[failed] 修复单元测试" in rendered
    assert "需要调整依赖版本" in rendered

    # Test sync to brief
    upsert_task(tmp_path, t_running)
    upsert_task(tmp_path, t_failed)
    sync_tasks_to_brief(tmp_path)

    brief = load_brief(tmp_path)
    assert "开发新功能" in brief.active_tasks
    assert "修复单元测试" in brief.active_tasks


def test_corrupt_tasks_json_fallback(tmp_path: Path):
    tasks_file = tmp_path / ".veya-project" / "tasks.json"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("invalid json {{{", encoding="utf-8")

    # Should not crash, should return empty dict
    tasks = load_tasks(tmp_path)
    assert tasks == {}
