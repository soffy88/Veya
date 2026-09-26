"""Canonical Qualification Suite for OpenCode L1 Executor & PI Model Alignment.

Verifies:
1. OPENCODE registration integrity across all canonical layers:
   - veya/remote/tool_adapter.py (_WORKER_TYPES, _CLI_WORKERS, _TIMEOUT_SEPARATED_CLI_WORKERS)
   - veya/remote/worker_runtime.py (WORKER_CAPABILITIES)
   - veya/remote/executor_health.py (DEFAULT_EXECUTOR_PREFERENCE, EXECUTOR_ALIASES)
   - veya/supervision/orchestrated.py (L1_WORKERS)
   - veya/supervision/retask.py (_RETASK_WORKERS)
   - veya/supervision/reap.py (EXECUTOR_MARKERS)
2. PI strictly pinned to `veya1.2-free` across CLI argv, model identity, and configuration.
3. OPENCODE command construction, model resolution, and proxy/HOME propagation.
4. Explicit pin authority: worker="opencode" / worker="pi" fail closed without silent fallback.
5. Health-aware routing sequence: AGY -> OPENCODE -> PI -> GROK -> DSH -> CODEX -> HICODE.
6. CODEX and HICODE auto-skipped on quota exhaustion without deletion.
7. Failure classification taxonomy handles OpenCode signals.
8. Process reap tracks OpenCode markers.
9. Worktree isolation and execution structure.
"""

from __future__ import annotations

from pathlib import Path

from runtime.coding.worktree import WorktreeManager, teardown_worktree
from veya.remote.executor_health import (
    DEFAULT_EXECUTOR_PREFERENCE,
    EXECUTOR_ALIASES,
    ExecutorFailureClass,
    ExecutorHealth,
    ExecutorHealthRegistry,
    classify_executor_failure,
    resolve_executor,
)
from veya.remote.tool_adapter import (
    _CLI_WORKERS,
    _TIMEOUT_SEPARATED_CLI_WORKERS,
    _WORKER_TYPES,
    _resolve_opencode_binary,
    _resolve_opencode_model,
    _worker_command,
    _worker_model_identity,
    worker_availability,
)
from veya.remote.worker_runtime import WORKER_CAPABILITIES, RecoveryCapability
from veya.supervision.orchestrated import L1_WORKERS
from veya.supervision.reap import EXECUTOR_MARKERS, find_orphans
from veya.supervision.retask import _RETASK_WORKERS


def test_01_opencode_registration_integrity() -> None:
    """1. Verify OPENCODE is registered across all canonical registries."""
    assert "opencode" in _WORKER_TYPES
    assert _WORKER_TYPES["opencode"] == "OPENCODE"

    assert "opencode" in _CLI_WORKERS
    assert _CLI_WORKERS["opencode"]["provider"] == "opencode-go"
    assert "deepseek" in _CLI_WORKERS["opencode"]["model"]

    assert "opencode" in _TIMEOUT_SEPARATED_CLI_WORKERS

    assert "opencode" in WORKER_CAPABILITIES
    caps = WORKER_CAPABILITIES["opencode"]
    assert caps.supports_receipts is True
    assert caps.supports_cancel is True
    assert caps.recovery_capability == str(RecoveryCapability.REATTACH)

    assert "opencode" in L1_WORKERS
    assert "opencode" in _RETASK_WORKERS
    assert "opencode" in EXECUTOR_MARKERS
    assert "opencode" in EXECUTOR_MARKERS["opencode"]

    avail = worker_availability()
    assert "OPENCODE" in avail["available_workers"]
    assert "opencode" in EXECUTOR_ALIASES
    assert EXECUTOR_ALIASES["opencode"] == "opencode"


