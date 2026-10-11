"""NANO-W8: Hands + Provider Models tests.

Tests the hands oprims, oskill operator loop, and provider registry.
All model calls and input devices are mocked.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Set test environment
os.environ["VEYA_HANDS"] = "1"
os.environ["VEYA_PROVIDER_REGISTRY"] = "1"
os.environ["VEYA_BOTS_ROOT"] = "/tmp/test_bots"
os.environ["VEYA_BOT_REGISTRY_PATH"] = "/tmp/test_bots/bot_registry.json"
os.environ["VEYA_RUNNERS_PATH"] = "/tmp/test_runners.json"


# ── Hands Oprim Tests ───────────────────────────────────────────────────


class TestHandsOprims:
    """Test hands atomic operations (mocked)."""

    @pytest.fixture(autouse=True)
    def _mock_subprocess(self):
        """Mock all subprocess calls."""
        with patch("veya.oprim.hands._run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            yield mock_run

    def test_hands_capabilities(self):
        """Test hands_capabilities returns expected structure."""
        from veya.oprim.hands import hands_capabilities

        caps = hands_capabilities()
        assert "enabled" in caps
        assert "platform" in caps
        assert "tools" in caps
        assert "operations" in caps

    def test_hands_screenshot_mss(self, _mock_subprocess):
        """Test hands_screenshot with mss available."""
        with patch("veya.oprim.hands._HAS_MSS", True), \
             patch("veya.oprim.hands.mss") as mock_mss:
            mock_sct = MagicMock()
            mock_sct.monitors = [{}, {"width": 1920, "height": 1080}]
            mock_mss.mss.return_value.__enter__.return_value = mock_sct

            from veya.oprim.hands import hands_screenshot
            result = hands_screenshot()

            assert "path" in result
            assert result["width"] == 1920
            assert result["height"] == 1080
            assert result["format"] == "png"

    def test_hands_screenshot_scrot_fallback(self, _mock_subprocess):
        """Test hands_screenshot falls back to scrot."""
        with patch("veya.oprim.hands._HAS_MSS", False), \
             patch("veya.oprim.hands._HAS_SCROT", True), \
             patch("veya.oprim.hands._HAS_XDOTOOL", True):

            from veya.oprim.hands import hands_screenshot
            result = hands_screenshot()

            assert "path" in result
            assert "width" in result
            assert "height" in result

    def test_hands_list_windows(self, _mock_subprocess):
        """Test hands_list_windows."""
        _mock_subprocess.side_effect = [
            MagicMock(returncode=0, stdout="12345 67890", stderr=""),
            MagicMock(returncode=0, stdout="Test Window", stderr=""),
            MagicMock(returncode=0, stdout="X=100\nY=200\nWIDTH=800\nHEIGHT=600", stderr=""),
            MagicMock(returncode=0, stdout="1234", stderr=""),
            MagicMock(returncode=0, stdout="Another Window", stderr=""),
            MagicMock(returncode=0, stdout="X=0\nY=0\nWIDTH=1024\nHEIGHT=768", stderr=""),
            MagicMock(returncode=0, stdout="5678", stderr=""),
        ]

        from veya.oprim.hands import hands_list_windows
        result = hands_list_windows()

        assert "windows" in result
        assert len(result["windows"]) == 2
        assert result["windows"][0]["id"] == "12345"
        assert result["windows"][0]["title"] == "Test Window"

    def test_hands_click(self, _mock_subprocess):
        """Test hands_click."""
        from veya.oprim.hands import hands_click
        result = hands_click(100, 200, button=1)
        assert result["success"] is True
        assert result["x"] == 100
        assert result["y"] == 200

    def test_hands_type(self, _mock_subprocess):
        """Test hands_type."""
        from veya.oprim.hands import hands_type
        result = hands_type("hello world", delay_ms=50)
        assert result["success"] is True
        assert result["chars_typed"] == 11

    def test_hands_key(self, _mock_subprocess):
        """Test hands_key."""
        from veya.oprim.hands import hands_key
        result = hands_key("ctrl+c")
        assert result["success"] is True
        assert result["key_sequence"] == "ctrl+c"

    def test_hands_scroll(self, _mock_subprocess):
        """Test hands_scroll."""
        from veya.oprim.hands import hands_scroll
        result = hands_scroll(direction="up", amount=5)
        assert result["success"] is True
        assert result["direction"] == "up"
        assert result["amount"] == 5

    def test_hands_focus_window(self, _mock_subprocess):
        """Test hands_focus_window."""
        from veya.oprim.hands import hands_focus_window
        result = hands_focus_window("12345")
        assert result["success"] is True
        assert result["window_id"] == "12345"

    def test_hands_unsupported_without_flag(self):
        """Test hands operations fail without VEYA_HANDS flag."""
        os.environ.pop("VEYA_HANDS", None)
        from veya.oprim.hands import HandsUnsupportedError, hands_screenshot

        with pytest.raises(HandsUnsupportedError):
            hands_screenshot()


# ── Hands Oskill Tests ──────────────────────────────────────────────────


class TestHandsOskill:
    """Test hands composite skills (mocked LLM)."""

    @pytest.fixture
    def mock_llm(self):
        """Mock LLM caller."""
        async def mock_caller(*, messages, tools=None, max_tokens=4096):
            return {
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "function": {
                                "name": "click",
                                "arguments": '{"x": 100, "y": 200}'
                            }
                        }]
                    }
                }]
            }
        return mock_caller

    @pytest.mark.asyncio
    async def test_hands_operator_loop_step(self, mock_llm):
        """Test one step of hands operator loop."""
        with patch("veya.oprim.hands.hands_screenshot") as mock_screenshot:
            mock_screenshot.return_value = {"path": "/tmp/test.png", "width": 1920, "height": 1080, "format": "png"}

            from veya.oskill.hands import hands_operator_loop_step
            action = await hands_operator_loop_step(
                llm=mock_llm,
                objective="Click the button",
            )

            assert action.action == "click"
            assert action.params["x"] == 100
            assert action.params["y"] == 200

    @pytest.mark.asyncio
    async def test_hands_operator_loop_step_done(self, mock_llm):
        """Test operator loop returns done when no tool call."""
        async def mock_done(*, messages, tools=None, max_tokens=4096):
            return {"choices": [{"message": {"content": "Task complete"}}]}

        with patch("veya.oprim.hands.hands_screenshot") as mock_screenshot:
            mock_screenshot.return_value = {"path": "/tmp/test.png", "width": 1920, "height": 1080, "format": "png"}

            from veya.oskill.hands import hands_operator_loop_step
            action = await hands_operator_loop_step(
                llm=mock_done,
                objective="Done",
            )

            assert action.action == "done"

    @pytest.mark.asyncio
    async def test_hands_approval_gate_approved(self):
        """Test approval gate approves and executes."""
        from veya.oskill.hands import HandsAction, hands_approval_gate

        action = HandsAction(action="click", params={"x": 100, "y": 200}, reasoning="Click button")
        mock_resolver = AsyncMock(return_value=True)
        mock_audit = AsyncMock()
        mock_ledger = AsyncMock()

        with patch("veya.oprim.hands.hands_click") as mock_click:
            mock_click.return_value = {"success": True, "x": 100, "y": 200}
            with patch("veya.oprim.hands.hands_screenshot") as mock_screenshot:
                mock_screenshot.return_value = {"path": "/tmp/post.png"}

                result = await hands_approval_gate(
                    action=action,
                    goal_run_id="goal-1",
                    bot_id="bot-1",
                    approval_resolver=mock_resolver,
                    audit_writer=mock_audit,
                    side_effect_ledger=mock_ledger,
                )

        assert result["approved"] is True
        assert result["executed"] is True

    @pytest.mark.asyncio
    async def test_hands_approval_gate_denied(self):
        """Test approval gate denies."""
        from veya.oskill.hands import HandsAction, hands_approval_gate

        action = HandsAction(action="click", params={"x": 100, "y": 200}, reasoning="Click button")
        mock_resolver = AsyncMock(return_value=False)

        result = await hands_approval_gate(
            action=action,
            goal_run_id="goal-1",
            bot_id="bot-1",
            approval_resolver=mock_resolver,
        )

        assert result["approved"] is False
        assert result["executed"] is False


# ── Provider Registry Tests ─────────────────────────────────────────────


class TestProviderRegistry:
    """Test VeyaProviderRegistry."""

    @pytest.fixture
    def registry(self, tmp_path):
        """Create a fresh registry for each test."""
        from veya.obase.provider_registry import VeyaProviderRegistry
        return VeyaProviderRegistry(storage_dir=tmp_path / "providers")

    def test_builtin_providers(self, registry):
        """Test builtin providers are loaded."""
        providers = registry.get_builtin_providers()
        assert "openai" in providers
        assert "anthropic" in providers
        assert "gemini" in providers
        assert "deepseek" in providers
        assert "openrouter" in providers
        assert "bailian" in providers
        assert "kimi" in providers
        assert "zhipu" in providers

        openai = providers["openai"]
        assert openai.protocol_family == "openai"
        assert "vision" in openai.capabilities
        assert openai.default_model == "gpt-4o"

    def test_custom_provider_registration(self, registry, tmp_path):
        """Test registering a custom OpenAI-compatible provider."""
        from veya.obase.provider_registry import VeyaProviderSpec

        spec = VeyaProviderSpec(
            name="my-custom",
            display_name="My Custom",
            protocol_family="openai",
            base_url="https://custom.example.com/v1",
            capabilities={"vision", "streaming"},
            models=["custom-model"],
            default_model="custom-model",
        )
        registry.register_custom_provider(spec)

        custom = registry.get_custom_providers()
        assert "my-custom" in custom
        assert custom["my-custom"].base_url == "https://custom.example.com/v1"

        # Persists across instances
        from veya.obase.provider_registry import VeyaProviderRegistry
        registry2 = VeyaProviderRegistry(storage_dir=tmp_path / "providers")
        assert "my-custom" in registry2.get_custom_providers()

    def test_user_api_key_storage(self, registry):
        """Test encrypted user API key storage."""
        registry.set_user_api_key("user-1", "openai", "sk-test-key-123")
        key = registry.get_user_api_key("user-1", "openai")
        assert key == "sk-test-key-123"

        # Different user can't see it
        assert registry.get_user_api_key("user-2", "openai") is None

        # List user providers
        assert "openai" in registry.list_user_providers("user-1")

        # Delete
        registry.delete_user_api_key("user-1", "openai")
        assert registry.get_user_api_key("user-1", "openai") is None

    def test_bot_model_source(self, registry):
        """Test bot model source configuration."""
        from veya.obase.provider_registry import ModelSourceConfig

        # Server default
        config = ModelSourceConfig(source="server")
        registry.set_bot_model_source("bot-1", config)
        effective = registry.get_effective_model_config("bot-1", "user-1")
        assert effective["source"] == "server"

        # User key
        registry.set_user_api_key("user-1", "anthropic", "sk-ant-key")
        config = ModelSourceConfig(source="user_key", provider="anthropic", model="claude-3-5-sonnet")
        registry.set_bot_model_source("bot-2", config)
        effective = registry.get_effective_model_config("bot-2", "user-1")
        assert effective["source"] == "user_key"
        assert effective["provider"] == "anthropic"
        assert effective["model"] == "claude-3-5-sonnet"
        assert effective["api_key"] == "sk-ant-key"

        # CLI agent
        config = ModelSourceConfig(source="cli", cli_agent="codex", cli_args=["--model", "gpt-4"])
        registry.set_bot_model_source("bot-3", config)
        effective = registry.get_effective_model_config("bot-3", "user-1")
        assert effective["source"] == "cli"
        assert effective["cli_agent"] == "codex"

    def test_cli_agent_detection(self, registry):
        """Test CLI agent detection (mocked)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="codex 1.0.0", stderr="")
            registry._detect_cli_agents()

        agents = registry.get_cli_agents()
        assert "codex" in agents
        assert agents["codex"]["available"] is True


