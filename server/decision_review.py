"""Opt-in dsh-decision-review integration (lazy tool slot only).

The upstream ``dsh-decision-review`` bundle (MIT, ninetyhe-90) adds an
independent reviewer model that second-guesses the primary model's choice at a
named decision point.  Phase 0 ports only its pure logic core
(:mod:`server.decision_review_core`) and exposes a single ``decision_review``
tool slot.  No review backend exists yet, so the tool always answers
``unavailable`` with ``route_unavailable`` and never dispatches a request.

The integration is disabled unless ``VEYA_DSH_DECISION_REVIEW=1``.  With the
flag off this module spawns nothing, registers nothing and constructs no
client, leaving the tool surface byte-identical to the frozen mainline.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

from server.decision_review_core import (
    DecisionInputError,
    _contains_sensitive_text,
    build_decision_request,
    normalize_decision_input,
    validate_plugin_config,
)
from server.feature_flags import enabled as _flag_enabled
from server.tool_registry import MasterToolRegistry, SideEffect, master_tools

logger = logging.getLogger("veya.decision_review")

FLAG_NAME = "VEYA_DSH_DECISION_REVIEW"
BASE_URL_ENV = "VEYA_DECISION_REVIEW_BASE_URL"
API_KEY_ENV = "DECISION_API_KEY"
SERVER_NAME = "decision_review"
TOOL_NAME = "decision_review"
UNAVAILABLE_FLAG = f"unavailable: flag disabled ({FLAG_NAME})"
UNAVAILABLE_ROUTE = "unavailable: no review backend configured (route_unavailable)"

#: Stable runtime reason codes mirrored from the upstream failure table
#: (``decision-model.mjs``).  Phase 0 only ever emits ``route_unavailable``.
GATEWAY_ERROR_REASONS = (
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

_client: Any = None
_client_url: str | None = None


def enabled() -> bool:
    """Whether the opt-in integration is enabled through the canonical flags."""
    try:
        return _flag_enabled(FLAG_NAME)
    except KeyError:
        return False


def base_url() -> str:
    """Configured review endpoint, or ``""`` when no backend is configured."""
    return os.environ.get(BASE_URL_ENV, "").strip()


def status() -> dict[str, Any]:
    """Read-only availability projection; never constructs a client."""
    if not enabled():
        return {"name": SERVER_NAME, "available": False, "reason": UNAVAILABLE_FLAG}
    url = base_url()
    if not url:
        return {"name": SERVER_NAME, "available": False, "reason": UNAVAILABLE_ROUTE}
    return {"name": SERVER_NAME, "available": True, "reason": "ready", "baseUrl": url}


def _unavailable_result(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reasonCode": "route_unavailable",
        "reason": reason,
        "adoptable": False,
    }


async def _review(payload: dict[str, Any]) -> dict[str, Any]:
    """Phase 0 tool body: validate the request, then report no backend.

    Input validation runs before the availability check so malformed input
    still surfaces its stable ``code``; a configured endpoint would receive
    the normalized request built here.
    """
    if not enabled():
        return _unavailable_result(UNAVAILABLE_FLAG)
    url = base_url()
    if not url:
        # Phase 0 has no review backend.  Still reject credential-bearing
        # input: the check is local, and a deployment that later configures a
        # route must never see those bytes leave.
        rejection = _input_rejection(payload)
        return rejection or _unavailable_result(UNAVAILABLE_ROUTE)
    try:
        config = validate_plugin_config(
            {
                "mode": "advisory",
                "baseUrl": url,
                "apiKeyEnv": API_KEY_ENV,
                "model": str(payload.get("model") or ""),
                "allowedDecisionPoints": payload.get("allowedDecisionPoints") or [],
            }
        )
    except TypeError as exc:
        return {
            "status": "rejected",
            "reasonCode": "INVALID_CONFIG",
            "reason": str(exc),
            "adoptable": False,
        }
    try:
        normalized = normalize_decision_input(payload, config)
    except DecisionInputError as exc:
        return {
            "status": "rejected",
            "reasonCode": exc.code,
            "reason": str(exc),
            "adoptable": False,
        }
    request = build_decision_request(normalized, config)
    return {
        **_unavailable_result(UNAVAILABLE_ROUTE),
        "mode": "advisory",
        "point": normalized.point,
        "primaryChoice": normalized.primaryChoice,
        "request": request,
        "reasonCode": "route_unavailable",
    }


def _input_rejection(payload: Any) -> dict[str, Any] | None:
    """Reject credential-bearing input with the stable ``SENSITIVE_INPUT`` code.

    Only the outbound-text scan is reused here — the local decision-point and
    shape checks need a configured ``baseUrl``/``model`` that Phase 0 does not
    have, so they stay in :func:`_review`'s configured branch.
    """
    if not isinstance(payload, dict):
        return None
    texts = [str(payload.get(key) or "") for key in ("question", "constraints")]
    texts.extend(str(item or "") for item in (payload.get("options") or []))
    texts.extend(str(item or "") for item in (payload.get("evidence") or []))
    if not any(_contains_sensitive_text(text) for text in texts):
        return None
    return {
        "status": "rejected",
        "reasonCode": "SENSITIVE_INPUT",
        "reason": "决策输入疑似包含凭据或密钥，禁止发送到 DECISION",
        "adoptable": False,
    }


async def _tool(**payload: Any) -> dict[str, Any]:
    return await _review(payload)


def _parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "point": {
                "type": "string",
                "description": "Named decision point (e.g. entity_resolution).",
            },
            "question": {
                "type": "string",
                "description": "The judgement being reviewed, as one sentence.",
            },
            "primaryChoice": {
                "type": "string",
                "description": "Option id the primary model picked.",
            },
            "options": {
                "type": "array",
                "description": "2-8 candidates, each {id, description, evidenceRefs?}.",
                "items": {"type": "object"},
            },
            "evidence": {
                "type": "array",
                "description": "Up to 12 {id, fact} pairs the candidates may cite.",
                "items": {"type": "object"},
            },
            "constraints": {
                "type": "array",
                "description": "Hard constraints the answer must respect.",
                "items": {"type": "string"},
            },
            "model": {
                "type": "string",
                "description": "Reviewer model name; must differ from the primary model.",
            },
            "allowedDecisionPoints": {
                "type": "array",
                "description": "Decision points this deployment allows to be reviewed.",
                "items": {"type": "string"},
            },
        },
        "required": ["point", "question", "primaryChoice", "options", "model"],
    }


def _description() -> str:
    return (
        "Ask an independent reviewer model for a second opinion on one named decision point. "
        "The reviewer only classifies, scores and ranks the candidates you supply: it has no tools, "
        "no file or network access and no approval authority, and the adopt choice stays with you. "
        "Returns a structured result (status/recommendation/confidence/risks/missingEvidence). "
        "In Phase 0 no review backend is configured, so every call answers "
        "`unavailable` + `route_unavailable` and the primary judgement is kept. "
        "Never use this to decide permissions, approvals, user confirmation, write operations, "
        "network targets, or whether to bypass a safety mechanism."
    )


async def wire_master_tools(
    *,
    registry: MasterToolRegistry | None = None,
    client_factory: Any = None,
) -> int:
    """Register the lazy ``decision_review`` tool slot.

    The tool is registered in the canonical registry but is absent from the
    resident set.  A model reaches it through ``search_tools``, at which point
    lazy loading appends it to the session surface.  With the flag off nothing
    is registered and no client is constructed.
    """
    global _client, _client_url

    if not enabled():
        return 0
    target = registry if registry is not None else master_tools
    if target.has(TOOL_NAME):
        return 0
    url = base_url()
    if not url:
        logger.info(
            "decision review backend not configured (%s), registering an unavailable slot",
            BASE_URL_ENV,
        )
        _client = None
        _client_url = None
        target.register(
            TOOL_NAME,
            _description(),
            _parameters(),
            _tool,
            max_result_chars=8000,
            parallel_safe=True,
            side_effect=SideEffect.PURE_READ,
        )
        return 1
    client: Any = None
    try:
        client = client_factory(url) if client_factory is not None else None
    except Exception as exc:
        logger.warning(
            "decision review client not constructed (%s: %s), degrading gracefully",
            type(exc).__name__,
            exc,
        )
        _client = None
        _client_url = None
        target.register(
            TOOL_NAME,
            _description(),
            _parameters(),
            _tool,
            max_result_chars=8000,
            parallel_safe=True,
            side_effect=SideEffect.PURE_READ,
        )
        return 1
    _client = client
    _client_url = url
    target.register(
        TOOL_NAME,
        _description(),
        _parameters(),
        _tool,
        max_result_chars=8000,
        parallel_safe=True,
        side_effect=SideEffect.PURE_READ,
    )
    return 1


async def close_decision_review() -> None:
    """Drop the review client handle (Phase 0 holds no live connection)."""
    global _client, _client_url
    client = _client
    _client = None
    _client_url = None
    if client is None:
        return
    with contextlib.suppress(Exception):
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "FLAG_NAME",
    "GATEWAY_ERROR_REASONS",
    "SERVER_NAME",
    "TOOL_NAME",
    "UNAVAILABLE_FLAG",
    "UNAVAILABLE_ROUTE",
    "_review",
    "base_url",
    "close_decision_review",
    "enabled",
    "status",
    "wire_master_tools",
]
