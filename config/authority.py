"""config/authority.py — ONE canonical configuration authority (P0_03).

All configuration questions in Veya converge here.  No consumer implements
its own precedence: callers express *key + scope/context + consumer +
environment/runtime* through :func:`resolve` and receive the effective value
together with its source, precedence position, full provenance, the default /
fallback applied, and the validation status.  :func:`explain` answers
"why is this the effective value?" with the winning source, the ordered
precedence chain, every overridden candidate, and the validation result.

Canonical precedence (highest wins), frozen for P0_03:

    1. ``runtime``     — explicit per-call override (function argument /
                         in-process runtime map).  Beats everything.
    2. ``environment`` — process environment variable (``os.environ`` or an
                         injected mapping).  Beats any file value, so an
                         operator can always override a checked-in file.
    3. ``file``        — explicit config dict / config file (project
                         ``.veya.json``, ``~/.veya/config.json``), with an
                         optional per-``scope`` subsection winning over the
                         global section of the same file.
    4. ``default``     — builtin default (documented per key).  Also the
                         fallback target when a higher layer is invalid.

``PRECEDENCE`` is the single source of truth for that order; every adapter
and consumer derives from it.  There is exactly one deliberate convergence
note: the legacy LLM helper resolved a caller-passed config dict *above*
the environment, while ``config/loader`` resolved environment above file.
The canonical chain retires the former — environment uniformly beats file —
so ``VEYA_LLM_PROVIDER``/``VEYA_LLM_MODEL`` behave like every other key
across restarts.  The protected ``veya.obase`` LLM layer itself is
untouched; the canonical path is offered via ``config/adapters.py``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Canonical precedence — the ONE chain.  Index = precedence position.
# ---------------------------------------------------------------------------

PRECEDENCE: tuple[str, ...] = ("runtime", "environment", "file", "default")

_RUNTIME, _ENVIRONMENT, _FILE, _DEFAULT = range(4)


class _UnsetType:
    """Sentinel for "this layer provided no value"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _UnsetType()

