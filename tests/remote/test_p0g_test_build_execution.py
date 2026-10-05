"""P0-G: real test/build execution.

Every command here runs a real subprocess through the real gateway. Nothing is
mocked at the execution layer, so a terminal state in this file is evidence that
a process actually ran and exited, not that a return value looked right.

The taxonomy tests exist because "the tests failed" and "the runner crashed" and
"we never managed to start the runner" are three different facts, and a caller
that cannot tell them apart will read a broken environment as a red test suite.
"""

from __future__ import annotations

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
from veya.remote.direct_exec import DirectDenied, run_direct_command
from veya.remote.execution import (
    ExecutionFailureClass,
    ExecutionStore,
)
from veya.remote.mcp_server import create_gateway
from veya.remote.runtime_profile import compare_runtime, declared_runtime
from veya.remote.tool_adapter import _direct_failure_class

PERMS = RemotePermissions(read=True, write=True, shell=True, git=True)
PY = sys.executable


class GuardedExecutor:
    """Runs the real mutation tool; refuses everything that would fake a result.

    ``file.write`` is delegated to the real registry function rather than
    answered with a canned "ok". A stub that returns success without writing
    makes the repair loop below pass while repairing nothing, which is the exact
    shape of fake evidence this phase is supposed to exclude.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.writes: list[str] = []

    async def __call__(self, name: str, kwargs: dict[str, Any]) -> str:
        self.calls.append(name)
        if name == "write_file":
            from server.tool_registry import _tool_write_file

            result = _tool_write_file(
                str(kwargs.get("filepath")),
                str(kwargs.get("content", "")),
                bool(kwargs.get("overwrite", True)),
            )
            self.writes.append(str(kwargs.get("filepath")))
            return json.dumps({"status": "ok", "data": {"result": str(result)}})
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


def make_gateway(workspaces: list[Path], executor: Any):
    auth = RemoteAuth()
    _record, secret = auth.issue(
        "tester", permissions=PERMS, workspaces=[str(w) for w in workspaces]
    )
    audit = RemoteAudit(None)
    adapter = RemoteToolAdapter(executor, redact=audit.redact, execution_store=ExecutionStore(None))
    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=4),
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


async def initialize(gateway, secret, workspace: str) -> str:
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


async def run_and_wait(gateway, secret, session, name, arguments, *, timeout: float = 90.0):
    """Submit a command tool and return its terminal record."""
    import asyncio

    envelope = await call(gateway, secret, session, name, arguments)
    execution_id = envelope.get("execution_id")
    assert execution_id, envelope
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        record = await call(
            gateway, secret, session, "process.status", {"execution_id": execution_id}
        )
        payload = record.get("result", record)
        if payload.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
            return payload
        await asyncio.sleep(0.05)
    raise AssertionError(f"{name} never reached a terminal state")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    return make_git_repo(tmp_path / "proj")


# ── §7.3 a real passing test run ──────────────────────────────────────
async def test_a_real_passing_command_reaches_completed_with_a_real_exit_code(
    repo: Path,
) -> None:
    gateway, secret = make_gateway([repo], GuardedExecutor())
    session = await initialize(gateway, secret, str(repo))

    record = await run_and_wait(
        gateway,
        secret,
        session,
        "test.run",
        {"command": f'{PY} -c "print(1+1)"', "workspace": str(repo), "wait": True},
    )

    assert record["status"] == "COMPLETED", record
    assert record["exit_code"] == 0
    # The output is the process's own, not an adapter's summary of it.
    assert "2" in record["stdout_tail"]


# ── §7.4 a real failing run is not a crash ────────────────────────────
async def test_a_failing_test_run_is_a_verdict_not_a_runtime_failure(repo: Path) -> None:
    gateway, secret = make_gateway([repo], GuardedExecutor())
    session = await initialize(gateway, secret, str(repo))

    record = await run_and_wait(
        gateway,
        secret,
        session,
        "test.run",
        {
            "command": f'{PY} -c "import sys; print(2 failed); sys.exit(1)"',
            "workspace": str(repo),
            "wait": True,
        },
    )

    assert record["status"] == "FAILED", record
    assert record["exit_code"] == 1
    assert record["failure_class"] == str(ExecutionFailureClass.TEST_SUITE_FAILED), record


async def test_the_same_non_zero_exit_from_shell_is_a_runtime_failure(repo: Path) -> None:
    """The new class is scoped to verdict-reporting tools, not to exit codes.

    Without this the taxonomy would say "the project's tests failed" for any
    command that happens to exit non-zero, which is a claim about the project's
    health that a shell command has no standing to make.

    ``grep`` is used rather than an inline interpreter because the permission
    engine grades ``python -c`` as P2_ROOT_MUTATION and would refuse the run
    before any process existed to classify.
    """
    gateway, secret = make_gateway([repo], GuardedExecutor())
    session = await initialize(gateway, secret, str(repo))

    record = await run_and_wait(
        gateway,
        secret,
        session,
        "shell.exec",
        {
            "command": "grep -q zzzz_no_such_token README.md",
            "workspace": str(repo),
            "wait": True,
        },
    )

    assert record["status"] == "FAILED", record
    assert record["exit_code"] == 1
    assert record["failure_class"] == str(ExecutionFailureClass.PROCESS_RUNTIME_FAILURE), record


async def test_a_command_that_cannot_start_is_a_start_failure(repo: Path) -> None:
    gateway, secret = make_gateway([repo], GuardedExecutor())
    session = await initialize(gateway, secret, str(repo))

    record = await run_and_wait(
        gateway,
        secret,
        session,
        "test.run",
        {
            "command": "./definitely-not-a-real-binary-xyz",
            "workspace": str(repo),
            "wait": True,
        },
    )

    assert record["status"] == "FAILED", record
    assert record["failure_class"] in {
        str(ExecutionFailureClass.PROCESS_START_FAILURE),
        str(ExecutionFailureClass.TEST_SUITE_FAILED),
    }, record


def test_the_taxonomy_distinguishes_the_four_outcomes() -> None:
    """Unit-level pin on the classifier the live tests exercise end to end."""

    class Result:
        def __init__(self, status, exit_code, stderr="", timed_out=False):
            self.status = status
            self.exit_code = exit_code
            self.stderr_tail = stderr
            self.timed_out = timed_out

    assert _direct_failure_class(Result("timeout", None, timed_out=True), tool="test.run") == str(
        ExecutionFailureClass.PROCESS_TIMEOUT
    )
    assert _direct_failure_class(
        Result("failed", None, "unable to execute command: x"), tool="test.run"
    ) == str(ExecutionFailureClass.PROCESS_START_FAILURE)
    assert _direct_failure_class(Result("failed", -9), tool="test.run") == str(
        ExecutionFailureClass.PROCESS_RUNTIME_FAILURE
    )
    assert _direct_failure_class(Result("failed", 1), tool="test.run") == str(
        ExecutionFailureClass.TEST_SUITE_FAILED
    )
    assert _direct_failure_class(Result("failed", 1), tool="build.run") == str(
        ExecutionFailureClass.TEST_SUITE_FAILED
    )
    assert _direct_failure_class(Result("failed", 1), tool="shell.exec") == str(
        ExecutionFailureClass.PROCESS_RUNTIME_FAILURE
    )
    # A signal death is never a verdict, whatever tool reported it.
    assert _direct_failure_class(Result("failed", 137), tool="test.run") == str(
        ExecutionFailureClass.PROCESS_RUNTIME_FAILURE
    )


# ── §7.5 a real repair loop, with no manual step ──────────────────────
async def test_baseline_failure_then_patch_then_rerun_passes(repo: Path) -> None:
    """Fail, repair through the file tool, rerun. Nothing is edited by hand.

    The repair and the verification share one execution target. That is not
    incidental: ``test.run`` defaults to a fresh isolated worktree, so a patch
    applied to the canonical repo is invisible to the rerun and the loop reports
    the original failure forever. Binding both to ``CANONICAL_WORKTREE`` is what
    makes "patch then verify" a statement about the same tree.
    """
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    test_file = repo / "test_calc.py"
    test_file.write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    command = f"{PY} -m pytest test_calc.py -q"
    canonical = {"workspace": str(repo), "execution_target": "CANONICAL_WORKTREE", "wait": True}

    executor = GuardedExecutor()
    gateway, secret = make_gateway([repo], executor)
    session = await initialize(gateway, secret, str(repo))

    baseline = await run_and_wait(
        gateway, secret, session, "test.run", {"command": command, **canonical}
    )
    assert baseline["status"] == "FAILED", baseline
    assert baseline["failure_class"] == str(ExecutionFailureClass.TEST_SUITE_FAILED), baseline
    assert "1 failed" in baseline["stdout_tail"], baseline

    # Read the defect through the read tool, then repair through the write tool.
    read = await call(
        gateway, secret, session, "file.read", {"path": "calc.py", "workspace": str(repo)}
    )
    assert read["ok"] is True, read
    assert "return a - b" in read["result"]["text"]

    write = await call(
        gateway,
        secret,
        session,
        "file.write",
        {
            "path": "calc.py",
            "content": "def add(a, b):\n    return a + b\n",
            "workspace": str(repo),
            "execution_target": "CANONICAL_WORKTREE",
        },
    )
    assert write["ok"] is True, write
    assert executor.writes, "file.write must reach a real write, not a stubbed ok"
    assert (repo / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

    rerun = await run_and_wait(
        gateway, secret, session, "test.run", {"command": command, **canonical}
    )
    assert rerun["status"] == "COMPLETED", rerun
    assert rerun["exit_code"] == 0


# ── §7.6 the project's own build/lint command ─────────────────────────
async def test_the_projects_own_lint_command_runs_for_real(repo: Path) -> None:
    """Ruff is the canonical lint for this repository, not an assumed pytest."""
    gateway, secret = make_gateway([repo], GuardedExecutor())
    session = await initialize(gateway, secret, str(repo))
    lintable = repo / "ok.py"
    lintable.write_text("VALUE = 1\n", encoding="utf-8")

    record = await run_and_wait(
        gateway,
        secret,
        session,
        "build.run",
        {
            "command": f"{PY} -m ruff check --isolated ok.py",
            "workspace": str(repo),
            "wait": True,
        },
    )

    assert record["status"] == "COMPLETED", record
    assert record["exit_code"] == 0


# ── §7.8 target / cwd validation ──────────────────────────────────────
async def test_a_cwd_outside_the_task_workspace_is_refused(tmp_path: Path) -> None:
    """The containment check is the only thing between a command and the host.

    This was the one P0-G invariant with no test behind it: removing the check
    left every suite green, because nothing ever asked it to refuse. Both halves
    are pinned here — a real directory outside the workspace, and a directory
    that does not exist at all.
    """
    workspace = make_git_repo(tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified\n", encoding="utf-8")

    with pytest.raises(DirectDenied):
        await run_direct_command(workspace, [PY, "-c", "print(1)"], cwd=outside)

    with pytest.raises(DirectDenied):
        await run_direct_command(workspace, [PY, "-c", "print(1)"], cwd=tmp_path / "nope")


async def test_a_command_inside_the_workspace_is_allowed(tmp_path: Path) -> None:
    """The refusal above must be about containment, not about refusing commands."""
    workspace = make_git_repo(tmp_path / "ws")

    result = await run_direct_command(workspace, [PY, "-c", "print('inside')"])

    assert result.status == "passed"
    assert result.exit_code == 0
    assert "inside" in result.stdout_tail


async def test_a_path_escaping_the_workspace_never_reaches_a_process(repo: Path) -> None:
    gateway, secret = make_gateway([repo], GuardedExecutor())
    session = await initialize(gateway, secret, str(repo))
    outside = repo.parent / "outside"
    outside.mkdir(exist_ok=True)

    envelope = await call(
        gateway,
        secret,
        session,
        "test.run",
        {
            "command": f'{PY} -c "print(1)"',
            "path": "../outside",
            "workspace": str(repo),
            "execution_target": "CANONICAL_WORKTREE",
            "wait": True,
        },
    )

    assert envelope["ok"] is False, envelope
    assert envelope.get("execution_id") is None, envelope


# ── §7.7 declared runtime versus actual runtime ──────────────────────
def test_declared_runtime_is_read_from_the_projects_own_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.11"\n', encoding="utf-8"
    )
    (tmp_path / "package.json").write_text('{"engines": {"node": ">=20"}}', encoding="utf-8")
    (tmp_path / "go.mod").write_text("module x\n\ngo 1.22\n", encoding="utf-8")

    declared = declared_runtime(tmp_path)

    assert declared["python"] == ">=3.11"
    assert declared["node"] == ">=20"
    assert declared["go"] == "1.22"


def test_a_project_that_declares_nothing_declares_nothing(tmp_path: Path) -> None:
    assert declared_runtime(tmp_path) == {}


def test_a_missing_interpreter_is_a_mismatch_not_a_pass() -> None:
    result = compare_runtime({"python": ">=3.12"}, {})
    assert result["mismatches"]
    assert result["mismatches"][0]["actual"] == "(not installed)"


def test_an_uninterpretable_constraint_is_reported_not_assumed() -> None:
    """A constraint this code cannot parse must not be reported as satisfied."""
    result = compare_runtime({"python": ">=3.12, !=3.13.*, ===3.12"}, {"python": "3.12.4"})
    assert result["mismatches"] or result["uncompared"]
    assert not (result["mismatches"] == [] and result["uncompared"] == [])


async def test_a_declared_runtime_that_is_not_satisfied_refuses_to_run(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.99"\n', encoding="utf-8"
    )
    gateway, secret = make_gateway([repo], GuardedExecutor())
    session = await initialize(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "test.run",
        {"command": f'{PY} -c "print(1)"', "workspace": str(repo), "wait": True},
    )

    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "ENVIRONMENT_MISMATCH", envelope
    assert "3.99" in envelope["message"]
    # Nothing ran, so there is no execution to mistake for a result.
    assert envelope.get("execution_id") is None


async def test_a_satisfied_runtime_is_recorded_next_to_the_result(repo: Path) -> None:
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.1"\n', encoding="utf-8"
    )
    gateway, secret = make_gateway([repo], GuardedExecutor())
    session = await initialize(gateway, secret, str(repo))

    envelope = await call(
        gateway,
        secret,
        session,
        "test.run",
        {"command": f'{PY} -c "print(1)"', "workspace": str(repo), "wait": True},
    )

    assert envelope["ok"] is True, envelope
    environment = envelope["result"]["environment"]
    assert environment["verdict"] == "OK", environment
    assert environment["declared"]["python"] == ">=3.1"
    assert environment["actual"]["python"]
    assert environment["mismatches"] == []


# ── §7.6/G2 no silent guess about the runner ─────────────────────────
async def test_test_run_without_a_detectable_command_refuses_to_guess(
    tmp_path: Path,
) -> None:
    """A project with no test command must be told, not handed pytest.

    build.run has always failed closed here. test.run used to answer
    "python -m pytest -q", so a Go or Rust project was given a pytest run whose
    failure looked like the project's tests failing.
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(bare), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(bare), "add", "."], check=True)
    subprocess.run(["git", "-C", str(bare), "commit", "-qm", "init"], check=True)

    gateway, secret = make_gateway([bare], GuardedExecutor())
    session = await initialize(gateway, secret, str(bare))

    envelope = await call(gateway, secret, session, "test.run", {"workspace": str(bare)})

    assert envelope["ok"] is False, envelope
    assert envelope["error_code"] == "INVALID_ARGUMENT", envelope
    assert "no test command detected" in envelope["message"]
    assert "pytest" not in envelope["message"]


def test_this_repository_declares_and_satisfies_its_own_runtime() -> None:
    """The check must be true of the repo it ships in, or it is theatre."""
    root = Path(__file__).resolve().parents[2]
    declared = declared_runtime(root)
    assert declared.get("python"), "this repository must declare its python floor"

    version = ".".join(str(part) for part in sys.version_info[:3])
    result = compare_runtime(declared, {"python": version})
    assert result["mismatches"] == [], result
