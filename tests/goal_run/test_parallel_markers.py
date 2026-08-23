"""[P] 并行标记提取(smart-ralph 内化，见 memory project_veya_pi_gap_audit)。"""

from __future__ import annotations

from server.goal_run.parallel_markers import extract_parallel_task_ids


def test_extracts_marked_tasks():
    tasks_md = (
        "- [ ] [P] T1: Scout auth module\n"
        "- [ ] T2: Build API endpoint\n"
        "  Depends: T1\n"
        "- [ ] [P] T3: Write docs\n"
    )
    assert extract_parallel_task_ids(tasks_md) == {"T1", "T3"}


def test_no_markers_returns_empty_set():
    tasks_md = "- [ ] T1: A\n- [ ] T2: B\n  Depends: T1\n"
    assert extract_parallel_task_ids(tasks_md) == set()


def test_empty_input_returns_empty_set():
    assert extract_parallel_task_ids("") == set()
    assert extract_parallel_task_ids("   \n  ") == set()


def test_dotted_subtask_ids():
    tasks_md = "- [ ] [P] T3.1: Sub-task\n- [ ] [P] T3.2: Another sub-task\n"
    assert extract_parallel_task_ids(tasks_md) == {"T3.1", "T3.2"}


def test_checked_boxes_also_detected():
    tasks_md = "- [x] [P] T1: Already done but still parallel-marked\n"
    assert extract_parallel_task_ids(tasks_md) == {"T1"}


def test_marker_must_be_right_after_checkbox_not_elsewhere():
    """[P] 出现在标题文字里不算标记, 只认 checkbox 后紧跟的那个。"""
    tasks_md = "- [ ] T1: This is a [P] mention inside the title, not a real marker\n"
    assert extract_parallel_task_ids(tasks_md) == set()


def test_star_bullet_style_also_supported():
    tasks_md = "* [ ] [P] T1: Star bullet style\n"
    assert extract_parallel_task_ids(tasks_md) == {"T1"}
