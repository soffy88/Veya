"""code_review_graph 技能包 — 代码审查知识图谱 (CRG CLI 桥)。

变更审查工作流: ensure(懒构建) → impact(影响面) → query(调用链/测试) → 动手。
零 veya 反向依赖 (oprim 原语 + subprocess)。
"""

from __future__ import annotations

from typing import Any

from oprim._code_review_graph import (
    graph_communities,
    graph_dead_code,
    graph_ensure,
    graph_impact,
    graph_query,
    graph_status,
)


def main(action: str, query_type: str = "", target: str = "",
         repo_path: str = "", **_: Any) -> dict[str, Any]:
    """执行 CRG 操作, 返回结构化结果。"""
    if action == "status":
        return graph_status()
    if action == "ensure":
        return graph_ensure(repo_path)
    if action == "query":
        if not query_type or not target:
            return {"ok": False, "error": "query 需要 query_type + target"}
        return graph_query(query_type, target)
    if action == "impact":
        if not target:
            return {"ok": False, "error": "impact 需要 target"}
        return graph_impact(target)
    if action == "dead_code":
        return graph_dead_code()
    if action == "communities":
        return graph_communities()
    return {"ok": False, "error": f"未知 action: {action}"}
