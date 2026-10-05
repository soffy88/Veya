"""P0-H: process lifecycle truthfulness.

Every execution here is a real subprocess observed through the real gateway.
The assertions are about what a caller can *know* at each point — whether an
execution_id exists, whether a terminal state is real, whether output was cut —
rather than about return values happening to look right.

Commands are carried by ``test.run`` rather than ``shell.exec``: the permission
engine grades an inline ``python -c`` under shell.exec as P2_ROOT_MUTATION and
refuses it at admission, so no execution_id is ever issued and there is no
lifecycle to observe. That refusal is correct behaviour and is left alone.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from veya.remote import (
    RemoteAudit,
    RemoteAuth,
    RemotePermissions,
    RemoteSessionManager,
    RemoteToolAdapter,
)
from veya.remote.execution import ExecutionStatus, ExecutionStore
from veya.remote.mcp_server import create_gateway

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PY = sys.executable
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED", "TIMED_OUT"}


class Executor:
    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        return json.dumps({"status": "ok", "data": {"stdout": "unused"}})


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def make_gateway(workspaces: list[Path], principal: str = "tester"):
    auth = RemoteAuth()
    _record, secret = auth.issue(
        principal, permissions=PERMS, workspaces=[str(w) for w in workspaces]
    )
    audit = RemoteAudit(None)
    adapter = RemoteToolAdapter(
        Executor(), redact=audit.redact, execution_store=ExecutionStore(None)
    )
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=adapter,
    )
    return gateway, secret


async def rpc(gateway, method, params, *, secret, session=None):
    return await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        authorization=f"Bearer {secret}",
        session_header=session,
    )


async def open_session(gateway, secret, workspace: str) -> str:
    response = await rpc(gateway, "initialize", {"workspace": workspace}, secret=secret)
    return response["result"]["sessionId"]


async def call(gateway, secret, session, name, arguments):
    response = await rpc(
        gateway,
        "tools/call",
        {"name": name, "arguments": arguments},
        secret=secret,
        session=session,
    )
    return response["result"]["structuredContent"]


async def record_of(gateway, secret, session, execution_id):
    envelope = await call(
        gateway, secret, session, "process.status", {"execution_id": execution_id}
    )
    assert envelope["ok"] is True, envelope
    return envelope["result"]


async def wait_terminal(gateway, secret, session, execution_id, timeout: float = 90.0):
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        last = await record_of(gateway, secret, session, execution_id)
        if last.get("status") in TERMINAL:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"never terminal; last status={last.get('status')}")


async def start(gateway, secret, session, repo: Path, command: str, **extra):
    return await call(
        gateway,
        secret,
        session,
        "test.run",
        {
            "command": command,
            "workspace": str(repo),
            "execution_target": "CANONICAL_WORKTREE",
            **extra,
        },
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_git_repo(tmp_path / "proj")


# ── §8.2 an execution_id exists only for an admitted execution ────────
async def test_a_refused_call_issues_no_execution_id(repo: Path) -> None:
    """ok=true with an execution_id, then "it never ran", is the failure mode.

    A refusal must carry no handle at all. Anything else invites a caller to
    poll a handle for an execution that does not exist.
    """
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "shell.exec",
        {
            "command": f'{PY} -c "import sys; sys.exit(0)"',
            "workspace": str(repo),
            "wait": True,
        },
    )

    assert envelope["ok"] is False, envelope
    assert envelope.get("execution_id") is None, envelope


async def test_an_admitted_call_carries_an_execution_id(repo: Path) -> None:
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await start(gateway, secret, session, repo, f'{PY} -c "print(1)"')

    assert envelope["ok"] is True, envelope
    assert envelope["execution_id"]


# ── §8.3 the three real lifecycle paths ───────────────────────────────
async def test_start_running_completed(repo: Path) -> None:
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await start(gateway, secret, session, repo, f'{PY} -c "print(1)"')
    execution_id = envelope["execution_id"]
    terminal = await wait_terminal(gateway, secret, session, execution_id)

    assert terminal["status"] == str(ExecutionStatus.COMPLETED), terminal
    assert terminal["is_terminal"] is True
    assert terminal["exit_code"] == 0
    assert terminal["phase"] in {"COMPLETED", "FINALIZING"}, terminal


async def test_start_running_failed(repo: Path) -> None:
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await start(
        gateway, secret, session, repo, f'{PY} -c "import sys; sys.exit(3)"'
    )
    terminal = await wait_terminal(gateway, secret, session, envelope["execution_id"])

    assert terminal["status"] == str(ExecutionStatus.FAILED), terminal
    assert terminal["exit_code"] == 3
    assert terminal["is_terminal"] is True


async def test_a_running_execution_reports_running_before_it_terminates(
    repo: Path,
) -> None:
    """RUNNING has to be observable, or "start → running → complete" is one step."""
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await start(
        gateway,
        secret,
        session,
        repo,
        f'{PY} -c "import time; print(\'go\', flush=True); time.sleep(3)"',
    )
    execution_id = envelope["execution_id"]

    seen: set[str] = set()
    saw_running_record: dict[str, Any] = {}
    deadline = asyncio.get_running_loop().time() + 60.0
    while asyncio.get_running_loop().time() < deadline:
        record = await record_of(gateway, secret, session, execution_id)
        seen.add(str(record["status"]))
        if record["status"] in TERMINAL:
            break
        saw_running_record = record
        await asyncio.sleep(0.05)

    assert str(ExecutionStatus.RUNNING) in seen, seen
    assert str(ExecutionStatus.COMPLETED) in seen, seen
    # A running execution must not claim to be finished. Without this, a status
    # projection that hardcoded is_terminal=True would satisfy every other
    # assertion in this file.
    assert saw_running_record, "never observed a non-terminal record"
    assert saw_running_record["is_terminal"] is False, saw_running_record


async def test_start_running_cancel_cancelled(repo: Path) -> None:
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await start(
        gateway,
        secret,
        session,
        repo,
        f'{PY} -c "import time; time.sleep(120)"',
    )
    assert envelope["ok"] is True, envelope
    execution_id = envelope["execution_id"]

    # Wait until it is genuinely running before cancelling, so this exercises
    # RUNNING -> CANCELLED rather than racing the start.
    running_record: dict[str, Any] = {}
    deadline = asyncio.get_running_loop().time() + 30.0
    while asyncio.get_running_loop().time() < deadline:
        record = await record_of(gateway, secret, session, execution_id)
        if record["status"] == str(ExecutionStatus.RUNNING):
            running_record = record
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("never reached RUNNING")
    assert running_record["is_terminal"] is False, running_record

    cancelled = await call(
        gateway, secret, session, "process.cancel", {"execution_id": execution_id}
    )
    assert cancelled["ok"] is True, cancelled

    terminal = await wait_terminal(gateway, secret, session, execution_id)
    assert terminal["status"] == str(ExecutionStatus.CANCELLED), terminal
    assert terminal["is_terminal"] is True


# ── §8.5 cancel semantics ─────────────────────────────────────────────
async def test_cancelling_a_terminal_execution_is_idempotent(repo: Path) -> None:
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))

    envelope = await start(gateway, secret, session, repo, f'{PY} -c "print(1)"')
    execution_id = envelope["execution_id"]
    first = await wait_terminal(gateway, secret, session, execution_id)
    assert first["status"] == str(ExecutionStatus.COMPLETED), first

    repeat = await call(
        gateway, secret, session, "process.cancel", {"execution_id": execution_id}
    )

    assert repeat["ok"] is True, repeat
    after = await record_of(gateway, secret, session, execution_id)
    # A completed execution stays completed. A cancel that "succeeded" in
    # rewriting a terminal state would be a second lie on top of the first.
    assert after["status"] == str(ExecutionStatus.COMPLETED), after


async def test_one_principal_cannot_cancel_another_principals_execution(
    repo: Path,
) -> None:
    gateway, secret = make_gateway([repo], principal="owner")
    session = await open_session(gateway, secret, str(repo))
    envelope = await start(
        gateway,
        secret,
        session,
        repo,
        f'{PY} -c "import time; time.sleep(120)"',
    )
    execution_id = envelope["execution_id"]

    other_gateway, other_secret = make_gateway([repo], principal="intruder")
    other_session = await open_session(other_gateway, other_secret, str(repo))
    attempt = await call(
        other_gateway, other_secret, other_session, "process.cancel",
        {"execution_id": execution_id},
    )

    assert attempt["ok"] is False, attempt
    # The owner's execution is untouched by the failed attempt.
    still = await record_of(gateway, secret, session, execution_id)
    assert still["status"] not in {"CANCELLED"}, still

    await call(gateway, secret, session, "process.cancel", {"execution_id": execution_id})


# ── §8.4 the status projection is bounded and says so ─────────────────
async def test_status_carries_the_lifecycle_fields_a_caller_needs(repo: Path) -> None:
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))
    envelope = await start(gateway, secret, session, repo, f'{PY} -c "print(1)"')
    terminal = await wait_terminal(gateway, secret, session, envelope["execution_id"])

    for field in (
        "execution_id",
        "status",
        "phase",
        "started_at",
        "updated_at",
        "exit_code",
        "stdout_tail",
        "stderr_tail",
        "target_type",
        "admission_status",
    ):
        assert field in terminal, f"status projection is missing {field}"

    assert terminal["admission_status"], terminal
    assert terminal["target_type"], terminal


async def test_a_clipped_tail_is_reported_as_clipped(repo: Path) -> None:
    """A bounded tail presented as the whole output is a quiet lie.

    The process writes far more than the tail can hold, so the projection has to
    say the output was cut. Without the flag a caller would quote a truncated
    tail as everything the process said.
    """
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))
    envelope = await start(
        gateway,
        secret,
        session,
        repo,
        f'{PY} -c "print(\'x\'*400000)"',
    )
    terminal = await wait_terminal(gateway, secret, session, envelope["execution_id"])

    assert terminal["status"] == str(ExecutionStatus.COMPLETED), terminal
    assert terminal["stdout_truncated"] is True, terminal
    assert terminal["stderr_truncated"] is False, terminal
    assert len(terminal["stdout_tail"]) < 400000
    assert terminal["bytes_stdout"] == 400001


async def test_short_output_is_not_reported_as_clipped(repo: Path) -> None:
    gateway, secret = make_gateway([repo])
    session = await open_session(gateway, secret, str(repo))
    envelope = await start(
        gateway, secret, session, repo, f'{PY} -c "print(\'short\')"'
    )
    terminal = await wait_terminal(gateway, secret, session, envelope["execution_id"])

    assert terminal["stdout_truncated"] is False, terminal
    assert "short" in terminal["stdout_tail"]


def test_a_record_round_trips_the_truncation_flags() -> None:
    """Persisted records must keep the flags, or a restart silently loses them."""
    from veya.remote.execution import ExecutionRecord

    def make(execution_id: str, **extra):
        return ExecutionRecord(
            execution_id=execution_id,
            task_id="t1",
            session_id="s1",
            token_id="tok1",
            principal="p1",
            tool="test.run",
            veya_tool="coding_run_tests",
            requested_workspace="/tmp",
            requested_realpath="/tmp",
            resolved_repo_root="/tmp",
            repo_identity="repo1",
            **extra,
        )

    restored = ExecutionRecord.from_json(
        make("e1", stdout_truncated=True, stderr_truncated=False).to_json()
    )
    assert restored.stdout_truncated is True
    assert restored.stderr_truncated is False

    # A record persisted before this change has no such field. It must still
    # load, defaulting to "not truncated" rather than failing to deserialise.
    legacy_payload = make("e2").to_json()
    legacy_payload.pop("stdout_truncated")
    legacy_payload.pop("stderr_truncated")
    legacy = ExecutionRecord.from_json(legacy_payload)
    assert legacy.stdout_truncated is False
    assert legacy.stderr_truncated is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
