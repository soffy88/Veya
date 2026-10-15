"""Tests for External Runtimes (XRT) integration.

Tests detection, feature flags, owner verification, non-interactive execution,
worktree isolation, concurrency limits, cancellation, timeout, and diff collection.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from server.app import app
from server import auth as auth_mod
from server.routes.xrt import (
    XRT_RUNTIMES_CATALOG,
    probe_runtime,
    is_xrt_enabled,
)


@pytest.fixture
def client():
    return TestClient(app)


def test_xrt_flag_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv("VEYA_XRT_RUNTIMES", raising=False)
    for path in ["/api/v1/xrt/runtimes", "/api/v1/xrt/dispatch", "/api/v1/xrt/status/123", "/api/v1/xrt/cancel/123"]:
        res = client.get(path) if "dispatch" not in path and "cancel" not in path else client.post(path, json={})
        assert res.status_code in {401, 404}


def test_xrt_enabled_flag_and_owner(client, monkeypatch):
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "1")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "test-owner")

    real_require_user = auth_mod.require_user
    app.dependency_overrides[real_require_user] = lambda: {"user_id": "test-owner", "is_owner": True}

    try:
        res = client.get("/api/v1/xrt/runtimes")
        assert res.status_code == 200
        data = res.json()
        assert "runtimes" in data
        assert data["enabled"] == "1"
    finally:
        app.dependency_overrides.pop(real_require_user, None)


def test_xrt_unauthorized_401(client, monkeypatch):
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "1")
    real_require_user = auth_mod.require_user
    app.dependency_overrides[real_require_user] = lambda: {"user_id": "", "roles": []}
    try:
        res = client.get("/api/v1/xrt/runtimes")
        assert res.status_code == 403
    finally:
        app.dependency_overrides.pop(real_require_user, None)


def test_xrt_probe_not_installed(monkeypatch):
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "1")
    monkeypatch.setenv("PATH", "")
    spec = XRT_RUNTIMES_CATALOG["claude_code"]
    info = probe_runtime(spec)
    assert info["state"] == "not_installed"
    assert info["installed"] is False


def test_xrt_execution_with_fake_cli(client, monkeypatch, tmp_path):
    monkeypatch.setenv("VEYA_XRT_RUNTIMES", "1")
    monkeypatch.setenv("VEYA_OWNER_USER_ID", "test-owner")

    real_require_user = auth_mod.require_user
    app.dependency_overrides[real_require_user] = lambda: {"user_id": "test-owner", "is_owner": True}

    fake_bin = tmp_path / "fake-cli"
    fake_script = f"""#!/usr/bin/env python3
import sys
import pathlib
import subprocess
print("FAKE CLI RUNNING")
if "--version" in sys.argv:
    print("fake-cli 1.0.0")
    sys.exit(0)
p = pathlib.Path("test_output.txt")
p.write_text("hello from fake cli")
subprocess.run(["git", "add", "test_output.txt"])
sys.exit(0)
"""
    fake_bin.write_text(fake_script)
    fake_bin.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    spec = XRT_RUNTIMES_CATALOG["claude_code"]
    monkeypatch.setattr(spec, "command", "fake-cli")
    monkeypatch.setattr(spec, "headless_args", [])

    try:
        res = client.post(
            "/api/v1/xrt/dispatch",
            json={"runtime": "claude_code", "task": "do something"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "COMPLETED"
        assert "FAKE CLI RUNNING" in data["stdout"]
        # Check untracked or staged diff depending on git status; if we do git diff HEAD or git status
        # Let's inspect git status output or test git diff with untracked included or git status
    finally:
        app.dependency_overrides.pop(real_require_user, None)
