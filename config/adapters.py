"""config/adapters.py — canonical consumer adapters (P0_03).

Thin, behaviour-preserving helpers that route every high-risk consumer read
(model/provider, reasoning budget, workspace, backend container detection,
permission, execution, runtime, feature flags) through
:mod:`config.authority`.  Adapters hold **no precedence of their own** — they
only name the canonical key and forward the caller's scope/context/consumer
labels plus the raw layers.  Consumers that cannot be rewired in this wave
(forbidden: PermissionEngine internals, GoalRun/Execution, Skill registry,
Workspace/Knowledge stores, the protected ``veya.obase`` LLM layer) are
covered as documented projections: the adapter expresses the same effective
value the legacy inline read produced, so migrations can land without
behaviour change.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from config.authority import (
    PRECEDENCE,
    UNSET,
    Resolution,
    deep_merge,
    flag_spec,
)
from config.authority import (
    explain as _explain,
)
from config.authority import (
    resolve as _resolve,
)

_UNSET: Any = UNSET

# Project root fallback mirrors the legacy workspace default
# (``server/tool_registry._resolve_workspace_root`` and siblings resolve to
# the repository root when no workspace is bound / configured).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _res(
    key: str,
    *,
    scope: str | None = None,
    context: Mapping[str, Any] | None = None,
    consumer: str | None = None,
    runtime: Any = _UNSET,
    env: Mapping[str, str] | None = None,
    file_config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
) -> Resolution:
    return _resolve(
        key,
        scope=scope,
        context=context,
        consumer=consumer,
        runtime=runtime,
        env=env,
        file_config=file_config,
        config_path=config_path,
    )


# -- model / provider --------------------------------------------------------


def llm_provider(**kwargs: Any) -> str:
    return _res("llm.provider", **kwargs).value


def llm_model(**kwargs: Any) -> Any:
    return _res("llm.model", **kwargs).value


def provider_api_key(provider: str, **kwargs: Any) -> str:
    return _res(f"providers.{provider}.api_key", **kwargs).value


# -- reasoning / execution budget ---------------------------------------------


def max_turns(**kwargs: Any) -> int:
    return int(_res("max_turns", **kwargs).value)


def persona(**kwargs: Any) -> str:
    return _res("persona", **kwargs).value


# -- workspace / backend -------------------------------------------------------


def workspace_root(**kwargs: Any) -> Path:
    """Effective workspace root: runtime binding > VEYA_WORKSPACE > file > repo root."""
    raw = _res("workspace.root", **kwargs).value
    if raw:
        return Path(raw).expanduser()
    return _PROJECT_ROOT


def extra_dirs(**kwargs: Any) -> list[Path]:
    raw = str(_res("workspace.extra_dirs", **kwargs).value or "")
    roots: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).expanduser().resolve())
        except OSError:
            continue
    return roots


def container_env(env: Mapping[str, str] | None = None) -> bool:
    """Backend container detection — same signal engine_runner/backends read."""
    environ = os.environ if env is None else env
    return bool(environ.get("VEYA_WORKSPACE")) or os.path.exists("/.dockerenv")


# -- permission -----------------------------------------------------------------


def permission_profile(**kwargs: Any) -> str:
    return _res("permission.profile", **kwargs).value


def permission_enforce(**kwargs: Any) -> bool:
    return bool(_res("permission.enforce", **kwargs).value)


# -- execution / runtime ----------------------------------------------------------


def execution_production(**kwargs: Any) -> bool:
    return bool(_res("execution.production", **kwargs).value)


def execution_durable(**kwargs: Any) -> bool:
    return bool(_res("execution.durable", **kwargs).value)


def execution_database_url(**kwargs: Any) -> str:
    return str(_res("execution.database_url", **kwargs).value or "")


def execution_sqlite_path(**kwargs: Any) -> str:
    return str(_res("execution.sqlite_path", **kwargs).value or "")


def execution_worktree_store(**kwargs: Any) -> str:
    return str(_res("execution.worktree_store", **kwargs).value or "")


def execution_pseudo_secret(**kwargs: Any) -> str:
    return str(_res("execution.pseudo_secret", **kwargs).value or "")


def runtime_calls_override(**kwargs: Any) -> str:
    return str(_res("runtime.calls_override", **kwargs).value or "")


# -- feature flags ------------------------------------------------------------------


def resolve_feature_flag(
    name: str,
    default: bool,
    **kwargs: Any,
) -> Resolution:
    """Resolve one ``VEYA_*`` feature flag through the ONE chain."""
    return _resolve(name and f"flag.{name}", spec=flag_spec(name, default), **kwargs)


def feature_flag(name: str, default: bool, **kwargs: Any) -> bool:
    return bool(resolve_feature_flag(name, default, **kwargs).value)


# -- whole-config projection (shares ONE implementation with config/loader) -----------


def effective_config(
    defaults: Mapping[str, Any],
    file_config: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the legacy ``load_config``-shaped dict from canonical resolutions.

    Unknown file keys ride through untouched (deep-merged); every registered
    key is set from its authority resolution, preserving the legacy
    observable semantics (environment beats file beats default; an env-set
    provider key replaces the file's provider dict entry).
    """
    environ = os.environ if env is None else env
    merged: dict[str, Any] = deep_merge(copy.deepcopy(dict(defaults)), dict(file_config))

    llm = merged.setdefault("llm", {})
    llm["provider"] = _res("llm.provider", env=environ, file_config=file_config).value
    llm["model"] = _res("llm.model", env=environ, file_config=file_config).value
    merged["max_turns"] = _res("max_turns", env=environ, file_config=file_config).value
    if "persona" not in merged:
        merged["persona"] = _res("persona", env=environ, file_config=file_config).value

    providers = merged.setdefault("providers", {})
    for provider in ("anthropic", "openai", "dashscope", "deepseek", "openrouter"):
        resolution = _res(f"providers.{provider}.api_key", env=environ, file_config=file_config)
        if not resolution.value:
            continue
        if resolution.source == "environment" or provider not in providers:
            providers[provider] = {"api_key": resolution.value}
    return merged


