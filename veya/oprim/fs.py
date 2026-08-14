"""veya/oprim/fs — 文件原子操作（物理触手，只能在 VfsSandbox 权限内）。

阶段 3 原子元素：oprim_fs_read / oprim_fs_write / oprim_fs_exists /
oprim_fs_listdir / oprim_fs_delete。

规则：
- 所有操作经注入的 VfsSandbox 句柄（默认取 veya.obase.container 全局句柄）；
- VFS 越界由沙盒层拒绝（ValueError），本层不做判断、不做业务逻辑；
- 原子性：一个函数 = 一个不可再分的文件动作。
"""

from __future__ import annotations

from typing import Any


def _sandbox_of(sandbox: Any) -> Any:
    if sandbox is not None:
        return sandbox
    from veya.obase.container import get_sandbox

    return get_sandbox()


async def fs_read(path: str, sandbox: Any = None) -> bytes:
    """读取沙盒内文件（字节）。越界抛 ValueError。"""
    return await _sandbox_of(sandbox).read(path)  # type: ignore[attr-defined]


async def fs_write(path: str, data: bytes | str, sandbox: Any = None) -> None:
    """写入沙盒内文件（自动建父目录）。越界抛 ValueError。"""
    await _sandbox_of(sandbox).write(path, data)  # type: ignore[attr-defined]


async def fs_read_text(path: str, sandbox: Any = None) -> str:
    """读取沙盒内文本文件。"""
    data = await _sandbox_of(sandbox).read(path)  # type: ignore[attr-defined]
    return data.decode("utf-8", errors="replace")


async def fs_write_text(path: str, text: str, sandbox: Any = None) -> None:
    """写入沙盒内文本文件。"""
    await _sandbox_of(sandbox).write(path, text)  # type: ignore[attr-defined]


async def fs_exists(path: str, sandbox: Any = None) -> bool:
    """沙盒内路径是否存在。"""
    return await _sandbox_of(sandbox).exists(path)  # type: ignore[attr-defined]


async def fs_listdir(path: str, sandbox: Any = None) -> list[str]:
    """列出沙盒内目录条目（仅名字）。"""
    return await _sandbox_of(sandbox).listdir(path)  # type: ignore[attr-defined]


async def fs_delete(path: str, sandbox: Any = None) -> None:
    """删除沙盒内文件或目录（递归）。"""
    await _sandbox_of(sandbox).delete(path)  # type: ignore[attr-defined]


__all__ = [
    "fs_delete",
    "fs_exists",
    "fs_listdir",
    "fs_read",
    "fs_read_text",
    "fs_write",
    "fs_write_text",
]
