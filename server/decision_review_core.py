"""Pure decision-review logic ported from ``dsh-decision-review``.

Upstream source: ``src/decision-core.mjs`` (398 lines, zero third-party
imports) of the MIT-licensed ``dsh-decision-review`` bundle by ninetyhe-90.
It is the protocol layer of the plugin: config validation, input
normalization, the LIMITS budget table, outbound credential rejection,
request construction, response parsing and result projection.

The port is stdlib-only and performs no I/O: no network, no files, no
subprocess, no logging.  Transport, prompt injection and DSH coupling live in
``server/decision_review.py`` (Phase 0 keeps that side a default-unavailable
tool slot).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MODES = frozenset({"off", "shadow", "advisory"})
POINT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
ROUTE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_API_KEY_ENV = "DECISION_API_KEY"
BASE_URL_ENV = "DECISION_BASE_URL"
DECISION_PATH = "/v1/decision"
SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)

LIMITS: dict[str, int] = {
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

BASE_INSTRUCTIONS = (
    "根据 state 中的 question、evidence 与 constraints 评估 criteria 中的候选，"
    "选择证据最充分的选项。约束和可验证证据优先于偏好；不得根据候选顺序选择。"
    "证据不足以区分候选时保持低置信度。"
)


class DecisionInputError(TypeError):
    """Input validation failure carrying a stable upstream ``code``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.name = "DecisionInputError"
        self.code = code


@dataclass(frozen=True)
class PluginConfig:
    mode: str
    baseUrl: str = ""
    apiKeyEnv: str = DEFAULT_API_KEY_ENV
    model: str = ""
    allowedDecisionPoints: tuple[str, ...] = ()
    timeoutMs: int = 5000
    minConfidence: float = 0.75
    abstainBelow: float = 0.5
    promptSection: bool = True
    promptSectionOrder: float = 30
    systemExtra: str = ""


@dataclass(frozen=True)
class DecisionInput:
    point: str
    question: str
    primaryChoice: str
    options: tuple[dict[str, Any], ...]
    optionIds: tuple[str, ...]
    evidence: tuple[dict[str, str], ...]
    constraints: tuple[str, ...]


