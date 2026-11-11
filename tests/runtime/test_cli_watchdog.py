"""External-CLI watchdog: silence is the failure, not runtime.

Every test here drives a *real* process group: the watchdog's whole job is to
decide when a child is wedged, and a fake process object cannot show that a
SIGTERM reaches the grandchildren an engine spawned. The subprocesses are
``python -c`` one-liners so the behaviour under test (silence, continuous
output, ignoring SIGTERM) is visible in the test itself.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from server import exec_process
from server.exec_process import (
    ActivityClock,
    WatchdogReason,
    supervise_process,
    watchdog_enabled,
    watchdog_idle_timeout,
    watchdog_kill_grace_s,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")

_ENV_VARS = (
    exec_process.WATCHDOG_ENV,
    exec_process.IDLE_TIMEOUT_ENV,
    exec_process.KILL_GRACE_ENV,
)


@pytest.fixture(autouse=True)
def _clean_watchdog_env(monkeypatch):
    """A developer's shell must not decide what these tests assert."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(exec_process.KILL_GRACE_ENV, "0.2")


async def _spawn(script: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


def _read_activity(proc: asyncio.subprocess.Process, activity: ActivityClock) -> asyncio.Task:
    """Drain stdout and count every read as activity (as an engine would)."""

    async def _pump() -> None:
        assert proc.stdout is not None
        while await proc.stdout.readline():
            activity.touch()

    return asyncio.create_task(_pump())


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not exec_process._proc_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# supervise_process against real children
# ---------------------------------------------------------------------------


async def test_continuous_output_is_not_killed():
    """An engine that keeps printing may run past the idle deadline."""
    proc = await _spawn(
        "import sys, time\n"
        "for _ in range(10):\n"
        "    print('working', flush=True)\n"
        "    time.sleep(0.05)\n"
    )
    activity = ActivityClock()
    pump = _read_activity(proc, activity)
    try:
        outcome = await asyncio.wait_for(
            supervise_process(proc, activity, idle_timeout_s=0.5, wall_timeout_s=20.0),
            timeout=15.0,
        )
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    await proc.wait()
    assert outcome.reason is WatchdogReason.EXITED
    assert outcome.signal == "none"
    assert proc.returncode == 0


async def test_silence_kills_the_child_and_its_descendants(tmp_path):
    """The idle guard must take the whole group, not just the leader."""
    proc = await _spawn(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(120)\n"
    )
    assert proc.stdout is not None
    grandchild = int((await proc.stdout.readline()).decode().strip())
    activity = ActivityClock()  # starts now: the line above is the last output
    outcome = await asyncio.wait_for(
        supervise_process(proc, activity, idle_timeout_s=0.4, wall_timeout_s=20.0),
        timeout=15.0,
    )
    await asyncio.wait_for(proc.wait(), timeout=5.0)
    assert outcome.reason is WatchdogReason.IDLE_TIMEOUT
    assert outcome.signal in {"sigterm", "sigkill"}
    assert await _wait_dead(proc.pid), "leader must be gone"
    assert await _wait_dead(grandchild), "a descendant left behind is an orphaned engine"


async def test_sigterm_ignoring_child_is_killed_after_grace():
    """SIGTERM first, then SIGKILL when the grace runs out."""
    proc = await _spawn(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n"
    )
    activity = ActivityClock()
    outcome = await asyncio.wait_for(
        supervise_process(proc, activity, idle_timeout_s=0.3, wall_timeout_s=20.0),
        timeout=15.0,
    )
    await asyncio.wait_for(proc.wait(), timeout=5.0)
    assert outcome.reason is WatchdogReason.IDLE_TIMEOUT
    assert outcome.signal == "sigkill"


async def test_wall_deadline_fires_even_while_output_flows():
    """Continuous output keeps the idle guard quiet; the wall clock still ends it."""
    proc = await _spawn(
        "import time\nwhile True:\n    print('working', flush=True)\n    time.sleep(0.05)\n"
    )
    activity = ActivityClock()
    pump = _read_activity(proc, activity)
    try:
        outcome = await asyncio.wait_for(
            supervise_process(proc, activity, idle_timeout_s=30.0, wall_timeout_s=0.6),
            timeout=15.0,
        )
    finally:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    await asyncio.wait_for(proc.wait(), timeout=5.0)
    assert outcome.reason is WatchdogReason.WALL_TIMEOUT
    assert await _wait_dead(proc.pid)


# ---------------------------------------------------------------------------
# env switches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", " ", "1", "true", "yes", "on"])
def test_watchdog_is_on_by_default(raw, monkeypatch):
    monkeypatch.setenv(exec_process.WATCHDOG_ENV, raw)
    assert watchdog_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no", "disabled", "none", " OFF "])
def test_watchdog_can_be_switched_off(raw, monkeypatch):
    monkeypatch.setenv(exec_process.WATCHDOG_ENV, raw)
    assert watchdog_enabled() is False


def test_idle_timeout_env_overrides_the_capability_default(monkeypatch):
    assert watchdog_idle_timeout(600.0) == 600.0
    monkeypatch.setenv(exec_process.IDLE_TIMEOUT_ENV, "42.5")
    assert watchdog_idle_timeout(600.0) == 42.5
    monkeypatch.setenv(exec_process.IDLE_TIMEOUT_ENV, "0")
    assert watchdog_idle_timeout(600.0) is None, "<= 0 means no idle guard"
    monkeypatch.setenv(exec_process.IDLE_TIMEOUT_ENV, "-1")
    assert watchdog_idle_timeout(600.0) is None


def test_malformed_idle_timeout_falls_back_instead_of_failing(monkeypatch):
    monkeypatch.setenv(exec_process.IDLE_TIMEOUT_ENV, "not-a-number")
    assert watchdog_idle_timeout(600.0) == 600.0


def test_kill_grace_default_and_override(monkeypatch):
    monkeypatch.delenv(exec_process.KILL_GRACE_ENV, raising=False)
    assert watchdog_kill_grace_s() == exec_process.DEFAULT_KILL_GRACE_S
    monkeypatch.setenv(exec_process.KILL_GRACE_ENV, "1.5")
    assert watchdog_kill_grace_s() == 1.5
    monkeypatch.setenv(exec_process.KILL_GRACE_ENV, "-3")
    assert watchdog_kill_grace_s() == 0.0
    monkeypatch.setenv(exec_process.KILL_GRACE_ENV, "soon")
    assert watchdog_kill_grace_s() == exec_process.DEFAULT_KILL_GRACE_S


# ---------------------------------------------------------------------------
# stream_engine wiring: the capability inventory decides the deadlines
# ---------------------------------------------------------------------------


def _fake_engine(tmp_path: Path, body: str) -> Path:
    """An executable named ``pi`` so build_argv's table resolves to us."""
    script = tmp_path / "pi"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path


@pytest.fixture
def fake_pi(tmp_path, monkeypatch):
    bin_dir = _fake_engine(tmp_path, "import time\ntime.sleep(120)\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


async def _collect(engine: str, *, timeout_s: float | None, limit: float = 20.0):
    from server.engine_runner import stream_engine

    async def _drain() -> list[dict[str, object]]:
        return [event async for event in stream_engine(engine, "hi", timeout_s=timeout_s)]

    return await asyncio.wait_for(_drain(), timeout=limit)


async def test_stream_engine_idle_timeout_kills_the_group(fake_pi, tmp_path, monkeypatch):
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

    events = await _collect("pi", timeout_s=20)
    final = events[-1]
    assert final["type"] == "engine_error"
    assert final["reason"] == "idle_timeout"
    assert "引擎静默超过" in str(final["error"])

    leader, grandchild = (int(part) for part in pidfile.read_text().split())
    assert await _wait_dead(leader)
    assert await _wait_dead(grandchild)


async def test_stream_engine_keeps_a_talkative_run_alive(fake_pi, monkeypatch):
    fake_pi.joinpath("pi").write_text(
        f"#!{sys.executable}\n"
        "import time\n"
        "for i in range(6):\n"
        "    print(f'line {i}', flush=True)\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    fake_pi.joinpath("pi").chmod(0o755)
    monkeypatch.setenv(exec_process.IDLE_TIMEOUT_ENV, "0.4")

    events = await _collect("pi", timeout_s=20)
    deltas = [event["delta"] for event in events if event["type"] == "text_delta"]
    assert events[-1]["type"] == "engine_done"
    assert events[-1]["status"] == "success"
    assert deltas == [f"line {i}" for i in range(6)]


async def test_stream_engine_wall_timeout_is_reported_separately(fake_pi):
    fake_pi.joinpath("pi").write_text(
        f"#!{sys.executable}\nimport time\nwhile True:\n    print('x', flush=True)\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    fake_pi.joinpath("pi").chmod(0o755)

    events = await _collect("pi", timeout_s=0.6)
    final = events[-1]
    assert final["type"] == "engine_error"
    assert final["reason"] == "wall_timeout"
    assert "引擎超时 (1s)" in str(final["error"])


async def test_switching_the_idle_guard_off_lets_a_slow_engine_finish(fake_pi, monkeypatch):
    """The escape hatch must actually work: silent for longer than idle, still fine."""
    fake_pi.joinpath("pi").write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(0.8)\nprint('late', flush=True)\n",
        encoding="utf-8",
    )
    fake_pi.joinpath("pi").chmod(0o755)
    monkeypatch.setenv(exec_process.IDLE_TIMEOUT_ENV, "0.2")
    monkeypatch.setenv(exec_process.WATCHDOG_ENV, "0")

    events = await _collect("pi", timeout_s=20)
    assert events[-1]["type"] == "engine_done"
    assert [e["delta"] for e in events if e["type"] == "text_delta"] == ["late"]


# ---------------------------------------------------------------------------
# _resolve_timeouts: caller wins, then capability, then the fallback
# ---------------------------------------------------------------------------


def test_resolve_timeouts_prefers_the_caller(monkeypatch):
    from server import engine_runner

    capability = engine_runner.find_capability("claude_code")
    idle, wall = engine_runner._resolve_timeouts("claude", 2400.0)
    assert wall == 2400.0
    assert idle == capability.default_idle_timeout_s


def test_resolve_timeouts_drops_an_idle_deadline_the_wall_would_win(monkeypatch):
    """A 120s wall with a 600s idle guard can only ever report ``wall_timeout``."""
    from server import engine_runner

    assert engine_runner._resolve_timeouts("claude", 120.0) == (None, 120.0)


def test_resolve_timeouts_uses_the_capability_default():
    from server import engine_runner

    capability = engine_runner.find_capability("codex")
    idle, wall = engine_runner._resolve_timeouts("codex", None)
    assert wall == capability.default_wall_timeout_s
    assert idle == capability.default_idle_timeout_s


def test_resolve_timeouts_falls_back_for_an_unknown_engine():
    from server import engine_runner

    idle, wall = engine_runner._resolve_timeouts("no-such-cli-binary", None)
    assert wall == engine_runner.DEFAULT_WALL_TIMEOUT_S
    # no capability record: the idle default is the wall deadline itself, which
    # is then dropped so the wall timeout is the only deadline reported
    assert idle is None


def test_resolve_timeouts_drops_an_idle_deadline_that_cannot_fire_first(monkeypatch):
    from server import engine_runner

    idle, wall = engine_runner._resolve_timeouts("claude", 60.0)
    assert wall == 60.0
    assert idle is None, "idle >= wall would mislabel a wall timeout"


def test_resolve_timeouts_honours_the_watchdog_switch(monkeypatch):
    from server import engine_runner

    monkeypatch.setenv(exec_process.WATCHDOG_ENV, "0")
    idle, wall = engine_runner._resolve_timeouts("claude", 60.0)
    assert idle is None
    assert wall == 60.0
