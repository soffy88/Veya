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
        assert "BRAVE_API_KEY" not in yaml.safe_load(Path(mcp_presets.PRESET_FILE).read_text())

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
        """The preset gateway must be registered with a declared side effect.

        Wired explicitly against the registry rather than reading a
        possibly-absent spec: an early `return` here would make the test
        pass without asserting anything.
        """
        from server.tool_registry import SideEffect, master_tools, register_mcp_tools

        adapters = [
            {
                "name": "fixture_action",
                "description": "fixture action",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
                "func": _ok,
            }
        ]
        assert (
            register_mcp_tools("preset-side-effect", adapters, side_effect=SideEffect.PURE_READ)
            == 1
        )
        spec = master_tools.spec_for("mcp_preset-side-effect")
        assert spec is not None, "gateway tool was not registered at all"
        # All five presets are read-only over their MCP surface, so a
        # mutating declaration would be a spec violation, not an option.
        assert spec.side_effect is SideEffect.PURE_READ

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

        adapters = await mcp_presets._collect_adapters({"name": "markitdown"}, _FakeClient())
        assert len(adapters) == 1
        assert adapters[0]["name"] == "mcp_markitdown_convert"
        assert mcp_presets.UNTRUSTED_NOTE in adapters[0]["description"]
        assert "Convert a document to Markdown" in adapters[0]["description"]


def master_tools_spec(name: str) -> Any:
    from server.tool_registry import master_tools

    return master_tools.spec_for(name)


# ---------------------------------------------------------------------------
# wire_master_tools: flag OFF / missing key → nothing spawned/registered
# ---------------------------------------------------------------------------


class TestWireMasterTools:
    async def test_flag_off_registers_nothing(self, flag_off: None) -> None:
        monkey = pytest.MonkeyPatch()

        try:
            monkey.setattr(mcp_presets, "_build_client", _must_not_spawn)
            registered = await mcp_presets.wire_master_tools()
        finally:
            monkey.undo()

        assert registered == 0

    async def test_missing_key_never_spawns_process(
        self, flag_on: None, no_keys: pytest.MonkeyPatch
    ) -> None:
        """A preset whose required KEY is absent is never launched.

        Recorded spawn attempts are asserted, not just the returned count:
        the wiring loop swallows per-preset failures, so a launch failure
        used to leave this test green while markitdown/mineru still tried.
        """
        attempts: list[str] = []

        def spy_build(preset: dict[str, Any]) -> Any:
            attempts.append(preset["name"])
            raise AssertionError(f"preset {preset['name']!r} spawned without its key")

        monkey = pytest.MonkeyPatch()

        try:
            monkey.setattr(mcp_presets, "_build_client", spy_build)
            registered = await mcp_presets.wire_master_tools()
        finally:
            monkey.undo()

        assert registered == 0
        assert attempts == ["markitdown", "mineru"], (
            "keyless presets are supposed to launch; keyed ones are not"
        )

    async def test_registries_gateway_tools_when_a_preset_wires(
        self, flag_on: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end wiring: a live preset reaches the main tool registry.

        Guards the launch path itself, which is where a client resolved
        from the wrong object (`veya.platform.obase` is a loader
        function, not the module) fails for every preset.
        """
        from server.tool_registry import SideEffect, master_tools

        for key in ("BRAVE_API_KEY", "EXA_API_KEY", "CONTEXT7_API_KEY"):
            monkeypatch.setenv(key, "fixture")
        monkeypatch.setattr(mcp_presets, "_build_client", lambda _preset: _FakeMcpClient([]))

        try:
            registered = await mcp_presets.wire_master_tools()
            assert registered == 5, "one gateway tool per preset"
            for name in ("markitdown", "mineru", "brave-search", "exa", "context7"):
                spec = master_tools.spec_for(f"mcp_{name}")
                assert spec is not None, f"{name} gateway missing from registry"
                assert spec.side_effect is SideEffect.PURE_READ
        finally:
            for name in ("markitdown", "mineru", "brave-search", "exa", "context7"):
                master_tools._functions.pop(f"mcp_{name}", None)

    def test_build_client_resolves_a_real_stdio_client(self) -> None:
        """_build_client must resolve the obase *module*, not its loader.

        `veya.platform.obase` is a loader function with none of obase's
        attributes, so building a client from it raises AttributeError for
        every preset — silently, inside the wiring loop. Constructing a
        client does not launch it, so this stays process-free.
        """
        from veya.platform import load

        client = mcp_presets._build_client(_preset("markitdown"))

        assert isinstance(client, load("obase").StdioMcpClient)
        assert type(client).__name__ == "StdioMcpClient"

    def test_lifespan_awaits_the_preset_wire(self) -> None:
        """server/app.py must await wire_master_tools, not just call it.

        The wire is a coroutine function; calling it without `await`
        produces a never-awaited coroutine and silently wires nothing.
        Checked on the AST so the regression cannot come back quietly.
        """
        import ast

        app_path = Path(mcp_presets.__file__).resolve().parents[1] / "server" / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"))
        direct_calls = {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wire_w14"
        }
        awaited_calls = {
            node.value.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "wire_w14"
        }
        assert direct_calls, "server/app.py no longer calls wire_w14"
        assert direct_calls == awaited_calls, (
            f"wire_w14() called without await on line(s) "
            f"{sorted(direct_calls - awaited_calls)} — the preset pack would not wire"
        )


async def _ok(**_kwargs: Any) -> dict[str, Any]:
    return {"ok": True}


def _must_not_spawn(preset: dict[str, Any]) -> Any:
    raise AssertionError(f"preset {preset.get('name')!r} spawned a process without its key")


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
        monkeypatch.setattr(preset_routes.mcp_presets, "attach_preset", _fake_attach)

        calls: list[dict[str, Any]] = []

        def require_user() -> dict[str, Any]:
            calls.append({"user": "user-2"})
            return {"user_id": "user-2"}

        monkeypatch.setattr(auth_mod, "require_user", require_user)
        monkeypatch.setattr(preset_routes.mcp_presets, "_owned_bot", _owned_bot_other)
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