def test_02_pi_fixed_veya1_2_free(tmp_path: Path, monkeypatch) -> None:
    """2. Verify PI strictly uses veya1.2-free in config, command, and identity."""
    assert _CLI_WORKERS["pi"]["model"] == "veya1.2-free"

    pi_identity = _worker_model_identity("pi")
    assert pi_identity == ("VEYA_LOCAL", "veya1.2-free")

    fake_pi = tmp_path / "node_modules" / "pi-coding-agent" / "cli.js"
    fake_pi.parent.mkdir(parents=True)
    fake_pi.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    fake_pi.chmod(0o755)
    pi_bin = tmp_path / "pi"
    pi_bin.symlink_to(fake_pi)
    monkeypatch.setenv("VEYA_PI_BIN", str(pi_bin))

    argv, _ = _worker_command("pi", "test-pi-task")
    assert "--model" in argv
    model_idx = argv.index("--model")
    assert argv[model_idx + 1] == "veya1.2-free"
    # Never fall back to veya1.2 without -free
    assert "veya1.2" not in [
        arg for i, arg in enumerate(argv) if i != model_idx and arg == "veya1.2"
    ]


def test_03_opencode_command_construction_and_proxy(tmp_path: Path, monkeypatch) -> None:
    """3. Verify OPENCODE CLI command generation, env propagation, and model resolution."""
    fake_opencode = tmp_path / "opencode"
    fake_opencode.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_opencode.chmod(0o755)

    monkeypatch.setenv("VEYA_OPENCODE_BIN", str(fake_opencode))
    monkeypatch.setenv("VEYA_OPENCODE_MODEL", "opencode-go/deepseek-v4.1-flash")
    monkeypatch.setenv("VEYA_RUNTIME_PROXY", "http://127.0.0.1:7890")

    bin_path = _resolve_opencode_binary()
    assert bin_path == str(fake_opencode)

    model = _resolve_opencode_model()
    assert model == "opencode-go/deepseek-v4.1-flash"

    identity = _worker_model_identity("opencode")
    assert identity == ("opencode-go", "opencode-go/deepseek-v4.1-flash")

    argv, env = _worker_command("opencode", "write a hello world script")
    assert argv[0] == str(fake_opencode)
    assert argv[1] == "run"
    assert "write a hello world script" in argv
    assert "--model" in argv
    assert "opencode-go/deepseek-v4.1-flash" in argv
    assert env.get("HOME")
    assert env.get("http_proxy") == "http://127.0.0.1:7890"
    assert env.get("https_proxy") == "http://127.0.0.1:7890"


def test_03b_opencode_coding_mode_binds_execution_worktree(tmp_path: Path, monkeypatch) -> None:
    fake_opencode = tmp_path / "opencode"
    fake_opencode.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_opencode.chmod(0o755)
    monkeypatch.setenv("VEYA_OPENCODE_BIN", str(fake_opencode))
    monkeypatch.setenv("VEYA_OPENCODE_MODEL", "qualified-model")

    argv, _ = _worker_command(
        "opencode",
        "create result.txt",
        worktree_path=str(tmp_path / "execution-worktree"),
        coding_mode=True,
        agent="build",
    )
    assert "--dir" in argv
    assert str(tmp_path / "execution-worktree") in argv
    assert "--format" in argv and "json" in argv
    assert "--auto" in argv
    assert "--agent" in argv and "build" in argv


