"""
性能优化层 - P0 核心能力
功能：响应缓存、并行工具调用、资源预加载
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any


class LRUCache:
    """
    线程安全的 LRU 缓存
    """

    def __init__(self, max_size: int = 1000):
        self.max_size = max_size
        self._cache: dict[str, Any] = {}
        self._access_order: list[str] = []
        self._lock = threading.Lock()

    def _update_access(self, key: str) -> None:
        """更新访问顺序(调用方必须持有 self._lock)。"""
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)

        # 清理超出大小的项目
        while len(self._access_order) > self.max_size:
            oldest_key = self._access_order.pop(0)
            if oldest_key in self._cache:
                del self._cache[oldest_key]

    def get(self, key: str) -> Any | None:
        """获取缓存值"""
        with self._lock:
            value = self._cache.get(key)
            if value is not None:
                self._update_access(key)
            return value

    def set(self, key: str, value: Any) -> None:
        """设置缓存值"""
        with self._lock:
            self._cache[key] = value
            self._update_access(key)

    def delete(self, key: str) -> None:
        """删除缓存项"""
        with self._lock:
            if key in self._cache:
                del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)

    def clear(self) -> None:
        """清空缓存"""
        with self._lock:
            self._cache.clear()
            self._access_order.clear()

    def get_stats(self) -> dict[str, Any]:
        """获取缓存统计信息"""
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "hit_rate": None,  # 需要跟踪命中/未命中
                "keys": list(self._cache.keys()),
            }


def generate_cache_key(func_name: str, *args: Any, **kwargs: Any) -> str:
    """生成缓存键"""
    # 创建字典包含函数名、位置参数和关键字参数
    cache_data = {
        "func": func_name,
        "args": args,
        "kwargs": sorted(kwargs.items()) if kwargs else None,
    }

    # 序列化并哈希
    data_str = json.dumps(cache_data, sort_keys=True, default=str)
    return hashlib.md5(data_str.encode("utf-8")).hexdigest()


def cached(ttl: int = 300, max_size: int = 1000) -> Callable[[Callable], Callable]:
    """
    缓存装饰器

    参数:
    - ttl: 缓存生存时间（秒）
    - max_size: 最大缓存数量
    """
    cache = LRUCache(max_size=max_size)
    timestamps: dict[str, float] = {}  # 记录每个键的创建时间

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 生成缓存键
            key = generate_cache_key(func.__name__, *args, **kwargs)

            current_time = time.time()

            # 检查缓存
            with cache._lock:
                if key in cache._cache:
                    # 检查 TTL
                    if current_time - timestamps.get(key, 0) < ttl:
                        print(f"[Cache] Hit: {key[:8]}...")
                        return cache._cache[key]
                    else:
                        # 过期，删除
                        if key in cache._cache:
                            del cache._cache[key]
                        timestamps.pop(key, None)
                        if key in cache._access_order:
                            cache._access_order.remove(key)

            # 执行函数
            print(f"[Cache] Miss: {key[:8]}... (executing)")
            result = (
                await func(*args, **kwargs)
                if inspect.iscoroutinefunction(func)
                else func(*args, **kwargs)
            )

            # 存入缓存
            with cache._lock:
                cache._cache[key] = result
                timestamps[key] = current_time
                cache._update_access(key)

            return result

        # 添加缓存管理方法
        wrapper.cache_get = lambda k: cache.get(k)  # type: ignore[attr-defined]
        wrapper.cache_delete = lambda k: cache.delete(k)  # type: ignore[attr-defined]
        wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        wrapper.cache_stats = cache.get_stats  # type: ignore[attr-defined]

        return wrapper

    return decorator


class ParallelExecutor:
    """
    并行执行器

    功能：
    1. 并行执行多个任务
    2. 资源限制
    3. 超时控制
    4. 错误处理
    """

    def __init__(self, max_concurrent: int = 5, timeout: float = 30.0):
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def execute_task(self, task_func: Callable, *args: Any, **kwargs: Any) -> Any:
        """执行单个任务（带资源限制）"""
        async with self.semaphore:
            try:
                if inspect.iscoroutinefunction(task_func):
                    return await asyncio.wait_for(task_func(*args, **kwargs), timeout=self.timeout)
                else:
                    # 同步函数在 executor 中运行
                    loop = asyncio.get_event_loop()
                    return await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: task_func(*args, **kwargs)),
                        timeout=self.timeout,
                    )
            except TimeoutError as exc:
                raise TimeoutError(f"Task timed out after {self.timeout}s") from exc
            except Exception:
                raise

    async def execute_all(self, tasks: list[tuple]) -> list[Any]:
        """
        并行执行多个任务

        参数:
        tasks: 列表，每个元素是 (函数, *args, **kwargs)
        """
        coroutines = [
            self.execute_task(task[0], *task[1], **task[2] if len(task) > 2 else {})
            for task in tasks
        ]

        results = await asyncio.gather(*coroutines, return_exceptions=True)
        return results

    async def execute_with_progress(
        self, tasks: list[tuple], progress_callback: Callable | None = None
    ) -> list[Any]:
        """
        带进度回调的并行执行
        """
        total = len(tasks)
        completed = 0

        async def update_progress(result: Any) -> Any:
            nonlocal completed
            completed += 1
            if progress_callback:
                await progress_callback(completed, total, result)
            return result

        # 创建带进度的任务
        async def task_with_progress(task: tuple) -> Any:
            try:
                result = await self.execute_task(
                    task[0], *task[1], **task[2] if len(task) > 2 else {}
                )
                await update_progress({"status": "success", "result": result})
                return result
            except Exception as e:
                error_result = {"status": "error", "error": str(e)}
                await update_progress(error_result)
                return error_result

        # 执行所有任务
        task_coros = [task_with_progress(task) for task in tasks]
        results = await asyncio.gather(*task_coros, return_exceptions=True)
        return results


class Preloader:
    """
    预加载器

    功能：
    1. 预测需要的资源
    2. 提前加载
    3. 智能缓存
    """

    def __init__(self, cache: LRUCache | None = None):
        self.cache = cache or LRUCache(max_size=500)
        self.predictions: dict[str, Any] = {}  # 用户行为预测

    async def preload_context(self, user_query: str, project_files: list[str]) -> None:
        """预加载可能需要的上下文"""
        # 基于查询预测相关文件
        relevant_files = self._predict_relevant_files(user_query, project_files)

        # 并行加载
        executor = ParallelExecutor(max_concurrent=3)
        load_tasks: list[tuple] = []

        for file_path in relevant_files:
            task: tuple = (self._load_file_async, (file_path,), {})
            load_tasks.append(task)

        if load_tasks:
            print(f"[Preloader] Preloading {len(load_tasks)} files...")
            results = await executor.execute_all(load_tasks)

            # 存入缓存
            for file_path, content in zip(relevant_files, results, strict=False):
                if isinstance(content, str):  # 成功加载
                    self.cache.set(f"file:{file_path}", content)

    def _predict_relevant_files(self, query: str, all_files: list[str]) -> list[str]:
        """预测相关文件（简化版）"""
        query_lower = query.lower()
        relevant = []

        for file_path in all_files:
            file_lower = file_path.lower()

            if any(kw in file_lower for kw in ["test", "spec"]):
                continue  # 排除测试文件

            if (
                (
                    any(kw in query_lower for kw in ["api", "endpoint", "route"])
                    and "api" in file_lower
                )
                or (
                    any(kw in query_lower for kw in ["database", "db", "sql"])
                    and "db" in file_lower
                )
                or (
                    any(kw in query_lower for kw in ["auth", "login", "user"])
                    and "auth" in file_lower
                )
                or (
                    any(kw in query_lower for kw in ["config", "setting"])
                    and any(kw in file_lower for kw in ["config", "setting"])
                )
            ):
                relevant.append(file_path)

        # 默认包含主文件
        main_files = ["main.py", "app.py", "index.py", "server.py"]
        for main_file in main_files:
            if main_file in all_files and main_file not in relevant:
                relevant.insert(0, main_file)

        return relevant[:5]  # 最多 5 个

    async def _load_file_async(self, file_path: str) -> str:
        """异步加载文件"""
        try:
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(
                None, lambda: Path(file_path).read_text(encoding="utf-8")
            )
            return content[:10000]  # 限制大小
        except Exception as e:
            return f"Error loading {file_path}: {e!s}"


# 全局实例
default_cache = LRUCache(max_size=1000)
default_preloader = Preloader(default_cache)


# 便捷函数
def create_parallel_executor(max_concurrent: int = 5, timeout: float = 30.0) -> ParallelExecutor:
    """创建并行执行器"""
    return ParallelExecutor(max_concurrent=max_concurrent, timeout=timeout)


def create_preloader() -> Preloader:
    """创建预加载器"""
    return Preloader()


if __name__ == "__main__":
    # 测试缓存
    async def test_cached_function(x: int, y: int) -> int:
        print(f"计算中... {x} + {y}")
        await asyncio.sleep(1)
        return x + y

    # 应用缓存装饰器
    @cached(ttl=10, max_size=100)
    async def cached_add(x: int, y: int) -> int:
        return await test_cached_function(x, y)

    # 测试
    async def test_cache() -> None:
        print("第一次调用:")
        result1 = await cached_add(2, 3)
        print(f"结果: {result1}\n")

        print("第二次调用（应该命中缓存）:")
        result2 = await cached_add(2, 3)
        print(f"结果: {result2}\n")

        # 显示缓存统计
        stats = cached_add.cache_stats()  # type: ignore[attr-defined]
        print(f"缓存统计: {stats}\n")

    # 测试并行执行
    async def slow_task(name: str, delay: float) -> str:
        print(f"开始任务 {name}")
        await asyncio.sleep(delay)
        print(f"完成任务 {name}")
        return f"{name} 完成"

    async def test_parallel() -> None:
        executor = create_parallel_executor(max_concurrent=2)

        tasks: list[tuple] = [
            (slow_task, ("A", 2), {}),
            (slow_task, ("B", 1), {}),
            (slow_task, ("C", 3), {}),
            (slow_task, ("D", 1), {}),
        ]

        print("开始并行执行:\n")
        results = await executor.execute_all(tasks)
        print(f"\n结果: {results}")

    # 运行测试
    print("=== 测试缓存 ===\n")
    asyncio.run(test_cache())

    print("\n=== 测试并行执行 ===\n")
    asyncio.run(test_parallel())
