"""veya.agent_project 测试 — 目录即 Agent / load_skill 渐进披露 / 声明式通道调度 (vercel/eve 内化)。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from veya.agent_project import (
    AgentLayout,
    SkillIndex,
    cron_match,
    discover_agent,
    load_channels,
    load_schedules,
    read_instructions,
)

# ── 1. 目录即 Agent ──────────────────────────────────────────────────

def _make_agent(tmp_path: Path) -> Path:
    """构造一个符合约定布局的 agent 目录。"""
    root = tmp_path / "my-agent"
    agent = root / "agent"
    (agent / "tools").mkdir(parents=True)
    (agent / "skills").mkdir()
    (agent / "channels").mkdir()
    (agent / "schedules").mkdir()
    (agent / "instructions.md").write_text("你是天气助手。", encoding="utf-8")
    return root


def test_discover_project_agent_dir(tmp_path: Path):
    root = _make_agent(tmp_path)
    layout = discover_agent(root)
    assert layout is not None
    assert layout.instructions is not None
    assert layout.tools_dir is not None
    assert layout.skills_dir is not None
    assert layout.complete is True


def test_discover_agent_dir_directly(tmp_path: Path):
    root = _make_agent(tmp_path)
    layout = discover_agent(root / "agent")
    assert layout is not None
    assert layout.name == "agent"
    assert layout.complete


def test_discover_empty_dir_returns_none(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert discover_agent(empty) is None


def test_read_instructions(tmp_path: Path):
    root = _make_agent(tmp_path)
    layout = discover_agent(root)
    assert "天气助手" in read_instructions(layout)
    assert read_instructions(AgentLayout(root=tmp_path / "nope", name="n")) == ""


# ── 2. load_skill 渐进披露 ───────────────────────────────────────────

def _make_skills(tmp_path: Path) -> Path:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "forecast.md").write_text(
        "使用天气工具回答天气问题。\n", encoding="utf-8"
    )
    pkg = skills / "research"
    pkg.mkdir()
    (pkg / "SKILL.md").write_text(
        "---\ndescription: Research unfamiliar topics before answering.\n---\n"
        "When the task is novel, gather evidence first.\n",
        encoding="utf-8",
    )
    return skills


def test_skill_index_scan_and_route(tmp_path: Path):
    skills = _make_skills(tmp_path)
    index = SkillIndex(skills)
    metas = index.list_skills()
    assert {m.name for m in metas} == {"forecast", "research"}
    # 无 frontmatter 的 forecast: 首行作 description
    forecast = next(m for m in metas if m.name == "forecast")
    assert "天气工具" in forecast.description
    # 打包目录带 frontmatter
    research = next(m for m in metas if m.name == "research")
    assert "Research" in research.description


def test_skill_index_select_and_load(tmp_path: Path):
    skills = _make_skills(tmp_path)
    index = SkillIndex(skills)
    # 路由: 天气任务 → forecast
    selected = index.select("今天北京天气怎么样")
    assert selected and selected[0].name == "forecast"
    # 按需加载正文
    body = index.load("forecast")
    assert "天气工具" in body
    with pytest.raises(KeyError):
        index.load("nope")


def test_skill_index_empty_dir():
    index = SkillIndex()
    assert index.list_skills() == []


# ── 3. 声明式通道/调度 ──────────────────────────────────────────────

def test_load_channels_json_and_source(tmp_path: Path):
    channels = tmp_path / "channels"
    channels.mkdir()
    (channels / "webhook.json").write_text(
        json_dumps({"name": "webhook", "kind": "http", "route": "/incoming", "handler": "app.h"}),
        encoding="utf-8",
    )
    (channels / "slack.ts").write_text(
        'export const kind = "slack";\nexport default { kind };',
        encoding="utf-8",
    )
    specs = load_channels(channels)
    by_name = {s.name: s for s in specs}
    assert by_name["webhook"].kind == "http"
    assert by_name["webhook"].route == "/incoming"
    assert by_name["slack.ts".replace(".ts", "")].kind == "slack"


def test_load_schedules_json(tmp_path: Path):
    schedules = tmp_path / "schedules"
    schedules.mkdir()
    (schedules / "weekly.json").write_text(
        json_dumps({"name": "weekly", "cron": "0 9 * * 1", "handler": "app.weekly"}),
        encoding="utf-8",
    )
    specs = load_schedules(schedules)
    assert specs[0].cron == "0 9 * * 1"


def test_cron_match():
    assert cron_match("0 9 * * *", datetime(2026, 8, 8, 9, 0)) is True
    assert cron_match("0 9 * * *", datetime(2026, 8, 8, 9, 30)) is False
    assert cron_match("* * * * *", datetime(2026, 8, 8, 12, 34)) is True
    assert cron_match("*/5 * * * *", datetime(2026, 8, 8, 12, 0)) is True  # 0%5==0
    assert cron_match("*/5 * * * *", datetime(2026, 8, 8, 12, 3)) is False
    assert cron_match("0 9 * * 1", datetime(2026, 8, 3, 9, 0)) is True  # 周一
    assert cron_match("0,30 * * * *", datetime(2026, 8, 8, 12, 30)) is True
    assert cron_match("8-10 * * * *", datetime(2026, 8, 8, 12, 9)) is True
    assert cron_match("bad expr", datetime.now()) is False


def json_dumps(data: dict) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)
