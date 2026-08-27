"""
代码智能分析 API - P1 核心能力
提供 AST 分析、符号检索、依赖图等功能
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, APIRouter, HTTPException
from server import auth as auth_mod
from pydantic import BaseModel

from veya.ast import create_ast_analyzer

router = APIRouter(
    prefix="/analysis", tags=["analysis"], dependencies=[Depends(auth_mod.require_user)]
)

# 全局 AST 分析器（实际应用中可以按需创建）
ast_analyzer = create_ast_analyzer()


class SearchRequest(BaseModel):
    query: str
    type: str | None = "signature"  # signature | name | references
    limit: int | None = 20


@router.get("/project")
async def analyze_project(project_path: str = ".") -> dict[str, Any]:
    """分析项目代码结构"""
    try:
        stats = ast_analyzer.analyze_project(project_path)

        # 获取主要函数和类
        functions = [s for s in ast_analyzer.symbols.values() if s.type == "function"]
        classes = [s for s in ast_analyzer.symbols.values() if s.type == "class"]

        return {
            "status": "success",
            "stats": stats,
            "functions": [
                {
                    "name": f.name,
                    "file": f.file_path,
                    "line": f.line,
                    "params": f.params,
                    "return_type": f.return_type,
                }
                for f in functions[:50]
            ],
            "classes": [
                {"name": c.name, "file": c.file_path, "line": c.line} for c in classes[:30]
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {e!s}")


@router.get("/symbols")
async def list_symbols(
    file_path: str | None = None, type_filter: str | None = None, limit: int = 100
) -> dict[str, Any]:
    """列出代码符号"""
    try:
        symbols = list(ast_analyzer.symbols.values())

        if file_path:
            symbols = [s for s in symbols if s.file_path == file_path]

        if type_filter:
            symbols = [s for s in symbols if s.type == type_filter]

        return {
            "status": "success",
            "count": len(symbols),
            "symbols": [
                {
                    "name": s.name,
                    "type": s.type,
                    "file": s.file_path,
                    "line": s.line,
                    "docstring": s.docstring[:200] + "..."
                    if s.docstring and len(s.docstring) > 200
                    else s.docstring,
                }
                for s in symbols[:limit]
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list symbols: {e!s}")


@router.get("/dependencies")
async def get_dependency_graph() -> dict[str, Any]:
    """获取调用依赖图"""
    try:
        graph = ast_analyzer.get_call_graph()
        return {"status": "success", "graph": graph}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get dependency graph: {e!s}")


@router.post("/search")
async def search_symbols(request: SearchRequest) -> dict[str, Any]:
    """搜索代码符号"""
    try:
        if request.type == "signature":
            results = ast_analyzer.search_by_signature(request.query)
        elif request.type == "name":
            results = [s for s in ast_analyzer.symbols.values() if request.query in s.name]
        elif request.type == "references":
            refs = ast_analyzer.find_references(request.query)
            return {"status": "success", "count": len(refs), "results": refs[: request.limit]}
        else:
            raise ValueError(f"Unknown search type: {request.type}")

        return {
            "status": "success",
            "count": len(results),
            "results": [
                {
                    "name": s.name,
                    "type": s.type,
                    "file": s.file_path,
                    "line": s.line,
                    "params": s.params,
                    "return_type": s.return_type,
                }
                for s in results[: request.limit]
            ],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {e!s}")


@router.get("/summary/{file_path:path}")
async def get_file_summary(file_path: str) -> dict[str, Any]:
    """获取文件摘要"""
    try:
        summary = ast_analyzer.generate_code_summary(file_path)
        return {"status": "success", "summary": summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate summary: {e!s}")