_FALSY = {"0", "false", "no", "off", ""}
_TRUTHY = {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Key registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeySpec:
    """Static definition of one canonical configuration key."""

    key: str
    env_names: tuple[str, ...]
    file_path: tuple[str, ...]
    default: Any
    kind: str = "str"  # one of: str | int | bool | path | any
    allow_none: bool = False
    sensitive: bool = False
    minimum: int | None = None  # for kind == "int"
    choices: tuple[str, ...] | None = None  # validated upper-cased for profiles
    normalize: str | None = None  # e.g. "upper" | "lower" | None
    description: str = ""


def _spec(
    key: str,
    env: tuple[str, ...],
    path: tuple[str, ...],
    default: Any,
    **kwargs: Any,
) -> KeySpec:
    return KeySpec(key=key, env_names=env, file_path=path, default=default, **kwargs)


KEYS: dict[str, KeySpec] = {}

for _s in (
    # -- model / provider (high-risk) -------------------------------------
    _spec(
        "llm.provider",
        ("VEYA_LLM_PROVIDER",),
        ("llm", "provider"),
        "dashscope",
        normalize="lower",
        description="Default LLM provider name.",
    ),
    _spec(
        "llm.model",
        ("VEYA_LLM_MODEL",),
        ("llm", "model"),
        None,
        allow_none=True,
        description="Default LLM model name (None = provider default).",
    ),
    _spec(
        "providers.anthropic.api_key",
        ("ANTHROPIC_API_KEY",),
        ("providers", "anthropic", "api_key"),
        "",
        sensitive=True,
    ),
    _spec(
        "providers.openai.api_key",
        ("OPENAI_API_KEY",),
        ("providers", "openai", "api_key"),
        "",
        sensitive=True,
    ),
    _spec(
        "providers.dashscope.api_key",
        ("DASHSCOPE_API_KEY",),
        ("providers", "dashscope", "api_key"),
        "",
        sensitive=True,
    ),
    _spec(
        "providers.deepseek.api_key",
        ("DEEPSEEK_API_KEY",),
        ("providers", "deepseek", "api_key"),
        "",
        sensitive=True,
    ),
    _spec(
        "providers.openrouter.api_key",
        ("OPENROUTER_API_KEY",),
        ("providers", "openrouter", "api_key"),
        "",
        sensitive=True,
    ),
    # -- reasoning / execution budget --------------------------------------
    # VEYA_MAX_TURNS is a new, narrowly-scoped knob (unset = legacy
    # behaviour): the reasoning/execution budget must be overridable per
    # environment like every other high-risk key.
    _spec(
        "max_turns",
        ("VEYA_MAX_TURNS",),
        ("max_turns",),
        50,
        kind="int",
        minimum=1,
        description="Max reasoning/execution turns.",
    ),
    _spec(
        "persona",
        (),
        ("persona",),
        "build",
        choices=("build", "plan", "research"),
        description="Agent persona (file-only, no env override — legacy behaviour).",
    ),
    # -- workspace / backend -----------------------------------------------
    _spec(
        "workspace.root",
        ("VEYA_WORKSPACE",),
        ("workspace", "root"),
        None,
        kind="path",
        allow_none=True,
        description="Workspace root (None = project-root fallback).",
    ),
    _spec(
        "workspace.extra_dirs",
        ("VEYA_WORKSPACE_EXTRA_DIRS",),
        ("workspace", "extra_dirs"),
        "",
        description="Extra readable roots, ':' separated.",
    ),
    # -- permission ----------------------------------------------------------
    _spec(
        "permission.profile",
        ("VEYA_PERMISSION_PROFILE",),
        ("permission", "profile"),
        "DEVELOPMENT",
        normalize="upper",
        choices=("READ_ONLY", "DEVELOPMENT", "PRODUCTION"),
        description="Permission profile档位.",
    ),
    _spec(
        "permission.enforce",
        ("VEYA_PERMISSION_PROFILE_ENFORCE",),
        ("permission", "enforce"),
        False,
        kind="bool",
        description="Enforce profile decisions (default observe).",
    ),
    # -- execution / runtime ---------------------------------------------------
    _spec(
        "execution.production",
        ("VEYA_EXECUTION_PRODUCTION",),
        ("execution", "production"),
        False,
        kind="bool",
    ),
    _spec(
        "execution.durable",
        ("VEYA_DURABLE_EXECUTION",),
        ("execution", "durable"),
        False,
        kind="bool",
    ),
    _spec(
        "execution.database_url",
        ("VEYA_EXECUTION_DATABASE_URL",),
        ("execution", "database_url"),
        "",
        sensitive=True,
    ),
    _spec(
        "execution.pseudo_secret",
        ("VEYA_PSEUDO_SECRET",),
        ("execution", "pseudo_secret"),
        "",
        sensitive=True,
        description="Pseudonymization secret (production must be explicit).",
    ),
    _spec(
        "execution.sqlite_path",
        ("VEYA_EXECUTION_SQLITE_PATH",),
        ("execution", "sqlite_path"),
        ".veya/execution-runtime.sqlite3",
    ),
    _spec(
        "execution.worktree_store",
        ("VEYA_EXECUTION_WORKTREE_STORE",),
        ("execution", "worktree_store"),
        "",
    ),
    _spec("runtime.calls_override", ("VEYA_RUNTIME_CALLS",), ("runtime", "calls_override"), ""),
    # -- P0-03 remaining domains -------------------------------------------
    # reasoning_effort / context / tools / skills / sandbox / verification /
    # evaluation had no canonical key at all, so each of those domains could
    # drift between backends. They resolve through this same chain now.
    _spec(
        "llm.reasoning_effort",
        ("VEYA_REASONING_EFFORT",),
        ("llm", "reasoning_effort"),
        "medium",
        description="Reasoning effort for the canonical model.",
    ),
    _spec(
        "context.max_tokens",
        ("VEYA_CONTEXT_MAX_TOKENS",),
        ("context", "max_tokens"),
        8000,
        kind="int",
        minimum=1,
        description="Token budget for a ContextProjection.",
    ),
    _spec(
        "context.budget_tokens",
        ("VEYA_CONTEXT_BUDGET",),
        ("context", "budget_tokens"),
        8000,
        kind="int",
        minimum=1,
        description="Token budget applied during context admission.",
    ),
    _spec("tools.enabled", ("VEYA_TOOLS_ENABLED",), ("tools", "enabled"), True, kind="bool"),
    _spec("skills.enabled", ("VEYA_SKILLS_ENABLED",), ("skills", "enabled"), True, kind="bool"),
    _spec("sandbox.profile", ("VEYA_SANDBOX_PROFILE",), ("sandbox", "profile"), "default"),
    _spec(
        "verification.required",
        ("VEYA_VERIFICATION_REQUIRED",),
        ("verification", "required"),
        True,
        kind="bool",
        description="Completion requires VerificationEvidence (INV-007).",
    ),
    _spec(
        "evaluation.suite_version",
        ("VEYA_EVALUATION_SUITE_VERSION",),
        ("evaluation", "suite_version"),
        "",
    ),
):
    KEYS[_s.key] = _s

# Feature flags are dynamic (registry lives in server/feature_flags.py, which
# must stay the import-direction leaf).  Adapters build these specs on the
# fly so flags still resolve through the ONE chain without config importing
# server code.


def flag_spec(name: str, default: bool) -> KeySpec:
    """Build the canonical spec for a ``VEYA_*`` feature flag."""
    return KeySpec(
        key=f"flag.{name}",
        env_names=(name,),
        file_path=("flags", name),
        default=default,
        kind="bool",
        description=f"Feature flag {name}.",
    )


def require_spec(key: str) -> KeySpec:
    try:
        return KEYS[key]
    except KeyError:
        raise KeyError(f"unknown config key: {key!r}") from None


# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """Effective value plus everything needed to justify it."""

    key: str
    value: Any
    source: str  # one of PRECEDENCE
    precedence: int  # index into PRECEDENCE
    provenance: Mapping[str, Any] = field(default_factory=dict)
    default: Any = None
    fallback_used: bool = False
    errors: tuple[str, ...] = ()
    scope: str | None = None
    consumer: str | None = None

    @property
    def valid(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# File layer helpers (single implementation shared with config/loader)
# ---------------------------------------------------------------------------


def default_paths() -> list[Path]:
    """Project config first, then user config — mirrors legacy discovery."""
    return [Path.cwd() / ".veya.json", Path.home() / ".veya" / "config.json"]


def read_file_config(config_path: str | Path | None = None) -> tuple[dict[str, Any], str | None]:
    """Read the raw file layer.  Returns (parsed_dict, path_used_or_None)."""
    if config_path is not None:
        p = Path(config_path)
        if not p.exists():
            return {}, None
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"config file must hold a JSON object: {p}")
        return data, str(p)
    for candidate in default_paths():
        if candidate.exists():
            with candidate.open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data, str(candidate)
    return {}, None


def deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive dict merge — the single implementation (config/loader shims it)."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, Mapping):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _lookup_path(mapping: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = mapping
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return UNSET
        node = node[part]
    return node


def _lookup_file_layer(file_config: Mapping[str, Any], spec: KeySpec, scope: str | None) -> Any:
    if scope:
        scoped = file_config.get("scopes")
        if isinstance(scoped, Mapping) and scope in scoped and isinstance(scoped[scope], Mapping):
            hit = _lookup_path(scoped[scope], spec.file_path)
            if hit is not UNSET:
                return hit
    return _lookup_path(file_config, spec.file_path)


# ---------------------------------------------------------------------------
# Coercion + validation (single implementation — no per-consumer copies)
# ---------------------------------------------------------------------------


def coerce(spec: KeySpec, raw: Any) -> tuple[Any, list[str]]:
    """Coerce a raw layer value to the key kind.  Returns (value, errors)."""
    if raw is None:
        if spec.allow_none:
            return None, []
        return spec.default, [f"{spec.key} must not be null"]
    if spec.kind == "any":
        return raw, []
    if spec.kind == "int":
        if isinstance(raw, bool):
            return spec.default, [f"{spec.key} must be int, got bool"]
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and raw.strip().lstrip("+-").isdigit():
            value = int(raw.strip())
        else:
            return spec.default, [f"{spec.key} must be int, got {raw!r}"]
        if spec.minimum is not None and value < spec.minimum:
            return spec.default, [f"{spec.key} must be >= {spec.minimum}, got {value}"]
        return value, []
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw, []
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in _TRUTHY:
                return True, []
            if lowered in _FALSY:
                return False, []
        elif isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw), []
        return spec.default, [f"{spec.key} must be bool, got {raw!r}"]
    # str / path
    if not isinstance(raw, str):
        return spec.default, [f"{spec.key} must be str, got {type(raw).__name__}"]
    value = raw.strip() if spec.kind == "str" else raw
    if spec.normalize == "lower":
        value = value.lower()
    elif spec.normalize == "upper":
        value = value.upper()
    if spec.kind == "str" and spec.key == "llm.provider" and not value:
        return spec.default, [f"{spec.key} must be a non-empty provider name"]
    if spec.choices is not None and value not in spec.choices:
        return spec.default, [f"{spec.key} must be one of {spec.choices}, got {value!r}"]
    return value, []


