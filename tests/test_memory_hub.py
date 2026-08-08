"""veya.memory_hub 测试 — VEYA 记忆中枢 (TencentDB 机制装配)。"""

from __future__ import annotations

from pathlib import Path

from oskill.memory_assets import VISIBILITY_PRIVATE, VISIBILITY_TEAM, MemoryAsset, Principal
from oskill.rrf_retrieval import RetrievalBudget

from veya.memory_hub import VeyaMemoryHub


def _hub(tmp_path: Path) -> VeyaMemoryHub:
    return VeyaMemoryHub(path=tmp_path / "hub.json", l1_threshold=2)


def test_remember_and_threshold(tmp_path: Path):
    hub = _hub(tmp_path)
    r1 = hub.remember("s1", "用户喜欢 Python 和异步编程")
    assert r1["distilled"] == 0  # 1 条 < 阈值 2
    r2 = hub.remember("s1", "用户偏好 FastAPI 技术栈")
    assert r2["distilled"] >= 1  # 达阈值自动蒸馏
    assert hub.pipeline.summary()["atoms"] >= 1


def test_recall_quick_and_merged(tmp_path: Path):
    hub = _hub(tmp_path)
    hub.remember("s1", "用户喜欢 Python 和异步编程")
    hub.remember("s1", "用户偏好 FastAPI 技术栈")
    result = hub.recall("Python 偏好", budget=RetrievalBudget(max_items=3))
    assert result["quick"]  # L2/L3 引导
    assert result["merged"]  # RRF 融合结果


def test_recall_rrf_prefers_relevant(tmp_path: Path):
    hub = _hub(tmp_path)
    hub.remember("s1", "用户喜欢 Python 异步编程")
    hub.remember("s1", "部署到腾讯云服务器")
    result = hub.recall("Python", budget=RetrievalBudget(max_items=3))
    assert "Python" in result["merged"][0] or "Python" in result["quick"]


def test_equip_loadout(tmp_path: Path):
    hub = _hub(tmp_path)
    hub.grant_team_membership(["alice"])
    hub.registry.register(MemoryAsset(id="skill-x", owner="veya", visibility=VISIBILITY_TEAM))
    loadout = hub.equip("builder-1", Principal("alice"), bindings={"builder-1": ["skill-x"]})
    assert loadout and loadout[0]["id"] == "skill-x"


def test_persistence_across_instances(tmp_path: Path):
    hub = _hub(tmp_path)
    hub.remember("s1", "用户喜欢 Python 和异步编程")
    hub.remember("s1", "用户偏好 FastAPI 技术栈")
    hub.grant_team_membership(["alice"])

    reloaded = VeyaMemoryHub(path=tmp_path / "hub.json")
    assert reloaded.pipeline.summary()["atoms"] >= 1
    assert reloaded.registry.team_members == ["alice"]
    result = reloaded.recall("FastAPI")
    assert result["quick"] or result["merged"]


def test_privacy_preserved(tmp_path: Path):
    hub = _hub(tmp_path)
    hub.registry.register(MemoryAsset(id="secret", owner="bob", visibility=VISIBILITY_PRIVATE))
    # alice 看不到 bob 的 private 资产
    loadout = hub.equip("builder-1", Principal("alice"), bindings={"builder-1": ["secret"]})
    assert loadout == []