def _require_plain_object(value: Any, code: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DecisionInputError(code, f"{field} 必须是普通对象")
    return value


def _contains_sensitive_text(value: str) -> bool:
    return any(pattern.search(value) for pattern in SENSITIVE_TEXT_PATTERNS)


def _require_string(
    value: Any,
    field: str,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise DecisionInputError("INVALID_STRING", f"{field} 必须是字符串")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise DecisionInputError(
            "INVALID_STRING_LENGTH", f"{field} 长度必须在 1 到 {max_length} 之间"
        )
    if pattern is not None and not pattern.search(normalized):
        raise DecisionInputError("INVALID_STRING_FORMAT", f"{field} 格式不合法")
    return normalized


def _optional_string_array(
    value: Any,
    field: str,
    max_items: int,
    max_item_length: int,
    total_limit: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > max_items:
        raise DecisionInputError("INVALID_ARRAY", f"{field} 数量不能超过 {max_items}")
    normalized = [
        _require_string(item, f"{field}[{index}]", max_item_length)
        for index, item in enumerate(value)
    ]
    if sum(len(item) for item in normalized) > total_limit:
        raise DecisionInputError("ARRAY_TOO_LARGE", f"{field} 超过总字符预算 {total_limit}")
    return normalized


def _env_value(name: str) -> str:
    import os

    return os.environ.get(name, "")


def validate_plugin_config(
    raw_config: Mapping[str, Any] | PluginConfig | None = None,
) -> PluginConfig:
    """Validate a raw plugin config, raising ``TypeError`` on bad shape.

    An already-validated :class:`PluginConfig` passes through unchanged, so the
    validate-once / reuse pattern (``normalize_decision_input`` and
    ``project_decision_result`` re-validate what they are handed) works.
    """
    if isinstance(raw_config, PluginConfig):
        return raw_config
    raw = dict(raw_config or {})
    mode = raw.get("mode", "off")
    if mode is None:
        mode = "off"
    if mode not in MODES:
        raise TypeError("mode 必须是 off、shadow 或 advisory")
    if mode == "off":
        return PluginConfig(mode=mode)

    configured_base_url = str(raw.get("baseUrl") or "").strip()
    raw_base_url = configured_base_url or _env_value(BASE_URL_ENV).strip()
    if not raw_base_url or len(raw_base_url) > LIMITS["baseUrl"]:
        raise TypeError(f"baseUrl 必须显式配置或通过 {BASE_URL_ENV} 环境变量提供")
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(raw_base_url)
    except ValueError as exc:
        raise TypeError("baseUrl 必须是合法 URL") from exc
    if not parsed.scheme or not parsed.hostname or parsed.scheme not in ("http", "https"):
        raise TypeError("baseUrl 必须使用 http 或 https 协议")
    if parsed.username or parsed.password:
        raise TypeError("baseUrl 不能内嵌凭据，凭据只能通过 apiKeyEnv 指定的环境变量提供")
    if parsed.query or parsed.fragment:
        raise TypeError("baseUrl 不能包含查询参数或片段")
    # 插件自行拼接 /v1/decision；按 OpenAI 惯例填成 .../v1 会请求到
    # /v1/v1/decision 并只得到 404。
    base_path = parsed.path.rstrip("/")
    if base_path.lower().endswith("/v1") or base_path.lower().endswith("/decision"):
        raise TypeError(
            f"baseUrl 应为根地址，不要包含 /v1 或 /decision：插件会自行拼接 {DECISION_PATH}"
        )

    api_key_env = raw.get("apiKeyEnv", DEFAULT_API_KEY_ENV)
    if api_key_env is None:
        api_key_env = DEFAULT_API_KEY_ENV
    if (
        not isinstance(api_key_env, str)
        or len(api_key_env) > LIMITS["apiKeyEnv"]
        or not ENV_NAME_PATTERN.search(api_key_env)
    ):
        raise TypeError("apiKeyEnv 必须是合法的环境变量名")

    model = str(raw.get("model") or "").strip()
    if not model:
        raise TypeError(
            "model 必须显式配置：网关只接受注册表内的决策模型名（如 decision-model），插件不提供默认值"
        )
    if len(model) > LIMITS["route"] or not ROUTE_PATTERN.search(model):
        raise TypeError(f"model 必须是长度不超过 {LIMITS['route']} 的合法模型名")
    if model.lower() == "auto":
        raise TypeError("model 不能为 auto：decision API 端点要求显式指定决策模型")

    raw_points = raw.get("allowedDecisionPoints")
    if (
        not isinstance(raw_points, (list, tuple))
        or not raw_points
        or len(raw_points) > LIMITS["allowedDecisionPoints"]
    ):
        raise TypeError(
            f"allowedDecisionPoints 数量必须在 1 到 {LIMITS['allowedDecisionPoints']} 之间"
        )
    allowed_decision_points = tuple(
        _require_string(point, f"allowedDecisionPoints[{index}]", LIMITS["point"], POINT_PATTERN)
        for index, point in enumerate(raw_points)
    )
    if len(set(allowed_decision_points)) != len(allowed_decision_points):
        raise TypeError("allowedDecisionPoints 不能重复")

    timeout_ms = raw.get("timeoutMs", 5000)
    if timeout_ms is None:
        timeout_ms = 5000
    min_confidence = raw.get("minConfidence", 0.75)
    if min_confidence is None:
        min_confidence = 0.75
    abstain_below = raw.get("abstainBelow", 0.5)
    if abstain_below is None:
        abstain_below = 0.5
    prompt_section = raw.get("promptSection", True)
    if prompt_section is None:
        prompt_section = True
    prompt_section_order = raw.get("promptSectionOrder", 30)
    if prompt_section_order is None:
        prompt_section_order = 30
    system_extra = raw.get("systemExtra", "")
    if system_extra is None:
        system_extra = ""

    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
        raise TypeError("timeoutMs 必须是 100 到 10000 之间的整数")
    if timeout_ms < 100 or timeout_ms > 10000:
        raise TypeError("timeoutMs 必须是 100 到 10000 之间的整数")
    for name, value in (("minConfidence", min_confidence), ("abstainBelow", abstain_below)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} 必须是 0 到 1 之间的数字")
        if not 0 <= float(value) <= 1:
            raise TypeError(f"{name} 必须是 0 到 1 之间的数字")
    if float(abstain_below) > float(min_confidence):
        raise TypeError("abstainBelow 不能高于 minConfidence，否则永远无法产出可采纳建议")
    if not isinstance(prompt_section, bool):
        raise TypeError("promptSection 必须是布尔值")
    if isinstance(prompt_section_order, bool) or not isinstance(prompt_section_order, (int, float)):
        raise TypeError("promptSectionOrder 必须是数字")
    if not isinstance(system_extra, str) or len(system_extra) > 4000:
        raise TypeError("systemExtra 必须是不超过 4000 字符的字符串")

    return PluginConfig(
        mode=mode,
        baseUrl=raw_base_url,
        apiKeyEnv=api_key_env,
        model=model,
        allowedDecisionPoints=allowed_decision_points,
        timeoutMs=int(timeout_ms),
        minConfidence=float(min_confidence),
        abstainBelow=float(abstain_below),
        promptSection=prompt_section,
        promptSectionOrder=float(prompt_section_order),
        systemExtra=system_extra.strip(),
    )


def normalize_decision_input(
    raw_input: Any,
    raw_config: Mapping[str, Any] | None = None,
) -> DecisionInput:
    """Validate and normalize a review request, rejecting credentials first."""
    config = validate_plugin_config(raw_config)
    if config.mode == "off":
        raise DecisionInputError("PLUGIN_OFF", "DECISION 插件未启用")
    raw = _require_plain_object(raw_input, "INVALID_INPUT", "input")

    point = _require_string(raw.get("point"), "point", LIMITS["point"], POINT_PATTERN)
    if point not in config.allowedDecisionPoints:
        raise DecisionInputError("DECISION_POINT_NOT_ALLOWED", f"决策点 {point} 不在允许列表中")
    question = _require_string(raw.get("question"), "question", LIMITS["question"])
    primary_choice = _require_string(
        raw.get("primaryChoice"), "primaryChoice", LIMITS["optionId"], ID_PATTERN
    )

    raw_options = raw.get("options")
    if not isinstance(raw_options, (list, tuple)) or not 2 <= len(raw_options) <= LIMITS["options"]:
        raise DecisionInputError(
            "INVALID_OPTIONS", f"options 数量必须在 2 到 {LIMITS['options']} 之间"
        )
    options: list[dict[str, Any]] = []
    for index, raw_option in enumerate(raw_options):
        option = _require_plain_object(raw_option, "INVALID_OPTION", f"options[{index}]")
        options.append(
            {
                "id": _require_string(
                    option.get("id"), f"options[{index}].id", LIMITS["optionId"], ID_PATTERN
                ),
                "description": _require_string(
                    option.get("description"),
                    f"options[{index}].description",
                    LIMITS["optionDescription"],
                ),
                "evidenceRefs": _optional_string_array(
                    option.get("evidenceRefs"),
                    f"options[{index}].evidenceRefs",
                    LIMITS["evidenceItems"],
                    LIMITS["optionId"],
                    LIMITS["evidenceItems"] * LIMITS["optionId"],
                ),
            }
        )
    option_ids = [option["id"] for option in options]
    if len(set(option_ids)) != len(option_ids):
        raise DecisionInputError("DUPLICATE_OPTION_ID", "候选 id 不能重复")
    if primary_choice not in option_ids:
        raise DecisionInputError("INVALID_PRIMARY_CHOICE", "primaryChoice 必须属于候选集合")

    raw_evidence = raw.get("evidence")
    if raw_evidence is None:
        raw_evidence = []
    elif not isinstance(raw_evidence, (list, tuple)):
        raise DecisionInputError("INVALID_EVIDENCE", "evidence 必须是数组")
    if len(raw_evidence) > LIMITS["evidenceItems"]:
        raise DecisionInputError(
            "EVIDENCE_TOO_LARGE", f"evidence 数量不能超过 {LIMITS['evidenceItems']}"
        )
    evidence = []
    for index, raw_item in enumerate(raw_evidence):
        item = _require_plain_object(raw_item, "INVALID_EVIDENCE", f"evidence[{index}]")
        evidence.append(
            {
                "id": _require_string(
                    item.get("id"), f"evidence[{index}].id", LIMITS["optionId"], ID_PATTERN
                ),
                "fact": _require_string(
                    item.get("fact"), f"evidence[{index}].fact", LIMITS["evidenceFact"]
                ),
            }
        )
    evidence_ids = [item["id"] for item in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise DecisionInputError("DUPLICATE_EVIDENCE_ID", "证据 id 不能重复")
    if sum(len(item["fact"]) for item in evidence) > LIMITS["evidenceTotal"]:
        raise DecisionInputError(
            "EVIDENCE_TOO_LARGE", f"evidence 超过总字符预算 {LIMITS['evidenceTotal']}"
        )
    evidence_id_set = set(evidence_ids)
    for option in options:
        for ref in option["evidenceRefs"]:
            if ref not in evidence_id_set:
                raise DecisionInputError(
                    "UNKNOWN_EVIDENCE_REF", f"候选 {option['id']} 引用了不存在的证据 {ref}"
                )

    constraints = _optional_string_array(
        raw.get("constraints"),
        "constraints",
        LIMITS["constraints"],
        LIMITS["constraint"],
        LIMITS["constraintsTotal"],
    )

    outbound_text = [
        question,
        *(option["description"] for option in options),
        *(item["fact"] for item in evidence),
        *constraints,
    ]
    if any(_contains_sensitive_text(text) for text in outbound_text):
        raise DecisionInputError(
            "SENSITIVE_INPUT", "决策输入疑似包含凭据或密钥，禁止发送到 DECISION"
        )

    return DecisionInput(
        point=point,
        question=question,
        primaryChoice=primary_choice,
        options=tuple(options),
        optionIds=tuple(option_ids),
        evidence=tuple(evidence),
        constraints=tuple(constraints),
    )


def build_decision_request(input_data: DecisionInput, config: PluginConfig) -> dict[str, Any]:
    """Build the upstream ``/v1/decision`` body (pure, no dispatch)."""
    instructions = (
        BASE_INSTRUCTIONS
        if config.systemExtra == ""
        else f"{BASE_INSTRUCTIONS} 领域约束：{config.systemExtra}"
    )
    return {
        "state": {
            "point": input_data.point,
            "question": input_data.question,
            "primaryChoice": input_data.primaryChoice,
            "evidence": [item["fact"] for item in input_data.evidence],
            "constraints": list(input_data.constraints),
        },
        "model": config.model,
        "questions": {
            "recommendation": {
                "type": "choice",
                "instructions": instructions,
                "criteria": {option["id"]: option["description"] for option in input_data.options},
            },
        },
    }


def parse_decision_response(
    value: Any, option_ids: list[str] | tuple[str, ...]
) -> dict[str, Any] | None:
    """Parse a gateway response; ``None`` means ``invalid_response``."""
    if not isinstance(value, dict):
        return None
    answers = value.get("answers")
    if not isinstance(answers, dict):
        return None
    recommendation = answers.get("recommendation")
    if not isinstance(recommendation, dict):
        return None
    if recommendation.get("type") != "choice":
        return None
    choice = recommendation.get("choice")
    if not isinstance(choice, str):
        return None
    choice = choice.strip()
    if not choice or choice not in option_ids:
        return None
    confidence = recommendation.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not 0 <= float(confidence) <= 1:
        return None
    probabilities: dict[str, float] | None = None
    if "probabilities" in recommendation and recommendation["probabilities"] is not None:
        raw_probabilities = recommendation["probabilities"]
        if not isinstance(raw_probabilities, dict):
            return None
        probabilities = {}
        for key, entry in raw_probabilities.items():
            if key not in option_ids:
                return None
            if isinstance(entry, bool) or not isinstance(entry, (int, float)):
                return None
            if not 0 <= float(entry) <= 1:
                return None
            probabilities[key] = float(entry)
    return {
        "recommendation": choice,
        "confidence": float(confidence),
        "probabilities": probabilities,
        "abstain": False,
        "risks": [],
        "missingEvidence": [],
    }


def project_decision_result(
    decision: Mapping[str, Any],
    input_data: DecisionInput,
    raw_config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a parsed decision into the advisory result shape."""
    config = validate_plugin_config(raw_config)
    if config.mode == "shadow":
        return {
            "status": "shadow",
            "mode": "shadow",
            "point": input_data.point,
            "primaryChoice": input_data.primaryChoice,
            "adoptable": False,
        }

    confidence = float(decision["confidence"])
    if confidence < config.abstainBelow:
        return {
            "status": "abstained",
            "mode": "advisory",
            "point": input_data.point,
            "primaryChoice": input_data.primaryChoice,
            "confidence": confidence,
            "risks": decision.get("risks", []),
            "missingEvidence": decision.get("missingEvidence", []),
            "abstain": True,
            "reasonCode": "insufficient_evidence",
            "adoptable": False,
        }

    adoptable = confidence >= config.minConfidence
    projected: dict[str, Any] = {
        "status": "ok" if adoptable else "not_adoptable",
        "mode": "advisory",
        "point": input_data.point,
        "primaryChoice": input_data.primaryChoice,
        "recommendation": decision["recommendation"],
        "confidence": confidence,
        "risks": decision.get("risks", []),
        "missingEvidence": decision.get("missingEvidence", []),
        "abstain": False,
        "adoptable": adoptable,
    }
    if decision.get("probabilities") is not None:
        projected["probabilities"] = decision["probabilities"]
    if not adoptable:
        projected["reasonCode"] = "low_confidence"
    return projected


decision_limits = LIMITS


__all__ = [
    "BASE_INSTRUCTIONS",
    "BASE_URL_ENV",
    "DECISION_PATH",
    "DEFAULT_API_KEY_ENV",
    "ID_PATTERN",
    "LIMITS",
    "MODES",
    "POINT_PATTERN",
    "ROUTE_PATTERN",
    "SENSITIVE_TEXT_PATTERNS",
    "DecisionInput",
    "DecisionInputError",
    "PluginConfig",
    "build_decision_request",
    "decision_limits",
    "normalize_decision_input",
    "parse_decision_response",
    "project_decision_result",
    "validate_plugin_config",
]
