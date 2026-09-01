"""Managed Reasonix runtime boundary and clean-install guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.hicode_runtime import HicodeExecutorAdapter, HicodeRuntimeError


def _fake_reasonix(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' 'reasonix v{version}'\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_managed_runtime_wins_over_explicit_override(tmp_path: Path, monkeypatch):
    managed = _fake_reasonix(tmp_path / "managed/node_modules/.bin/reasonix", "1.21.3")
    monkeypatch.setenv("HICODE_MANAGED_RUNTIME_ROOT", str(tmp_path / "managed"))
    monkeypatch.setenv("HICODE_BIN", str(tmp_path / "missing-reasonix"))
    monkeypatch.delenv("HICODE_PRODUCTION", raising=False)

    adapter = HicodeExecutorAdapter()
    status = adapter.status()

    assert status.executable == str(managed.resolve())
    assert status.source == "managed"
    assert status.managed_reasonix_available is True
    assert status.managed_reasonix_version == "1.21.3"
    assert status.managed_reasonix_compatible is True


def test_production_does_not_accept_explicit_binary(tmp_path: Path, monkeypatch):
    override = _fake_reasonix(tmp_path / "override/reasonix", "1.21.3")
    monkeypatch.setenv("HICODE_MANAGED_RUNTIME_ROOT", str(tmp_path / "empty"))
    monkeypatch.setenv("HICODE_BIN", str(override))
    monkeypatch.setenv("HICODE_PRODUCTION", "1")

    status = HicodeExecutorAdapter().status()

    assert status.managed_reasonix_available is False
    assert status.managed_reasonix_compatible is False
    assert "disabled in production" in (status.error or "")


def test_locator_does_not_scan_path_or_nvm(tmp_path: Path, monkeypatch):
    path_reasonix = _fake_reasonix(tmp_path / "path/reasonix", "1.21.3")
    nvm_reasonix = _fake_reasonix(tmp_path / "home/.nvm/versions/node/v26/bin/reasonix", "1.21.3")
    monkeypatch.setenv("HICODE_MANAGED_RUNTIME_ROOT", str(tmp_path / "empty"))
    monkeypatch.delenv("HICODE_MANAGED_BIN", raising=False)
    monkeypatch.delenv("HICODE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(path_reasonix.parent))
    monkeypatch.setenv("HOME", str(nvm_reasonix.parents[4]))

    with pytest.raises(HicodeRuntimeError, match="managed Reasonix"):
        HicodeExecutorAdapter().resolve_binary()


def test_version_mismatch_is_unhealthy(tmp_path: Path, monkeypatch):
    _fake_reasonix(tmp_path / "managed/node_modules/.bin/reasonix", "1.21.4")
    monkeypatch.setenv("HICODE_MANAGED_RUNTIME_ROOT", str(tmp_path / "managed"))
    monkeypatch.delenv("HICODE_BIN", raising=False)

    status = HicodeExecutorAdapter().status()

    assert status.managed_reasonix_available is True
    assert status.managed_reasonix_version == "1.21.4"
    assert status.managed_reasonix_compatible is False
    assert "expected Reasonix 1.21.3" in (status.error or "")


def test_generated_config_is_veya_owned_and_secret_free(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HICODE_RUNTIME_DATA_ROOT", str(tmp_path / "veya-runtime"))
    monkeypatch.setenv("HICODE_REASONIX_API_KEY_ENV", "REASONIX_RUNTIME_KEY")
    monkeypatch.delenv("HICODE_REASONIX_BASE_URL", raising=False)

    adapter = HicodeExecutorAdapter()
    config_path = adapter.ensure_config()
    content = config_path.read_text(encoding="utf-8")

    assert config_path == tmp_path / "veya-runtime/reasonix-home/.reasonix/config.toml"
    assert "REASONIX_RUNTIME_KEY" in content
    assert "~/.reasonix" not in str(config_path)
    assert "api_key =" not in content
    assert adapter.execution_environment()["HOME"] == str(tmp_path / "veya-runtime/reasonix-home")
    assert adapter.execution_environment()["REASONIX_STATE_HOME"] == str(
        tmp_path / "veya-runtime/state"
    )


def test_proxy_mode_uses_the_veya_local_proxy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HICODE_RUNTIME_DATA_ROOT", str(tmp_path / "veya-runtime"))
    monkeypatch.setenv("HICODE_PROXY", "1")

    config = HicodeExecutorAdapter().ensure_config().read_text(encoding="utf-8")

    assert 'base_url = "http://127.0.0.1:10103/v1"' in config


def test_environment_does_not_change_parent_process_home(tmp_path: Path, monkeypatch):
    original_home = os.environ.get("HOME")
    monkeypatch.setenv("HICODE_RUNTIME_DATA_ROOT", str(tmp_path / "runtime"))

    HicodeExecutorAdapter().execution_environment()

    assert os.environ.get("HOME") == original_home
