"""server.skill_catalog 测试 — SKILL.md 目录的索引/搜索/晋降管理。

补 SkillsGate 调研对比出的空档: veya 对 2000+ 纯 SKILL.md 知识 skill 完全无感知
(skill_hub 只认 manifest.json 格式), 管理靠 README 手写 mv 命令。全程用 tmp_path
隔离, 不触真实 ~/.agents/。
"""

from __future__ import annotations

import time
from pathlib import Path

from server.skill_catalog import (
    build_index,
    demote,
    list_skills,
    promote,
    search,
)


def _write_skill(root: Path, name: str, description: str, body: str = "内容") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n\n{body}\n', encoding="utf-8"
    )
    return d


def _roots(hot: Path, cold: Path):
    return [(str(hot), "hot"), (str(cold), "cold")]


def test_build_index_scans_both_roots(tmp_path):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(hot, "a", "热路径技能 A")
    _write_skill(cold, "b", "冷存储技能 B")
    stats = build_index(roots=_roots(hot, cold), db=str(db))
    assert stats.scanned == 2 and stats.indexed == 2 and stats.errors == 0
    rows = list_skills(db=str(db))
    assert {r["name"] for r in rows} == {"a", "b"}
    assert {r["name"]: r["location"] for r in rows} == {"a": "hot", "b": "cold"}


def test_duplicate_name_across_roots_hot_wins(tmp_path):
    """真实磁盘上发现过: 同名 skill 在 hot 和 cold 各存一份。roots 顺序即优先级
    (hot 排前面), 后扫到的 cold 副本不该靠 ON CONFLICT 静默覆盖 location。"""
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(hot, "dup", "hot 版本描述")
    _write_skill(cold, "dup", "cold 版本描述")
    stats = build_index(roots=_roots(hot, cold), db=str(db))
    assert stats.duplicates == 1
    rows = list_skills(db=str(db))
    assert len(rows) == 1
    assert rows[0]["location"] == "hot"  # 未被 cold 那份覆盖
    assert rows[0]["description"] == "hot 版本描述"


def test_incremental_refresh_skips_unchanged(tmp_path):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(hot, "a", "描述")
    build_index(roots=_roots(hot, cold), db=str(db))
    stats2 = build_index(roots=_roots(hot, cold), db=str(db))
    assert stats2.unchanged == 1 and stats2.indexed == 0 and stats2.updated == 0


def test_refresh_picks_up_content_change(tmp_path):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    d = _write_skill(hot, "a", "旧描述")
    build_index(roots=_roots(hot, cold), db=str(db))
    time.sleep(0.01)
    (d / "SKILL.md").write_text(
        '---\nname: a\ndescription: "新描述"\n---\n\n内容\n', encoding="utf-8"
    )
    os_mtime_bump = d / "SKILL.md"
    os_mtime_bump.touch()
    stats = build_index(roots=_roots(hot, cold), db=str(db))
    assert stats.updated == 1
    rows = list_skills(db=str(db))
    assert rows[0]["description"] == "新描述"


def test_deleted_skill_removed_from_index(tmp_path):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    d = _write_skill(hot, "gone", "会被删")
    build_index(roots=_roots(hot, cold), db=str(db))
    import shutil

    shutil.rmtree(d)
    stats = build_index(roots=_roots(hot, cold), db=str(db))
    assert stats.removed == 1
    assert list_skills(db=str(db)) == []


def test_search_finds_by_description(tmp_path):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(cold, "demand-elasticity", "需求弹性理论与测量方法")
    _write_skill(cold, "unrelated", "完全不相关的另一个主题")
    build_index(roots=_roots(hot, cold), db=str(db))
    results = search("需求弹性", db=str(db))
    assert any(r["name"] == "demand-elasticity" for r in results)
    assert not any(r["name"] == "unrelated" for r in results)


def test_search_empty_db_returns_empty(tmp_path):
    db = tmp_path / "cat.db"
    assert search("anything", db=str(db)) == []


def test_list_filters_by_location_and_category(tmp_path):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(cold, "paper-advmath-en-abc123", "数学论文")
    _write_skill(cold, "paper-paper-def456", "无明确领域的论文")
    build_index(roots=_roots(hot, cold), db=str(db))
    math_only = list_skills(category="advmath", db=str(db))
    assert [r["name"] for r in math_only] == ["paper-advmath-en-abc123"]
    cold_only = list_skills(location="cold", db=str(db))
    assert len(cold_only) == 2
    # "paper" 域名段占存量绝大多数、无实际分类信息, 不应被当类目
    assert list_skills(category="paper", db=str(db)) == []


def test_mechanical_score_recorded(tmp_path):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(
        cold,
        "clean",
        "干净的技能",
        body="足够长的正文内容, 超过 mechanical_score 的最短长度门槛, 不触发过短扣分",
    )
    build_index(roots=_roots(hot, cold), db=str(db))
    rows = list_skills(db=str(db))
    assert rows[0]["mech_score"] == 1.0


def test_promote_moves_cold_to_hot(tmp_path, monkeypatch):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(cold, "x", "待晋升")
    build_index(roots=_roots(hot, cold), db=str(db))

    monkeypatch.setenv("VEYA_AGENTS_SKILLS_DIR", str(hot))  # promote() 用它定位目标根
    assert promote("x", db=str(db)) is True

    assert not (cold / "x").exists()
    assert (hot / "x" / "SKILL.md").is_file()
    row = list_skills(db=str(db))[0]
    assert row["location"] == "hot" and row["dir_path"] == str(hot / "x")


def test_demote_moves_hot_to_cold(tmp_path, monkeypatch):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(hot, "y", "待降级")
    build_index(roots=_roots(hot, cold), db=str(db))

    monkeypatch.setenv("VEYA_AGENTS_ARCHIVE_DIR", str(cold))
    assert demote("y", db=str(db)) is True

    assert not (hot / "y").exists()
    assert (cold / "y" / "SKILL.md").is_file()


def test_promote_unknown_name_returns_false(tmp_path):
    db = tmp_path / "cat.db"
    build_index(roots=[], db=str(db))
    assert promote("nope", db=str(db)) is False


def test_promote_already_hot_is_idempotent(tmp_path, monkeypatch):
    hot, cold, db = tmp_path / "hot", tmp_path / "cold", tmp_path / "cat.db"
    _write_skill(hot, "already", "已经在热路径")
    build_index(roots=_roots(hot, cold), db=str(db))
    monkeypatch.setenv("VEYA_AGENTS_SKILLS_DIR", str(hot))
    assert promote("already", db=str(db)) is True
    assert (hot / "already").exists()  # 未被误移动