# ── Bot Spec Model Source Tests ─────────────────────────────────────────


class TestBotSpecModelSource:
    """Test BotSpec with model_source fields."""

    def test_bot_spec_default_model_source(self):
        """Test BotSpec defaults to server model source."""
        from server.goal_run.bot_identity import BotSpec

        spec = BotSpec(bot_id="test", owner_id="user-1")
        assert spec.model_source == "server"
        assert spec.model_source_config == {}

    def test_bot_spec_custom_model_source(self):
        """Test BotSpec with custom model source."""
        from server.goal_run.bot_identity import BotSpec

        spec = BotSpec(
            bot_id="test",
            owner_id="user-1",
            model_source="user_key",
            model_source_config={"provider": "openai", "model": "gpt-4o"},
        )
        assert spec.model_source == "user_key"
        assert spec.model_source_config == {"provider": "openai", "model": "gpt-4o"}

    def test_bot_spec_from_dict_model_source(self):
        """Test BotSpec.from_dict loads model_source."""
        from server.goal_run.bot_identity import BotSpec

        data = {
            "bot_id": "test",
            "owner_id": "user-1",
            "model_source": "cli",
            "model_source_config": {"cli_agent": "codex"},
        }
        spec = BotSpec.from_dict(data)
        assert spec.model_source == "cli"
        assert spec.model_source_config == {"cli_agent": "codex"}

    def test_validate_bot_spec_model_source(self):
        """Test validate_bot_spec validates model_source."""
        from server.goal_run.bot_identity import BotSpec, validate_bot_spec

        spec = BotSpec(bot_id="test", owner_id="user-1", model_source="invalid")
        errors = validate_bot_spec(spec)
        assert any("model_source" in e for e in errors)


