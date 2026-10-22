"""Phase 0 dsh-decision-review integration tests (no real process or network).

Mirrors ``tests/server/test_world_intel_mcp.py``'s isolation technique
(monkeypatch clears the flag and env, ``lazy_tools.reset_sessions()``) and
adds the four Phase 0 assertions from section 10.7 of
``docs/dsh-decision-review-integration.md``:

1. flag off -> canonical tool surface bytes are unchanged, no client built;
2. sensitive input rejected and never dispatched;
3. timeout and parent cancellation return on time;
4. reasonCode mapping is correct.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from server import decision_review, decision_review_core, lazy_tools
from server.tool_registry import MasterToolRegistry, SideEffect, master_tools

_FLAG = decision_review.FLAG_NAME
_BASE_URL_ENV = decision_review.BASE_URL_ENV


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.delenv(_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(decision_review_core.BASE_URL_ENV, raising=False)
    lazy_tools.reset_sessions()
    yield
    lazy_tools.reset_sessions()


def _schema_bytes(schemas: list[dict[str, Any]]) -> bytes:
    return json.dumps(schemas, ensure_ascii=False, separators=(",", ":")).encode()


def _config(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": "advisory",
        "baseUrl": "https://decision.example.com",
        "apiKeyEnv": "DECISION_API_KEY",
        "model": "decision-model",
        "allowedDecisionPoints": ["candidate_selection"],
    }
    payload.update(overrides)
    return payload


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "point": "candidate_selection",
        "question": "哪个指标与用户要求的收入确认口径一致",
        "primaryChoice": "metric_a",
        "options": [
            {"id": "metric_a", "description": "系统预估收入"},
            {"id": "metric_b", "description": "财务确认收入"},
        ],
        "evidence": [
            {"id": "E1", "fact": "系统预估口径包含尚未确认的订单"},
            {"id": "E2", "fact": "用户要求使用财务确认口径"},
        ],
        "constraints": ["只能从候选集合中选择"],
        "model": "decision-model",
        "allowedDecisionPoints": ["candidate_selection"],
    }
    payload.update(overrides)
    return payload


# ── 1. flag off: canonical surface bytes are unchanged ──────────────────────


async def test_flag_off_global_surface_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off is a no-op: nothing registered and no client constructed."""
    built: list[str] = []

    def _factory(url: str) -> Any:
        built.append(url)
        raise AssertionError("constructed a client while disabled")

    before = _schema_bytes(master_tools.get_all_schemas())

    assert decision_review.status() == {
        "name": decision_review.SERVER_NAME,
        "available": False,
        "reason": decision_review.UNAVAILABLE_FLAG,
    }
    assert await decision_review.wire_master_tools(client_factory=_factory) == 0
    assert _schema_bytes(master_tools.get_all_schemas()) == before
    assert not master_tools.has(decision_review.TOOL_NAME)
    assert built == []


async def test_flag_off_injected_registry_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str] = []

    def _factory(url: str) -> Any:
        built.append(url)
        raise AssertionError("constructed a client while disabled")

    registry = MasterToolRegistry()
    registry.register("alpha", "alpha", {"type": "object", "properties": {}}, lambda: "ok")
    before = _schema_bytes(registry.get_all_schemas())

    assert await decision_review.wire_master_tools(registry=registry, client_factory=_factory) == 0
    assert _schema_bytes(registry.get_all_schemas()) == before
    assert built == []


async def test_flag_off_registers_nothing_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = MasterToolRegistry()

    assert await decision_review.wire_master_tools(registry=registry) == 0
    assert not registry.has(decision_review.TOOL_NAME)
    assert decision_review.TOOL_NAME not in lazy_tools.resident_tool_names()


# ── 2. sensitive input is rejected and never dispatched ─────────────────────


