"""
语义搜索 API - P2 核心能力
提供基于 embedding 的代码语义搜索、相似度匹配等功能
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hicode.semantic_search import create_semantic_search

router = APIRouter(prefix="/search", tags=["search"])

# 全局语义搜索引擎
semantic_search = create_semantic_search()


class SearchRequest(BaseModel):
    query: str
    top_k: int | None = 10
    hybrid: bool | None = True


class IndexRequest(BaseModel):
    project_path: str
    file_extensions: list[str] | None = None


@router.post("/index")
async def index_project(request: IndexRequest) -> dict[str, Any]:
    """索引项目代码"""
    try:
        indexed = semantic_search.index_project(request.project_path, request.file_extensions)
        return {"status": "success", "indexed_files": indexed, "stats": semantic_search.get_stats()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Indexing failed: {e!s}")


@router.post("/search")
async def search_code(request: SearchRequest) -> dict[str, Any]:
    """语义搜索代码"""
    try:
        results = semantic_search.search(request.query, top_k=request.top_k, hybrid=request.hybrid)

        return {
            "status": "success",
            "count": len(results),
            "results": [
                {
                    "id": r.id,
                    "text": r.text,
                    "file_path": r.file_path,
                    "score": r.score,
                    "start_line": r.start_line,
                    "end_line": r.end_line,
                    "metadata": r.metadata,
                }
                for r in results
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e!s}")


@router.get("/recommend")
async def recommend_completion(partial_code: str, top_k: int = 3) -> dict[str, Any]:
    """推荐代码补全"""
    try:
        recommendations = semantic_search.recommend_completion(partial_code)
        return {
            "status": "success",
            "count": len(recommendations),
            "recommendations": recommendations[:top_k],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Recommendation failed: {e!s}")


@router.get("/stats")
async def get_search_stats() -> dict[str, Any]:
    """获取搜索统计"""
    try:
        stats = semantic_search.get_stats()
        return {"status": "success", "stats": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get stats: {e!s}")