def test_03c_opencode_runtime_state_is_not_in_execution_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_home = tmp_path / "managed-opencode"
    source_data = tmp_path / "source-data"
    worktree = tmp_path / "execution-worktree"
    source_auth = source_data / "opencode" / "auth.json"
    source_auth.parent.mkdir(parents=True)
    source_auth.write_text('{"opencode-go": {"type": "api"}}\n', encoding="utf-8")
    monkeypatch.setenv("VEYA_OPENCODE_RUNTIME_HOME", str(runtime_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(source_data))
    monkeypatch.setenv("VEYA_OPENCODE_BIN", "/bin/true")

    _argv, env = _worker_command(
        "opencode",
        "read base.txt",
        worktree_path=str(worktree),
        coding_mode=True,
        agent="build",
    )
    assert env["HOME"] != str(worktree)
    assert env["XDG_DATA_HOME"] == str(runtime_home / "data")
    assert env["XDG_STATE_HOME"] == str(runtime_home / "state")
    assert env["XDG_CACHE_HOME"] == str(runtime_home / "cache")
    mirrored_auth = runtime_home / "data" / "opencode" / "auth.json"
    assert mirrored_auth.read_text(encoding="utf-8") == source_auth.read_text(encoding="utf-8")
    assert mirrored_auth.stat().st_mode & 0o777 == 0o600
    assert all(
        not Path(env[key]).is_relative_to(worktree)
        for key in (
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        )
    )


def test_04_explicit_pin_no_silent_fallback() -> None:
    """4. Explicit pin must execute real requested worker, failing closed without substitution."""
    reg = ExecutorHealthRegistry()
    reg.record_failure("opencode", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    reg.record_failure("pi", ExecutorFailureClass.TRANSPORT_FAILURE)

    # Even if opencode is UNAVAILABLE, explicit_pin=True NEVER substitutes
    selected, sub = resolve_executor(requested="opencode", explicit_pin=True, health_registry=reg)
    assert selected == "opencode"
    assert sub is None

    # Even if pi is UNAVAILABLE, explicit_pin=True NEVER substitutes
    selected_pi, sub_pi = resolve_executor(requested="pi", explicit_pin=True, health_registry=reg)
    assert selected_pi == "pi"
    assert sub_pi is None


def test_05_health_aware_fallback_sequence() -> None:
    """5. Health-aware routing sequence: AGY -> OPENCODE -> PI -> GROK -> DSH -> CODEX -> HICODE."""
    reg = ExecutorHealthRegistry()

    # 1. Healthy baseline -> AGY
    cand1, sub1 = resolve_executor(health_registry=reg)
    assert cand1 == "antigravity"
    assert sub1 is None

    # 2. AGY down -> OPENCODE
    reg.record_failure("antigravity", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    cand2, sub2 = resolve_executor(health_registry=reg)
    assert cand2 == "opencode"
    assert sub2 is not None and sub2.selected_executor == "opencode"

    # 3. AGY + OPENCODE down -> PI
    reg.record_failure("opencode", ExecutorFailureClass.TRANSPORT_FAILURE)
    cand3, sub3 = resolve_executor(health_registry=reg)
    assert cand3 == "pi"
    assert sub3 is not None and sub3.selected_executor == "pi"

    # 4. AGY + OPENCODE + PI down -> GROK
    reg.record_failure("pi", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    cand4, sub4 = resolve_executor(health_registry=reg)
    assert cand4 == "grok"
    assert sub4 is not None and sub4.selected_executor == "grok"

    # 5. AGY + OPENCODE + PI + GROK down -> DSH
    reg.record_failure("grok", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    cand5, sub5 = resolve_executor(health_registry=reg)
    assert cand5 == "dsh"
    assert sub5 is not None and sub5.selected_executor == "dsh"

    # 6. AGY..DSH down -> CODEX
    reg.record_failure("dsh", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    cand6, sub6 = resolve_executor(health_registry=reg)
    assert cand6 == "codex"
    assert sub6 is not None and sub6.selected_executor == "codex"

    # 7. AGY..CODEX down -> HICODE
    reg.record_failure("codex", ExecutorFailureClass.PROVIDER_UNAVAILABLE)
    cand7, sub7 = resolve_executor(health_registry=reg)
    assert cand7 == "hicode"
    assert sub7 is not None and sub7.selected_executor == "hicode"


def test_06_codex_and_hicode_quota_exhausted_auto_skipped() -> None:
    """6. CODEX/HICODE auto-skipped on quota exhaustion without permanent deregistration."""
    reg = ExecutorHealthRegistry()
    for w in ("antigravity", "opencode", "pi", "grok", "dsh"):
        reg.record_failure(w, ExecutorFailureClass.PROVIDER_UNAVAILABLE)

    # Codex hits 429 quota exhausted
    reg.record_failure(
        "codex", ExecutorFailureClass.PROVIDER_UNAVAILABLE, detail="429 rate limit quota exceeded"
    )
    assert reg.get_health("codex") == ExecutorHealth.UNAVAILABLE

    # Routing auto-skips CODEX to HICODE
    selected, sub = resolve_executor(health_registry=reg)
    assert selected == "hicode"
    assert sub is not None and sub.selected_executor == "hicode"

    # Both remain in canonical preference definition
    assert "codex" in DEFAULT_EXECUTOR_PREFERENCE
    assert "hicode" in DEFAULT_EXECUTOR_PREFERENCE


def test_07_failure_taxonomy_for_opencode() -> None:
    """7. Failure taxonomy correctly classifies OpenCode failure signals."""
    # 429 / quota
    fc_quota = classify_executor_failure(
        detail="OpenCode provider 429: quota exhausted, purchase more credits"
    )
    assert fc_quota == ExecutorFailureClass.PROVIDER_UNAVAILABLE

    # auth failure
    fc_auth = classify_executor_failure(
        detail="OpenCode authentication failed: 401 unauthorized bad credentials"
    )
    assert fc_auth == ExecutorFailureClass.AUTH_FAILURE

    # transport failure
    fc_trans = classify_executor_failure(
        detail="OpenCode transport channel closed: connection refused"
    )
    assert fc_trans == ExecutorFailureClass.TRANSPORT_FAILURE

    # binary missing
    fc_env = classify_executor_failure(detail="opencode: command not found")
    assert fc_env == ExecutorFailureClass.ENVIRONMENT_FAILURE

    # non-zero crash
    fc_crash = classify_executor_failure(exit_code=1)
    assert fc_crash == ExecutorFailureClass.WORKER_CRASH

    # timeout
    fc_timeout = classify_executor_failure(
        error=TimeoutError("opencode run hard max runtime exceeded")
    )
    assert fc_timeout == ExecutorFailureClass.WORKER_TIMEOUT


def test_08_process_reap_opencode_marker() -> None:
    """8. Process reap tracks opencode executor markers."""
    assert "opencode" in EXECUTOR_MARKERS
    assert "opencode" in EXECUTOR_MARKERS["opencode"]
    # Ensure find_orphans can query opencode without error
    orphans = find_orphans("/tmp/nonexistent-workspace", "opencode")
    assert orphans == []


def test_09_worktree_isolation(tmp_path: Path) -> None:
    """9. Worktree isolation verified for opencode task lane."""
    repo_path = tmp_path / "repo"
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    wm = WorktreeManager(repo_path)
    rec = wm.create("task-opencode-01", "opencode task objective")
    wt_path = Path(rec.path)
    assert wt_path.exists()
    assert (wt_path / "base.txt").exists()

    # Teardown
    teardown_worktree(wt_path, execution_status="COMPLETED")


async def test_10_opencode_text_transport_smoke(tmp_path: Path) -> None:
    """10. Text transport smoke only; this is not coding qualification."""
    import shutil
    import subprocess

    import pytest

    if not shutil.which("opencode"):
        pytest.skip("opencode binary not installed")

    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
        status,
        wait_phase,
    )

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {"tasks": [{"worker": "opencode", "task": "Return exactly: VEYA_OPENCODE_LIVE_OK"}]},
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]

    final = await wait_phase(
        gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=120
    )
    assert final["status"] == "COMPLETED"

    child_st = await status(gateway, secret, session, child_id)
    assert child_st["status"] == "COMPLETED"
    assert child_st["worker_type"] == "OPENCODE"
    assert child_st["execution_mode"] == "direct_opencode"
    assert "VEYA_OPENCODE_LIVE_OK" in (child_st.get("result_summary") or "")

    # Check process reaping
    orphans = find_orphans(str(repo_path), "opencode")
    assert orphans == []


async def test_10b_live_opencode_read_qualification(tmp_path: Path) -> None:
    """10b. Real READ qualification is separate from text transport smoke."""
    import os
    import shutil
    import subprocess

    import pytest

    if not shutil.which("opencode"):
        pytest.skip("opencode binary not installed")
    if not os.environ.get("VEYA_OPENCODE_AGENT"):
        pytest.skip("VEYA_OPENCODE_AGENT is required for read qualification")

    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("VEYA_READ_OK\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
        status,
        wait_phase,
    )

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {
                    "worker": "opencode",
                    "task": (
                        "Read base.txt and report its exact contents as VEYA_READ_OK. "
                        "Do not modify any file."
                    ),
                    "task_kind": "READ",
                    "effect_requirement": "READ_ONLY",
                    "verification_requirement": "NONE",
                    "commit_requirement": "NONE",
                    "promotion_policy": "NONE",
                }
            ]
        },
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]
    final = await wait_phase(
        gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=180
    )
    child_st = await status(gateway, secret, session, child_id)
    assert final["status"] == "COMPLETED"
    assert child_st["status"] == "COMPLETED"
    assert "VEYA_READ_OK" in (child_st.get("result_summary") or "")
    assert child_st.get("effect_receipt", {}).get("changed_files", []) == []
    assert (repo_path / "base.txt").read_text(encoding="utf-8") == "VEYA_READ_OK\n"


async def test_11_live_opencode_real_write_qualification(tmp_path: Path) -> None:
    """11. Real OpenCode WRITE qualification, enabled only when configured."""
    import os
    import shutil
    import subprocess

    import pytest

    if not shutil.which("opencode"):
        pytest.skip("opencode binary not installed")
    if not os.environ.get("VEYA_OPENCODE_AGENT"):
        pytest.skip("VEYA_OPENCODE_AGENT is required for coding qualification")
    if os.environ.get("VEYA_OPENCODE_WRITE_QUALIFIED") != "1":
        pytest.skip("live coding qualification is not explicitly enabled")

    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("BASE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
        status,
        wait_phase,
    )

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {
                    "worker": "opencode",
                    "task": (
                        "Read base.txt. Create result.txt with exactly "
                        "VEYA_OPENCODE_WRITE_OK and verify its contents."
                    ),
                    "task_kind": "WRITE",
                    "effect_requirement": "FILES_CHANGED",
                    "verification_requirement": "REQUIRED",
                    "commit_requirement": "REQUIRED",
                    "promotion_policy": "MANUAL",
                    "allowed_files": ["result.txt"],
                    "verification_command": 'test "$(cat result.txt)" = VEYA_OPENCODE_WRITE_OK',
                }
            ]
        },
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]
    final = await wait_phase(
        gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=180
    )
    assert final["status"] == "COMPLETED"
    child_st = await status(gateway, secret, session, child_id)
    assert child_st["status"] == "COMPLETED"
    assert child_st.get("effect_receipt", {}).get("changed_files") == ["result.txt"]
    assert child_st.get("execution_commit_sha")
    assert not (repo_path / "result.txt").exists()


