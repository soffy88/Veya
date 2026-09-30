"""P0_03 configuration authority tests.

Every claim needs implementation + wiring + tests + runtime proof:
- default / explicit file config / environment override / full precedence
- invalid config → closed fallback, fallback flag, validation errors
- explain(): winning source, ordered chain, overridden candidates, default,
  env/file/runtime sources, validation result, secret redaction
- provenance: scope / context / consumer recorded; scoped file subsections win
- consumer compatibility: load_config / load_settings / feature_flags
  behaviour-preserving (including empty-string env parity)
- restart / new-process consistency: a fresh interpreter with the same raw
  sources resolves the same effective values
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from config import adapters
from config.authority import PRECEDENCE, explain, resolve


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    for var in (
        "VEYA_LLM_PROVIDER",
        "VEYA_LLM_MODEL",
        "VEYA_MAX_TURNS",
        "VEYA_WORKSPACE",
        "VEYA_WORKSPACE_EXTRA_DIRS",
        "VEYA_PERMISSION_PROFILE",
        "VEYA_PERMISSION_PROFILE_ENFORCE",
        "VEYA_EXECUTION_PRODUCTION",
        "VEYA_DURABLE_EXECUTION",
        "VEYA_EXECUTION_DATABASE_URL",
        "VEYA_EXECUTION_SQLITE_PATH",
        "VEYA_PSEUDO_SECRET",
        "ANTHROPIC_API_KEY",
        "VEYA_TASK_CENTER_V1",
        "VEYA_EVENT_STORE_V1",
    ):
        monkeypatch.delenv(var, raising=False)


# -- default ------------------------------------------------------------------


def test_default_resolution_reports_source_and_provenance():
    resolution = resolve("llm.provider", env={}, file_config={})
    assert resolution.value == "dashscope"
    assert resolution.source == "default"
    assert resolution.precedence == PRECEDENCE.index("default") == 3
    assert resolution.default == "dashscope"
    assert resolution.fallback_used is False
    assert resolution.valid
    assert resolution.provenance["observed"] == {
        "runtime": None,
        "environment": None,
        "file": None,
        "default": "dashscope",
    }


def test_canonical_precedence_order_is_frozen():
    assert PRECEDENCE == ("runtime", "environment", "file", "default")


# -- explicit file config -------------------------------------------------------


def test_explicit_file_config_wins_over_default():
    resolution = resolve("llm.provider", env={}, file_config={"llm": {"provider": "fileprov"}})
    assert (resolution.value, resolution.source) == ("fileprov", "file")
    assert resolution.precedence == 2


def test_scoped_file_subsection_beats_global_file_section():
    file_config = {
        "llm": {"provider": "global"},
        "scopes": {"prod": {"llm": {"provider": "scoped"}}},
    }
    assert resolve("llm.provider", env={}, file_config=file_config).value == "global"
    scoped = resolve("llm.provider", env={}, file_config=file_config, scope="prod")
    assert scoped.value == "scoped"
    assert scoped.scope == "prod"


# -- environment override ---------------------------------------------------------


def test_environment_override_beats_file():
    resolution = resolve(
        "llm.provider",
        env={"VEYA_LLM_PROVIDER": "envprov"},
        file_config={"llm": {"provider": "fileprov"}},
    )
    assert (resolution.value, resolution.source) == ("envprov", "environment")
    assert resolution.precedence == 1


def test_empty_string_env_counts_as_unset_like_legacy_walrus():
    resolution = resolve(
        "llm.provider",
        env={"VEYA_LLM_PROVIDER": ""},
        file_config={"llm": {"provider": "fileprov"}},
    )
    assert (resolution.value, resolution.source) == ("fileprov", "file")


def test_numeric_string_env_coerces_for_int_keys():
    resolution = resolve("max_turns", env={"VEYA_MAX_TURNS": "44"}, file_config={})
    assert (resolution.value, resolution.source) == (44, "environment")
    assert resolution.valid


# -- full precedence ---------------------------------------------------------------


def test_runtime_beats_environment_beats_file_beats_default():
    file_config = {"llm": {"provider": "fileprov"}}
    env = {"VEYA_LLM_PROVIDER": "envprov"}
    assert resolve("llm.provider", env=env, file_config=file_config).value == "envprov"
    assert (
        resolve("llm.provider", env=env, file_config=file_config, runtime="rtprov").value
        == "rtprov"
    )
    top = resolve("llm.provider", env=env, file_config=file_config, runtime="rtprov")
    assert (top.source, top.precedence) == ("runtime", 0)


# -- invalid config → closed fallback --------------------------------------------------


@pytest.mark.parametrize("bad", [0, -3, "abc", "4.5", True, ""])
def test_invalid_max_turns_falls_back_closed(bad):
    resolution = resolve("max_turns", env={}, file_config={"max_turns": bad})
    assert resolution.value == 50
    assert resolution.source == "default"
    assert resolution.fallback_used is True
    assert not resolution.valid
    assert resolution.errors


def test_invalid_bool_falls_back_closed():
    resolution = resolve(
        "execution.durable", env={"VEYA_DURABLE_EXECUTION": "maybe"}, file_config={}
    )
    assert resolution.value is False
    assert resolution.fallback_used is True
    assert not resolution.valid


def test_invalid_permission_profile_falls_back_to_development():
    resolution = resolve(
        "permission.profile", env={"VEYA_PERMISSION_PROFILE": "godmode"}, file_config={}
    )
    assert resolution.value == "DEVELOPMENT"
    assert resolution.fallback_used is True


def test_explicit_fallback_for_missing_model_is_none():
    resolution = resolve("llm.model", env={}, file_config={})
    assert resolution.value is None
    assert resolution.source == "default"
    assert resolution.valid


# -- explain ---------------------------------------------------------------------------


def test_explain_answers_why_with_chain_and_overridden():
    exp = explain(
        "llm.provider",
        env={"VEYA_LLM_PROVIDER": "envprov"},
        file_config={"llm": {"provider": "fileprov"}},
        runtime="rtprov",
        scope="prod",
        consumer="proof-consumer",
        context={"request": "demo"},
    )
    assert exp["effective_value"] == "rtprov"
    assert exp["winning_source"] == "runtime"
    assert [step["source"] for step in exp["precedence_chain"]] == list(PRECEDENCE)
    assert [step["precedence"] for step in exp["precedence_chain"]] == [0, 1, 2, 3]
    assert sum(step["selected"] for step in exp["precedence_chain"]) == 1
    assert {o["source"] for o in exp["overridden"]} == {"environment", "file"}
    assert exp["default"] == "dashscope"
    assert exp["fallback_used"] is False
    assert exp["env_sources"] == {"VEYA_LLM_PROVIDER": "envprov"}
    assert exp["file_sources"]["observed"] == "fileprov"
    assert exp["runtime_source"] == "rtprov"
    assert exp["validation"] == {"valid": True, "errors": []}
    assert exp["scope"] == "prod"
    assert exp["consumer"] == "proof-consumer"
    assert exp["context"] == {"request": "demo"}


def test_explain_redacts_sensitive_values():
    exp = explain(
        "providers.anthropic.api_key",
        env={"ANTHROPIC_API_KEY": "sk-real"},
        file_config={},
    )
    assert exp["effective_value"] == "***REDACTED***"
    assert exp["env_sources"] == {"ANTHROPIC_API_KEY": "***REDACTED***"}
    assert "sk-real" not in json.dumps(exp)


def test_explain_shows_fallback_and_validation_errors():
    exp = explain("max_turns", env={}, file_config={"max_turns": 0})
    assert exp["effective_value"] == 50
    assert exp["winning_source"] == "default"
    assert exp["fallback_used"] is True
    assert exp["validation"]["valid"] is False
    assert exp["validation"]["errors"]


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        resolve("nope.not-a-key", env={}, file_config={})


# -- provenance ---------------------------------------------------------------------------


def test_provenance_records_scope_context_consumer():
    resolution = resolve(
        "max_turns",
        env={},
        file_config={},
        scope="prod",
        context={"turn": 7},
        consumer="unit-test",
    )
    assert resolution.scope == "prod"
    assert resolution.consumer == "unit-test"
    assert resolution.provenance["scope"] == "prod"
    assert resolution.provenance["consumer"] == "unit-test"
    assert resolution.provenance["context"] == {"turn": 7}


# -- adapters: no private precedence -------------------------------------------------------


def test_adapters_cover_high_risk_keys_without_own_precedence():
    file_config = {
        "llm": {"provider": "fileprov", "model": "filemodel"},
        "max_turns": 33,
        "permission": {"profile": "production", "enforce": True},
        "execution": {"durable": True, "production": False},
    }
    env = {
        "VEYA_LLM_PROVIDER": "envprov",
        "VEYA_PERMISSION_PROFILE_ENFORCE": "1",
    }
    kw = {"env": env, "file_config": file_config}
    assert adapters.llm_provider(**kw) == "envprov"
    assert adapters.llm_model(**kw) == "filemodel"
    assert adapters.max_turns(**kw) == 33
    assert adapters.permission_profile(**kw) == "PRODUCTION"
    assert adapters.permission_enforce(**kw) is True
    assert adapters.execution_durable(**kw) is True
    assert adapters.execution_production(**kw) is False


def test_workspace_adapter_matches_legacy_expression(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_WORKSPACE", str(tmp_path))
    resolved = adapters.workspace_root()
    legacy = Path(os.environ.get("VEYA_WORKSPACE", str(Path(".")))).resolve()
    assert resolved == legacy
    probe = resolved / "p003-proof.txt"
    probe.write_text("effective-config-drives-io")
    assert probe.read_text() == "effective-config-drives-io"


def test_feature_flag_adapter_matches_legacy_semantics(monkeypatch):
    from server.feature_flags import enabled

    monkeypatch.delenv("VEYA_EVENT_STORE_V1", raising=False)
    assert enabled("VEYA_EVENT_STORE_V1") is True
    for raw, expected in [
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("FALSE", False),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("", True),
    ]:
        monkeypatch.setenv("VEYA_EVENT_STORE_V1", raw)
        legacy = raw.strip().lower() not in {"0", "false", "no", "off"}
        assert enabled("VEYA_EVENT_STORE_V1") is legacy is expected
    with pytest.raises(KeyError):
        enabled("VEYA_NO_SUCH_FLAG")


# -- consumer compatibility: loader / settings ---------------------------------------------


def _write_cfg(tmp_path: Path, payload: dict) -> str:
    path = tmp_path / ".veya.json"
    path.write_text(json.dumps(payload))
    return str(path)


def test_loader_explicit_file_and_env_override(tmp_path, monkeypatch):
    from config.loader import load_config

    monkeypatch.chdir(tmp_path)
    path = _write_cfg(
        tmp_path,
        {"llm": {"provider": "fileprov", "model": "filemodel"}, "max_turns": 33},
    )
    cfg = load_config(path)
    assert cfg["llm"] == {"provider": "fileprov", "model": "filemodel"}
    assert cfg["max_turns"] == 33
    monkeypatch.setenv("VEYA_LLM_PROVIDER", "envprov")
    monkeypatch.setenv("VEYA_LLM_MODEL", "envmodel")
    monkeypatch.setenv("VEYA_MAX_TURNS", "44")
    cfg = load_config(path)
    assert cfg["llm"] == {"provider": "envprov", "model": "envmodel"}
    assert cfg["max_turns"] == 44


def test_loader_provider_key_replace_semantics(tmp_path, monkeypatch):
    from config.loader import load_config

    monkeypatch.chdir(tmp_path)
    path = _write_cfg(
        tmp_path, {"providers": {"anthropic": {"api_key": "file-key", "base_url": "x"}}}
    )
    assert load_config(path)["providers"]["anthropic"] == {
        "api_key": "file-key",
        "base_url": "x",
    }
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert load_config(path)["providers"]["anthropic"] == {"api_key": "env-key"}


def test_loader_repeat_consistency_no_default_leak(tmp_path, monkeypatch):
    from config.loader import _DEFAULTS, load_config

    monkeypatch.chdir(tmp_path)
    path = _write_cfg(tmp_path, {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert load_config(path)["providers"]["anthropic"] == {"api_key": "env-key"}
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert load_config(path)["providers"] == {}
    assert _DEFAULTS["providers"] == {}


def test_settings_matches_loader(tmp_path, monkeypatch):
    from config.loader import load_config
    from config.settings import load_settings

    monkeypatch.chdir(tmp_path)
    path = _write_cfg(tmp_path, {"max_turns": 31})
    assert load_settings(path) == load_config(path)


def test_schema_rejects_bad_values_and_accepts_good_ones():
    from config.schema import validate_schema

    assert validate_schema({"max_turns": 10, "persona": "plan"}) == []
    errors = validate_schema({"max_turns": 0, "persona": "plan"})
    assert any("max_turns" in e for e in errors)
    assert validate_schema({"persona": "nope"}) != []
    assert validate_schema({"llm": {"provider": ""}}) != []
    assert validate_schema("nope") != []  # type: ignore[arg-type]


# -- production readiness projection agrees with the app guard --------------------------------


def test_production_readiness_mirrors_app_guard():
    base = {
        "VEYA_EXECUTION_PRODUCTION": "1",
        "VEYA_DURABLE_EXECUTION": "1",
        "VEYA_EXECUTION_DATABASE_URL": "postgresql://veya:t@db/veya",
        "VEYA_PSEUDO_SECRET": "proof-secret",
    }
    assert adapters.production_readiness(env=dict(base)) == []
    missing = dict(base)
    del missing["VEYA_PSEUDO_SECRET"]
    assert any("VEYA_PSEUDO_SECRET" in e for e in adapters.production_readiness(env=missing))
    dev_secret = dict(base, VEYA_PSEUDO_SECRET="veya-dev-secret")
    assert any("development" in e for e in adapters.production_readiness(env=dev_secret))
    no_durable = dict(base, VEYA_DURABLE_EXECUTION="0")
    assert any("VEYA_DURABLE_EXECUTION" in e for e in adapters.production_readiness(env=no_durable))
    assert adapters.production_readiness(env={}) == []


# -- restart / new-process consistency -----------------------------------------------------------


def test_restart_consistency_fresh_interpreter(tmp_path):
    file_config = {
        "llm": {"provider": "fileprov", "model": "filemodel"},
        "max_turns": 33,
        "permission": {"profile": "production"},
    }
    path = _write_cfg(tmp_path, file_config)
    env = {
        "VEYA_LLM_PROVIDER": "envprov",
        "VEYA_MAX_TURNS": "44",
        "VEYA_EVENT_STORE_V1": "0",
    }
    expected = {
        "provider": adapters.llm_provider(env=env, file_config=file_config),
        "model": adapters.llm_model(env=env, file_config=file_config),
        "max_turns": adapters.max_turns(env=env, file_config=file_config),
        "profile": adapters.permission_profile(env=env, file_config=file_config),
        "flag": adapters.feature_flag("VEYA_EVENT_STORE_V1", True, env=env),
    }
    script = (
        "import json;"
        "from config import adapters;"
        f"cfg={file_config!r};"
        f"env={env!r};"
        "print(json.dumps({"
        "'provider': adapters.llm_provider(env=env, file_config=cfg),"
        "'model': adapters.llm_model(env=env, file_config=cfg),"
        "'max_turns': adapters.max_turns(env=env, file_config=cfg),"
        "'profile': adapters.permission_profile(env=env, file_config=cfg),"
        "'flag': adapters.feature_flag(\"VEYA_EVENT_STORE_V1\", True, env=env),"
        "'file_provider': adapters.llm_provider(env={}, config_path=" + repr(path) + "),"
        "}))"
    )
    proc_env = dict(os.environ)
    proc_env.pop("VEYA_LLM_PROVIDER", None)
    proc_env.pop("VEYA_MAX_TURNS", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
        env=proc_env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    fresh = json.loads(completed.stdout)
    for field, value in expected.items():
        assert fresh[field] == value, field
    # Same raw file read from disk in the fresh process agrees too.
    assert fresh["file_provider"] == "fileprov"
