"""W17 group A — ssh core, backend, host fields, flags (offline)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from server import feature_flags
from server.remote_control import flags, process
from server.remote_control import ssh as ssh_mod
from server.remote_control.backend import BackendError, LocalBackend, SshBackend, classify_or_refuse
from server.remote_control.hosts import HostValidationError, RemoteHost, validate_host
from tests.w17_fakes import LocalExecRunner, RecordingRunner


def _host(tmp_path: Path | None = None, **kw) -> RemoteHost:
    base = dict(host_id="lap", display_name="Laptop", tailnet_ip="100.64.0.7", owner_id="u1")
    if tmp_path is not None:
        base.update(write_root=str(tmp_path / "w"), default_cwd=str(tmp_path / "w"))
    base.update(kw)
    return RemoteHost(**base)


@pytest.fixture(autouse=True)
def _cm_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SSH_HOSTS_CM_DIR", str(tmp_path / "cm"))
    monkeypatch.setenv("VEYA_SSH_HOSTS_KEYS_DIR", str(tmp_path / "keys"))


# ── flags ──


def test_flag_registered_appended() -> None:
    names = [f.name for f in feature_flags.FLAGS]
    assert "VEYA_SSH_HOSTS" in names
    assert names.index("VEYA_SSH_HOSTS") > names.index("VEYA_MCP_PRESETS")
    assert next(f for f in feature_flags.FLAGS if f.name == "VEYA_SSH_HOSTS").default is False


def test_flag_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("VEYA_SSH_HOSTS", raising=False)
    monkeypatch.setenv("VEYA_REMOTE_CONTROL", "1")
    assert flags.ssh_enabled() is False
    monkeypatch.setenv("VEYA_SSH_HOSTS", "1")
    assert flags.ssh_enabled() is True
    monkeypatch.delenv("VEYA_REMOTE_CONTROL", raising=False)
    assert flags.ssh_enabled() is False  # needs the W13 flag too


# ── host record ──


def test_host_ssh_fields_default_and_validate() -> None:
    old = RemoteHost.from_dict({"host_id": "old", "tailnet_ip": "100.64.0.9", "owner_id": "u"})
    assert (old.ssh_port, old.os_family, old.write_root, old.ssh_key_ref) == (22, "linux", "", "")
    validate_host(old)  # a W13 record stays valid
    validate_host(
        _host(write_root="/home/me/work", ssh_user="me", ssh_port=2222, ssh_key_ref="lap_key")
    )
    for bad in (
        {"os_family": "plan9"},
        {"ssh_port": 70000},
        {"write_root": "/"},
        {"write_root": "relative/dir"},
        {"ssh_host_key_policy": "no"},
        {"ssh_user": "-oProxyCommand=x"},
    ):
        with pytest.raises(HostValidationError):
            validate_host(_host(**bad))


def test_key_ref_rejects_path() -> None:
    for ref in ("/home/me/.ssh/id_ed25519", "..\\key", "ssh key", "-----BEGIN"):
        with pytest.raises(HostValidationError):
            validate_host(_host(ssh_key_ref=ref))
    validate_host(_host(ssh_key_ref="laptop-key.v1"))


def test_key_ref_pem_materialised_0600_and_masked(tmp_path) -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
    host = _host(ssh_key_ref="lap_key")
    path, _secrets = ssh_mod.resolve_key_path(
        host, lookup=lambda ref: pem if ref == "lap_key" else None
    )
    assert Path(path).read_text().strip() == pem
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    runner = RecordingRunner(stdout=pem.encode())
    result = ssh_mod.run_remote(host, ["cat", "x"], runner=runner, key_lookup=lambda ref: pem)
    assert "PRIVATE KEY" not in result.stdout and "[REDACTED]" in result.stdout
    with pytest.raises(ssh_mod.SshError):
        ssh_mod.resolve_key_path(host, lookup=lambda ref: None)


# ── argv ──


def test_ssh_argv_shape() -> None:
    runner = RecordingRunner()
    ssh_mod.run_remote(
        _host(ssh_user="me", ssh_port=2222), ["ls", "-la", "a b; rm -rf ~"], runner=runner
    )
    argv = runner.calls[0]["argv"]
    assert argv[0] == "ssh"
    for opt in (
        "BatchMode=yes",
        "StrictHostKeyChecking=accept-new",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
        "ControlMaster=auto",
    ):
        assert opt in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert argv[-3:-1] == ["me@100.64.0.7", "--"]
    # model text is ONE quoted word on the remote side, never a second command
    assert argv[-1] == "ls -la 'a b; rm -rf ~'"
    strict = RecordingRunner()
    ssh_mod.run_remote(_host(ssh_host_key_policy="strict"), ["true"], runner=strict)
    assert "StrictHostKeyChecking=yes" in strict.calls[0]["argv"]


def test_bad_target_and_newlines_refused() -> None:
    with pytest.raises(ssh_mod.SshError):
        ssh_mod.ssh_target(_host(tailnet_ip="-oProxyCommand=evil"))
    with pytest.raises(ssh_mod.SshError):
        ssh_mod.quote_remote(["echo", "a\rb"])
    with pytest.raises(ssh_mod.SshError):
        ssh_mod.run_remote(_host(os_family="windows-wsl"), ["true"], runner=RecordingRunner())


def test_probe_reachable_unreachable() -> None:
    ok = ssh_mod.probe(_host(), runner=RecordingRunner(stdout=b"Darwin\n"))
    assert ok.reachable and ok.os_family_echo == "macos"
    down = ssh_mod.probe(
        _host(), runner=RecordingRunner(returncode=255, stdout=b"", stderr=b"Connection refused")
    )
    assert not down.reachable and "refused" in down.error
    slow = ssh_mod.probe(
        _host(), runner=RecordingRunner(returncode=None, timed_out=True, stdout=b"")
    )
    assert not slow.reachable and slow.error == "timeout"


def test_output_truncated(monkeypatch) -> None:
    monkeypatch.setenv("VEYA_SSH_HOSTS_MAX_OUTPUT", "100")
    result = ssh_mod.run_remote(_host(), ["x"], runner=RecordingRunner(stdout=b"y" * 5000))
    assert len(result.stdout) < 200 and "truncated" in result.stdout


def test_spawn_argv_list_shell_false(monkeypatch) -> None:
    seen = {}

    def fake_run(args, **kw):
        seen.update(args=args, **kw)

        class R:
            returncode, stdout, stderr = 0, b"ok", b""

        return R()

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    monkeypatch.setattr(process, "_resolve", lambda b: "/usr/bin/" + b)
    out = process.run_capture(["ssh", "--", "echo hi; rm x"], timeout_s=3)
    assert isinstance(seen["args"], list) and seen["args"][-1] == "echo hi; rm x"
    assert "shell" not in seen and out.returncode == 0


# ── backend ──


@pytest.fixture
def ssh_backend(tmp_path) -> SshBackend:
    (tmp_path / "w").mkdir()
    (tmp_path / "outside").mkdir()
    return SshBackend(_host(tmp_path), runner=LocalExecRunner())


def test_ssh_backend_read_write_list_grep(ssh_backend, tmp_path) -> None:
    root = tmp_path / "w"
    msg = ssh_backend.write_text("sub/hello.txt", "hello 'world'\n$(touch pwned)\n", overwrite=True)
    assert "wrote" in msg
    assert (root / "sub" / "hello.txt").read_text() == "hello 'world'\n$(touch pwned)\n"
    assert not (root / "pwned").exists()
    assert ssh_backend.read_text(str(root / "sub/hello.txt")).startswith("hello 'world'")
    names = {row["name"]: row["type"] for row in ssh_backend.list_dir(str(root))}
    assert names == {"sub": "d"}
    assert "hello.txt:1:hello" in ssh_backend.grep("hel+o", root=str(root))
    with pytest.raises(BackendError):
        ssh_backend.write_text("sub/hello.txt", "x", overwrite=False)
    ssh_backend.replace_text("sub/hello.txt", "world", "there")
    assert "there" in (root / "sub/hello.txt").read_text()


def test_path_escape_refused(ssh_backend, tmp_path) -> None:
    (tmp_path / "outside" / "secret.txt").write_text("s")
    for bad in ("../outside/x.txt", str(tmp_path / "outside" / "x.txt"), "/etc/passwd"):
        with pytest.raises(BackendError):
            ssh_backend.write_text(bad, "x")
    with pytest.raises(BackendError):
        ssh_backend.read_text(str(tmp_path / "outside" / "secret.txt"))
    # symlink inside write_root pointing outside: caught by the remote realpath re-check
    os.symlink(tmp_path / "outside", tmp_path / "w" / "link")
    with pytest.raises(BackendError):
        ssh_backend.write_text("link/evil.txt", "x")
    assert not (tmp_path / "outside" / "evil.txt").exists()


def test_read_only_host_refuses_writes(tmp_path) -> None:
    backend = SshBackend(_host(default_cwd=str(tmp_path)), runner=LocalExecRunner())
    with pytest.raises(BackendError, match="read-only"):
        backend.write_text(str(tmp_path / "a.txt"), "x")


def test_ssh_backend_run_and_policy(ssh_backend, tmp_path) -> None:
    out = ssh_backend.run("echo hi && pwd")
    assert out.returncode == 0 and "hi" in out.stdout and str(tmp_path / "w") in out.stdout
    with pytest.raises(BackendError, match="refused by policy"):
        classify_or_refuse("rm -rf /", workspace_root=str(tmp_path / "w"))
    with pytest.raises(BackendError):
        classify_or_refuse("bash", workspace_root="/")  # interactive shell


def test_bg_start_status_kill(ssh_backend) -> None:
    handle = ssh_backend.spawn_bg("echo started; sleep 0.3; echo done", name="job1")
    assert handle.log_path.endswith(".veya/run/job1.log")
    deadline = time.time() + 10
    status = ssh_backend.bg_status("job1")
    while not status["finished"] and time.time() < deadline:
        time.sleep(0.1)
        status = ssh_backend.bg_status("job1")
    assert status["finished"] and status["exit_code"] == 0 and "done" in status["log_tail"]
    ssh_backend.spawn_bg("sleep 30", name="job2")
    time.sleep(0.2)
    assert ssh_backend.bg_status("job2")["finished"] is False
    ssh_backend.bg_kill("job2")
    killed = ssh_backend.bg_status("job2")
    assert killed["finished"] and killed["exit_code"] == 143
    assert {r["name"] for r in ssh_backend.list_runs()} == {"job1", "job2"}
    assert ssh_backend.bg_status("nope")["found"] is False
    with pytest.raises(BackendError):
        ssh_backend.spawn_bg("true", name="../x")


def test_local_backend_parity(tmp_path, monkeypatch) -> None:
    """Same file, local canonical helpers vs ssh backend: same content/listing."""
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path / "w"))
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "a.txt").write_text("same content\n")
    local = LocalBackend()
    remote = SshBackend(_host(tmp_path), runner=LocalExecRunner())
    path = str(tmp_path / "w" / "a.txt")
    assert local.read_text(path, max_bytes=100) == remote.read_text(path, max_bytes=100)
    strip = lambda rows: [(r["name"], r["type"]) for r in rows]  # noqa: E731
    assert strip(local.list_dir(str(tmp_path / "w"))) == strip(remote.list_dir(str(tmp_path / "w")))
