"""Execution process-group ownership: identity checks and real termination."""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path

from server import exec_process


def _spawn_group(workspace: Path, script: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["/bin/sh", "-c", script],
        cwd=workspace,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_record_and_terminate_group(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # leader forks a descendant, then both sleep: a group with >1 member
    proc = _spawn_group(workspace, "sleep 120 & sleep 120")
    time.sleep(0.4)
    pidfile = exec_process.pidfile_for(tmp_path / "missions" / "m1", 0)
    exec_process.record_current(pidfile, proc, str(workspace))
    rec = exec_process.read(pidfile)
    assert rec["pid"] == proc.pid
    assert rec["pgid"] == proc.pid, "start_new_session makes the child its own group leader"

    result = exec_process.terminate(pidfile, str(workspace))
    assert result["refused"] is None
    assert result["killed"] >= 1
    with contextlib.suppress(ChildProcessError):
        os.waitpid(proc.pid, 0)  # reap the zombie we created
    time.sleep(0.2)
    assert not os.path.exists(f"/proc/{proc.pid}"), "leader must be gone"
    assert not exec_process._group_members(proc.pid), "no descendant may survive"


def test_refuses_own_process_group(tmp_path):
    pidfile = exec_process.pidfile_for(tmp_path, 0)
    exec_process.record(pidfile, pid=os.getpid(), pgid=os.getpgid(0))
    assert exec_process.terminate(pidfile)["refused"] == "own_process_group"


def test_refuses_unverified_group_when_leader_is_gone(tmp_path):
    """A bare pgid that exists is not proof of ownership."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    victim = _spawn_group(workspace, "sleep 120")
    time.sleep(0.3)
    pidfile = exec_process.pidfile_for(tmp_path / "missions" / "m2", 0)
    # record a *stale* identity for the same pgid: leader pid is not the group leader
    exec_process.record(pidfile, pid=999_999, pgid=victim.pid, workspace="/nonexistent")
    result = exec_process.terminate(pidfile, str(tmp_path / "other"))
    assert result["killed"] == 0
    assert result["refused"] in {"identity_mismatch", "unverified_group"}
    assert os.path.exists(f"/proc/{victim.pid}"), "an unverified group must not be killed"
    victim.kill()
    os.waitpid(victim.pid, 0)


def test_missing_record_is_safe(tmp_path):
    result = exec_process.terminate(tmp_path / "nope.json")
    assert result["refused"] == "no_record" and result["killed"] == 0
