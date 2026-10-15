"""NANO-W8: CLI Hub integration tests.

Tests the CLI-Hub tool ladder, discovery, install, invoke, and output verification.
All external calls mocked.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set test environment
os.environ["VEYA_CLI_HUB"] = "1"
os.environ["VEYA_HANDS"] = "1"
os.environ["VEYA_PROVIDER_REGISTRY"] = "1"


class TestCLIHubOprims:
    """Test CLI-Hub atomic operations."""

    @pytest.fixture(autouse=True)
    def _mock_http(self):
        """Mock HTTP calls."""
        with (
            patch("httpx.AsyncClient.get") as mock_get,
            patch("httpx.AsyncClient.post") as mock_post,
        ):
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"tools": [], "skills": []},
                raise_for_status=lambda: None,
            )
            mock_post.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True},
                raise_for_status=lambda: None,
            )
            yield mock_get, mock_post

    def test_search_cli(self):
        """Test search_cli oprim."""
        from veya.oprim.cli_hub import search_cli

        http_get = MagicMock()
        http_get.return_value.json = lambda: {
            "results": [{"name": "test-tool", "description": "A test tool"}]
        }
        result = search_cli(query="test", http_get=http_get)
        assert "results" in result

    def test_read_cli_skill(self):
        """Test read_cli_skill oprim."""
        from veya.oprim.cli_hub import read_cli_skill

        http_get = MagicMock()
        http_get.return_value.text = "# Test Skill\n\nDescription..."
        result = read_cli_skill(skill_id="test-skill", http_get=http_get)
        assert "content" in result

    def test_install_cli_on_runner(self):
        """Test install_cli_on_runner oprim."""
        from veya.oprim.cli_hub import install_cli_on_runner

        run_cmd = MagicMock()
        run_cmd.return_value = MagicMock(returncode=0, stdout="installed", stderr="")
        result = install_cli_on_runner(package="test-package", version="1.0.0", run_cmd=run_cmd)
        assert result["success"] is True

    def test_invoke_cli(self):
        """Test invoke_cli oprim."""
        from veya.oprim.cli_hub import invoke_cli

        run_cmd = MagicMock()
        run_cmd.return_value = MagicMock(returncode=0, stdout='{"result": "success"}', stderr="")
        result = invoke_cli(command=["test-cli", "run"], args={"input": "data"}, run_cmd=run_cmd)
        assert result["success"] is True
        assert "output" in result

    def test_cli_hub_flag_required(self):
        """Test CLI Hub oprims require VEYA_CLI_HUB flag."""
        os.environ.pop("VEYA_CLI_HUB", None)
        from veya.oprim.cli_hub import CLIHubUnsupportedError, search_cli

        with pytest.raises(CLIHubUnsupportedError):
            search_cli(query="test", http_get=MagicMock())


class TestCLIHubToolLadder:
    """Test CLI-Hub tool ladder oskill."""

    def test_tool_ladder_ranks_api_first(self):
        """Test tool ladder prefers API/MCP over CLI-Hub over hands."""
        from veya.oskill.cli_hub import cli_hub_tool_ladder

        # Task with API available
        result = cli_hub_tool_ladder(
            task_tags=["api", "web-search"],
            available_tools={
                "api": ["web_search", "fetch_url"],
                "mcp": [],
                "cli_hub": ["search-cli"],
                "hands": ["click", "type"],
            },
        )

        # API should be ranked first
        assert result["ranked"][0]["source"] == "api"

    def test_tool_ladder_ranks_cli_hub_second(self):
        """Test CLI-Hub ranked when no API available."""
        from veya.oskill.cli_hub import cli_hub_tool_ladder

        result = cli_hub_tool_ladder(
            task_tags=["file-processing"],
            available_tools={
                "api": [],
                "mcp": [],
                "cli_hub": ["convert-pdf", "ocr"],
                "hands": ["click", "type"],
            },
        )

        assert result["ranked"][0]["source"] == "cli_hub"

    def test_tool_ladder_ranks_hands_last(self):
        """Test hands ranked last when no other options."""
        from veya.oskill.cli_hub import cli_hub_tool_ladder

        result = cli_hub_tool_ladder(
            task_tags=["legacy-app"],
            available_tools={
                "api": [],
                "mcp": [],
                "cli_hub": [],
                "hands": ["click", "type", "screenshot"],
            },
        )

        assert result["ranked"][0]["source"] == "hands"

    def test_tool_ladder_records_why_no_cli(self):
        """Test hands request records why no CLI was usable."""
        from veya.oskill.cli_hub import cli_hub_tool_ladder

        result = cli_hub_tool_ladder(
            task_tags=["legacy-app"],
            available_tools={
                "api": [],
                "mcp": [],
                "cli_hub": [],
                "hands": ["click", "type"],
            },
        )

        hands_entry = next(e for e in result["ranked"] if e["source"] == "hands")
        assert "reason" in hands_entry
        assert "cli" in hands_entry["reason"].lower() or "no" in hands_entry["reason"].lower()


class TestCLIHubOutputVerification:
    """Test output verification oskill."""

    def test_verify_pdf_magic_bytes(self):
        """Test PDF verification via magic bytes."""
        from veya.oskill.cli_hub import cli_hub_verify_output

        # PDF magic bytes: %PDF
        pdf_content = b"%PDF-1.4\n%...\n%%EOF"
        result = cli_hub_verify_output(
            file_path="/tmp/test.pdf",
            expected_type="pdf",
            content=pdf_content,
        )
        assert result["verified"] is True
        assert result["mime_type"] == "application/pdf"

    def test_verify_png_magic_bytes(self):
        """Test PNG verification via magic bytes."""
        from veya.oskill.cli_hub import cli_hub_verify_output

        # PNG magic bytes
        png_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        result = cli_hub_verify_output(
            file_path="/tmp/test.png",
            expected_type="png",
            content=png_content,
        )
        assert result["verified"] is True
        assert result["mime_type"] == "image/png"

    def test_verify_zip_ooxml(self):
        """Test ZIP/OOXML verification (docx, xlsx, pptx)."""
        from veya.oskill.cli_hub import cli_hub_verify_output

        # ZIP magic bytes
        zip_content = b"PK\x03\x04\x14\x00\x00\x00\x08\x00"
        result = cli_hub_verify_output(
            file_path="/tmp/test.docx",
            expected_type="docx",
            content=zip_content,
        )
        assert result["verified"] is True

    def test_verify_mp4(self):
        """Test MP4 verification."""
        from veya.oskill.cli_hub import cli_hub_verify_output

        # MP4 magic bytes (ftyp)
        mp4_content = b"\x00\x00\x00\x18ftypmp42"
        result = cli_hub_verify_output(
            file_path="/tmp/test.mp4",
            expected_type="mp4",
            content=mp4_content,
        )
        assert result["verified"] is True

    def test_verify_wav(self):
        """Test WAV verification."""
        from veya.oskill.cli_hub import cli_hub_verify_output

        # WAV magic bytes
        wav_content = b"RIFF\x00\x00\x00\x00WAVEfmt "
        result = cli_hub_verify_output(
            file_path="/tmp/test.wav",
            expected_type="wav",
            content=wav_content,
        )
        assert result["verified"] is True

    def test_verify_non_empty(self):
        """Test non-empty file check."""
        from veya.oskill.cli_hub import cli_hub_verify_output

        result = cli_hub_verify_output(
            file_path="/tmp/test.txt",
            expected_type="txt",
            content=b"",
        )
        assert result["verified"] is False
        assert "empty" in result["reason"].lower()

    def test_verify_size_bounds(self):
        """Test file size bounds check."""
        from veya.oskill.cli_hub import cli_hub_verify_output

        # 10MB file, max 5MB
        large_content = b"x" * (10 * 1024 * 1024)
        result = cli_hub_verify_output(
            file_path="/tmp/large.txt",
            expected_type="txt",
            content=large_content,
            max_size_mb=5,
        )
        assert result["verified"] is False
        assert "size" in result["reason"].lower()


class TestCLIHubRoutes:
    """Test CLI Hub routes."""

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("VEYA_CLI_HUB", "1")

        from server import auth as auth_mod
        from server.routes.cli_hub import router as cli_hub_router

        app = FastAPI()
        app.include_router(cli_hub_router)

        async def mock_get_current_user():
            return {"user_id": "test-user"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        return TestClient(app)

    def test_search_endpoint(self, client):
        """Test CLI-Hub search endpoint."""
        response = client.get("/api/v1/cli-hub/search", params={"q": "pdf"})
        assert response.status_code == 200

    def test_install_endpoint_requires_approval(self, client):
        """Test install requires ActionGateway approval."""
        response = client.post(
            "/api/v1/cli-hub/install",
            json={
                "package": "test-package",
                "version": "1.0.0",
            },
        )
        # Should require approval (403 or 202 with approval pending)
        assert response.status_code in (202, 403)

    def test_flag_required(self):
        """Test flag is required."""
        from server import auth as auth_mod
        from server.routes.cli_hub import router as cli_hub_router
        from veya.oprim.cli_hub import CLIHubUnsupportedError, search_cli

        # Remove flag and expect error
        with patch.dict("os.environ", {}, clear=True), pytest.raises(CLIHubUnsupportedError):
            search_cli(query="test")

        app = FastAPI()
        app.include_router(cli_hub_router)

        async def mock_get_current_user():
            return {"user_id": "test-user"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        client = TestClient(app)

        response = client.get("/api/v1/cli-hub/search", params={"q": "test"})
        assert response.status_code == 404


# Run with: python -m pytest tests/test_nano_w8_cli_hub.py -v