async def test_12_live_opencode_shell_tool_qualification(tmp_path: Path) -> None:
    """12. Real OpenCode shell tool evidence is distinct from file mutation."""
    import os
    import shutil
    import subprocess

    import pytest

    if not shutil.which("opencode"):
        pytest.skip("opencode binary not installed")
    if not os.environ.get("VEYA_OPENCODE_AGENT"):
        pytest.skip("VEYA_OPENCODE_AGENT is required for coding qualification")
    if os.environ.get("VEYA_OPENCODE_WRITE_QUALIFIED") != "1":
        pytest.skip("live coding qualification is not explicitly enabled")

    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("BASE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)

    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
        status,
        wait_phase,
    )

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {
                    "worker": "opencode",
                    "task": (
                        "Use your shell tool to run exactly: printf 'VEYA_OPENCODE_SHELL_OK\\n' "
                        "> shell-result.txt. Then read shell-result.txt and verify its exact value. "
                        "Do not use a file-edit tool for this file; perform the shell command."
                    ),
                    "task_kind": "WRITE",
                    "effect_requirement": "FILES_CHANGED",
                    "verification_requirement": "REQUIRED",
                    "commit_requirement": "REQUIRED",
                    "promotion_policy": "MANUAL",
                    "allowed_files": ["shell-result.txt"],
                    "verification_command": 'test "$(cat shell-result.txt)" = VEYA_OPENCODE_SHELL_OK',
                }
            ]
        },
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]
    final = await wait_phase(
        gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=180
    )
    assert final["status"] == "COMPLETED"
    child_st = await status(gateway, secret, session, child_id)
    receipt = child_st.get("effect_receipt", {})
    assert child_st["status"] == "COMPLETED"
    assert receipt.get("changed_files") == ["shell-result.txt"]
    assert receipt.get("shell_calls")
    assert receipt.get("tool_calls")
    assert not (repo_path / "shell-result.txt").exists()


