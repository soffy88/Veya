"""NANO-W8: Runner self-update and service tests.

Tests runner self-update check, download, stage, restart, and service management.
All external calls and system operations mocked.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set test environment
os.environ["VEYA_RUNNER_SELFUPDATE"] = "1"
os.environ["VEYA_HANDS"] = "1"


class TestRunnerOpsOprims:
    """Test runner operations atomic operations."""

    def test_runner_self_update_check(self):
        """Test runner_self_update_check oprim."""
        from veya.oprim.runner_ops import runner_self_update_check

        with patch("veya.oprim.runner_ops.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "version": "1.2.0",
                    "signature": "abc123",
                    "url": "https://example.com/veya-runner-1.2.0.tar.gz",
                },
            )
            result = runner_self_update_check(current_version="1.1.0")
            assert result["update_available"] is True
            assert result["latest_version"] == "1.2.0"

    def test_runner_self_update_check_no_update(self):
        """Test runner_self_update_check when no update available."""
        from veya.oprim.runner_ops import runner_self_update_check

        with patch("veya.oprim.runner_ops.httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "version": "1.1.0",
                    "signature": "abc123",
                },
            )
            result = runner_self_update_check(current_version="1.1.0")
            assert result["update_available"] is False

    def test_runner_self_update_download(self):
        """Test runner_self_update_download oprim."""
        from veya.oprim.runner_ops import runner_self_update_download

        # A real 32-byte ed25519 public key, base64: the pinned key is decoded
        # before the (mocked) verifier runs, so a placeholder string that is
        # not base64 never reaches the signature check at all.
        pinned_key_b64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

        with (
            patch("veya.oprim.runner_ops.httpx.Client") as mock_client,
            patch("veya.oprim.runner_ops.ed25519") as mock_ed25519,
        ):
            mock_client.return_value.__enter__.return_value.get.return_value = MagicMock(
                status_code=200,
                content=b"fake binary content",
                headers={"content-length": "1024"},
            )
            mock_ed25519.Ed25519PublicKey.from_public_bytes.return_value.verify.return_value = None

            result = runner_self_update_download(
                url="https://example.com/veya-runner.tar.gz",
                expected_signature="abc123",
                public_key_b64=pinned_key_b64,
            )
            assert result["success"] is True
            assert "path" in result
            assert result["signature_verified"] is True

    def test_runner_self_update_stage(self):
        """Test runner_self_update_stage oprim."""
        from veya.oprim.runner_ops import runner_self_update_stage

        with (
            patch("veya.oprim.runner_ops.shutil.unpack_archive") as mock_unpack,
            patch("veya.oprim.runner_ops.Path.rename") as mock_rename,
        ):
            result = runner_self_update_stage(
                archive_path="/tmp/veya-runner.tar.gz",
                install_dir="/opt/veya",
            )
            assert result["success"] is True

    def test_runner_self_update_restart(self):
        """Test runner_self_update_restart oprim."""
        from veya.oprim.runner_ops import runner_self_update_restart

        with patch("veya.oprim.runner_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = runner_self_update_restart(
                runner_binary="/opt/veya/veya-runner",
                args=["--config", "/etc/veya/config.toml"],
            )
            assert result["success"] is True

    def test_runner_service_install(self):
        """Test runner_service_install oprim."""
        from veya.oprim.runner_ops import runner_service_install

        with patch("veya.oprim.runner_ops.Path.write_text") as mock_write:
            result = runner_service_install(
                runner_binary="/opt/veya/veya-runner",
                user="testuser",
                working_dir="/home/testuser",
            )
            assert result["success"] is True
            assert "unit_path" in result

    def test_runner_service_uninstall(self):
        """Test runner_service_uninstall oprim."""
        from veya.oprim.runner_ops import runner_service_uninstall

        with patch("veya.oprim.runner_ops.Path.unlink") as mock_unlink:
            result = runner_service_uninstall(user="testuser")
            assert result["success"] is True

    def test_runner_service_status(self):
        """Test runner_service_status oprim."""
        from veya.oprim.runner_ops import runner_service_status

        with patch("veya.oprim.runner_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="active\n", stderr="")
            result = runner_service_status(user="testuser")
            assert result["status"] == "active"

    def test_runner_safe_to_restart(self):
        """Test runner_safe_to_restart oskill."""
        from veya.oskill.runner_ops import runner_safe_to_restart

        # No active turns - safe to restart
        result = runner_safe_to_restart(
            runner_state={
                "active_turns": [],
                "pending_turns": 0,
            }
        )
        assert result["safe"] is True
        assert result["reason"] == "no active turns"

        # Active turn - not safe
        result = runner_safe_to_restart(
            runner_state={
                "active_turns": [{"bot_id": "bot-1", "task_id": "task-1"}],
                "pending_turns": 0,
            }
        )
        assert result["safe"] is False
        assert "active" in result["reason"].lower()


class TestRunnerOpsRoutes:
    """Test runner ops routes."""

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("VEYA_RUNNER_SELFUPDATE", "1")

        from server import auth as auth_mod
        from server.routes.runner_ops import router as runner_ops_router

        app = FastAPI()
        app.include_router(runner_ops_router)

        async def mock_get_current_user():
            return {"user_id": "test-user"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        return TestClient(app)

    def test_check_update_endpoint(self, client):
        """Test check update endpoint."""
        response = client.get(
            "/api/v1/runner/self-update/check", params={"current_version": "1.0.0"}
        )
        assert response.status_code == 200

    def test_request_update_endpoint_requires_approval(self, client):
        """Test update request goes through ActionGateway."""
        response = client.post(
            "/api/v1/runner/self-update/request",
            json={
                "runner_id": "runner-1",
            },
        )
        assert response.status_code in (202, 403)

    def test_service_install_endpoint(self, client):
        """Test service install endpoint."""
        response = client.post(
            "/api/v1/runner/service/install",
            json={
                "runner_binary": "/opt/veya/veya-runner",
            },
        )
        assert response.status_code == 200

    def test_service_status_endpoint(self, client):
        """Test service status endpoint."""
        response = client.get("/api/v1/runner/service/status")
        assert response.status_code == 200

    def test_flag_required(self, monkeypatch):
        """Test flag is required."""
        monkeypatch.delenv("VEYA_RUNNER_SELFUPDATE", raising=False)

        from server import auth as auth_mod
        from server.routes.runner_ops import router as runner_ops_router

        app = FastAPI()
        app.include_router(runner_ops_router)

        async def mock_get_current_user():
            return {"user_id": "test-user"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        client = TestClient(app)

        response = client.get(
            "/api/v1/runner/self-update/check", params={"current_version": "1.0.0"}
        )
        assert response.status_code == 404


class TestDevicesPageRunnerView:
    """Test devices page shows runner version, last seen, update available."""

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("VEYA_HANDS", "1")
        monkeypatch.setenv("VEYA_RUNNER_SELFUPDATE", "1")

        from server import auth as auth_mod
        from server.routes.runners import router as runners_router

        app = FastAPI()
        app.include_router(runners_router)

        async def mock_get_current_user():
            return {"user_id": "test-user"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        return TestClient(app)

    def test_list_runners_includes_hands_capable(self, client):
        """Test runner list includes hands capability."""
        response = client.get("/api/v1/runners")
        assert response.status_code == 200
        data = response.json()
        assert "runners" in data
        for runner in data["runners"]:
            assert "hands_capable" in runner

    def test_runner_health_includes_hands_tools(self, client):
        """Test runner health includes hands tools."""
        response = client.get("/api/v1/runners/local/health")
        assert response.status_code == 200
        data = response.json()
        assert "hands_capable" in data
        assert "hands_tools" in data


# Run with: python -m pytest tests/test_nano_w8_runner_ops.py -v
