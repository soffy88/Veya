"""Veya Workspace RAG: 全局代码库语境引擎(薄适配层)。

3O 单一来源 (§1.4): 引擎本体已固化为主库 oskill.workspace_rag.WorkspaceRAGSkill
(组合 oprim._ast_chunk / _vector_encode / _distance + obase.rag_index_store)。
本层只做两件事:
1. 把词频 embedding provider 注册进 obase.ProviderRegistry(主库 vector_encode 消费);
2. 保留 Veya 既有 API(WorkspaceRAGEngine / get_rag_engine / reset_rag_engine)。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from veya.platform import obase as _load_obase
from veya.platform import oprim as _load_oprim
from veya.platform import oskill as _load_oskill

_oprim = _load_oprim()
_oskill = _load_oskill()

logger = logging.getLogger("rag")


class _WordFreqEmbed:
    """词频 embedding provider(veya 侧装配, 供 oprim.vector_encode 调用)。

    §1.4: 主库提供 vector_encode 原子, veya 负责注册 provider 实现;
    未来换 bge-m3 / OpenAI embedding 只需注册新 provider, RAG 零改动。
    """

    def __init__(self) -> None:
        from veya.semantic_search import EmbeddingModel

        self._model = EmbeddingModel()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(texts)


_registered_embedding = False


def _ensure_embedding_provider() -> None:
    """把词频 embedding 注册进 obase ProviderRegistry(幂等)。"""
    global _registered_embedding
    if _registered_embedding:
        return
    try:
        reg = _load_obase().ProviderRegistry.get()
        reg._generic.setdefault("embedding", {})
        reg._generic["embedding"].setdefault("default", _WordFreqEmbed())
        _registered_embedding = True
    except Exception:
        logger.warning("[Workspace RAG] embedding provider 注册失败, 使用主库 stub")


class WorkspaceRAGEngine:
    """全局代码库语境引擎(委托主库 oskill.workspace_rag 技能)。"""

    def __init__(
        self,
        workspace_root: str | Path | None = None,
        *,
        persist_index: bool = False,  # 默认不落盘(部署时可显式开启)
    ):
        default_root = Path(__file__).resolve().parent.parent
        _ensure_embedding_provider()
        self.workspace_root = (
            Path(workspace_root or os.environ.get("VEYA_WORKSPACE", str(default_root)))
            .expanduser()
            .resolve()
        )
        self._skill = _oskill.workspace_rag.WorkspaceRAGSkill(
            workspace_root=self.workspace_root,
            persist_index=persist_index,
        )

    def reindex_workspace(self, force: bool = False) -> str:
        return self._skill.reindex_workspace(force=force)

    def search_context(self, query: str, top_k: int = 3) -> str:
        return self._skill.search_context(query, top_k=top_k)

    def get_stats(self) -> dict[str, Any]:
        return self._skill.get_stats()


# 模块级惰性单例(主脑/工具共享)
_rag_engine: WorkspaceRAGEngine | None = None


def get_rag_engine(workspace_root: str | Path | None = None) -> WorkspaceRAGEngine:
    global _rag_engine
    if _rag_engine is None:
        _rag_engine = WorkspaceRAGEngine(workspace_root=workspace_root)
    return _rag_engine


def reset_rag_engine() -> None:
    """测试用: 重置全局单例。"""
    global _rag_engine
    _rag_engine = None