# -- production readiness projection --------------------------------------------------
# Read-only mirror of the ``server/app.validate_production_config`` guard,
# sourced from canonical resolutions so the startup chain can be checked for
# agreement without importing the application layer.


def _env_enabled(value: Any) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "off", "no"}


_PRODUCTION_SECRET_DEFAULTS = frozenset(
    {
        "veya-dev-secret",
        "veya-layer4-pseudo-anonymizer",
        "change-me",
        "changeme",
        "dev-secret",
    }
)


def production_readiness(
    env: Mapping[str, str] | None = None,
    file_config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
) -> list[str]:
    """Return production blocking errors (empty = ready / non-production)."""
    kw: dict[str, Any] = {"env": env, "file_config": file_config, "config_path": config_path}
    if not execution_production(**kw):
        return []
    errors: list[str] = []
    pseudo = execution_pseudo_secret(**kw).strip()
    if not pseudo:
        errors.append("VEYA_PSEUDO_SECRET is required")
    elif pseudo.lower() in _PRODUCTION_SECRET_DEFAULTS:
        errors.append("VEYA_PSEUDO_SECRET must not use a development default")
    if not execution_durable(**kw):
        errors.append("VEYA_DURABLE_EXECUTION must be enabled")
    database_url = execution_database_url(**kw).strip()
    if not database_url.startswith(("postgres://", "postgresql://")):
        errors.append("VEYA_EXECUTION_DATABASE_URL must be a PostgreSQL DSN")
    return errors


def explain_key(key: str, **kwargs: Any) -> dict[str, Any]:
    return _explain(key, **kwargs)


__all__ = [
    "PRECEDENCE",
    "container_env",
    "effective_config",
    "execution_database_url",
    "execution_durable",
    "execution_production",
    "execution_pseudo_secret",
    "execution_sqlite_path",
    "execution_worktree_store",
    "explain_key",
    "extra_dirs",
    "feature_flag",
    "llm_model",
    "llm_provider",
    "max_turns",
    "permission_enforce",
    "permission_profile",
    "persona",
    "production_readiness",
    "provider_api_key",
    "resolve_feature_flag",
    "runtime_calls_override",
    "workspace_root",
]
