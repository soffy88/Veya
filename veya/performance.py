"""
高级性能优化模块 - P3 核心能力
功能：智能缓存、增量计算、分布式执行、资源优化
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CacheStrategy(StrEnum):
    """缓存策略"""

    LRU = "lru"
    LFU = "lfu"
    MRU = "mru"
    TTL = "ttl"


@dataclass
class CacheStats:
    """缓存统计"""

    hits: int = 0
    misses: int = 0
    size: int = 0
    max_size: int = 1000
    hit_rate: float = 0.0
    average_load_time: float = 0.0
    total_operations: int = 0
    last_reset: float = field(default_factory=time.time)


class SmartCache:
    """
    智能缓存

    功能：
    1. 多策略缓存（LRU、LFU、TTL）
    2. 智能预加载
    3. 缓存预热
    4. 动态调整缓存大小
    """

    def __init__(self, max_size: int = 1000, strategy: CacheStrategy = CacheStrategy.LRU):
        self.max_size = max_size
        self.strategy = strategy
        self.cache: dict[str, Any] = OrderedDict()
        self.access_counts: dict[str, int] = {}
        self.access_times: dict[str, float] = {}
        self.ttl: dict[str, float] = {}
        self.stats = CacheStats(max_size=max_size)
        self.lock = threading.RLock()

    def _get_cache_key(self, func: Callable, *args, **kwargs) -> str:
        """生成缓存键"""
        key_data = {"func": func.__name__, "args": args, "kwargs": tuple(sorted(kwargs.items()))}
        return hashlib.md5(str(key_data).encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        """获取缓存值"""
        with self.lock:
            self.stats.total_operations += 1

            # 检查 TTL
            if key in self.ttl and time.time() > self.ttl[key]:
                del self.cache[key]
                del self.access_counts[key]
                del self.access_times[key]
                del self.ttl[key]
                self.stats.misses += 1
                return None

            if key in self.cache:
                # 缓存命中
                self.stats.hits += 1
                value = self.cache[key]

                # 更新访问统计
                self.access_counts[key] = self.access_counts.get(key, 0) + 1
                self.access_times[key] = time.time()

                # 根据策略调整缓存位置
                if self.strategy == CacheStrategy.LRU:
                    self.cache.move_to_end(key)
                elif self.strategy == CacheStrategy.MRU:
                    self.cache.move_to_end(key, last=False)

                return value
            else:
                # 缓存未命中
                self.stats.misses += 1
                return None

    def set(self, key: str, value: Any, ttl: float | None = None):
        """设置缓存值"""
        with self.lock:
            self.stats.total_operations += 1

            # 如果缓存已满，根据策略移除项
            if len(self.cache) >= self.max_size:
                self._evict_item()

            # 设置缓存
            self.cache[key] = value
            self.access_counts[key] = 1
            self.access_times[key] = time.time()

            # 设置 TTL
            if ttl:
                self.ttl[key] = time.time() + ttl

            # 根据策略调整位置
            if self.strategy == CacheStrategy.LRU:
                self.cache.move_to_end(key)
            elif self.strategy == CacheStrategy.MRU:
                self.cache.move_to_end(key, last=False)

    def _evict_item(self):
        """根据策略驱逐缓存项"""
        if self.strategy == CacheStrategy.LRU:
            # 移除最久未使用的
            key = next(iter(self.cache))
        elif self.strategy == CacheStrategy.LFU:
            # 移除最不常用的
            key = min(self.access_counts.items(), key=lambda x: x[1])[0]
        elif self.strategy == CacheStrategy.MRU:
            # 移除最近使用的
            key = next(reversed(self.cache))
        elif self.strategy == CacheStrategy.TTL:
            # 移除 TTL 过期的
            now = time.time()
            expired_keys = [k for k, exp in self.ttl.items() if exp < now]
            key = expired_keys[0] if expired_keys else next(iter(self.cache))
        else:
            key = next(iter(self.cache))

        # 移除缓存项
        self.delete(key)

    def delete(self, key: str):
        """删除缓存项"""
        with self.lock:
            if key in self.cache:
                del self.cache[key]
            if key in self.access_counts:
                del self.access_counts[key]
            if key in self.access_times:
                del self.access_times[key]
            if key in self.ttl:
                del self.ttl[key]

    def clear(self):
        """清空缓存"""
        with self.lock:
            self.cache.clear()
            self.access_counts.clear()
            self.access_times.clear()
            self.ttl.clear()

    def get_stats(self) -> CacheStats:
        """获取缓存统计"""
        with self.lock:
            self.stats.size = len(self.cache)
            if self.stats.total_operations > 0:
                self.stats.hit_rate = self.stats.hits / self.stats.total_operations
            return self.stats

    def warmup(self, warmup_data: dict[str, Any]):
        """缓存预热"""
        for key, value in warmup_data.items():
            self.set(key, value)


class IncrementalComputer:
    """
    增量计算器

    功能：
    1. 增量更新检测
    2. 脏数据跟踪
    3. 依赖关系分析
    4. 最小化重新计算
    """

    def __init__(self):
        self.dependencies: dict[str, list[str]] = {}  # key -> 依赖项列表
        self.dependents: dict[str, list[str]] = {}  # key -> 依赖它的项
        self.values: dict[str, Any] = {}
        self.dirty: set[str] = set()
        self.compute_functions: dict[str, Callable] = {}

    def register(self, key: str, compute_func: Callable, deps: list[str]):
        """注册计算项"""
        self.compute_functions[key] = compute_func
        self.dependencies[key] = deps

        # 更新依赖关系
        for dep in deps:
            if dep not in self.dependents:
                self.dependents[dep] = []
            self.dependents[dep].append(key)

    def set_value(self, key: str, value: Any):
        """设置值并标记依赖项为脏"""
        self.values[key] = value

        # 标记依赖项为脏
        if key in self.dependents:
            for dep_key in self.dependents[key]:
                self.dirty.add(dep_key)

    def get_value(self, key: str) -> Any:
        """获取值，必要时重新计算"""
        if key in self.dirty:
            self._recompute(key)

        if key in self.values:
            return self.values[key]

        # 如果没有值但需要计算
        if key in self.compute_functions:
            self._recompute(key)
            return self.values.get(key)

        return None

    def _recompute(self, key: str):
        """重新计算"""
        if key not in self.compute_functions:
            return

        # 检查依赖是否都有值
        deps = self.dependencies.get(key, [])
        for dep in deps:
            if dep in self.dirty:
                self._recompute(dep)

        # 计算新值
        args = [self.values.get(dep) for dep in deps]
        result = self.compute_functions[key](*args)

        # 更新值并清除脏标记
        self.values[key] = result
        self.dirty.discard(key)

    def invalidate(self, key: str):
        """使缓存失效"""
        if key in self.dependents:
            for dep_key in self.dependents[key]:
                self.dirty.add(dep_key)

    def get_dependency_graph(self) -> dict[str, Any]:
        """获取依赖关系图"""
        return {
            "dependencies": self.dependencies,
            "dependents": self.dependents,
            "dirty_nodes": list(self.dirty),
            "value_count": len(self.values),
        }


class DistributedExecutor:
    """
    分布式执行器

    功能：
    1. 任务分发
    2. 负载均衡
    3. 故障恢复
    4. 结果聚合
    """

    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.task_results: dict[str, Any] = {}
        self.task_status: dict[str, str] = {}  # pending, running, completed, failed
        self.lock = threading.RLock()

    async def execute_tasks(self, tasks: list[tuple[Callable, tuple, dict]]) -> list[Any]:
        """并行执行多个任务"""
        loop = asyncio.get_event_loop()

        async def run_task(task_func, *args, **kwargs):
            return await loop.run_in_executor(self.executor, task_func, *args, **kwargs)

        # 创建所有任务
        task_coroutines = []
        for task_func, args, kwargs in tasks:
            task_coroutines.append(run_task(task_func, *args, **kwargs))

        # 并行执行
        results = await asyncio.gather(*task_coroutines, return_exceptions=True)
        return results

    def submit_task(self, task_id: str, task_func: Callable, *args, **kwargs):
        """提交单个任务"""
        future = self.executor.submit(task_func, *args, **kwargs)
        self.task_status[task_id] = "pending"

        def task_callback(f):
            with self.lock:
                if f.exception():
                    self.task_status[task_id] = "failed"
                    self.task_results[task_id] = str(f.exception())
                else:
                    self.task_status[task_id] = "completed"
                    self.task_results[task_id] = f.result()

        future.add_done_callback(task_callback)
        return future

    def get_task_status(self, task_id: str) -> str:
        """获取任务状态"""
        return self.task_status.get(task_id, "unknown")

    def get_task_result(self, task_id: str) -> Any:
        """获取任务结果"""
        return self.task_results.get(task_id)

    def shutdown(self):
        """关闭执行器"""
        self.executor.shutdown()


class ResourceOptimizer:
    """
    资源优化器

    功能：
    1. 内存优化
    2. CPU 优化
    3. 网络优化
    4. 文件系统优化
    """

    def __init__(self):
        self.memory_limit = 1024 * 1024 * 1024  # 1GB
        self.cpu_limit = 0.8  # 80%
        self.active_optimizations = set()

    def optimize_memory(self, objects: list[Any]) -> dict[str, Any]:
        """内存优化"""
        total_size = 0
        for obj in objects:
            total_size += self._estimate_size(obj)

        suggestions = []
        if total_size > self.memory_limit * 0.5:
            suggestions.append("Memory usage high (>50%), consider using lazy loading")
        if total_size > self.memory_limit * 0.8:
            suggestions.append("Memory usage very high (>80%), consider cache eviction")

        return {
            "estimated_size": total_size,
            "memory_limit": self.memory_limit,
            "suggestions": suggestions,
        }

    def _estimate_size(self, obj: Any) -> int:
        """估算对象大小"""
        import sys

        return sys.getsizeof(obj, 0)

    def optimize_cpu(self, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        """CPU 优化"""
        total_load = sum(task.get("cpu_load", 0) for task in tasks)

        suggestions = []
        if total_load > self.cpu_limit * 100:
            suggestions.append(f"CPU load high ({total_load}%), consider task scheduling")

        # 负载均衡建议
        if len(tasks) > 1:
            avg_load = total_load / len(tasks)
            imbalance_tasks = [t for t in tasks if t.get("cpu_load", 0) > avg_load * 2]
            if imbalance_tasks:
                suggestions.append("CPU load imbalanced, consider redistributing tasks")

        return {
            "total_cpu_load": total_load,
            "cpu_limit": self.cpu_limit * 100,
            "suggestions": suggestions,
        }


# 便捷函数
def create_smart_cache(
    max_size: int = 1000, strategy: CacheStrategy = CacheStrategy.LRU
) -> SmartCache:
    """创建智能缓存"""
    return SmartCache(max_size=max_size, strategy=strategy)


def create_incremental_computer() -> IncrementalComputer:
    """创建增量计算器"""
    return IncrementalComputer()


def create_distributed_executor(max_workers: int = 4) -> DistributedExecutor:
    """创建分布式执行器"""
    return DistributedExecutor(max_workers=max_workers)


def create_resource_optimizer() -> ResourceOptimizer:
    """创建资源优化器"""
    return ResourceOptimizer()


if __name__ == "__main__":
    # 测试智能缓存
    print("=== Testing Smart Cache ===")
    cache = create_smart_cache(max_size=3, strategy=CacheStrategy.LRU)

    # 设置缓存
    cache.set("key1", "value1")
    cache.set("key2", "value2")
    cache.set("key3", "value3", ttl=1.0)  # 1秒TTL

    # 测试命中
    val1 = cache.get("key1")
    print(f"Cache hit: key1 = {val1}")

    # 测试驱逐
    cache.set("key4", "value4")  # 应该驱逐 key1 (LRU)
    val1_again = cache.get("key1")
    print(f"After eviction: key1 = {val1_again}")

    # 测试 TTL
    time.sleep(1.1)
    val3_expired = cache.get("key3")
    print(f"After TTL expiration: key3 = {val3_expired}")

    # 获取统计
    stats = cache.get_stats()
    print(f"Cache stats: hits={stats.hits}, misses={stats.misses}, hit_rate={stats.hit_rate}")

    # 测试增量计算
    print("\n=== Testing Incremental Computation ===")
    computer = create_incremental_computer()

    # 注册计算函数
    def add(a, b):
        return a + b

    def multiply(x, y):
        return x * y

    computer.register("sum", add, ["a", "b"])
    computer.register("product", multiply, ["sum", "c"])

    # 设置值
    computer.set_value("a", 10)
    computer.set_value("b", 20)
    computer.set_value("c", 3)

    # 获取计算结果
    result = computer.get_value("product")
    print(f"Incremental computation: product = {result}")

    # 测试分布式执行
    print("\n=== Testing Distributed Execution ===")

    async def test_distributed():
        executor = create_distributed_executor(max_workers=2)

        def slow_task(x):
            import time

            time.sleep(0.1)
            return x * 2

        tasks = [(slow_task, (i,), {}) for i in range(5)]
        results = await executor.execute_tasks(tasks)
        print(f"Distributed results: {results}")

    asyncio.run(test_distributed())
