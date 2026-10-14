"""Tests for W16 Project Brain: Brief parsing, rendering, and store (Group A)."""

import json
from pathlib import Path

from server.project_brain.brief import (
    BriefRule,
    ProjectBrief,
    load_brief,
    parse_brief,
    parse_rule_line,
    render_brief,
    save_brief,
    update_brief_section,
)


def test_rule_line_parsing_standard():
    rule = parse_rule_line("- when: tests pass → do: deploy [approval: auto]")
    assert rule is not None
    assert rule.when == "tests pass"
    assert rule.do == "deploy"
    assert rule.approval == "auto"

    rule_ask = parse_rule_line("- when: code changed -> do: request review [approval: ask]")
    assert rule_ask is not None
    assert rule_ask.when == "code changed"
    assert rule_ask.do == "request review"
    assert rule_ask.approval == "ask"


def test_rule_line_parsing_omitted_approval_defaults_to_ask():
    # §11: 收尾动作默认全部 ask
    rule = parse_rule_line("- when: build succeeds → do: release")
    assert rule is not None
    assert rule.when == "build succeeds"
    assert rule.do == "release"
    assert rule.approval == "ask"


def test_rule_line_parsing_invalid():
    assert parse_rule_line("") is None
    assert parse_rule_line("   ") is None
    assert parse_rule_line("暂无") is None
    assert parse_rule_line("Just a regular note") is None
    assert parse_rule_line("- when only without action") is None


def test_parse_and_render_roundtrip():
    original = ProjectBrief(
        goal_scope="实现 Hark 式主脑，长任务持续巡检直到完成。",
        key_files=["server/project_brain/brief.py", "server/project_brain/tasks.py"],
        rules=[
            BriefRule(when="stalled", do="restart worker", approval="auto"),
            BriefRule(when="task completed", do="merge and deploy", approval="ask"),
        ],
        active_tasks="### 进行中\n- [running] 测试任务 (ID: task-1)",
        conventions="架构不可新增第二套 scheduler/approval。",
        updated_at="2026-10-14T05:00:00Z",
    )

    md = render_brief(original)
    parsed = parse_brief(md)

    assert parsed.goal_scope == original.goal_scope
    assert parsed.key_files == original.key_files
    assert len(parsed.rules) == 2
    assert parsed.rules[0].when == "stalled"
    assert parsed.rules[0].do == "restart worker"
    assert parsed.rules[0].approval == "auto"
    assert parsed.rules[1].when == "task completed"
    assert parsed.rules[1].do == "merge and deploy"
    assert parsed.rules[1].approval == "ask"
    assert parsed.active_tasks == original.active_tasks
    assert parsed.conventions == original.conventions


def test_corrupt_fallback():
    # 1. Empty string
    empty = parse_brief("")
    assert empty.goal_scope == ""
    assert empty.rules == []
    assert empty.key_files == []

    # 2. Plain text with no ## headings
    garbage = "一些随机文本，没有任何标准 markdown 标题。"
    corrupt = parse_brief(garbage)
    assert corrupt.goal_scope == garbage
    assert corrupt.rules == []
    assert corrupt.key_files == []

    # 3. Heading with garbage inside rules
    partial = (
        "## 1. 目标与范围\n"
        "目标明确\n\n"
        "## 3. 规则\n"
        "非规则行一\n"
        "- when: 合法规则 → do: 执行动作 [approval: auto]\n"
        "非规则行二\n"
    )
    parsed = parse_brief(partial)
    assert parsed.goal_scope == "目标明确"
    assert len(parsed.rules) == 1
    assert parsed.rules[0].when == "合法规则"
    assert parsed.rules[0].approval == "auto"


def test_load_and_save_brief_filesystem(tmp_path: Path):
    brief = ProjectBrief(
        goal_scope="文件系统读写测试目标",
        key_files=["file_a.py", "file_b.py"],
        rules=[BriefRule(when="done", do="notify user", approval="auto")],
        conventions="约定项 A",
    )

    save_brief(tmp_path, brief)

    state_path = tmp_path / ".veya-project" / "PROJECT_STATE.md"
    brief_json = tmp_path / ".veya-project" / "brief.json"
    assert state_path.exists()
    assert brief_json.exists()

    loaded = load_brief(tmp_path)
    assert loaded.goal_scope == brief.goal_scope
    assert loaded.key_files == ["file_a.py", "file_b.py"]
    assert len(loaded.rules) == 1
    assert loaded.rules[0].when == "done"

    raw_json = json.loads(brief_json.read_text(encoding="utf-8"))
    assert raw_json["goal_scope"] == brief.goal_scope
    assert raw_json["rules"][0]["do"] == "notify user"


def test_update_brief_section(tmp_path: Path):
    # Initialize empty layout
    load_brief(tmp_path)

    # Update goal
    update_brief_section(tmp_path, "goal_scope", "新目标")
    assert load_brief(tmp_path).goal_scope == "新目标"

    # Update files
    update_brief_section(tmp_path, "key_files", ["src/index.ts", "package.json"])
    assert load_brief(tmp_path).key_files == ["src/index.ts", "package.json"]

    # Update rules with string lines
    update_brief_section(tmp_path, "rules", "- when: check fails → do: fix [approval: ask]")
    brief = load_brief(tmp_path)
    assert len(brief.rules) == 1
    assert brief.rules[0].when == "check fails"
    assert brief.rules[0].approval == "ask"


def test_project_store_logging(tmp_path: Path):
    from server.project_store import ProjectStore

    store = ProjectStore(tmp_path)
    store.append_log("第一条日志：项目初始化完成。")
    store.append_log("第二条日志：任务启动。")

    log_content = store.read_log()
    assert "第一条日志：项目初始化完成。" in log_content
    assert "第二条日志：任务启动。" in log_content

    files = store.list_logs()
    assert len(files) == 1
    assert files[0].endswith(".md")