# ── Bot Team Routes Model Source Tests ──────────────────────────────────


class TestBotTeamModelSource:
    """Test bot team routes with model_source."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        """Create test client with isolated storage."""
        bot_root = tmp_path / "bots"
        registry_path = tmp_path / "bot_registry.json"
        monkeypatch.setenv("VEYA_BOTS_ROOT", str(bot_root))
        monkeypatch.setenv("VEYA_BOT_REGISTRY_PATH", str(registry_path))
        monkeypatch.setenv("VEYA_HANDS", "1")
        monkeypatch.setenv("VEYA_PROVIDER_REGISTRY", "1")

        from server import auth as auth_mod
        from server.routes.bot_team import router as bots_router

        app = FastAPI()
        app.include_router(bots_router)

        # Mock auth
        async def mock_get_current_user():
            return {"user_id": "test-user", "username": "test"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        app.dependency_overrides[auth_mod.require_user] = mock_get_current_user  # W11 D2
        return TestClient(app)

    def test_create_bot_with_model_source(self, client):
        """Test creating bot with model_source."""
        response = client.post("/api/v1/bots", json={
            "bot_id": "test-bot",
            "model_source": "user_key",
            "model_source_config": {"provider": "openai", "model": "gpt-4o"},
        })
        assert response.status_code == 200
        bot = response.json()["bot"]
        assert bot["model_source"] == "user_key"
        assert bot["model_source_config"]["provider"] == "openai"

    def test_patch_bot_model_source(self, client):
        """Test patching bot model_source."""
        # Create bot first
        client.post("/api/v1/bots", json={"bot_id": "test-bot"})

        # Patch model_source
        response = client.patch("/api/v1/bots/test-bot", json={
            "model_source": "cli",
            "model_source_config": {"cli_agent": "codex"},
        })
        assert response.status_code == 200
        bot = response.json()["bot"]
        assert bot["model_source"] == "cli"
        assert bot["model_source_config"]["cli_agent"] == "codex"

    def test_invalid_model_source_rejected(self, client):
        """Test invalid model_source is rejected."""
        response = client.post("/api/v1/bots", json={
            "bot_id": "test-bot",
            "model_source": "invalid",
        })
        assert response.status_code == 422


# ── Provider Routes Tests ───────────────────────────────────────────────


class TestProviderRoutes:
    """Test provider registry routes."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        """Create test client."""
        monkeypatch.setenv("VEYA_PROVIDER_REGISTRY", "1")
        monkeypatch.setenv("VEYA_HANDS", "1")

        from server import auth as auth_mod
        from server.routes.providers import router as providers_router

        app = FastAPI()
        app.include_router(providers_router)

        async def mock_get_current_user():
            return {"user_id": "test-user", "username": "test"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        return TestClient(app)

    def test_list_providers(self, client):
        """Test listing providers."""
        response = client.get("/api/v1/providers")
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        assert "openai" in data["providers"]
        assert data["providers"]["openai"]["capabilities"] == ["vision", "function-calling", "streaming"]

    def test_register_custom_provider(self, client):
        """Test registering custom provider."""
        response = client.post("/api/v1/providers/custom", json={
            "name": "my-provider",
            "display_name": "My Provider",
            "protocol_family": "openai",
            "base_url": "https://custom.example.com/v1",
            "capabilities": ["streaming"],
            "models": ["custom-model"],
            "default_model": "custom-model",
        })
        assert response.status_code == 200

        # Verify it appears in list
        response = client.get("/api/v1/providers")
        assert "my-provider" in response.json()["providers"]

    def test_user_api_key_crud(self, client):
        """Test user API key CRUD."""
        # Set key
        response = client.post("/api/v1/providers/keys", json={
            "provider": "openai",
            "api_key": "sk-test-123",
        })
        assert response.status_code == 200

        # List shows it
        response = client.get("/api/v1/providers")
        assert response.json()["providers"]["openai"]["has_user_key"] is True

        # Delete
        response = client.delete("/api/v1/providers/keys/openai")
        assert response.status_code == 200

    def test_bot_model_source_crud(self, client):
        """Test bot model source CRUD."""
        response = client.post("/api/v1/providers/bot-model-source", json={
            "bot_id": "test-bot",
            "source": "user_key",
            "provider": "anthropic",
            "model": "claude-3-5-sonnet",
        })
        assert response.status_code == 200

        response = client.get("/api/v1/providers/bot-model-source/test-bot")
        assert response.json()["source"] == "user_key"
        assert response.json()["provider"] == "anthropic"

    def test_flag_required(self, monkeypatch):
        """Test flag is required."""
        monkeypatch.delenv("VEYA_PROVIDER_REGISTRY", raising=False)

        from server import auth as auth_mod
        from server.routes.providers import router as providers_router

        app = FastAPI()
        app.include_router(providers_router)

        async def mock_get_current_user():
            return {"user_id": "test-user"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        client = TestClient(app)

        response = client.get("/api/v1/providers")
        assert response.status_code == 404


# ── Runner Hands Capability Tests ───────────────────────────────────────


class TestRunnerHandsCapability:
    """Test runner health includes hands capability."""

    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("VEYA_HANDS", "1")

        from server import auth as auth_mod
        from server.routes.runners import router as runners_router

        app = FastAPI()
        app.include_router(runners_router)

        async def mock_get_current_user():
            return {"user_id": "test-user"}

        app.dependency_overrides[auth_mod.get_current_user] = mock_get_current_user
        return TestClient(app)

    def test_local_runner_health_includes_hands(self, client):
        """Test local runner health includes hands capability."""
        response = client.get("/api/v1/runners/local/health")
        assert response.status_code == 200
        data = response.json()
        assert "hands_capable" in data
        assert "hands_tools" in data


# ── CLI Hub Tests (placeholder for VEYA_CLI_HUB) ────────────────────────


class TestCLIHub:
    """Test CLI Hub integration (mocked)."""

    def test_cli_hub_flag_gated(self, monkeypatch):
        """Test CLI Hub is gated behind flag."""
        monkeypatch.delenv("VEYA_CLI_HUB", raising=False)
        # When flag is off, oprims should raise unsupported
        # Implementation would be in oprim/cli_hub.py


# ── Runner Self-Update Tests (placeholder for VEYA_RUNNER_SELFUPDATE) ───


class TestRunnerSelfUpdate:
    """Test runner self-update (mocked)."""

    def test_runner_selfupdate_flag_gated(self, monkeypatch):
        """Test runner self-update is gated behind flag."""
        monkeypatch.delenv("VEYA_RUNNER_SELFUPDATE", raising=False)
        # Implementation would be in oprim/runner_ops.py


# Run with: python -m pytest tests/test_nano_w8_hands_models.py -v