async def test_13_live_opencode_write_finalize_and_promote(tmp_path: Path, monkeypatch) -> None:
    """13. A real OpenCode commit reaches canonical only through git.promote."""
    import os
    import shutil
    import subprocess

    import pytest

    if not shutil.which("opencode"):
        pytest.skip("opencode binary not installed")
    if not os.environ.get("VEYA_OPENCODE_AGENT"):
        pytest.skip("VEYA_OPENCODE_AGENT is required for coding qualification")
    if os.environ.get("VEYA_OPENCODE_WRITE_QUALIFIED") != "1":
        pytest.skip("live coding qualification is not explicitly enabled")

    repo_path = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_path)], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "t"], check=True)
    (repo_path / "base.txt").write_text("BASE\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-qm", "init"], check=True)
    base_sha = subprocess.check_output(
        ["git", "-C", str(repo_path), "rev-parse", "refs/heads/main"], text=True
    ).strip()
    monkeypatch.setenv("VEYA_EXECUTION_SQLITE_PATH", str(tmp_path / "promotion.sqlite3"))

    from tests.remote.test_l1_parallel import (
        call_tool,
        initialize,
        make_gateway,
        status,
        wait_phase,
    )

    gateway, secret = make_gateway(repo_path)
    session = await initialize(gateway, secret)
    envelope = await call_tool(
        gateway,
        secret,
        session,
        "worker.dispatch",
        {
            "tasks": [
                {
                    "worker": "opencode",
                    "task": (
                        "Read base.txt. Create result.txt containing exactly "
                        "VEYA_OPENCODE_PROMOTE_OK. Verify the exact value."
                    ),
                    "task_kind": "WRITE",
                    "effect_requirement": "FILES_CHANGED",
                    "verification_requirement": "REQUIRED",
                    "commit_requirement": "REQUIRED",
                    "promotion_policy": "MANUAL",
                    "allowed_files": ["result.txt"],
                    "verification_command": 'test "$(cat result.txt)" = VEYA_OPENCODE_PROMOTE_OK',
                }
            ]
        },
    )
    assert envelope["ok"] is True
    parent_id = envelope["execution_id"]
    child_id = envelope["result"]["child_execution_ids"][0]
    parent = await wait_phase(
        gateway, secret, session, parent_id, {"COMPLETED", "FAILED"}, timeout=180
    )
    assert parent["status"] == "COMPLETED"
    child = await status(gateway, secret, session, child_id)
    assert child["status"] == "COMPLETED"
    assert child["finalization_status"] == "PROMOTABLE"
    assert child["execution_commit_sha"]
    assert child["effect_receipt"]["base_sha"] == base_sha
    assert not (repo_path / "result.txt").exists()

    before = subprocess.check_output(
        ["git", "-C", str(repo_path), "rev-parse", "refs/heads/main"], text=True
    ).strip()
    assert before == base_sha
    promoted = await call_tool(
        gateway,
        secret,
        session,
        "git.promote",
        {"execution_id": child_id, "expected_base_sha": base_sha},
    )
    assert promoted["ok"] is True, promoted
    assert promoted["result"]["execution_commit_sha"] == child["execution_commit_sha"]
    after = subprocess.check_output(
        ["git", "-C", str(repo_path), "rev-parse", "refs/heads/main"], text=True
    ).strip()
    assert after == child["execution_commit_sha"]
    assert (
        subprocess.check_output(
            ["git", "-C", str(repo_path), "show", f"{after}:result.txt"], text=True
        ).strip()
        == "VEYA_OPENCODE_PROMOTE_OK"
    )
    assert not (repo_path / "result.txt").exists()

    replay = await call_tool(
        gateway,
        secret,
        session,
        "git.promote",
        {"execution_id": child_id, "expected_base_sha": base_sha},
    )
    assert replay["ok"] is True, replay
    assert replay["result"]["promotion_mode"] == "IDEMPOTENT_ALREADY_PROMOTED"
    assert (
        subprocess.check_output(
            ["git", "-C", str(repo_path), "rev-parse", "refs/heads/main"], text=True
        ).strip()
        == after
    )