async def test_sensitive_input_is_rejected_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on, no backend: a credential-bearing payload never reaches a route.

    The registered tool body is what a model would actually call; the request
    it would have built must not exist in the answer, and no transport client
    is constructed for a rejected payload.
    """
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.delenv(_BASE_URL_ENV, raising=False)
    registry = MasterToolRegistry()
    built: list[str] = []

    def _factory(url: str) -> Any:
        built.append(url)
        raise AssertionError("constructed a client while disabled")

    assert await decision_review.wire_master_tools(registry=registry, client_factory=_factory) == 1
    try:
        result = json.loads(
            await registry.execute(
                decision_review.TOOL_NAME,
                _payload(question="使用 Authorization: Bearer abcdefghijklmnop 做判断"),
            )
        )
    finally:
        await decision_review.close_decision_review()

    assert result["status"] == "rejected"
    assert result["reasonCode"] == "SENSITIVE_INPUT"
    assert result["adoptable"] is False
    assert "request" not in result
    assert built == []


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "authorization: Bearer abcdefghijklmnop",
        "api_key: sk-abcdefghijklmnopqrst",
        "password=hunter2hunter2",
        "sk-abcdefghijklmnopqrstuvwx",
    ],
)
def test_core_rejects_each_sensitive_pattern(text: str) -> None:
    with pytest.raises(decision_review_core.DecisionInputError) as excinfo:
        decision_review_core.normalize_decision_input(_payload(question=text), _config())
    assert excinfo.value.code == "SENSITIVE_INPUT"


def test_core_sensitive_check_rejects_all_pattern_shapes() -> None:
    """Positive/negative pair: the scan must not flag ordinary prose."""
    assert decision_review_core._contains_sensitive_text("-----BEGIN PRIVATE KEY-----")
    assert not decision_review_core._contains_sensitive_text("按财务口径比较两个候选")
    assert not decision_review_core._contains_sensitive_text("authorization: none")


# ── 3. timeout and parent cancellation return on time ──────────────────────


async def test_timeout_returns_before_the_would_be_slow_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool body must answer without awaiting a transport that hangs.

    Section 10.7 requires timeout handling.  Phase 0 has no transport at all,
    so the proof is negative but real: the body completes while an injected
    transport would still be sleeping, and it never touches that transport.
    """
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv(_BASE_URL_ENV, "https://decision.example.com")
    registry = MasterToolRegistry()
    entered = asyncio.Event()

    async def _hang_factory(url: str) -> Any:
        entered.set()
        await asyncio.sleep(3600)
        raise AssertionError("must never be reached")

    assert (
        await decision_review.wire_master_tools(registry=registry, client_factory=_hang_factory)
        == 1
    )
    try:
        result = await asyncio.wait_for(
            registry.execute(decision_review.TOOL_NAME, _payload()),
            timeout=5.0,  # upstream default timeoutMs; Phase 0 answers far sooner
        )
    finally:
        await decision_review.close_decision_review()

    body = json.loads(result)
    assert body["status"] == "unavailable"
    assert body["reasonCode"] == "route_unavailable"
    # The injected transport was never even entered, so nothing could hang.
    assert not entered.is_set()


async def test_parent_cancellation_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled caller is not swallowed: cancellation stays cancellation."""
    monkeypatch.setenv(_FLAG, "1")

    async def _caller() -> None:
        await decision_review._review(_payload())
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_caller())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled() is True


# ── 4. reasonCode mapping ──────────────────────────────────────────────────


async def test_flag_off_reason_code_is_route_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_FLAG, raising=False)
    result = await decision_review._review(_payload())
    assert result == {
        "status": "unavailable",
        "reasonCode": "route_unavailable",
        "reason": decision_review.UNAVAILABLE_FLAG,
        "adoptable": False,
    }


async def test_missing_backend_reason_code_is_route_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.delenv(_BASE_URL_ENV, raising=False)
    result = await decision_review._review(_payload())
    assert result["reasonCode"] == "route_unavailable"
    assert result["reason"] == decision_review.UNAVAILABLE_ROUTE


async def test_input_errors_map_to_their_stable_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every DecisionInputError code survives the tool boundary unchanged."""
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv(_BASE_URL_ENV, "https://decision.example.com")

    cases: list[tuple[dict[str, Any], str]] = [
        (_payload(point="permission_check"), "DECISION_POINT_NOT_ALLOWED"),
        (_payload(point="Bad Point"), "INVALID_STRING_FORMAT"),
        (
            _payload(
                options=[
                    {"id": "metric_a", "description": "x"},
                    {"id": "metric_a", "description": "y"},
                ]
            ),
            "DUPLICATE_OPTION_ID",
        ),
        (_payload(primaryChoice="metric_c"), "INVALID_PRIMARY_CHOICE"),
        (_payload(options=[{"id": "only", "description": "x"}]), "INVALID_OPTIONS"),
        (_payload(question=""), "INVALID_STRING_LENGTH"),
        (
            _payload(
                evidence=[{"id": "E9", "fact": "x"}],
                options=[
                    {"id": "metric_a", "description": "x", "evidenceRefs": ["E1", "E9"]},
                    {"id": "metric_b", "description": "y"},
                ],
            ),
            "UNKNOWN_EVIDENCE_REF",
        ),
    ]
    for payload, expected in cases:
        result = await decision_review._review(payload)
        assert result["status"] == "rejected", payload
        assert result["reasonCode"] == expected, payload
        assert result["adoptable"] is False


