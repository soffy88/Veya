"""ORCA-W4 tests: routines probe + per-routine model + workspace binding; public artifact links."""

import json
import os
import secrets
import tempfile
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.bot_team.routine_clock import RoutineClock
from server.goal_run.models import RoutineSpec
from server.goal_run.routine_registry import RoutineRegistry
from server.goal_run.routine_dispatch import run_probe, dispatch_trigger
from server.goal_run.models import GoalRunState, RoutineState
from server.routes.routines import router as routines_router
from server.routes.artifacts import router as artifacts_router
from server.routes.public_artifacts import router as public_artifacts_router
from server.routes.settings import router as settings_router
from server.artifact_model import Artifact
from server.project_store import ProjectStore


@pytest.fixture
def test_app(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_ROUTINE_REGISTRY_PATH", str(tmp_path / "routines.json"))
    monkeypatch.setenv("VEYA_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("VEYA_PUBLIC_ARTIFACTS_DIR", str(tmp_path / "public_artifacts"))
    monkeypatch.setenv("VEYA_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("VEYA_AUTH_DB_PATH", str(tmp_path / "auth.db"))
    app = FastAPI()
    app.include_router(routines_router)
    app.include_router(artifacts_router)
    app.include_router(public_artifacts_router)
    app.include_router(settings_router)
    return app


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository for testing probes."""
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True)
    (repo_path / "file.txt").write_text("initial")
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_path, capture_output=True)
    return repo_path


class TestProbeFunction:
    """Tests for the run_probe function."""

    def test_probe_none(self):
        """Test that 'none' probe always returns success."""
        result = run_probe({"kind": "none"})
        assert result["success"] is True
        assert result["kind"] == "none"
        assert result["detail"] == "no probe"

    def test_probe_git_changes_no_changes(self, git_repo):
        """Test git_changes probe with no changes returns success but no changes."""
        result = run_probe({"kind": "git_changes", "path": str(git_repo)})
        assert result["success"] is True
        assert result["kind"] == "git_changes"
        assert result["data"]["has_changes"] is False
        assert result["detail"] == "no changes"

    def test_probe_git_changes_with_changes(self, git_repo):
        """Test git_changes probe with uncommitted changes."""
        (git_repo / "new_file.txt").write_text("new content")
        result = run_probe({"kind": "git_changes", "path": str(git_repo)})
        assert result["success"] is True
        assert result["kind"] == "git_changes"
        assert result["data"]["has_changes"] is True
        assert result["detail"] == "changes detected"

    def test_probe_git_changes_invalid_path(self):
        """Test git_changes probe with invalid path."""
        result = run_probe({"kind": "git_changes", "path": "/nonexistent/path"})
        assert result["success"] is False
        assert result["kind"] == "git_changes"
        assert "not a git repository" in result["detail"]

    def test_probe_command_success(self):
        """Test command probe with successful exit code."""
        result = run_probe({"kind": "command", "command": "echo hello"})
        assert result["success"] is True
        assert result["kind"] == "command"
        assert result["data"]["exit_code"] == 0

    def test_probe_command_failure(self):
        """Test command probe with failed exit code."""
        result = run_probe({"kind": "command", "command": "exit 1"})
        assert result["success"] is False
        assert result["kind"] == "command"
        assert result["data"]["exit_code"] == 1

    def test_probe_command_with_workspace(self, tmp_path):
        """Test command probe with workspace_path parameter."""
        # Create a script that writes to a file in the workspace
        script = tmp_path / "test_script.sh"
        script.write_text("#!/bin/bash\necho 'test' > output.txt")
        script.chmod(0o755)
        result = run_probe(
            {"kind": "command", "command": str(script)},
            workspace_path=str(tmp_path)
        )
        assert result["success"] is True
        assert (tmp_path / "output.txt").exists()

    def test_probe_unknown_kind(self):
        """Test probe with unknown kind defaults to success."""
        result = run_probe({"kind": "unknown"})
        assert result["success"] is True
        assert result["kind"] == "unknown"


class TestDispatchTriggerProbe:
    """Tests for dispatch_trigger probe integration."""

    @pytest.mark.asyncio
    async def test_dispatch_trigger_probe_skipped(self, tmp_path, git_repo):
        """Test dispatch_trigger skips routine when probe returns no changes."""
        registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            enabled=True,
            probe={"kind": "git_changes", "path": str(git_repo)},
        )
        registry.register(spec)

        state = GoalRunState()
        routine_state = RoutineState(routine_id="r1")
        state.routine_states["r1"] = routine_state

        mock_dispatch = AsyncMock()
        with patch("server.goal_run.routine_dispatch.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            mock_dt.fromisoformat = datetime.fromisoformat

            handoffs = await dispatch_trigger(
                topic="schedule.trigger",
                trigger_id="trigger_1",
                registry=registry,
                state=state,
                dispatch_fn=mock_dispatch,
            )

        assert len(handoffs) == 0
        assert len(routine_state.run_history) == 1
        assert routine_state.run_history[0]["status"] == "skipped"
        assert routine_state.run_history[0]["probe"]["kind"] == "git_changes"

    @pytest.mark.asyncio
    async def test_dispatch_trigger_probe_dispatched(self, tmp_path, git_repo):
        """Test dispatch_trigger dispatches when probe detects changes."""
        registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            enabled=True,
            probe={"kind": "git_changes", "path": str(git_repo)},
        )
        registry.register(spec)

        # Add changes to repo
        (git_repo / "new_file.txt").write_text("new content")

        state = GoalRunState()
        routine_state = RoutineState(routine_id="r1")
        state.routine_states["r1"] = routine_state

        mock_dispatch = AsyncMock()
        with patch("server.goal_run.routine_dispatch.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            mock_dt.fromisoformat = datetime.fromisoformat

            handoffs = await dispatch_trigger(
                topic="schedule.trigger",
                trigger_id="trigger_1",
                registry=registry,
                state=state,
                dispatch_fn=mock_dispatch,
            )

        assert len(handoffs) == 1
        assert handoffs[0]["routine_id"] == "r1"
        assert len(routine_state.run_history) == 1
        assert routine_state.run_history[0]["status"] == "dispatched"

    @pytest.mark.asyncio
    async def test_dispatch_trigger_probe_error(self, tmp_path):
        """Test dispatch_trigger records probe_error when probe fails."""
        registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            enabled=True,
            probe={"kind": "git_changes", "path": "/nonexistent/path"},
        )
        registry.register(spec)

        state = GoalRunState()
        routine_state = RoutineState(routine_id="r1")
        state.routine_states["r1"] = routine_state

        mock_dispatch = AsyncMock()
        with patch("server.goal_run.routine_dispatch.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            mock_dt.fromisoformat = datetime.fromisoformat

            handoffs = await dispatch_trigger(
                topic="schedule.trigger",
                trigger_id="trigger_1",
                registry=registry,
                state=state,
                dispatch_fn=mock_dispatch,
            )

        assert len(handoffs) == 0
        assert len(routine_state.run_history) == 1
        assert routine_state.run_history[0]["status"] == "probe_error"

    @pytest.mark.asyncio
    async def test_dispatch_trigger_command_probe(self, tmp_path):
        """Test dispatch_trigger with command probe."""
        registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            enabled=True,
            probe={"kind": "command", "command": "echo test"},
        )
        registry.register(spec)

        state = GoalRunState()
        routine_state = RoutineState(routine_id="r1")
        state.routine_states["r1"] = routine_state

        mock_dispatch = AsyncMock()
        with patch("server.goal_run.routine_dispatch.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            mock_dt.fromisoformat = datetime.fromisoformat

            handoffs = await dispatch_trigger(
                topic="schedule.trigger",
                trigger_id="trigger_1",
                registry=registry,
                state=state,
                dispatch_fn=mock_dispatch,
            )

        assert len(handoffs) == 1
        assert routine_state.run_history[0]["status"] == "dispatched"


class TestRoutineClockProbe:
    """Tests for RoutineClock probe integration."""

    @pytest.mark.asyncio
    async def test_clock_probe_skipped(self, tmp_path, git_repo):
        """Test RoutineClock skips routine when probe returns no changes."""
        registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            enabled=True,
            probe={"kind": "git_changes", "path": str(git_repo)},
        )
        registry.register(spec)

        clock = RoutineClock(registry, clock_path=str(tmp_path / "clock.json"), dispatch_fn=AsyncMock())

        with patch("server.bot_team.routine_clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            mock_dt.fromisoformat = datetime.fromisoformat

            handoffs = await clock.tick()

        assert len(handoffs) == 0

    @pytest.mark.asyncio
    async def test_clock_probe_dispatched(self, tmp_path, git_repo):
        """Test RoutineClock dispatches when probe detects changes."""
        registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            enabled=True,
            probe={"kind": "git_changes", "path": str(git_repo)},
        )
        registry.register(spec)

        # Add changes to repo
        (git_repo / "new_file.txt").write_text("new content")

        clock = RoutineClock(registry, clock_path=str(tmp_path / "clock.json"), dispatch_fn=AsyncMock())

        with patch("server.bot_team.routine_clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            mock_dt.fromisoformat = datetime.fromisoformat

            handoffs = await clock.tick()

        assert len(handoffs) == 1
        assert handoffs[0]["routine_id"] == "r1"

    @pytest.mark.asyncio
    async def test_clock_probe_error(self, tmp_path):
        """Test RoutineClock records probe_error when probe fails."""
        registry = RoutineRegistry(bot_id="b1", storage_path=str(tmp_path / "reg.json"))
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            enabled=True,
            probe={"kind": "git_changes", "path": "/nonexistent/path"},
        )
        registry.register(spec)

        clock = RoutineClock(registry, clock_path=str(tmp_path / "clock.json"), dispatch_fn=AsyncMock())

        with patch("server.bot_team.routine_clock.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
            mock_dt.fromisoformat = datetime.fromisoformat

            handoffs = await clock.tick()

        assert len(handoffs) == 0


class TestRoutinesREST:
    """Tests for routines REST API."""

    @pytest.mark.asyncio
    async def test_create_routine_with_probe_model_workspace(self, test_app):
        """Test creating routine with probe, model, and workspace_path fields."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/bots/bot_123/routines",
                json={
                    "objective": "test routine",
                    "schedule": "every 30m",
                    "probe": {"kind": "git_changes", "path": "/tmp/repo"},
                    "model": "deepseek-v4.1-flash",
                    "workspace_path": "/home/user/worktrees/my-worktree",
                    "enabled": True,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["probe"]["kind"] == "git_changes"
            assert data["model"] == "deepseek-v4.1-flash"
            assert data["workspace_path"] == "/home/user/worktrees/my-worktree"
            assert data["enabled"] is True

    @pytest.mark.asyncio
    async def test_create_routine_disabled(self, test_app):
        """Test creating routine with enabled=false."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            resp = await ac.post(
                "/api/v1/bots/bot_123/routines",
                json={
                    "objective": "test routine",
                    "schedule": "every 30m",
                    "enabled": False,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is False

    @pytest.mark.asyncio
    async def test_run_now_on_disabled_routine(self, test_app):
        """Test POST /routines/{id}/run-now works on disabled routines."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Create disabled routine
            resp = await ac.post(
                "/api/v1/bots/bot_123/routines",
                json={
                    "objective": "test routine",
                    "schedule": "every 30m",
                    "enabled": False,
                },
            )
            assert resp.status_code == 200
            rid = resp.json()["routine_id"]

            # Run now
            mock_dispatch = AsyncMock()
            with patch("server.routes.routines.get_dispatch_consumer", return_value=mock_dispatch):
                resp = await ac.post(f"/api/v1/bots/bot_123/routines/{rid}/run_now")
                assert resp.status_code == 200
                mock_dispatch.assert_called_once()
                handoff = mock_dispatch.call_args[0][0][0]
                assert handoff["routine_id"] == rid

    @pytest.mark.asyncio
    async def test_run_history_shows_status_chips(self, test_app):
        """Test history endpoint shows skipped/probe_error/dispatched statuses."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Create routine
            resp = await ac.post(
                "/api/v1/bots/bot_123/routines",
                json={
                    "objective": "test routine",
                    "schedule": "every 30m",
                    "enabled": True,
                },
            )
            assert resp.status_code == 200
            rid = resp.json()["routine_id"]

            # Check history endpoint
            resp = await ac.get(f"/api/v1/bots/bot_123/routines/{rid}/history")
            assert resp.status_code == 200
            data = resp.json()
            assert "history" in data
            # Should have run_history entries with status field


class TestArtifactsSharing:
    """Tests for public artifact sharing."""

    @pytest.mark.asyncio
    async def test_share_disabled_returns_403(self, test_app):
        """Test that sharing artifacts returns 403 when public_sharing_enabled is false (default)."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Try to share an artifact (id can be any string)
            resp = await ac.post("/api/v1/artifacts/test-artifact/share")
            assert resp.status_code == 403
            assert "disabled" in resp.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_enable_public_sharing(self, test_app):
        """Test enabling public sharing via PUT /api/v1/settings/artifacts."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            resp = await ac.put(
                "/api/v1/settings/artifacts",
                json={"public_sharing_enabled": True},
            )
            assert resp.status_code == 200
            assert resp.json()["public_sharing_enabled"] is True

    @pytest.mark.asyncio
    async def test_share_artifact_creates_token(self, test_app):
        """Test POST /artifacts/{id}/share creates unguessable token."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Enable sharing first
            await ac.put("/api/v1/settings/artifacts", json={"public_sharing_enabled": True})

            # Create an artifact first (simulate)
            artifact_id = "test-artifact-123"
            resp = await ac.post(f"/api/v1/artifacts/{artifact_id}/share")
            assert resp.status_code == 200
            data = resp.json()
            assert "token" in data
            assert "url" in data
            assert len(data["token"]) > 20  # token_urlsafe(24) gives ~32 chars
            assert data["url"].endswith(f"/a/{data['token']}")

    @pytest.mark.asyncio
    async def test_public_artifact_access(self, test_app, tmp_path):
        """Test GET /a/{token} serves artifact without login."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Enable sharing
            await ac.put("/api/v1/settings/artifacts", json={"public_sharing_enabled": True})

            # Create artifact file
            artifacts_dir = Path(os.environ["VEYA_ARTIFACTS_DIR"])
            artifact_id = "test-artifact-123"
            artifact_dir = artifacts_dir / artifact_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "file.txt").write_text("test content")

            # Share it
            resp = await ac.post(f"/api/v1/artifacts/{artifact_id}/share")
            token = resp.json()["token"]

            # Access via public endpoint
            resp = await ac.get(f"/api/v1/a/{token}")
            assert resp.status_code == 200
            assert resp.text == "test content"

    @pytest.mark.asyncio
    async def test_revoke_artifact_share(self, test_app):
        """Test DELETE /artifacts/{id}/share revokes the token."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Enable sharing
            await ac.put("/api/v1/settings/artifacts", json={"public_sharing_enabled": True})

            # Create artifact
            artifacts_dir = Path(os.environ["VEYA_ARTIFACTS_DIR"])
            artifact_id = "test-artifact-123"
            artifact_dir = artifacts_dir / artifact_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "file.txt").write_text("test content")

            # Share it
            resp = await ac.post(f"/api/v1/artifacts/{artifact_id}/share")
            token = resp.json()["token"]

            # Verify access works
            resp = await ac.get(f"/api/v1/a/{token}")
            assert resp.status_code == 200

            # Revoke
            resp = await ac.delete(f"/api/v1/artifacts/{artifact_id}/share")
            assert resp.status_code == 200

            # Verify access no longer works
            resp = await ac.get(f"/api/v1/a/{token}")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_shares(self, test_app):
        """Test GET /artifacts/shares lists all shared artifacts."""
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Enable sharing
            await ac.put("/api/v1/settings/artifacts", json={"public_sharing_enabled": True})

            # Create and share multiple artifacts
            artifacts_dir = Path(os.environ["VEYA_ARTIFACTS_DIR"])
            for i in range(3):
                artifact_id = f"test-artifact-{i}"
                artifact_dir = artifacts_dir / artifact_id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                (artifact_dir / "file.txt").write_text(f"content {i}")
                await ac.post(f"/api/v1/artifacts/{artifact_id}/share")

            # List shares
            resp = await ac.get("/api/v1/artifacts/shares")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["shares"]) >= 3

    @pytest.mark.asyncio
    async def test_agent_context_rejection(self, test_app):
        """Test that PUT /settings/artifacts rejects calls from agent/tool context (delegation_depth > 0)."""
        from server.routes.settings import _delegation_depth_ctx

        async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as ac:
            # Set delegation_depth to simulate agent context
            _delegation_depth_ctx.set(1)
            try:
                resp = await ac.put(
                    "/api/v1/settings/artifacts",
                    json={"public_sharing_enabled": True},
                )
                assert resp.status_code == 403
                assert "agent" in resp.json().get("detail", "").lower()
            finally:
                _delegation_depth_ctx.set(0)


class TestRoutineSpecValidation:
    """Tests for RoutineSpec validation with new fields."""

    def test_routine_spec_with_probe(self):
        """Test RoutineSpec accepts probe field."""
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            probe={"kind": "git_changes", "path": "/tmp/repo"},
        )
        assert spec.probe == {"kind": "git_changes", "path": "/tmp/repo"}

    def test_routine_spec_with_model(self):
        """Test RoutineSpec accepts model field."""
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            model="deepseek-v4.1-flash",
        )
        assert spec.model == "deepseek-v4.1-flash"

    def test_routine_spec_with_workspace_path(self):
        """Test RoutineSpec accepts workspace_path field."""
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            workspace_path="/home/user/worktrees/my-worktree",
        )
        assert spec.workspace_path == "/home/user/worktrees/my-worktree"

    def test_routine_spec_defaults(self):
        """Test RoutineSpec defaults for new fields."""
        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
        )
        assert spec.probe == {"kind": "none"}
        assert spec.model == ""
        assert spec.workspace_path == ""

    def test_routine_registry_validate_workspace_path(self, tmp_path):
        """Test RoutineRegistry validates workspace_path exists."""
        from server.goal_run.routine_registry import validate_routine_spec

        # Create a valid worktree
        worktree = tmp_path / "valid_worktree"
        worktree.mkdir()

        spec = RoutineSpec(
            routine_id="r1",
            trigger_topic="schedule.trigger",
            objective="test",
            goal_template="test",
            bot_id="b1",
            schedule="every 30m",
            workspace_path=str(worktree),
        )
        # Should pass validation
        validate_routine_spec(spec)

        # Test with non-existent path
        spec.workspace_path = "/nonexistent/path"
        with pytest.raises(ValueError, match="workspace_path does not exist"):
            validate_routine_spec(spec)


class TestProbeWorkspacePassthrough:
    """Test that probe workspace_path is passed through correctly."""

    def test_run_probe_uses_workspace_path(self, tmp_path):
        """Test run_probe uses workspace_path for command probes."""
        # Create a script that writes to a file in the workspace
        script = tmp_path / "test_script.sh"
        script.write_text("#!/bin/bash\necho 'test' > output.txt")
        script.chmod(0o755)

        result = run_probe(
            {"kind": "command", "command": str(script)},
            workspace_path=str(tmp_path)
        )
        assert result["success"] is True
        assert (tmp_path / "output.txt").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])