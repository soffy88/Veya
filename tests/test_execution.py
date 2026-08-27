"""veya.execution 测试 — 执行路由 + 生命周期 + 同步括号 (cloudflare/computer 内化)。"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from veya.execution import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    ExecOptions,
    ExecResult,
    LocalSafeBackend,
    SyncBracket,
    get_exec_backend,
    list_exec_backends,
    register_exec_backend,
    runtime_exec,
)


def _run(coro):
    return asyncio.run(coro)


# ── 后端注册表 ───────────────────────────────────────────────────────


def test_default_backends_registered():
    backends = list_exec_backends()
    assert {"fast-path", "local-safe", "python-module"} <= set(backends)


def test_get_unknown_backend_raises():
    with pytest.raises(ValueError, match="unknown exec backend"):
        get_exec_backend("nope")


def test_register_overrides():
    backend = LocalSafeBackend("test-backend")
    register_exec_backend(backend)
    assert get_exec_backend("test-backend") is backend
    from veya.execution import _BACKENDS

    _BACKENDS.pop("test-backend")


# ── 快档 (fast-path) ─────────────────────────────────────────────────


def test_fast_path_exec():
    async def _t():
        handle = await runtime_exec("echo hello", ExecOptions(backend="fast-path"))
        result = await handle.result()
        return result

    result = _run(_t())
    assert result.status == STATUS_COMPLETED
    assert result.stdout.strip() == "hello"


def test_fast_path_dangerous_blocked():
    async def _t():
        return await (
            await runtime_exec("rm -rf /tmp/x", ExecOptions(backend="fast-path"))
        ).result()

    result = _run(_t())
    assert result.status == STATUS_FAILED
    assert "blocked" in result.stderr


def test_fast_path_timeout():
    async def _t():
        return await (
            await runtime_exec("sleep 5", ExecOptions(backend="fast-path", timeout_ms=200))
        ).result()

    result = _run(_t())
    assert result.status == STATUS_FAILED
    assert "timeout" in result.stderr


# ── 结构化档 (python-module) ─────────────────────────────────────────


def test_python_module_structured_value():
    src = "def main(p):\n    return {'got': p['n'] * 2}\n"

    async def _t():
        return await (
            await runtime_exec(src, ExecOptions(backend="python-module", input={"n": 21}))
        ).result()

    result = _run(_t())
    assert result.status == STATUS_COMPLETED
    assert result.value == {"got": 42}


def test_python_module_result_var():
    src = "result = {'ok': True, 'sum': 1 + 2}\n"

    async def _t():
        return await (await runtime_exec(src, ExecOptions(backend="python-module"))).result()

    result = _run(_t())
    assert result.value == {"ok": True, "sum": 3}


# ── 重档 + 同步括号 (local-safe) ────────────────────────────────────


def test_local_safe_pulls_products():
    async def _t(tmp: pathlib.Path):
        (tmp / "base.txt").write_text("base")
        return await (
            await runtime_exec(
                "echo prod > out.txt",
                ExecOptions(backend="local-safe", cwd=str(tmp), out_paths=["out.txt"]),
            )
        ).result()

    tmp = pathlib.Path(__import__("tempfile").mkdtemp(prefix="veya-test-"))
    try:
        result = _run(_t(tmp))
        assert result.status == STATUS_COMPLETED
        assert result.sync is not None
        assert result.sync.applied == 1
        assert (tmp / "out.txt").read_text().strip() == "prod"
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_local_safe_out_paths_filter():
    async def _t(tmp: pathlib.Path):
        return await (
            await runtime_exec(
                "echo a > a.txt && echo b > b.txt",
                ExecOptions(backend="local-safe", cwd=str(tmp), out_paths=["a.txt"]),
            )
        ).result()

    tmp = pathlib.Path(__import__("tempfile").mkdtemp(prefix="veya-test-"))
    try:
        result = _run(_t(tmp))
        assert result.sync and result.sync.applied == 1
        assert (tmp / "a.txt").exists()
        assert not (tmp / "b.txt").exists()  # 未指定 → 不回写
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


# ── 执行句柄生命周期 ────────────────────────────────────────────────


def test_handle_status_and_kill():
    async def _t():
        handle = await runtime_exec("sleep 3", ExecOptions(backend="fast-path"))
        assert handle.status() == STATUS_RUNNING
        await handle.kill()
        assert handle.status() == STATUS_CANCELLED
        return handle

    handle = _run(_t())
    assert handle.status() == STATUS_CANCELLED


def test_handle_result_idempotent():
    async def _t():
        handle = await runtime_exec("echo x", ExecOptions(backend="fast-path"))
        r1 = await handle.result()
        r2 = await handle.result()
        assert r1 is r2
        assert r1.stdout.strip() == "x"
        return r1

    result = _run(_t())
    assert isinstance(result, ExecResult)


# ── SyncBracket 单独 ─────────────────────────────────────────────────


def test_sync_bracket_pull_only_new_files():
    tmp = pathlib.Path(__import__("tempfile").mkdtemp(prefix="veya-bracket-"))
    try:
        authority = tmp / "auth"
        isolated = tmp / "iso"
        authority.mkdir()
        isolated.mkdir()
        (authority / "existing.txt").write_text("keep")
        (authority / "changed.txt").write_text("old")
        (isolated / "changed.txt").write_text("new")  # 变更
        (isolated / "brand-new.txt").write_text("fresh")  # 新增

        bracket = SyncBracket(authority)
        bracket.push()  # 基线: existing + changed(old)
        # 隔离目录先写入权威基线 (模拟 exec 前 push 到隔离)
        (isolated / "existing.txt").write_text("keep")
        (isolated / "changed.txt").write_text("new")

        state = bracket.pull(isolated)
        assert state.applied == 2  # changed + brand-new
        assert (authority / "changed.txt").read_text() == "new"
        assert (authority / "brand-new.txt").read_text() == "fresh"
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_sync_bracket_respects_baseline():
    """基线未覆盖的既有文件不算产物。"""
    tmp = pathlib.Path(__import__("tempfile").mkdtemp(prefix="veya-bracket-"))
    try:
        authority = tmp / "auth"
        isolated = tmp / "iso"
        authority.mkdir()
        isolated.mkdir()
        (authority / "baseline.txt").write_text("v1")
        bracket = SyncBracket(authority)
        bracket.push()
        # 模拟: 隔离目录完全复制权威 (无变更)
        (isolated / "baseline.txt").write_text("v1")
        state = bracket.pull(isolated)
        assert state.applied == 0
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)
