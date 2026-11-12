"""引擎路由门禁 — 缺失 CLI 必须返回结构化错误 (不能 500/520)。"""

from __future__ import annotations

import asyncio
import os
import stat
import sys

import pytest

from server import exec_process


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not exec_process._proc_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
def fake_pi(tmp_path, monkeypatch):
    """一个可执行 `pi`，使 build_argv 的能力表解析落到我们身上。"""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "pi"
    script.write_text(f"#{sys.executable}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


@pytest.mark.asyncio
async def test_stream_engine_missing_cli_yields_error():
    """engine CLI 缺失 → engine_error 事件, 不抛异常 (远端 520 根因)。"""
    from server.engine_runner import stream_engine

    events = [evt async for evt in stream_engine("no-such-cli-binary", "hi", timeout_s=10)]
    assert events, "必须产出事件"
    assert events[0]["type"] == "engine_error"
    assert "不可用" in events[0]["error"] or "not" in events[0]["error"].lower()


@pytest.mark.asyncio
async def test_run_engine_missing_cli_returns_ok_false():
    """run 契约: CLI 缺失返回 ok=False + 错误信息。"""
    from server.engine_runner import run_engine

    result = await run_engine("no-such-cli-binary", "hi", timeout_s=10)
    assert result["ok"] is False
    assert "不可用" in result["error"]


def test_available_engines_always_has_master():
    """master 恒可用 (builtin), 其余按本机 CLI 探测。"""
    from server.engine_runner import available_engines

    engines = available_engines()
    assert engines.get("master") == "builtin"


def test_engines_endpoint():
    """GET /api/v1/engines 返回引擎清单 (前端禁用依据)。"""
    from fastapi.testclient import TestClient

    from server.app import app

    res = TestClient(app).get("/api/v1/engines")
    assert res.status_code == 200
    data = res.json()
    assert "engines" in data
    assert data["engines"]["master"] == "builtin"


# ---------------------------------------------------------------------------
# 容器环境: master + pi 精确探测 (claude/codex 账号侧 403 未修 → 禁用)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_container_blocks_claude_codex(monkeypatch):
    """容器内: 凭据/端点未就绪的引擎被拒绝 (诚实声明)。"""
    import server.engine_runner as er

    monkeypatch.setattr(er, "_IN_CONTAINER", True)
    monkeypatch.setattr(er, "_container_pi_usable", lambda: True)
    monkeypatch.setattr(er, "_container_claude_usable", lambda: False)
    monkeypatch.setattr(er, "_container_codex_usable", lambda: False)
    monkeypatch.setattr(er, "_container_opencode_usable", lambda: False)
    monkeypatch.setattr(er, "_container_grok_usable", lambda: False)
    monkeypatch.setattr(er, "_container_dsh_usable", lambda: False)

    engines = er.available_engines()
    assert set(engines) == {"master", "pi"}  # pi 凭据齐全 → 放行
    assert "claude" not in engines and "codex" not in engines

    events = [evt async for evt in er.stream_engine("claude", "hi", timeout_s=10)]
    assert events[0]["type"] == "engine_error"
    assert "容器环境" in events[0]["error"]

    result = await er.run_engine("codex", "hi", timeout_s=10)
    assert result["ok"] is False
    assert "容器环境" in result["error"]


@pytest.mark.asyncio
async def test_container_engine_probes_respect_credentials(monkeypatch, tmp_path):
    """容器内 claude/codex 探测: 凭据齐全即放行 (不再一刀切)。"""
    import server.engine_runner as er

    monkeypatch.setattr(er, "_IN_CONTAINER", True)
    monkeypatch.setenv("HOME", str(tmp_path))
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda name: f"/bin/{name}" if name in ("pi", "claude", "codex") else None
    )

    # claude: 无凭据 → 不可用
    assert er._container_claude_usable() is False
    monkeypatch.setattr(er, "_container_gateway_ip", lambda: "192.168.16.1")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    (tmp_path / ".claude.json").write_text("{}")
    assert er._container_claude_usable() is True

    # codex: 无 config/auth → 不可用 (端点探测不触发)
    assert er._container_codex_usable() is False
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-5"')
    (tmp_path / ".codex" / "auth.json").write_text("{}")
    monkeypatch.setattr(er, "_ensure_container_opencodex", lambda: False)
    assert er._container_codex_usable() is False  # opencodex 不可达 → 拒绝
    monkeypatch.setattr(er, "_ensure_container_opencodex", lambda: True)
    assert er._container_codex_usable() is True  # 自举可达 → 放行

    # build_argv: 容器内 codex 覆盖 base_url (loopback 自举实例)
    monkeypatch.setattr(er, "_container_codex_base_url", lambda: "http://127.0.0.1:10100/v1")
    argv = er.build_argv("codex", "hi")
    assert "-c" in argv and "openai_base_url=http://127.0.0.1:10100/v1" in argv

    # 宿主 (非容器): 无需端点覆盖
    monkeypatch.setattr(er, "_IN_CONTAINER", False)
    argv_host = er.build_argv("codex", "hi")
    assert "-c" not in argv_host


