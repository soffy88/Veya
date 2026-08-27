"""Project map REST — 项目图谱 (P5, 借鉴 ccgui Project Map)。

复用 codebase memory 的 AST 图谱后端 (Neo4j-style graph): 文件/符号节点 +
依赖/调用边。只读查询。前端 ProjectMap 渲染为依赖浏览器。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["project-map"])


async def _connector():
    from server.codebase_memory import get_connector

    conn = get_connector()
    if not conn.ready:
        await conn.start()
    if not conn.ready:
        raise HTTPException(status_code=503, detail="codebase 图谱未就绪 (索引服务不可达)")
    return conn


@router.get("/api/v1/graph/schema")
async def graph_schema() -> dict:
    """图谱节点/关系类型 (供前端理解数据结构)。"""
    try:
        conn = await _connector()
    except HTTPException:
        raise
    try:
        rows = await conn.query_cypher("CALL db.schema.visualization()", max_rows=5)
    except Exception as exc:  # noqa: BLE001
        # 兼容无 schema 可视化的后端: 返回空, 前端降级
        return {"nodes": [], "relationships": [], "error": str(exc)[:120]}
    return {"nodes": rows[:1], "relationships": []}


@router.get("/api/v1/graph/files")
async def graph_files(limit: int = 200) -> dict:
    """文件级依赖图: 文件节点 + import 依赖边 (供项目图谱渲染)。"""
    try:
        conn = await _connector()
    except HTTPException:
        raise
    limit = max(10, min(int(limit), 500))
    # 未索引时先索引 (AST 全量 ~5s, 幂等; 已索引则跳过)
    try:
        await conn.ensure_indexed(force=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"索引失败: {exc}")
    cypher = "MATCH (a:File)-[r]->(b:File) RETURN a.name AS src, b.name AS dst LIMIT 500"
    try:
        rows = await conn.query_cypher(cypher, max_rows=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"图谱查询失败: {exc}")
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    for r in rows:
        # query_graph 返回 rows 每行为 list [src, dst] (兼容 dict)
        src = str(r[0] if isinstance(r, list) else (r.get("src") or "")).strip()
        dst = str(r[1] if isinstance(r, list) else (r.get("dst") or "")).strip()
        if not src or not dst or src == dst:
            continue
        nodes.setdefault(src, {"id": src, "type": "file", "deps": 0, "dependents": 0})
        nodes.setdefault(dst, {"id": dst, "type": "file", "deps": 0, "dependents": 0})
        nodes[src]["deps"] += 1
        nodes[dst]["dependents"] += 1
        edges.append({"src": src, "dst": dst, "weight": 1})
    return {"nodes": list(nodes.values()), "edges": edges, "total": len(edges)}
