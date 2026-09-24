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


def test_main_shell_deep_links_are_ssr_safe() -> None:
    assert 'import { browser } from "$app/environment"' in MAIN
    assert 'if (!browser) return;' in MAIN
    assert 'window.location.search' in MAIN


def test_workbench_default_activity_is_human_readable() -> None:
    assert "INTERNAL_ACTIVITY_TOPICS" in WORKBENCH
    assert "activityLabel(event)" in WORKBENCH
    assert 'class="mt-0.5 block text-xs text-terminal-dim"' in WORKBENCH
    assert "min-h-11" in WORKBENCH


def test_primary_mobile_actions_use_44px_targets() -> None:
    assert "min-h-11" in TASKS
    assert "size-11" in TASKS
    assert "min-h-11" in MISSION_NEW


def test_chat_cleanup_is_ssr_safe() -> None:
    assert 'if (typeof window !== "undefined") window.removeEventListener' in CHAT


def test_work_cards_resist_unbroken_long_text() -> None:
    assert "min-h-52 min-w-0 flex-col" in TASKS
    assert "[overflow-wrap:anywhere]" in TASKS


def test_search_palette_traps_keyboard_focus() -> None:
    assert "dialogEl" in SEARCH
    assert 'event.key !== "Tab"' in SEARCH
    assert "event.preventDefault()" in SEARCH
    assert "focusable[nextIndex]?.focus()" in SEARCH


def test_shell_navigation_uses_touch_sized_controls() -> None:
    assert MAIN.count("min-h-11") >= 9
