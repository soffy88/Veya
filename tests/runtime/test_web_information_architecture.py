from __future__ import annotations

from pathlib import Path

WEB = Path("apps/web/src")
MAIN = (WEB / "routes/+page.svelte").read_text(encoding="utf-8")
TASKS = (WEB / "lib/components/TaskCenterPanel.svelte").read_text(encoding="utf-8")
SEARCH = (WEB / "lib/components/SearchPalette.svelte").read_text(encoding="utf-8")
CHAT = (WEB / "lib/components/ChatConsole.svelte").read_text(encoding="utf-8")
PROCESS = (WEB / "lib/components/WorkProcess.svelte").read_text(encoding="utf-8")
WORKBENCH = (WEB / "routes/workbench/[task_id]/+page.svelte").read_text(encoding="utf-8")
MISSION_NEW = (WEB / "routes/missions/new/+page.svelte").read_text(encoding="utf-8")
MISSION_LIST = (WEB / "routes/missions/+page.svelte").read_text(encoding="utf-8")
MISSION_DETAIL = (WEB / "routes/missions/[mission_id]/+page.svelte").read_text(encoding="utf-8")


def test_work_is_one_user_facing_history_surface() -> None:
    assert "Work 历史" in MAIN
    assert "Work 历史" in TASKS
    assert "listMissions" in TASKS
    assert 'api("gateway", "api/v1/tasks"' in TASKS
    assert "<table" not in TASKS
    assert "sm:grid-cols-2" in TASKS
    assert 'href="/?view=work"' in MISSION_LIST
    assert 'href="/?view=tasks"' in MISSION_DETAIL


def test_search_covers_work_projects_files_and_artifacts() -> None:
    assert "api/v1/supervision/missions" in SEARCH
    assert "matchedMissions" in SEARCH
    assert "matchedProjects" in SEARCH
    assert "matchedFiles" in SEARCH
    assert "matchedArtifacts" in SEARCH
    assert "openProject" in SEARCH
    assert "veya:insert-chat-text" in SEARCH


def test_chat_uses_aggregated_work_process_without_raw_tool_json() -> None:
    assert "<WorkProcess" in CHAT
    assert "工作过程" in PROCESS
    assert "JSON.stringify(ev.tool_args)" not in PROCESS
    assert "读取文件" in PROCESS
    assert "运行测试" in PROCESS


def test_workbench_is_mobile_first_and_hides_engineering_details() -> None:
    assert "xl:grid-cols-" in WORKBENCH
    assert "md:hidden" in WORKBENCH
    assert "pendingApprovalCount" in WORKBENCH
    assert "Developer details" in WORKBENCH
    assert WORKBENCH.index("Developer details") < WORKBENCH.index("Task / GoalRun")


def test_mission_executor_is_progressively_disclosed() -> None:
    advanced = MISSION_NEW.index("{#if advanced}")
    executor = MISSION_NEW.index("执行引擎")
    assert executor > advanced
    assert "工作目录" in MISSION_NEW


def test_heavy_surfaces_are_lazy_loaded() -> None:
    for component in (
        "ProductShell.svelte",
        "Dashboard.svelte",
        "PlanBoard.svelte",
        "GitPanel.svelte",
        "ProjectMap.svelte",
        "FlowConsole.svelte",
        "PluginPanel.svelte",
        "AutomationPanel.svelte",
        "KanbanPanel.svelte",
        "TaskCenterPanel.svelte",
        "PersonalContextPanel.svelte",
    ):
        assert f'await import("$lib/components/{component}")' in MAIN