@pytest.mark.asyncio
async def test_container_pi_requires_credentials(monkeypatch):
    """容器内 pi 精确探测: 凭据缺失 → 拒绝 (挂载 ≠ 可用)。"""
    import server.engine_runner as er

    monkeypatch.setattr(er, "_IN_CONTAINER", True)
    monkeypatch.setattr(er, "_container_pi_usable", lambda: False)
    monkeypatch.setattr(er, "_container_claude_usable", lambda: False)
    monkeypatch.setattr(er, "_container_codex_usable", lambda: False)
    monkeypatch.setattr(er, "_container_opencode_usable", lambda: False)
    monkeypatch.setattr(er, "_container_grok_usable", lambda: False)
    monkeypatch.setattr(er, "_container_dsh_usable", lambda: False)

    assert set(er.available_engines()) == {"master"}
    result = await er.run_engine("pi", "hi", timeout_s=10)
    assert result["ok"] is False
    assert "容器环境" in result["error"]


@pytest.mark.asyncio
async def test_container_pi_usable_detection(monkeypatch, tmp_path):
    """_container_pi_usable 三条件: ~/.pi + auth.json + PATH 二进制。"""
    import server.engine_runner as er

    monkeypatch.setattr(er, "_IN_CONTAINER", True)
    monkeypatch.setenv("HOME", str(tmp_path))  # expanduser 读 HOME
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/bin/pi" if name == "pi" else None)

    # 无 ~/.pi → 不可用
    assert er._container_pi_usable() is False
    # 目录 + agent/auth.json → 可用 (pi 认证位置)
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    (tmp_path / ".pi" / "agent" / "auth.json").write_text("{}")
    assert er._container_pi_usable() is True
    # auth.json 缺失 → 不可用
    (tmp_path / ".pi" / "agent" / "auth.json").unlink()
    (tmp_path / ".pi" / "agent" / "settings.json").write_text("{}")
    assert er._container_pi_usable() is False


def test_engines_endpoint_in_container(monkeypatch):
    """容器内 /api/v1/engines: 无 pi 凭据 → 只 master; 有 → master+pi。"""
    from fastapi.testclient import TestClient

    import server.engine_runner as er
    from server.app import app

    monkeypatch.setattr(er, "_IN_CONTAINER", True)
    monkeypatch.setattr(er, "_container_pi_usable", lambda: False)
    monkeypatch.setattr(er, "_container_claude_usable", lambda: False)
    monkeypatch.setattr(er, "_container_codex_usable", lambda: False)
    monkeypatch.setattr(er, "_container_opencode_usable", lambda: False)
    monkeypatch.setattr(er, "_container_grok_usable", lambda: False)
    monkeypatch.setattr(er, "_container_dsh_usable", lambda: False)
    res = TestClient(app).get("/api/v1/engines")
    assert res.status_code == 200
    assert set(res.json()["engines"]) == {"master"}

    monkeypatch.setattr(er, "_container_pi_usable", lambda: True)
    monkeypatch.setattr(er, "_container_claude_usable", lambda: True)
    monkeypatch.setattr(er, "_container_codex_usable", lambda: True)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda n: f"/bin/{n}")
    res2 = TestClient(app).get("/api/v1/engines")
    assert set(res2.json()["engines"]) == {"master", "pi", "claude", "codex"}


