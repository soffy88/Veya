"""Production profile must fail closed instead of using development fallbacks."""

from __future__ import annotations

import pytest

from server.app import validate_production_config


def _valid_production_env() -> dict[str, str]:
    return {
        "VEYA_EXECUTION_PRODUCTION": "1",
        "VEYA_DURABLE_EXECUTION": "1",
        "VEYA_EXECUTION_DATABASE_URL": "postgresql://veya:test@db/veya",
        "VEYA_PSEUDO_SECRET": "a-real-production-secret-ref-value",
    }


def test_production_config_accepts_postgres_and_explicit_secret() -> None:
    validate_production_config(_valid_production_env())


def test_production_config_rejects_missing_required_secret() -> None:
    env = _valid_production_env()
    env.pop("VEYA_PSEUDO_SECRET")

    with pytest.raises(RuntimeError, match="VEYA_PSEUDO_SECRET"):
        validate_production_config(env)


def test_production_config_rejects_development_secret() -> None:
    env = _valid_production_env()
    env["VEYA_PSEUDO_SECRET"] = "veya-dev-secret"

    with pytest.raises(RuntimeError, match="development"):
        validate_production_config(env)


def test_production_config_rejects_disabled_durable_runtime() -> None:
    env = _valid_production_env()
    env["VEYA_DURABLE_EXECUTION"] = "0"

    with pytest.raises(RuntimeError, match="VEYA_DURABLE_EXECUTION"):
        validate_production_config(env)


def test_pseudo_anonymizer_has_no_production_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from veya.oskill.im.pseudo import PseudoAnonymizer

    monkeypatch.setenv("VEYA_EXECUTION_PRODUCTION", "1")
    monkeypatch.delenv("VEYA_PSEUDO_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="VEYA_PSEUDO_SECRET"):
        PseudoAnonymizer()
