"""veya/oprim/fs — 文件原子操作（物理触手，只能在 VfsSandbox 权限内）。

阶段 3 原子元素：oprim_fs_read / oprim_fs_write / oprim_fs_exists /
oprim_fs_listdir / oprim_fs_delete。

规则：
- 所有操作经注入的 VfsSandbox 句柄（默认取 veya.obase.container 全局句柄）；
- VFS 越界由沙盒层拒绝（ValueError），本层不做判断、不做业务逻辑；
- 原子性：一个函数 = 一个不可再分的文件动作。
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Any


def _sandbox_of(sandbox: Any) -> Any:
    if sandbox is not None:
        return sandbox
    from veya.obase.container import get_sandbox

    return get_sandbox()


# Synchronous host-side primitives for persistent local-service state.  They live
# in this existing physical I/O facade because threaded HTTP handlers and their
# persistent data paths are synchronous and must not be redirected into the
# temporary VfsSandbox workspace.  Callers must derive destinations from their
# service-owned root and validate untrusted names before constructing paths.
def local_path_mkdir(path: str | Path, *, parents: bool = True, exist_ok: bool = True, mode: int = 0o700) -> Path:
    target = Path(path)
    target.mkdir(parents=parents, exist_ok=exist_ok, mode=mode)
    return target


def local_path_chmod(path: str | Path, mode: int) -> None:
    os.chmod(path, mode)


def local_path_open(path: str | Path, mode: str):
    return Path(path).open(mode)


def local_path_replace(source: str | Path, destination: str | Path) -> None:
    os.replace(source, destination)


def local_path_unlink(path: str | Path, *, missing_ok: bool = False) -> None:
    Path(path).unlink(missing_ok=missing_ok)


def local_path_exists(path: str | Path) -> bool:
    return Path(path).exists()


def local_path_is_symlink(path: str | Path) -> bool:
    return Path(path).is_symlink()


def local_path_is_file(path: str | Path) -> bool:
    return Path(path).is_file()


def local_path_is_dir(path: str | Path) -> bool:
    return Path(path).is_dir()


def local_path_rglob(path: str | Path, pattern: str) -> list[Path]:
    return list(Path(path).rglob(pattern))


def local_path_rmdir(path: str | Path) -> None:
    Path(path).rmdir()


def local_path_read_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def local_path_resolve(path: str | Path, *, strict: bool = False) -> Path:
    return Path(path).resolve(strict=strict)


def local_path_size(path: str | Path) -> int:
    return Path(path).stat().st_size


def local_read_bytes_limited(path: str | Path, limit: int) -> bytes | None:
    target = Path(path)
    if target.stat().st_size > limit:
        return None
    return target.read_bytes()


def local_fsync(stream: Any) -> None:
    os.fsync(stream.fileno())


def local_atomic_write(path: str | Path, temporary: str | Path, data: bytes, *, parent_mode: int = 0o700, file_mode: int = 0o600) -> None:
    target, temp = Path(path), Path(temporary)
    target.parent.mkdir(parents=True, exist_ok=True, mode=parent_mode)
    with suppress(OSError):
        os.chmod(target.parent, parent_mode)
    with temp.open("xb") as stream:
        with suppress(OSError):
            os.chmod(temp, file_mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, target)


async def fs_read(path: str, sandbox: Any = None) -> bytes:
    """读取沙盒内文件（字节）。越界抛 ValueError。"""
    return await _sandbox_of(sandbox).read(path)


async def fs_write(path: str, data: bytes | str, sandbox: Any = None) -> None:
    """写入沙盒内文件（自动建父目录）。越界抛 ValueError。"""
    await _sandbox_of(sandbox).write(path, data)


async def fs_read_text(path: str, sandbox: Any = None) -> str:
    """读取沙盒内文本文件。"""
    data = await _sandbox_of(sandbox).read(path)
    return data.decode("utf-8", errors="replace")


async def fs_write_text(path: str, text: str, sandbox: Any = None) -> None:
    """写入沙盒内文本文件。"""
    await _sandbox_of(sandbox).write(path, text)


async def fs_exists(path: str, sandbox: Any = None) -> bool:
    """沙盒内路径是否存在。"""
    return await _sandbox_of(sandbox).exists(path)


async def fs_listdir(path: str, sandbox: Any = None) -> list[str]:
    """列出沙盒内目录条目（仅名字）。"""
    return await _sandbox_of(sandbox).listdir(path)


async def fs_delete(path: str, sandbox: Any = None) -> None:
    """删除沙盒内文件或目录（递归）。"""
    await _sandbox_of(sandbox).delete(path)


__all__ = [
    "fs_delete",
    "fs_exists",
    "fs_listdir",
    "fs_read",
    "fs_read_text",
    "fs_write",
    "fs_write_text",
]