# ---------------------------------------------------------------------------
# 宿主直拉路径 (_run_external_cli): 与 stream_engine 同源的统一看门狗 + 杀整棵树
# ---------------------------------------------------------------------------


async def test_run_external_cli_idle_timeout_kills_the_group(fake_pi, tmp_path, monkeypatch):
    """直拉引擎静默超空闲截止 → 整个进程组（含孙进程）被收掉。"""
    pidfile = tmp_path / "pids"
    fake_pi.joinpath("pi").write_text(
        f"#!{sys.executable}\n"
        "import os, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pidfile)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    fake_pi.joinpath("pi").chmod(0o755)
    monkeypatch.setenv(exec_process.IDLE_TIMEOUT_ENV, "0.4")

    from server.engine_runner import _run_external_cli

    result = await asyncio.wait_for(_run_external_cli("pi", "hi", timeout_s=20), timeout=30.0)
    assert result["ok"] is False
    assert "静默" in str(result["error"]) or "超时" in str(result["error"])

    leader, grandchild = (int(part) for part in pidfile.read_text().split())
    assert await _wait_dead(leader)
    assert await _wait_dead(grandchild)


async def test_run_external_cli_returns_output_on_success(fake_pi, monkeypatch):
    """直拉引擎正常退出 → 返回全文输出 (ok=True)。"""
    fake_pi.joinpath("pi").write_text(
        f"#!{sys.executable}\nimport sys\nprint('RESULT OK', flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.joinpath("pi").chmod(0o755)

    from server.engine_runner import _run_external_cli

    result = await asyncio.wait_for(_run_external_cli("pi", "hi", timeout_s=20), timeout=30.0)
    assert result["ok"] is True
    assert "RESULT OK" in result["output"]


def test_run_engine_uses_unified_supervisor_by_default(monkeypatch):
    """默认路径: run_engine 宿主直拉走 _run_external_cli (统一看门狗)。"""
    import server.engine_runner as er

    monkeypatch.setattr(er, "_IN_CONTAINER", False)
    called = {}

    async def _fake_cli(engine, prompt, **kwargs):
        called["cli"] = (engine, prompt)
        return {"ok": True, "output": "x", "duration_s": 0.0}

    monkeypatch.setattr(er, "_run_external_cli", _fake_cli)
    monkeypatch.setattr(er, "_engine_bin_available", lambda e: True)
    monkeypatch.setattr(er, "opencode_client", type("X", (), {"send_message": None}))

    async def _go():
        return await er.run_engine("pi", "hi", timeout_s=20)

    outcome = asyncio.run(_go())
    assert outcome["ok"] is True
    assert called.get("cli") == ("pi", "hi")


def test_cli_host_direct_default_on(monkeypatch):
    """默认走统一看门狗直拉路径 (VEYA_CLI_HOST_DIRECT=1)。"""
    import server.engine_runner as er

    monkeypatch.delenv("VEYA_CLI_HOST_DIRECT", raising=False)
    assert er._cli_host_direct() is True
    monkeypatch.setenv("VEYA_CLI_HOST_DIRECT", "1")
    assert er._cli_host_direct() is True


def test_cli_host_direct_switch_off_restores_harness(monkeypatch):
    """VEYA_CLI_HOST_DIRECT=0 → 退回 run_harness 兼容路径。"""
    import server.engine_runner as er

    monkeypatch.setenv("VEYA_CLI_HOST_DIRECT", "0")
    assert er._cli_host_direct() is False


def test_build_argv_is_oskill_table():
    """argv 单源在 oskill.harness_argv；master 不是 harness。"""
    from server.engine_runner import build_argv
    from veya.platform import load

    argv = build_argv("claude", "fix it", model="sonnet")
    rec = load("oskill").harness_argv("claude", "fix it", model="sonnet")
    assert argv == rec["argv"]
    with pytest.raises(ValueError, match="not a harness"):
        build_argv("master", "hi")


def test_omodul_broker_export():
    from veya.platform import load

    broker = load("omodul").get_broker()
    assert broker.slots["hicode_serve"] == 1
    assert callable(load("omodul").run_harness)
