"""XRT execution tests use an isolated fake CLI; no real provider CLI is run."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import shutil
import shutil
from pathlib import Path

import pytest

from runtime.coding import cli_agents
from runtime.coding.cli_agents import probe_xrt_runtime, safe_runtime_env
from veya.remote import executor_adapter, xrt_runtime


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _prepare_fake_cli(
    tmp_path: Path, monkeypatch, *, login: bool = True
) -> tuple[Path, Path, Path, list[str]]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "XRT Test")
    _git(root, "config", "user.email", "xrt-test@example.invalid")
    (root / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "baseline")

    home = tmp_path / "home"
    (home / ".devin").mkdir(parents=True)
    if login:
        (home / ".devin" / "credentials.json").write_text("test fixture only", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_cli = fake_bin / "fake-devin"
    fake_cli.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('fake-devin 1.0')\n"
        "    raise SystemExit(0)\n"
        "prompt = ' '.join(sys.argv[1:])\n"
        "print('FAKE_CLI_STARTED', flush=True)\n"
        "if 'fail' in prompt:\n"
        "    print('FAKE_CLI_FAILURE', flush=True)\n"
        "    raise SystemExit(7)\n"
        "if 'slow' in prompt:\n"
        "    time.sleep(20)\n"
        "pathlib.Path('tracked.txt').write_text('changed by fake CLI\\n', encoding='utf-8')\n"
        "print('FAKE_CLI_FINISHED', flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "devin")
    monkeypatch.setenv("VEYA_PROJECT_ROOT", str(root))
    monkeypatch.setenv("HOME", str(home))
    git_path = shutil.which("git")
    assert git_path is not None
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + str(Path(git_path).parent))
    monkeypatch.setattr(cli_agents, "_BUILTIN_PROFILES", {"devin": ("devin", "--version")})
    monkeypatch.setitem(cli_agents.XRT_RUNTIME_PROFILES["devin"], "command", str(fake_cli))
    monkeypatch.setattr(executor_adapter.event_store, "append", lambda *_args, **_kwargs: {})
    from server import events
    monkeypatch.setattr(events, "append_canonical_event", lambda *_args, **_kwargs: {})
    captured: list[str] = []
    monkeypatch.setattr(
        executor_adapter.event_store,
        "append",
        lambda envelope, **_kwargs: captured.append(getattr(envelope, "topic", "")) or {},
    )
    return root, home, fake_cli, captured


async def _wait_for_status(execution_id: str, limit_s: float = 8.0) -> dict:
    deadline = asyncio.get_running_loop().time() + limit_s
    while asyncio.get_running_loop().time() < deadline:
        current = xrt_runtime.get_status(execution_id)
        if current and current["status"] != "RUNNING":
            return current
        await asyncio.sleep(0.05)
    raise AssertionError(f"execution did not finish: {execution_id}")


def test_fake_cli_dispatch_collects_worktree_diff_and_stream(monkeypatch, tmp_path) -> None:
    root, _home, _fake_cli, captured = _prepare_fake_cli(tmp_path, monkeypatch)
    async def scenario() -> tuple[dict, dict]:
        started = await xrt_runtime.dispatch("devin", "edit tracked file", str(root), 5)
        return started, await _wait_for_status(started["execution_id"])
    started, result = asyncio.run(scenario())
    assert result["status"] == "COMPLETED"
    assert "FAKE_CLI_FINISHED" in result["stdout"]
    assert "changed by fake CLI" in result["git_diff"]
    assert "tracked.txt" in result["changed_files"]
    assert any(topic == "execution.adapter.output" for topic in captured)
    manifest = Path(result["artifact_manifest"])
    assert manifest.is_file()
    assert {"outputs/worktree.diff", "evidence/execution_result.json"}.issubset(
        {item["path"] for item in __import__("json").loads(manifest.read_text(encoding="utf-8"))["artifacts"]}
    )
    assert Path(result["worktree_path"]).resolve() != root.resolve()


def test_fake_cli_timeout_and_cancel(monkeypatch, tmp_path) -> None:
    root, _home, _fake_cli, _captured = _prepare_fake_cli(tmp_path, monkeypatch)
    async def scenario() -> tuple[dict, dict, dict]:
        started = await xrt_runtime.dispatch("devin", "slow task", str(root), 1)
        timed_out = await _wait_for_status(started["execution_id"], limit_s=8)
        second = await xrt_runtime.dispatch("devin", "slow task again", str(root), 10)
        cancelled = await xrt_runtime.cancel(second["execution_id"])
        final = await _wait_for_status(second["execution_id"], limit_s=8)
        return timed_out, {"cancelled": cancelled}, final
    timed_out, cancelled, final = asyncio.run(scenario())
    assert timed_out["status"] == "TIMEOUT"
    assert timed_out["exit_code"] == 124
    assert cancelled["cancelled"] is True
    assert final["status"] == "CANCELLED"


def test_fake_cli_concurrency_limit_is_enforced(monkeypatch, tmp_path) -> None:
    root, _home, _fake_cli, _captured = _prepare_fake_cli(tmp_path, monkeypatch)
    monkeypatch.setenv("VEYA_XRT_MAX_CONCURRENT", "2")
    monkeypatch.setenv("VEYA_XRT_MAX_PER_RUNTIME", "1")
    async def scenario() -> tuple[bool, dict]:
        started = await xrt_runtime.dispatch("devin", "slow concurrency task", str(root), 10)
        with pytest.raises(RuntimeError, match="concurrency limit"):
            await xrt_runtime.dispatch("devin", "second task", str(root), 5)
        cancelled = await xrt_runtime.cancel(started["execution_id"])
        final = await _wait_for_status(started["execution_id"], limit_s=8)
        return cancelled, final
    cancelled, final = asyncio.run(scenario())
    assert cancelled is True
    assert final["status"] == "CANCELLED"


def test_missing_runtime_is_reported_without_a_real_cli(monkeypatch, tmp_path) -> None:
    root, home, _fake_cli, _captured = _prepare_fake_cli(tmp_path, monkeypatch)
    missing = tmp_path / "not-installed-cli"
    monkeypatch.setitem(cli_agents.XRT_RUNTIME_PROFILES["devin"], "command", str(missing))
    result = probe_xrt_runtime("devin", root)
    assert result["installed"] is False
    assert result["state"] == "not_installed"
    assert safe_runtime_env("devin")["HOME"] == str(home)


def test_fake_cli_nonzero_exit_is_reported_as_failed(monkeypatch, tmp_path) -> None:
    root, _home, _fake_cli, _captured = _prepare_fake_cli(tmp_path, monkeypatch)
    async def scenario() -> tuple[dict, dict]:
        started = await xrt_runtime.dispatch("devin", "please fail this task", str(root), 5)
        return started, await _wait_for_status(started["execution_id"])
    started, result = asyncio.run(scenario())
    assert result["execution_id"] == started["execution_id"]
    assert result["status"] == "FAILED"
    assert result["exit_code"] == 7
    assert "FAKE_CLI_FAILURE" in result["stdout"]


def test_installed_fake_cli_without_login_is_not_ready(monkeypatch, tmp_path) -> None:
    root, _home, _fake_cli, _captured = _prepare_fake_cli(tmp_path, monkeypatch, login=False)
    result = probe_xrt_runtime("devin", root)
    assert result["installed"] is True
    assert result["logged_in"] is False
    assert result["state"] == "installed_not_logged_in"
