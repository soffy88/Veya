"""veya.memory_hub — VEYA 记忆中枢 (TencentDB Agent Memory 机制装配)。

组合 oskill 三原语, 让 veya 具备跨会话记忆能力:
  * **remember** — 记录对话 (L0) 进 DistillPipeline, 达到阈值自动 L1 蒸馏;
  * **recall** — 双层检索: L2/L3 快速引导 + RRF 混合回退 L1 (BM25 稀疏,
    向量结果可注入);
  * **equip** — 按 Agent 装配记忆 loadout (AssetRegistry + ACL 过滤);
  * 持久化到 ~/.veya/memory/hub.json, 跨会话/跨进程复用。

蒸馏/摘要函数可注入 (默认用确定性规则降级, 不依赖 LLM 也能跑通闭环)。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from oskill.memory_assets import (
    ACL,
    VISIBILITY_TEAM,
    AssetRegistry,
    MemoryAsset,
    Principal,
)
from oskill.memory_layers import (
    ATOM_FACT,
    DistillPipeline,
    L1Atom,
    L2Scenario,
    L3Persona,
)
from oskill.rrf_retrieval import (
    RetrievalBudget,
    bm25_score,
    hybrid_search,
    tokenize,
)

_DEFAULT_HUB_PATH = Path.home() / ".veya" / "memory" / "hub.json"

L1Fn = Callable[[list], list[L1Atom]]
L2Fn = Callable[[list[L1Atom]], list[L2Scenario]]
L3Fn = Callable[[list[L2Scenario]], L3Persona]
VectorFn = Callable[[str], list[dict[str, Any]]]


def _default_l1(entries: list) -> list[L1Atom]:
    """确定性降级 L1: 按长度/关键词提取原子 (无 LLM 也能跑通)。"""
    atoms: list[L1Atom] = []
    for entry in entries:
        text = entry.text if hasattr(entry, "text") else str(entry)
        if len(text) < 8:
            continue
        score = 6.0 if len(text) > 40 else 4.0
        atoms.append(L1Atom(kind=ATOM_FACT, text=text[:200], score=score, source_entry=text[:60]))
    return atoms


def _default_l2(atoms: list[L1Atom]) -> list[L2Scenario]:
    """确定性降级 L2: 全部原子归一个场景。"""
    if not atoms:
        return []
    scenario = L2Scenario(
        id="s1", title="会话记忆", content="; ".join(a.text[:80] for a in atoms[:5])
    )
    scenario.atom_ids = [a.text for a in atoms]
    return [scenario]


def _default_l3(scenarios: list[L2Scenario]) -> L3Persona:
    """确定性降级 L3: 拼接场景概要。"""
    if not scenarios:
        return L3Persona()
    return L3Persona(profile="; ".join(s.title for s in scenarios))


class VeyaMemoryHub:
    """VEYA 记忆中枢: 记录 → 蒸馏 → 检索 → 装配 (JSON 持久化)。"""

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        l1_fn: L1Fn | None = None,
        l2_fn: L2Fn | None = None,
        l3_fn: L3Fn | None = None,
        vector_fn: VectorFn | None = None,
        l1_threshold: int = 4,
    ) -> None:
        self.path = Path(path) if path else _DEFAULT_HUB_PATH
        self.l1_fn = l1_fn or _default_l1
        self.l2_fn = l2_fn or _default_l2
        self.l3_fn = l3_fn or _default_l3
        self.vector_fn = vector_fn
        self.pipeline = DistillPipeline(l1_threshold=l1_threshold, l2_threshold=2)
        self.registry = AssetRegistry(team_members=[])
        if self.path.exists():
            self._load()

    # ── 记录 (L0) ─────────────────────────────────────────────────────

    def remember(self, session_id: str, text: str) -> dict[str, Any]:
        """记录一条对话 (L0), 达阈值自动 L1 蒸馏。

        Args:
            session_id: 会话 id。
            text: 对话文本。

        Returns:
            {recorded, distilled: n, pending} 状态。
        """
        self.pipeline.record(text, session_id=session_id)
        distilled = 0
        if self.pipeline.should_distill():
            distilled = len(self.pipeline.distill_l1(self.l1_fn))
            if self.pipeline.should_build_scenario():
                self.pipeline.build_scenarios(self.l2_fn)
                self.pipeline.stabilize_persona(self.l3_fn)
            self._sync_assets()
        self._save()
        return {"recorded": True, "distilled": distilled, "pending": len(self.pipeline.pending_l1)}

    # ── 检索 (双层) ──────────────────────────────────────────────────

    def recall(
        self,
        query: str,
        *,
        budget: RetrievalBudget | None = None,
        vector_results: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """双层检索: L2/L3 快速引导 + RRF 混合回退 L1。

        Args:
            query: 查询。
            budget: 检索预算。
            vector_results: 稠密向量结果 (调用方注入); None 时若配置
                vector_fn 则自动调用。

        Returns:
            {quick, atoms, merged} — quick 为 L2/L3 引导文本, merged 为
            RRF 融合结果。
        """
        quick = self.pipeline.recall_quick()
        atoms = self.pipeline.recall_atoms()
        # BM25 稀疏
        query_tokens = tokenize(query)
        df: dict[str, int] = {}
        doc_tokens_all: list[list[str]] = []
        for atom in atoms:
            tokens = tokenize(atom.text)
            doc_tokens_all.append(tokens)
            for tok in set(tokens):
                df[tok] = df.get(tok, 0) + 1
        n_docs = max(len(doc_tokens_all), 1)
        avg_dl = sum(len(t) for t in doc_tokens_all) / n_docs
        fts_results = [
            {
                "id": atom.text,
                "text": atom.text,
                "score": bm25_score(query_tokens, tokens, df=df, n_docs=n_docs, avg_dl=avg_dl),
            }
            for atom, tokens in zip(atoms, doc_tokens_all, strict=True)
            if bm25_score(query_tokens, tokens, df=df, n_docs=n_docs, avg_dl=avg_dl) > 0
        ]
        fts_results.sort(key=lambda x: -x["score"])
        if vector_results is None and self.vector_fn is not None:
            vector_results = self.vector_fn(query)
        merged = hybrid_search(
            fts_results,
            vector_results or [],
            budget=budget or RetrievalBudget(),
        )
        return {
            "quick": quick,
            "atoms": [a.text for a in atoms],
            "merged": [m["id"] for m in merged],
        }

    # ── 装配 (loadout) ────────────────────────────────────────────────

    def equip(
        self,
        agent_id: str,
        principal: Principal,
        *,
        bindings: dict[str, list[str]] | None = None,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """为 Agent 装配记忆 loadout (按 ACL 过滤)。"""
        assets = self.registry.assemble_loadout(
            agent_id,
            principal,
            bindings=bindings,
            top_k=top_k,
        )
        return [a.to_dict() for a in assets]

    def grant_team_membership(self, users: list[str]) -> None:
        """配置 team 成员 (team 可见性资产可被这些用户访问)。"""
        self.registry.team_members = list(users)
        self._save()

    # ── 资产同步 + 持久化 ─────────────────────────────────────────────

    def _sync_assets(self) -> None:
        """把蒸馏产物同步为记忆资产 (chat_memory 类型)。"""
        quick = self.pipeline.recall_quick()
        self.registry.register(
            MemoryAsset(
                id="chat-memory",
                asset_type="chat_memory",
                owner="veya",
                visibility=VISIBILITY_TEAM,
                title="会话记忆",
                content=quick,
            )
        )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pipeline": {
                "atoms": [a.to_dict() for a in self.pipeline.atoms],
                "scenarios": [s.to_dict() for s in self.pipeline.scenarios.values()],
                "persona": self.pipeline.persona.profile,
                "pending": len(self.pipeline.pending_l1),
            },
            "assets": {k: v.to_dict() for k, v in self.registry.assets.items()},
            "team_members": self.registry.team_members,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        pipe = data.get("pipeline", {})
        for atom_data in pipe.get("atoms", []):
            self.pipeline.atoms.append(L1Atom(**atom_data))
        for sc_data in pipe.get("scenarios", []):
            self.pipeline.scenarios[sc_data["id"]] = L2Scenario(**sc_data)
        if pipe.get("persona"):
            self.pipeline.persona = L3Persona(profile=pipe["persona"])
        for asset_data in data.get("assets", {}).values():
            acl_data = asset_data.pop("acl", {})
            if "type" in asset_data and "asset_type" not in asset_data:
                asset_data["asset_type"] = asset_data.pop("type")
            asset = MemoryAsset(**asset_data)
            asset.acl = ACL(**acl_data)
            self.registry.register(asset)
        self.registry.team_members = list(data.get("team_members", []))

    def summary(self) -> dict[str, Any]:
        """中枢概览。"""
        return {
            "pipeline": self.pipeline.summary(),
            "registry": self.registry.summary(),
            "path": str(self.path),
        }


__all__ = ["VeyaMemoryHub"]
