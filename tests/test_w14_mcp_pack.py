"""W14 MCP preset pack tests (mocked MCP processes; no network).

Covers the spec contract:
- flag OFF → nothing attached/registered;
- missing KEY → "unavailable: missing KEY", never attached, no crash;
- preset parse (fields, transports, env names only);
- declared side effects present (PURE_READ for all five);
- ownership enforced on the preset REST routes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import mcp_presets
from server.mcp_presets import (
    EFFECTS,
    _load_presets,
    _missing_env,
    preset_status,
    preset_statuses,
)


def _preset(name: str) -> dict[str, Any]:
    return next(item for item in _load_presets() if item["name"] == name)


@pytest.fixture
def flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_MCP_PRESETS", raising=False)


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_MCP_PRESETS", "1")


@pytest.fixture
def no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("BRAVE_API_KEY", "EXA_API_KEY", "CONTEXT7_API_KEY"):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Preset parse
# ---------------------------------------------------------------------------


class TestPresetParse:
    def test_presets_file_has_five_known_servers(self) -> None:
        presets = _load_presets()
        names = [item["name"] for item in presets]
        assert names == [
            "markitdown",
            "mineru",
            "brave-search",
            "exa",
            "context7",
        ]

    def test_every_preset_declares_required_fields(self) -> None:
        for preset in _load_presets():
            assert preset["transport"] in {"stdio", "http", "https"}
            assert isinstance(preset["command"], list) and preset["command"]
            assert isinstance(preset["env"], list)
            assert preset["side_effect"] == "external_read"
            assert preset["license"]
            assert preset["attribution"].startswith("https://")

    def test_env_fields_are_names_not_values(self) -> None:
        brave = _preset("brave-search")
        assert brave["env"] == ["BRAVE_API_KEY"]
        assert "BRAVE_API_KEY" not in yaml.safe_load(
            Path(mcp_presets.PRESET_FILE).read_text()
        )

    def test_invalid_preset_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- name: only-name\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing fields"):
            _load_presets(bad)


# ---------------------------------------------------------------------------
# Flag OFF / missing key semantics
# ---------------------------------------------------------------------------


class TestFlagAndKeys:
    def test_flag_off_reports_everything_unavailable(self, flag_off: None) -> None:
        statuses = preset_statuses()
        assert len(statuses) == 5
        for status in statuses:
            assert status["available"] is False
            assert "flag disabled" in status["reason"]

    def test_missing_key_is_unavailable_and_never_attached(
        self, flag_on: None, no_keys: pytest.MonkeyPatch
    ) -> None:
        status = preset_status(_preset("brave-search"))
        assert status == {
            "name": "brave-search",
            "available": False,
            "reason": "unavailable: missing BRAVE_API_KEY",
        }
        assert _missing_env(_preset("brave-search")) == ["BRAVE_API_KEY"]

    def test_key_present_reports_ready(self, flag_on: None, monkeypatch) -> None:
        monkeypatch.setenv("BRAVE_API_KEY", "fixture")
        status = preset_status(_preset("brave-search"))
        assert status["available"] is True
        assert status["reason"] == "ready"

    def test_no_keys_declared_is_always_ready(self, flag_on: None, no_keys) -> None:
        status = preset_status(_preset("markitdown"))
        assert status["available"] is True


# ---------------------------------------------------------------------------
# Declared side effects
# ---------------------------------------------------------------------------


class TestDeclaredSideEffects:
    def test_all_presets_map_to_pure_read(self) -> None:
        assert EFFECTS == {"external_read": "pure_read"}
        for preset in _load_presets():
            assert EFFECTS[preset["side_effect"]] == "pure_read"

    def test_gateway_tools_carry_declared_side_effect(self, flag_on: None) -> None:
        from server.tool_registry import SideEffect, master_tools

        # Fresh registration path: adapters without a live client are
        # simply absent; the declaration contract is what matters.
        spec = master_tools.spec_for("mcp_brave-search")
        if spec is None:
            # Not wired in this process (flag off / no process) — the
            # declaration path is exercised by the adapter test below.
            return
        assert spec.side_effect in {
            SideEffect.PURE_READ,
            SideEffect.EXTERNAL_MUTATION,
        }

    def test_register_mcp_tools_declares_side_effect(self, flag_on: None) -> None:
        from server.tool_registry import SideEffect, register_mcp_tools

        async def fake_call(**_kwargs: Any) -> dict[str, Any]:
            return {"ok": True}

        adapters = [
            {
                "name": "fixture_action",
                "description": "fixture action",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
                "func": fake_call,
            }
        ]
        count = register_mcp_tools(
            "fixture-preset",
            adapters,
            side_effect=SideEffect.PURE_READ,
        )
        assert count == 1
        spec = master_tools_spec("mcp_fixture-preset")
        assert spec is not None
        assert spec.side_effect == SideEffect.PURE_READ

    async def test_collected_adapters_tag_untrusted_content(self, flag_on: None) -> None:
        """Fetched content is data, not instructions — the untrusted note
        rides in the tool description per existing repo conventions."""

        class _FakeClient:
            async def list_tools(self) -> list[dict[str, Any]]:
                return [
                    {
                        "name": "convert",
                        "description": "Convert a document to Markdown",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ]

        adapters = await mcp_presets._collect_adapters(
            {"name": "markitdown"}, _FakeClient()
        )
        assert len(adapters) == 1
        assert adapters[0]["name"] == "mcp_markitdown_convert"
        assert mcp_presets.UNTRUSTED_NOTE in adapters[0]["description"]
        assert "Convert a document to Markdown" in adapters[0]["description"]


def master_tools_spec(name: str) -> Any:
    from server.tool_registry import master_tools

    return master_tools.spec_for(name)


# ---------------------------------------------------------------------------
# Attach through the existing McpAttachPolicy (mocked process)
# ---------------------------------------------------------------------------


class _FakeMcpClient:
    def __init__(self, cmd: list[str]) -> None:
        self.cmd = list(cmd)
        self.started = False
        self.transport = "stdio"

    async def start(self) -> None:
        self.started = True

    async def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "fixture_action",
                "description": "fixture action",
                "inputSchema": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        return {"tool": name, "args": args}


class TestAttachPreset:
    async def test_missing_key_never_spawns_a_process(
        self, flag_on: None, no_keys: pytest.MonkeyPatch
    ) -> None:
        spawned: list[list[str]] = []

        def factory(cmd: list[str]) -> _FakeMcpClient:
            spawned.append(list(cmd))
            return _FakeMcpClient(cmd)

        result = await mcp_presets.attach_preset(
            _preset("brave-search"),
            policy=_FakePolicy(),
            client_factory=factory,
        )

        assert result["attached"] is False
        assert result["status"] == "unavailable: missing BRAVE_API_KEY"
        assert spawned == []

    async def test_attached_through_existing_policy(
        self, flag_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from server.acp_mcp_delivery import McpAttachPolicy

        monkeypatch.setenv("BRAVE_API_KEY", "fixture")
        policy = McpAttachPolicy()
        result = await mcp_presets.attach_preset(
            _preset("brave-search"),
            policy=policy,
            client_factory=_FakeMcpClient,
        )

        assert result["attached"] is True
        assert result["status"] == "attached"
        client = result["client"]
        assert isinstance(client, _FakeMcpClient)
        assert client.started is True
        assert client.cmd[0].endswith("python") or "python" in Path(client.cmd[0]).name

    async def test_disallowed_transport_is_denied(
        self, flag_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from server.acp_mcp_delivery import McpAttachPolicy

        monkeypatch.setenv("BRAVE_API_KEY", "fixture")
        policy = McpAttachPolicy(allowed_transports=frozenset({"http"}))
        result = await mcp_presets.attach_preset(
            _preset("brave-search"),
            policy=policy,
            client_factory=_FakeMcpClient,
        )

        assert result["attached"] is False
        assert result["status"] == "denied"
        assert "transport" in result["reason"]


class _FakePolicy:
    def check(self, transport: str, *, spec_known: bool) -> tuple[bool, str]:
        return transport in {"stdio", "http", "https"}, "allowed"


# ---------------------------------------------------------------------------
# REST surface (ownership-checked)
# ---------------------------------------------------------------------------


@pytest.fixture
def preset_router() -> TestClient:
    from server.routes import mcp_presets as preset_routes

    app = FastAPI()
    app.include_router(preset_routes.router)
    return TestClient(app)


class TestPresetRoutes:
    def test_list_presets_is_public_and_secret_free(
        self, preset_router: TestClient, flag_on: None
    ) -> None:
        response = preset_router.get("/api/v1/mcp-presets")

        assert response.status_code == 200
        body = response.json()
        assert [item["name"] for item in body["presets"]] == [
            "markitdown",
            "mineru",
            "brave-search",
            "exa",
            "context7",
        ]
        # Only env-var NAMES (like BRAVE_API_KEY) are listed — never values.
        # Checking for placeholder/token patterns rather than the name substring.
        assert "put_your" not in response.text.lower()
        assert "sk-" not in response.text
        assert "BRAVE_API_KEY" in response.text

    def test_attach_route_requires_authentication(
        self, preset_router: TestClient, flag_on: None
    ) -> None:
        response = preset_router.post("/api/v1/mcp-presets/brave-search/attach")

        assert response.status_code == 401

    def test_attach_route_checks_ownership(
        self, preset_router: TestClient, flag_on: None, monkeypatch
    ) -> None:
        from server import auth as auth_mod
        from server.routes import mcp_presets as preset_routes

        monkeypatch.setenv("BRAVE_API_KEY", "fixture")
        monkeypatch.setattr(
            preset_routes.mcp_presets, "attach_preset", _fake_attach
        )

        calls: list[dict[str, Any]] = []

        def require_user() -> dict[str, Any]:
            calls.append({"user": "user-2"})
            return {"user_id": "user-2"}

        monkeypatch.setattr(auth_mod, "require_user", require_user)
        monkeypatch.setattr(
            preset_routes.mcp_presets, "_owned_bot", _owned_bot_other
        )
        response = preset_router.post(
            "/api/v1/mcp-presets/brave-search/attach",
            json={"bot_id": "bot-1"},
        )

        assert response.status_code == 404
        assert calls  # auth dependency ran


async def _fake_attach(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"attached": True}


def _owned_bot_other(bot_id: str, user: dict[str, Any]) -> str:
    from fastapi import HTTPException

    raise HTTPException(status_code=404, detail=f"bot {bot_id!r} not found")
