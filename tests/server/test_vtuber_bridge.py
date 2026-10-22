"""Phase 0 Open-LLM-VTuber (OLV) bridge tests.

Covers the four Phase 0 deliverables from docs/open-llm-vtuber-integration.md
§7.2/§7.3: the default-off flag, the read-only module, the optional route, and
the OLV config template. No OLV process, no gateway, no network, no token.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server import feature_flags, vtuber_bridge
from server.tool_registry import master_tools

FLAG = "VEYA_OLV"
REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "config" / "olv.conf.yaml"


@pytest.fixture(autouse=True)
def _isolate_flags(monkeypatch: pytest.MonkeyPatch):
    for name in (
        vtuber_bridge.FLAG_NAME,
        vtuber_bridge.HOST_ENV,
        vtuber_bridge.PORT_ENV,
        vtuber_bridge.MODEL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# -- flag ----------------------------------------------------------------------


def test_flag_registered_with_default_off() -> None:
    spec = next(item for item in feature_flags.FLAGS if item.name == FLAG)
    assert spec.default is False
    assert spec.owner and spec.removal_date


def test_flag_defaults_off_and_falsy_spellings_stay_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    assert feature_flags.enabled(FLAG) is False
    for raw in ("0", "false", "no", "off", "FALSE", ""):
        monkeypatch.setenv(FLAG, raw)
        assert feature_flags.enabled(FLAG) is False, raw


def test_flag_truthy_spellings_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw in ("1", "true", "yes", "on"):
        monkeypatch.setenv(FLAG, raw)
        assert feature_flags.enabled(FLAG) is True, raw


# -- read-only module ----------------------------------------------------------


def test_status_while_disabled_is_exactly_enabled_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    assert vtuber_bridge.status() == {"enabled": False}


def test_status_while_enabled_is_secret_free_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(vtuber_bridge.PORT_ENV, "8899")
    monkeypatch.setenv(vtuber_bridge.HOST_ENV, "veya.internal")
    monkeypatch.setenv(vtuber_bridge.MODEL_ENV, "veya1.2-fast")

    status = vtuber_bridge.status()

    assert status["enabled"] is True
    assert status["flag"] == FLAG
    assert status["token_required"] is True
    assert status["model"] == "veya1.2-fast"
    assert status["gateway_url"] == "http://veya.internal:8899/v1"
    assert status["chat_completions_url"] == "http://veya.internal:8899/v1/chat/completions"
    assert status["models_url"] == "http://veya.internal:8899/v1/models"
    assert status["health_url"] == "http://veya.internal:8899/health"
    # Emotion keys are the Live2D expression contract, not secrets.
    assert "joy" in status["emotion_keys"]


def test_gateway_port_defaults_to_8791_and_rejects_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(vtuber_bridge.PORT_ENV, raising=False)
    assert vtuber_bridge.gateway_port() == 8791
    for raw in ("", "abc", "0", "70000", "-1"):
        monkeypatch.setenv(vtuber_bridge.PORT_ENV, raw)
        assert vtuber_bridge.gateway_port() == 8791, raw


def test_config_template_while_disabled_is_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    assert vtuber_bridge.config_template() == {"enabled": False}


def test_config_template_points_at_the_veya_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(vtuber_bridge.PORT_ENV, "8791")
    monkeypatch.setenv(vtuber_bridge.MODEL_ENV, "veya1.2")

    template = vtuber_bridge.config_template()

    assert template["llm"]["provider"] == "openai_compatible_llm"
    assert template["llm"]["base_url"] == "http://127.0.0.1:8791/v1"
    assert template["llm"]["model"] == "veya1.2"
    # OLV's own MCP client and memory stay off: one tool authority only.
    assert template["agent"]["use_mcpp"] is False
    assert template["agent"]["conversation_agent_choice"] == "basic_memory_agent"
    # CPU / key-free ASR + TTS keeps the bypass dependency-free.
    assert template["asr"]["asr_model"] == "sherpa_onnx_asr"
    assert template["tts"]["tts_model"] == "edge_tts"
    # Expressions depend on the injected prompt.
    assert template["live2d_expression_prompt"] is True
    assert "joy" in template["emotion_keys"]
    assert "joy" in template["persona_prompt"]
    assert "veya_llm_gateway_tokens issue" in template["token_issue_command"]


def test_no_module_output_carries_a_token_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FLAG, "1")
    blob = json.dumps([vtuber_bridge.status(), vtuber_bridge.config_template()], ensure_ascii=False)
    assert "vygw_" not in blob
    assert vtuber_bridge.config_template()["llm"]["llm_api_key"] == "<issued token>"


def test_module_does_not_spawn_or_register_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    """No subprocess/popen attribute and no tool registration while enabled."""
    monkeypatch.setenv(FLAG, "1")
    before = json.dumps(master_tools.get_all_schemas(), ensure_ascii=False, sort_keys=True)

    assert vtuber_bridge.status()["enabled"] is True

    after = json.dumps(master_tools.get_all_schemas(), ensure_ascii=False, sort_keys=True)
    assert after == before
    for banned in ("subprocess", "Popen", "os.system", "spawn", "register"):
        assert not hasattr(vtuber_bridge, banned)


# -- optional route ------------------------------------------------------------
#
# The root app wears the SEC H1 default-deny gate, so a caller needs a token to
# reach any non-public path. Use the shared owner fixtures rather than widening
# the gate or whitelisting this route.


def _client() -> TestClient:
    from server.app import app

    return TestClient(app, base_url="http://localhost")


def test_status_route_404_while_disabled(owner_headers: dict[str, str]) -> None:
    with _client() as client:
        res = client.get("/api/v1/vtuber/status", headers=owner_headers)
    assert res.status_code == 404


def test_status_route_200_and_secret_free_while_enabled(
    monkeypatch: pytest.MonkeyPatch, owner_headers: dict[str, str]
) -> None:
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(vtuber_bridge.PORT_ENV, "8791")
    with _client() as client:
        res = client.get("/api/v1/vtuber/status", headers=owner_headers)
    assert res.status_code == 200
    payload: dict[str, Any] = res.json()
    assert payload["enabled"] is True
    assert payload["gateway_url"] == "http://127.0.0.1:8791/v1"
    assert "vygw_" not in json.dumps(payload, ensure_ascii=False)


def test_status_route_requires_a_token_and_404s_anonymously() -> None:
    """SEC H1: the route is not on the public whitelist."""
    from server import auth as auth_mod

    assert auth_mod.is_public_path("/api/v1/vtuber/status") is False
    with _client() as client:
        assert client.get("/api/v1/vtuber/status").status_code == 401


def test_status_route_has_no_write_endpoints() -> None:
    """Only a GET status route exists under /api/v1/vtuber — no write surface."""
    from server.app import app

    paths: list[tuple[str, set[str]]] = []
    for entry in app.routes:
        # app.routes holds _IncludedRouter wrappers; the real APIRoutes live on
        # their original_router.
        candidates = [entry, *getattr(getattr(entry, "original_router", None), "routes", [])]
        for route in candidates:
            path = getattr(route, "path", "")
            if path.startswith("/api/v1/vtuber"):
                paths.append((path, set(getattr(route, "methods", set()) or set())))

    assert paths == [("/api/v1/vtuber/status", {"GET"})], paths


# -- config template file ------------------------------------------------------


def test_olv_config_template_file_targets_the_gateway() -> None:
    """The shipped overlay must agree with the module's read-only projection."""
    text = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "http://127.0.0.1:8791/v1" in text
    assert "model: 'veya1.2'" in text
    assert "use_mcpp: False" in text
    assert "asr_model: 'sherpa_onnx_asr'" in text
    assert "tts_model: 'edge_tts'" in text
    assert "live2d_expression_prompt: 'live2d_expression_prompt'" in text
    assert "veya_llm_gateway_tokens issue" in text
    # No credential and no Live2D asset path in the overlay.
    assert "vygw_" not in text
    assert "live2d-models/" not in text
    assert ".model3.json" not in text