# ---------------------------------------------------------------------------
# resolve / explain — the authority surface
# ---------------------------------------------------------------------------


def _as_env_map(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def resolve(
    key: str,
    *,
    scope: str | None = None,
    context: Mapping[str, Any] | None = None,
    consumer: str | None = None,
    runtime: Any = UNSET,
    env: Mapping[str, str] | None = None,
    file_config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    spec: KeySpec | None = None,
) -> Resolution:
    """Resolve one canonical key through the ONE precedence chain.

    ``runtime`` is an explicit per-call override; ``env`` an injected
    environment mapping (defaults to ``os.environ``); ``file_config`` an
    already-parsed config dict (otherwise loaded from ``config_path`` or
    the default discovery paths).  ``scope``/``context``/``consumer`` are
    recorded in the provenance for audit; ``scope`` additionally selects a
    ``scopes.<scope>`` subsection of the file layer when present.
    """
    key_spec = spec or require_spec(key)
    environ = _as_env_map(env)

    if file_config is None:
        file_config, file_used = read_file_config(config_path)
    else:
        file_used = str(config_path) if config_path is not None else None

    raw_runtime = runtime
    raw_environment: Any = UNSET
    env_var_used: str | None = None
    env_observed: dict[str, Any] = {}
    for name in key_spec.env_names:
        seen = environ.get(name, UNSET)
        # Empty-string export conventionally means "unset" (this matches the
        # legacy ``if value := ...`` idiom in config/loader and keeps
        # ``VEYA_X=""`` from shadowing an explicit file value).  Deliberate
        # file/runtime values stay strict — only the shell layer is lenient.
        if seen == "":
            seen = UNSET
        env_observed[name] = None if seen is UNSET else seen
        if raw_environment is UNSET and seen is not UNSET:
            raw_environment = seen
            env_var_used = name
    raw_file = _lookup_file_layer(file_config, key_spec, scope)

    candidates: list[tuple[str, Any]] = [
        ("runtime", raw_runtime),
        ("environment", raw_environment),
        ("file", raw_file),
        ("default", key_spec.default),
    ]
    winner_source = "default"
    winner_raw: Any = key_spec.default
    for source, raw in candidates:
        if raw is not UNSET:
            winner_source = source
            winner_raw = raw
            break

    value, errors = coerce(key_spec, winner_raw)
    fallback_used = False
    if errors and winner_source != "default":
        # Invalid higher layer: fall back closed to the default, keep evidence.
        value = key_spec.default
        fallback_used = True
        winner_source = "default"

    provenance: dict[str, Any] = {
        "key": key_spec.key,
        "scope": scope,
        "context": dict(context) if context else {},
        "consumer": consumer,
        "env_var": env_var_used,
        "env_observed": env_observed,
        "file_path": ".".join(key_spec.file_path),
        "file_used": file_used,
        "observed": {source: (None if raw is UNSET else raw) for source, raw in candidates},
    }
    return Resolution(
        key=key_spec.key,
        value=value,
        source=winner_source,
        precedence=PRECEDENCE.index(winner_source),
        provenance=provenance,
        default=key_spec.default,
        fallback_used=fallback_used,
        errors=tuple(errors),
        scope=scope,
        consumer=consumer,
    )


def _display(spec: KeySpec, value: Any) -> Any:
    if value is None:
        return None
    if spec.sensitive and isinstance(value, str) and value:
        return "***REDACTED***"
    return value


def explain(
    key: str,
    *,
    scope: str | None = None,
    context: Mapping[str, Any] | None = None,
    consumer: str | None = None,
    runtime: Any = UNSET,
    env: Mapping[str, str] | None = None,
    file_config: Mapping[str, Any] | None = None,
    config_path: str | Path | None = None,
    spec: KeySpec | None = None,
) -> dict[str, Any]:
    """Answer "why is this the effective value?" for one canonical key."""
    key_spec = spec or require_spec(key)
    resolution = resolve(
        key_spec.key,
        scope=scope,
        context=context,
        consumer=consumer,
        runtime=runtime,
        env=env,
        file_config=file_config,
        config_path=config_path,
        spec=key_spec,
    )
    observed: dict[str, Any] = resolution.provenance["observed"]
    chain = [
        {
            "source": source,
            "precedence": idx,
            "observed": _display(key_spec, observed[source]),
            "selected": source == resolution.source,
        }
        for idx, source in enumerate(PRECEDENCE)
    ]
    overridden = [
        {"source": entry["source"], "observed": entry["observed"]}
        for entry in chain
        if not entry["selected"]
        and entry["source"] != "default"  # default has its own field below
        and entry["observed"] is not None
    ]
    reason_parts = [f"effective source: {resolution.source}"]
    if resolution.fallback_used:
        reason_parts.append("fallback used due to invalid value")
    if overridden:
        reason_parts.append(
            f"overridden by {resolution.source}: "
            + ", ".join(f"{o['source']}={o['observed']}" for o in overridden)
        )
    return {
        "key": key_spec.key,
        "scope": scope,
        "context": dict(context) if context else {},
        "consumer": consumer,
        "effective_value": _display(key_spec, resolution.value),
        "effective_source": resolution.source,
        "winning_source": resolution.source,
        "precedence": resolution.precedence,
        "precedence_chain": chain,
        "overridden_values": overridden,
        "overridden": overridden,
        "default": _display(key_spec, resolution.default),
        "fallback_used": resolution.fallback_used,
        "reason": "; ".join(reason_parts),
        "env_sources": {
            name: _display(key_spec, resolution.provenance["env_observed"].get(name))
            for name in key_spec.env_names
        },
        "file_sources": {
            "path": ".".join(key_spec.file_path),
            "file": resolution.provenance["file_used"],
            "observed": _display(key_spec, observed["file"]),
        },
        "runtime_source": _display(key_spec, observed["runtime"]),
        "validation": {"valid": resolution.valid, "errors": list(resolution.errors)},
    }


__all__ = [
    "KEYS",
    "PRECEDENCE",
    "UNSET",
    "KeySpec",
    "Resolution",
    "coerce",
    "deep_merge",
    "default_paths",
    "explain",
    "flag_spec",
    "read_file_config",
    "require_spec",
    "resolve",
]
