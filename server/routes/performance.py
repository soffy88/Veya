"""
性能优化 API - P3 核心能力
提供智能缓存、增量计算、资源优化等功能
"""

from __future__ import annotations

import ast
import contextlib
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod

from veya.performance import create_incremental_computer, create_smart_cache

router = APIRouter(prefix="/performance", tags=["performance"],
              dependencies=[Depends(auth_mod.require_user)])

# 全局实例
smart_cache = create_smart_cache(max_size=1000)
incremental_computer = create_incremental_computer()


class CacheSetRequest(BaseModel):
    key: str
    value: Any
    ttl: float | None = None  # 秒
    strategy: str = "lru"  # lru, lfu, mru, ttl


class CacheGetRequest(BaseModel):
    key: str


class IncrementalRegisterRequest(BaseModel):
    key: str
    expression: str  # Python 表达式字符串
    dependencies: list[str]


class IncrementalSetValueRequest(BaseModel):
    key: str
    value: Any


@router.post("/cache/set")
async def cache_set(request: CacheSetRequest) -> dict[str, Any]:
    """设置缓存值"""
    try:
        from veya.performance import CacheStrategy

        # 转换策略字符串
        strategy_map = {
            "lru": CacheStrategy.LRU,
            "lfu": CacheStrategy.LFU,
            "mru": CacheStrategy.MRU,
            "ttl": CacheStrategy.TTL,
        }

        strategy = strategy_map.get(request.strategy.lower())
        if not strategy:
            raise HTTPException(status_code=400, detail=f"Invalid strategy: {request.strategy}")

        # 设置缓存
        smart_cache.set(request.key, request.value, ttl=request.ttl)

        return {"status": "success", "key": request.key, "ttl": request.ttl}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cache set failed: {e!s}")


@router.post("/cache/get")
async def cache_get(request: CacheGetRequest) -> dict[str, Any]:
    """获取缓存值"""
    try:
        value = smart_cache.get(request.key)

        return {
            "status": "success" if value is not None else "miss",
            "key": request.key,
            "value": value,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cache get failed: {e!s}")


@router.get("/cache/stats")
async def cache_stats() -> dict[str, Any]:
    """获取缓存统计"""
    try:
        stats = smart_cache.get_stats()
        return {
            "status": "success",
            "stats": {
                "hits": stats.hits,
                "misses": stats.misses,
                "size": stats.size,
                "max_size": stats.max_size,
                "hit_rate": stats.hit_rate,
                "total_operations": stats.total_operations,
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get cache stats: {e!s}")


@router.get("/cache/warmup")
async def cache_warmup(data: str | None = None) -> dict[str, Any]:
    """缓存预热"""
    try:
        warmup_data = {}

        # 默认预热数据
        for i in range(10):
            key = f"prewarm_key_{i}"
            warmup_data[key] = {"value": f"value_{i}", "timestamp": i}

        # 如果提供了特定数据
        if data:
            with contextlib.suppress(Exception):
                warmup_data = json.loads(data)

        smart_cache.warmup(warmup_data)

        return {"status": "success", "warmed_keys": len(warmup_data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cache warmup failed: {e!s}")


# AST 白名单: 只允许数字运算/比较/布尔, 禁止属性访问/调用/导入 (防 RCE)
_SAFE_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult,
    ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.And, ast.Or, ast.Not, ast.BoolOp,
)


def _safe_eval(expr: str, env: dict) -> Any:
    """安全求值: AST 白名单 + 无内置函数。非法节点/未知变量抛 ValueError。"""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            raise ValueError(f"unsupported node: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in env:
            raise ValueError(f"unknown var: {node.id}")
    return eval(compile(tree, "<safe>", "eval"), {"__builtins__": {}}, env)


@router.post("/incremental/register")
async def incremental_register(request: IncrementalRegisterRequest) -> dict[str, Any]:
    """注册增量计算项"""
    try:
        # 简化：使用 eval 执行表达式（实际应用中应使用 AST 解析）
        # 这是一个简化的实现
        def create_func(expr):
            def func(*args):
                # 简单的变量替换
                result = expr
                for i, arg in enumerate(args):
                    result = result.replace(f"$dep{i}$", str(arg))
                try:
                    return _safe_eval(result, {})
                except Exception:
                    return None

            return func

        func = create_func(request.expression)
        incremental_computer.register(request.key, func, request.dependencies)

        return {"status": "success", "key": request.key, "dependencies": request.dependencies}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Registration failed: {e!s}")


@router.post("/incremental/set")
async def incremental_set_value(request: IncrementalSetValueRequest) -> dict[str, Any]:
    """设置增量计算值"""
    try:
        incremental_computer.set_value(request.key, request.value)

        return {"status": "success", "key": request.key, "value": request.value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Set value failed: {e!s}")


@router.get("/incremental/get")
async def incremental_get_value(key: str) -> dict[str, Any]:
    """获取增量计算值"""
    try:
        value = incremental_computer.get_value(key)

        if value is None:
            return {"status": "not_computed", "key": key}

        return {"status": "success", "key": key, "value": value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Get value failed: {e!s}")


@router.get("/incremental/graph")
async def get_dependency_graph() -> dict[str, Any]:
    """获取依赖关系图"""
    try:
        graph = incremental_computer.get_dependency_graph()
        return {"status": "success", "graph": graph}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get graph: {e!s}")


@router.post("/memory/analyze")
async def analyze_memory_usage(objects: list[dict[str, Any]]) -> dict[str, Any]:
    """分析内存使用"""
    try:
        from veya.performance import create_resource_optimizer

        optimizer = create_resource_optimizer()

        # 转换对象
        obj_list = [o.get("data", o) for o in objects]
        stats = optimizer.optimize_memory(obj_list)

        return {"status": "success", "analysis": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory analysis failed: {e!s}")


@router.post("/cpu/analyze")
async def analyze_cpu_usage(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """分析 CPU 使用"""
    try:
        from veya.performance import create_resource_optimizer

        optimizer = create_resource_optimizer()
        stats = optimizer.optimize_cpu(tasks)

        return {"status": "success", "analysis": stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"CPU analysis failed: {e!s}")