def test_olv_config_template_file_matches_upstream_schema() -> None:
    """Nesting must match OLV's AgentConfig: llm_configs sits beside agent_settings."""
    import yaml

    doc = yaml.safe_load(TEMPLATE_PATH.read_text(encoding="utf-8"))
    agent = doc["character_config"]["agent_config"]

    assert set(agent) == {"conversation_agent_choice", "agent_settings", "llm_configs"}
    assert agent["conversation_agent_choice"] == "basic_memory_agent"
    basic = agent["agent_settings"]["basic_memory_agent"]
    assert basic["llm_provider"] == "openai_compatible_llm"
    assert basic["use_mcpp"] is False
    llm = agent["llm_configs"]["openai_compatible_llm"]
    # Every required field of OLV's OpenAICompatibleConfig, no extra keys.
    assert llm["base_url"] == "http://127.0.0.1:8791/v1"
    assert llm["model"] == "veya1.2"
    assert set(llm) == {
        "base_url",
        "llm_api_key",
        "model",
        "organization_id",
        "project_id",
        "temperature",
        "interrupt_method",
    }
    assert doc["character_config"]["asr_config"]["asr_model"] == "sherpa_onnx_asr"
    assert doc["character_config"]["tts_config"]["tts_model"] == "edge_tts"
    assert doc["system_config"]["tool_prompts"]["live2d_expression_prompt"]
