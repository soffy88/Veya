"""Tests for RemoteSandbox (host-daemon srt client).

These tests start the real daemon on a temp socket and talk to it through
create_sandbox() with VEYA_SANDBOX_REMOTE_SOCKET set, so they exercise the
full path: client -> socket -> daemon -> srt on host.

Skipped automatically when srt cannot run on this machine (e.g. inside a
Docker container where bwrap cannot create namespaces).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from veya.obase.sandbox import SandboxConfig, create_sandbox

REPO_ROOT = Path(__file__).resolve().parents[1]


def _daemon_env(socket_dir: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["VEYA_SANDBOX_BACKEND"] = "srt"
    env["VEYA_SRT_ALLOW_ALL_UNIX_SOCKETS"] = "1"
    # srt is a node script: its shebang needs node on PATH, and it shells
    # out to rg. The pytest process's PATH may not have either, so add
    # both explicitly (same layout the systemd unit uses).
    nvm_bin = Path.home() / ".nvm" / "versions" / "node" / "v26.4.0" / "bin"
    local_bin = Path.home() / ".local" / "bin"
    env["PATH"] = os.pathsep.join([str(nvm_bin), str(local_bin), env.get("PATH", "")])
    # Prefer the global srt install; fall back to the smoke install.
    global_srt = nvm_bin / "srt"
    smoke = Path.home() / "srt-smoke" / "node_modules" / ".bin" / "srt"
    if global_srt.is_file():
        env["VEYA_SRT_BIN"] = str(global_srt)
    elif smoke.is_file():
        env["VEYA_SRT_BIN"] = str(smoke)
    return env


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    socket_dir = tmp_path_factory.mktemp("sandbox-sockets")
    socket_path = socket_dir / "sandbox-exec.sock"
    proc = subprocess.Popen(
        [sys.executable, "server/sandbox_remote_daemon.py",
         "--socket-dir", str(socket_dir)],
        cwd=REPO_ROOT,
        env=_daemon_env(socket_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 20
        while not socket_path.exists():
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.skip(f"daemon failed to start: {out[-2000:]}")
            if time.time() > deadline:
                pytest.fail("daemon socket did not appear in 20s")
            time.sleep(0.2)
        yield str(socket_path)
    finally:
        proc.terminate()
        with __import__("contextlib").suppress(Exception):
            proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_remote_echo_and_backend(daemon, monkeypatch):
    monkeypatch.setenv("VEYA_SANDBOX_REMOTE_SOCKET", daemon)
    sandbox = create_sandbox(SandboxConfig(network_blocked=True))
    result = await sandbox.execute("echo hello-remote")
    assert result["exit_code"] == 0
    assert "hello-remote" in result["stdout"]
    assert result["sandbox_backend"] == "srt-remote"


@pytest.mark.asyncio
async def test_remote_credential_masked(daemon, monkeypatch):
    # The daemon runs as the host user, whose real ~/.ssh exists. A remote
    # run must NOT be able to read it (srt masks credential dirs).
    monkeypatch.setenv("VEYA_SANDBOX_REMOTE_SOCKET", daemon)
    sandbox = create_sandbox(SandboxConfig(network_blocked=True))
    home = str(Path.home())
    result = await sandbox.execute(f"cat {home}/.ssh/id_ed25519 2>&1; echo MARKER")
    assert "MARKER" in result["stdout"]
    assert "PRIVATE KEY" not in result["stdout"]


@pytest.mark.asyncio
async def test_remote_write_confined(daemon, monkeypatch):
    monkeypatch.setenv("VEYA_SANDBOX_REMOTE_SOCKET", daemon)
    sandbox = create_sandbox(SandboxConfig(network_blocked=True, allow_write=True))
    result = await sandbox.execute("echo ok > $TMPDIR/x.txt && cat $TMPDIR/x.txt")
    assert result["exit_code"] == 0
    assert "ok" in result["stdout"]
    # allow_write only adds the workdir (and /tmp) to the write allowlist.
    # With allow_write=False, a write outside the workdir must be refused.
    strict = create_sandbox(SandboxConfig(network_blocked=True, allow_write=False))
    outside = await strict.execute("echo pwn > $HOME/veya_remote_probe_outside.txt 2>&1")
    assert outside["exit_code"] != 0 or "Read-only" in outside["stderr"]


@pytest.mark.asyncio
async def test_remote_network_blocked(daemon, monkeypatch):
    monkeypatch.setenv("VEYA_SANDBOX_REMOTE_SOCKET", daemon)
    sandbox = create_sandbox(SandboxConfig(network_blocked=True))
    # No pipeline: a pipeline's exit code is the last command's, which
    # would mask curl's failure.
    result = await sandbox.execute("curl -sS --max-time 5 https://example.com")
    assert result["exit_code"] != 0


@pytest.mark.asyncio
async def test_remote_dangerous_rejected_locally(daemon, monkeypatch):
    # Dangerous commands are rejected by the client before touching the socket.
    monkeypatch.setenv("VEYA_SANDBOX_REMOTE_SOCKET", daemon)
    sandbox = create_sandbox(SandboxConfig(network_blocked=True))
    result = await sandbox.execute("rm -rf /")
    assert result["exit_code"] == -3


@pytest.mark.asyncio
async def test_remote_unavailable_falls_back(monkeypatch):
    monkeypatch.setenv("VEYA_SANDBOX_REMOTE_SOCKET", "/nonexistent-dir/sandbox-exec.sock")
    sandbox = create_sandbox(SandboxConfig(network_blocked=True))
    result = await sandbox.execute("echo fallback-works")
    assert result["exit_code"] == 0
    assert "fallback-works" in result["stdout"]
    assert result["sandbox_backend"] == "legacy"
    assert result.get("sandbox_fallback") == "remote-unavailable"