async def test_configured_backend_still_reports_no_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 0 never dispatches, even when a route is configured."""
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv(_BASE_URL_ENV, "https://decision.example.com")

    result = await decision_review._review(_payload())

    assert result["status"] == "unavailable"
    assert result["reasonCode"] == "route_unavailable"
    assert result["request"]["model"] == "decision-model"
    assert result["point"] == "candidate_selection"
    assert result["primaryChoice"] == "metric_a"


async def test_invalid_config_maps_to_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv(_BASE_URL_ENV, "https://decision.example.com/v1")

    result = await decision_review._review(_payload())

    assert result["status"] == "rejected"
    assert result["reasonCode"] == "INVALID_CONFIG"
    assert result["adoptable"] is False


def test_gateway_error_reasons_mirror_upstream_table() -> None:
    """The failure table mirrors ``decision-model.mjs`` verbatim."""
    assert decision_review.GATEWAY_ERROR_REASONS == (
        "timeout",
        "cancelled",
        "upstream_timeout",
        "credential_unavailable",
        "authentication_failed",
        "model_not_authorized",
        "model_unsupported",
        "invalid_request",
        "route_unavailable",
        "rate_limited",
        "quota_exceeded",
        "context_too_large",
        "invalid_response",
        "response_too_large",
        "provider_error",
    )


# ── registration shape (section 10.3) ──────────────────────────────────────


async def test_flag_on_registers_lazy_pure_read_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.delenv(_BASE_URL_ENV, raising=False)
    registry = MasterToolRegistry()
    registry.register("alpha", "alpha", {"type": "object", "properties": {}}, lambda: "ok")

    assert await decision_review.wire_master_tools(registry=registry) == 1
    assert registry.has(decision_review.TOOL_NAME)
    assert decision_review.TOOL_NAME not in lazy_tools.resident_tool_names()
    assert registry.spec_for(decision_review.TOOL_NAME).side_effect is SideEffect.PURE_READ

    description = registry.describe(decision_review.TOOL_NAME)
    assert "unavailable" in description
    assert "permissions" in description

    assert await decision_review.wire_master_tools(registry=registry) == 0


async def test_flag_on_with_backend_constructs_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv(_BASE_URL_ENV, "https://decision.example.com")
    registry = MasterToolRegistry()
    built: list[str] = []

    class _Client:
        closed = False

        async def close(self) -> None:
            self.closed = True

    client = _Client()

    def _factory(url: str) -> _Client:
        built.append(url)
        return client

    assert await decision_review.wire_master_tools(registry=registry, client_factory=_factory) == 1
    assert built == ["https://decision.example.com"]
    assert registry.has(decision_review.TOOL_NAME)

    await decision_review.close_decision_review()
    assert client.closed is True


async def test_client_factory_failure_degrades_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_FLAG, "1")
    monkeypatch.setenv(_BASE_URL_ENV, "https://decision.example.com")
    registry = MasterToolRegistry()

    assert (
        await decision_review.wire_master_tools(
            registry=registry,
            client_factory=lambda _url: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        == 1
    )
    assert registry.has(decision_review.TOOL_NAME)
    await decision_review.close_decision_review()


# ── pure-logic port regression (section 10.2) ──────────────────────────────


def test_limits_table_matches_upstream() -> None:
    assert decision_review_core.LIMITS == {
        "point": 64,
        "route": 128,
        "baseUrl": 256,
        "apiKeyEnv": 64,
        "allowedDecisionPoints": 8,
        "question": 500,
        "options": 8,
        "optionId": 64,
        "optionDescription": 500,
        "evidenceItems": 12,
        "evidenceFact": 1200,
        "evidenceTotal": 6000,
        "constraints": 10,
        "constraint": 500,
        "constraintsTotal": 2500,
    }
    assert decision_review_core.DECISION_PATH == "/v1/decision"


def test_off_mode_skips_route_and_whitelist_checks() -> None:
    config = decision_review_core.validate_plugin_config({"mode": "off"})
    assert config.mode == "off"
    assert config.baseUrl == ""
    assert config.allowedDecisionPoints == ()
    with pytest.raises(TypeError):
        decision_review_core.validate_plugin_config({"mode": "enforced"})


def test_missing_base_url_and_model_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(decision_review_core.BASE_URL_ENV, raising=False)
    with pytest.raises(TypeError):
        decision_review_core.validate_plugin_config({"mode": "shadow"})
    with pytest.raises(TypeError):
        decision_review_core.validate_plugin_config(
            {"mode": "shadow", "baseUrl": "https://decision.example.com"}
        )
    monkeypatch.setenv(decision_review_core.BASE_URL_ENV, "https://decision.example.com")
    config = decision_review_core.validate_plugin_config(
        {
            "mode": "shadow",
            "model": "decision-model",
            "allowedDecisionPoints": ["entity_resolution"],
        }
    )
    assert config.baseUrl == "https://decision.example.com"


@pytest.mark.parametrize(
    "base_url",
    [
        "not a url",
        "file:///etc/passwd",
        "ftp://decision.example.com",
        "https://gw.example.com/v1",
        "https://gw.example.com/v1/",
        "https://gw.example.com/prefix/v1",
        "https://gw.example.com/v1/decision",
        "https://user:pass@gw.example.com",
        "https://gw.example.com?key=abc",
        "https://gw.example.com#frag",
    ],
)
def test_base_url_rejection_branches(base_url: str) -> None:
    with pytest.raises(TypeError):
        decision_review_core.validate_plugin_config(_config(baseUrl=base_url))


def test_base_url_accepts_root_and_prefixed_paths() -> None:
    for base_url in ("https://gw.example.com/", "https://gw.example.com/gateway"):
        config = decision_review_core.validate_plugin_config(_config(baseUrl=base_url))
        assert config.baseUrl == base_url


def test_model_and_api_key_env_validation() -> None:
    for model in (None, "  ", "auto", "AUTO", "not a model!"):
        with pytest.raises(TypeError):
            decision_review_core.validate_plugin_config(_config(model=model))
    with pytest.raises(TypeError):
        decision_review_core.validate_plugin_config(_config(apiKeyEnv="not-an-env-name!"))


def test_confidence_bounds_and_whitelist_shape() -> None:
    for abstain_below in (-0.1, 1.1, 0.9):
        with pytest.raises(TypeError):
            decision_review_core.validate_plugin_config(_config(abstainBelow=abstain_below))
    for points in ([], ["a"] * 9, ["dup", "dup"], ["Bad Point"]):
        with pytest.raises(TypeError):
            decision_review_core.validate_plugin_config(_config(allowedDecisionPoints=points))
    with pytest.raises(TypeError):
        decision_review_core.validate_plugin_config(_config(timeoutMs=50))


def test_normalize_rejects_whitelist_and_duplicates_and_budgets() -> None:
    config = _config()
    cases: list[tuple[dict[str, Any], str]] = [
        (_payload(point="permission_check"), "DECISION_POINT_NOT_ALLOWED"),
        (
            _payload(
                options=[
                    {"id": "metric_a", "description": "x"},
                    {"id": "metric_a", "description": "y"},
                ]
            ),
            "DUPLICATE_OPTION_ID",
        ),
        (_payload(primaryChoice="metric_c"), "INVALID_PRIMARY_CHOICE"),
        (_payload(options=[{"id": "only", "description": "x"}]), "INVALID_OPTIONS"),
        (
            _payload(evidence=[{"id": f"E{index}", "fact": "x" * 1100} for index in range(6)]),
            "EVIDENCE_TOO_LARGE",
        ),
        (
            _payload(
                options=[
                    {"id": "metric_a", "description": "x", "evidenceRefs": ["E9"]},
                    {"id": "metric_b", "description": "y"},
                ]
            ),
            "UNKNOWN_EVIDENCE_REF",
        ),
    ]
    for payload, expected in cases:
        with pytest.raises(decision_review_core.DecisionInputError) as excinfo:
            decision_review_core.normalize_decision_input(payload, config)
        assert excinfo.value.code == expected


def test_normalize_preserves_primary_choice_and_builds_request() -> None:
    normalized = decision_review_core.normalize_decision_input(_payload(), _config())
    assert normalized.primaryChoice == "metric_a"
    assert normalized.optionIds == ("metric_a", "metric_b")

    request = decision_review_core.build_decision_request(
        normalized, decision_review_core.validate_plugin_config(_config())
    )
    assert sorted(request) == ["model", "questions", "state"]
    assert request["model"] == "decision-model"
    assert request["state"]["evidence"] == [
        "系统预估口径包含尚未确认的订单",
        "用户要求使用财务确认口径",
    ]
    assert request["state"]["constraints"] == ["只能从候选集合中选择"]
    assert sorted(request["questions"]["recommendation"]) == [
        "criteria",
        "instructions",
        "type",
    ]
    assert request["questions"]["recommendation"]["criteria"] == {
        "metric_a": "系统预估收入",
        "metric_b": "财务确认收入",
    }
    assert "证据" in request["questions"]["recommendation"]["instructions"]


def test_build_request_appends_system_extra_as_domain_constraint() -> None:
    normalized = decision_review_core.normalize_decision_input(
        _payload(), _config(systemExtra="优先财务口径")
    )
    request = decision_review_core.build_decision_request(
        normalized, decision_review_core.validate_plugin_config(_config(systemExtra="优先财务口径"))
    )
    assert "领域约束：优先财务口径" in request["questions"]["recommendation"]["instructions"]


def test_parse_decision_response_accepts_and_rejects() -> None:
    option_ids = ["metric_a", "metric_b"]
    parsed = decision_review_core.parse_decision_response(
        {
            "model": "decision-model",
            "answers": {
                "recommendation": {
                    "type": "choice",
                    "choice": "metric_b",
                    "confidence": 0.99,
                    "probabilities": {"metric_b": 1.0, "metric_a": 0.0},
                }
            },
            "usage": {"input_tokens": 396, "output_tokens": 37},
        },
        option_ids,
    )
    assert parsed == {
        "recommendation": "metric_b",
        "confidence": 0.99,
        "probabilities": {"metric_b": 1.0, "metric_a": 0.0},
        "abstain": False,
        "risks": [],
        "missingEvidence": [],
    }

    ok = decision_review_core.parse_decision_response(
        {
            "answers": {
                "recommendation": {"type": "choice", "choice": "metric_a", "confidence": 0.8}
            }
        },
        option_ids,
    )
    assert ok is not None and ok["probabilities"] is None

    invalid: list[Any] = [
        {},
        "not an object",
        {"answers": {"recommendation": {"type": "text", "choice": "metric_a", "confidence": 0.9}}},
        {
            "answers": {
                "recommendation": {"type": "choice", "choice": "metric_c", "confidence": 0.9}
            }
        },
        {
            "answers": {
                "recommendation": {"type": "choice", "choice": "metric_a", "confidence": 1.5}
            }
        },
        {
            "answers": {
                "recommendation": {"type": "choice", "choice": "metric_a", "confidence": "high"}
            }
        },
        {
            "answers": {
                "recommendation": {
                    "type": "choice",
                    "choice": "metric_a",
                    "confidence": 0.8,
                    "probabilities": {"metric_c": 1},
                }
            }
        },
        {
            "answers": {
                "recommendation": {
                    "type": "choice",
                    "choice": "metric_a",
                    "confidence": 0.8,
                    "probabilities": {"metric_a": 1.2},
                }
            }
        },
    ]
    for value in invalid:
        assert decision_review_core.parse_decision_response(value, option_ids) is None


def test_project_decision_result_four_states() -> None:
    config = _config()
    normalized = decision_review_core.normalize_decision_input(_payload(), config)
    validated = decision_review_core.validate_plugin_config(config)

    shadow = decision_review_core.project_decision_result(
        {"recommendation": "metric_b", "confidence": 0.91},
        normalized,
        decision_review_core.validate_plugin_config(_config(mode="shadow")),
    )
    assert shadow["status"] == "shadow"
    assert shadow["primaryChoice"] == "metric_a"
    assert "recommendation" not in shadow
    assert shadow["adoptable"] is False

    abstained = decision_review_core.project_decision_result(
        {"recommendation": "metric_b", "confidence": 0.3}, normalized, validated
    )
    assert abstained["status"] == "abstained"
    assert abstained["reasonCode"] == "insufficient_evidence"
    assert abstained["abstain"] is True
    assert "recommendation" not in abstained

    low = decision_review_core.project_decision_result(
        {"recommendation": "metric_b", "confidence": 0.6}, normalized, validated
    )
    assert low["status"] == "not_adoptable"
    assert low["reasonCode"] == "low_confidence"
    assert low["adoptable"] is False

    high = decision_review_core.project_decision_result(
        {"recommendation": "metric_b", "confidence": 0.91}, normalized, validated
    )
    assert high["status"] == "ok"
    assert high["adoptable"] is True
    assert high["recommendation"] == "metric_b"


def test_off_mode_input_is_rejected_with_plugin_off() -> None:
    with pytest.raises(decision_review_core.DecisionInputError) as excinfo:
        decision_review_core.normalize_decision_input(_payload(), {"mode": "off"})
    assert excinfo.value.code == "PLUGIN_OFF"
