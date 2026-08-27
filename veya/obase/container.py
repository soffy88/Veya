"""veya/obase/container — 全局单例句柄层（阶段 1）。

业务逻辑**禁止直接 import 旧实现**；一律经本模块取句柄：

    from veya.obase.container import get_sandbox, get_bus, get_kv

    sb = get_sandbox()
    res = await sb.execute_args(["echo", "hi"])

- 默认实现 = 阶段 1 适配器（现有能力形态转换，行为不变）；
- ``configure(...)`` 允许测试/双轨运行注入替代实现；
- ``reset()`` 清空全部句柄（测试隔离用）。

本层是「全局单例句柄」的真相源：业务逻辑被挡在句柄后面，
阶段 3+ 替换底层实现（namespace 沙盒 / gRPC 总线 / 文件 KV）无需改业务。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

# 句柄槽位（None = 尚未创建）
_sandbox: Any = None
_bus: Any = None
_barrier: Any = None
_kv: Any = None
_llm: Any = None

_configured: dict[str, Any] = {}


def configure(**handles: Any) -> None:
    """注入句柄覆盖（sandbox/bus/barrier/kv/llm）。替换现有句柄，可传 None 恢复默认。"""
    valid = {"sandbox", "bus", "barrier", "kv", "llm"}
    unknown = set(handles) - valid
    if unknown:
        raise ValueError(f"未知句柄: {sorted(unknown)} (可选: {sorted(valid)})")
    _configured.update(handles)
    # 已创建的实例被覆盖时同步替换
    for name in valid:
        if name in handles:
            globals()[f"_{name}"] = handles[name]


def _lazy(name: str, factory: Any) -> Any:
    handle = globals().get(f"_{name}")
    if handle is None:
        handle = _configured.get(name)
        if handle is None:
            handle = factory()
        globals()[f"_{name}"] = handle
    return handle


def get_sandbox() -> Any:
    """VfsSandbox 句柄（默认 SandboxVfsAdapter → ProcessSandbox）。"""
    from veya.obase.adapters import SandboxVfsAdapter

    return _lazy("sandbox", SandboxVfsAdapter)


def get_bus() -> Any:
    """DaemonBus 句柄（默认 InProcessDaemonBus）。"""
    from veya.obase.adapters import InProcessDaemonBus

    return _lazy("bus", InProcessDaemonBus)


def get_barrier() -> Any:
    """EventBarrier 句柄（默认 TelemetryEventBarrier → telemetry.emit）。"""
    from veya.obase.adapters import TelemetryEventBarrier

    return _lazy("barrier", TelemetryEventBarrier)


def get_kv() -> Any:
    """KvStore 句柄（默认 SQLite :memory:，阶段 4 接线文件路径做整树快照）。"""
    from veya.obase.adapters import SqliteKvStore

    return _lazy("kv", SqliteKvStore)


def get_llm() -> Any:
    """LlmClient 句柄（默认 LlmClientAdapter → veya.obase.llm）。"""
    from veya.obase.adapters import LlmClientAdapter

    return _lazy("llm", LlmClientAdapter)


def close_all() -> None:
    """关闭全部已创建句柄并清空（进程退出/测试 teardown）。

    同步语境下 async 闭包会收尾（无事件循环时用 asyncio.run）；
    事件循环内请用 ``await aclose_all()``。
    """
    global _sandbox, _bus, _barrier, _kv, _llm
    handles = [globals()[f"_{n}"] for n in ("sandbox", "bus", "barrier", "kv", "llm")]
    for handle in handles:
        if handle is None:
            continue
        closer = getattr(handle, "close", None)
        if not callable(closer):
            continue
        try:
            result = closer()
            if inspect.iscoroutine(result):
                try:
                    asyncio.get_running_loop()
                    # 事件循环内: 交给 aclose_all, 这里跳过
                    import warnings

                    warnings.warn(
                        "close_all() 在事件循环内遇到 async 闭包 — 请用 await aclose_all()",
                        stacklevel=2,
                    )
                except RuntimeError:
                    asyncio.run(result)
        except Exception:
            pass
    _sandbox = _bus = _barrier = _kv = _llm = None


async def aclose_all() -> None:
    """事件循环内关闭全部句柄并清空。"""
    global _sandbox, _bus, _barrier, _kv, _llm
    for name in ("sandbox", "bus", "barrier", "kv", "llm"):
        handle = globals().get(f"_{name}")
        if handle is None:
            continue
        closer = getattr(handle, "close", None)
        if callable(closer):
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
    _sandbox = _bus = _barrier = _kv = _llm = None


def reset() -> None:
    """清空句柄与覆盖（测试隔离）。"""
    global _sandbox, _bus, _barrier, _kv, _llm
    _sandbox = _bus = _barrier = _kv = _llm = None
    _configured.clear()


__all__ = [
    "aclose_all",
    "close_all",
    "configure",
    "get_barrier",
    "get_bus",
    "get_kv",
    "get_llm",
    "get_sandbox",
    "reset",
]
