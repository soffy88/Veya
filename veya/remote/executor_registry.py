"""The single source of truth for L1 executor runtime identity.

This module deliberately contains identity discovery only.  It does not make
permission decisions and it never exposes credential values.  Adapters,
health, manifests, dispatch and telemetry must project from this registry.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from veya.obase import canonical_proxies as _cp

_ALIASES = {
    "agy": "antigravity",
    "antigravity": "antigravity",
    "open-code": "opencode",
    "opencode_go": "opencode",
}
_KNOWN = ("pi", "hicode", "codex", "antigravity", "opencode", "dsh")


def normalize_executor_id(value: str) -> str:
    key = str(value or "").strip().lower()
    return _ALIASES.get(key, key)


@dataclass(frozen=True)
class ExecutorRuntimeIdentity:
    executor_id: str
    executor_kind: str
    provider: str | None
    model: str | None
    auth_state: str
    reachable: bool
    launcher: str | None
    capabilities: frozenset[str] = frozenset()
    runtime_source: str = "unknown"
    updated_at: float = 0.0
    authenticated: bool = False
    status: str = "UNKNOWN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "executor_kind": self.executor_kind,
            "provider": self.provider,
            "model": self.model,
            "auth_state": self.auth_state,
            "authenticated": self.authenticated,
            "reachable": self.reachable,
            "launcher": self.launcher,
            "capabilities": sorted(self.capabilities),
            "runtime_source": self.runtime_source,
            "updated_at": self.updated_at,
            "status": self.status,
        }


def _credential_present(names: list[str], files: list[Path]) -> bool:
    return any(bool(os.environ.get(name)) for name in names) or any(
        path.is_file() for path in files
    )


def _launcher(names: list[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _configured_launcher(executor_id: str) -> str | None:
    env_name = {
        "pi": "VEYA_PI_BIN",
        "codex": "VEYA_CODEX_BIN",
        "antigravity": "VEYA_ANTIGRAVITY_BIN",
        "opencode": "VEYA_OPENCODE_BIN",
        "dsh": "VEYA_DSH_BIN",
    }.get(executor_id)
    configured = os.environ.get(env_name, "").strip() if env_name else ""
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return None


def _pi_config() -> tuple[str | None, str | None, bool, str]:
    path = Path(os.environ.get("PI_MODELS_CONFIG", "~/.pi/agent/models.json")).expanduser()
    settings_path = Path(
        os.environ.get("PI_SETTINGS_CONFIG", "~/.pi/agent/settings.json")
    ).expanduser()
    settings: Mapping[str, Any] = {}
    try:
        loaded_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(loaded_settings, Mapping):
            settings = loaded_settings
    except (OSError, ValueError):
        pass
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, False, str(path)
    if not isinstance(data, Mapping):
        return None, None, False, str(path)
    provider_name = (
        os.environ.get("PI_PROVIDER")
        or settings.get("defaultProvider")
        or data.get("defaultProvider")
    )
    model_name = (
        os.environ.get("PI_MODEL") or settings.get("defaultModel") or data.get("defaultModel")
    )
    providers = data.get("providers")
    if isinstance(providers, Mapping):
        if not provider_name:
            provider_name = next(
                (str(name) for name, value in providers.items() if isinstance(value, Mapping)), None
            )
        provider = providers.get(provider_name) if provider_name else None
        if isinstance(provider, Mapping) and not model_name:
            models = provider.get("models")
            if isinstance(models, list) and models and isinstance(models[0], Mapping):
                model_name = models[0].get("id")
    return (
        str(provider_name) if provider_name else None,
        str(model_name) if model_name else None,
        bool(provider_name and model_name),
        str(settings_path if settings else path),
    )


def _hicode_config() -> tuple[str | None, str | None, str]:
    provider = os.environ.get("HICODE_REASONIX_PROVIDER")
    model = os.environ.get("HICODE_REASONIX_MODEL") or os.environ.get("HICODE_MODEL")
    path = Path(__file__).resolve().parents[2] / "config" / "hicode_model_mapping.json"
    if not provider or not model:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not model and isinstance(data, Mapping):
            active = next(
                (
                    value
                    for value in data.values()
                    if isinstance(value, Mapping) and value.get("active")
                ),
                None,
            )
            if isinstance(active, Mapping):
                model = str(active.get("requested_model") or "") or None
                provider = provider or str(active.get("requested_provider") or "") or None
    source = "env:hicode" if os.environ.get("HICODE_REASONIX_PROVIDER") else str(path)
    return provider, model, source


@dataclass
class ExecutorRegistry:
    """Canonical identity authority; adapters receive projections from here."""

    overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    _identities: dict[str, ExecutorRuntimeIdentity] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for key in _KNOWN:
            self._identities[key] = self._discover(key)
        for key, value in self.overrides.items():
            self._identities[normalize_executor_id(key)] = self._from_mapping(
                normalize_executor_id(key), value
            )

    def _from_mapping(self, executor_id: str, value: Mapping[str, Any]) -> ExecutorRuntimeIdentity:
        launcher = value.get("launcher")
        launcher = str(launcher) if launcher else None
        authenticated = bool(value.get("authenticated", value.get("auth_state") == "AUTHENTICATED"))
        reachable = bool(value.get("reachable", bool(launcher)))
        status = str(
            value.get("status") or ("READY" if authenticated and reachable else "DEGRADED")
        )
        return ExecutorRuntimeIdentity(
            executor_id=executor_id,
            executor_kind=str(value.get("executor_kind") or "l1_worker"),
            provider=str(value["provider"]) if value.get("provider") else None,
            model=str(value["model"]) if value.get("model") else None,
            auth_state=str(
                value.get("auth_state") or ("AUTHENTICATED" if authenticated else "MISSING")
            ),
            authenticated=authenticated,
            reachable=reachable,
            launcher=launcher,
            capabilities=frozenset(str(item) for item in value.get("capabilities", ())),
            runtime_source=str(value.get("runtime_source") or "injected"),
            updated_at=float(value.get("updated_at") or time.time()),
            status=status,
        )

    def _discover(self, executor_id: str) -> ExecutorRuntimeIdentity:
        source = "provider-registry"
        if executor_id == "pi":
            provider, model, _configured, source = _pi_config()
            auth = _credential_present(
                ["PI_API_KEY", "ANTHROPIC_API_KEY"],
                [Path("~/.pi/agent/auth.json").expanduser()],
            )
            launcher = _configured_launcher(executor_id) or _launcher(["pi"])
        elif executor_id == "hicode":
            provider, model, source = _hicode_config()
            auth = bool(provider and model)
            try:
                from server.hicode_runtime import get_hicode_executor

                launcher = get_hicode_executor().resolve_binary()
            except Exception:
                launcher = None
        else:
            contract = _cp.executor_contract(executor_id)
            env_prefix = {
                "antigravity": "ANTIGRAVITY",
                "codex": "CODEX",
                "dsh": "DSH",
                "opencode": "OPENCODE",
            }.get(executor_id, executor_id.upper())
            provider = (
                os.environ.get(f"VEYA_{env_prefix}_PROVIDER")
                or str(contract.get("provider") or "")
                or None
            )
            model = (
                os.environ.get(f"VEYA_{env_prefix}_MODEL")
                or str(contract.get("model") or "")
                or None
            )
            auth = _credential_present(
                [str(item) for item in contract.get("auth_env", ())],
                [Path("~/.codex/config.json").expanduser()] if executor_id == "codex" else [],
            )
            bins = [str(item) for item in contract.get("bins", ())]
            bins.extend(
                {"codex": ["codex"], "opencode": ["opencode"], "dsh": ["dsh"]}.get(executor_id, [])
            )
            launcher = _configured_launcher(executor_id) or _launcher(bins)
        reachable = launcher is not None
        authenticated = bool(auth)
        status = (
            "READY" if reachable and authenticated else "DEGRADED" if reachable else "UNAVAILABLE"
        )
        return ExecutorRuntimeIdentity(
            executor_id=executor_id,
            executor_kind="internal_hicode" if executor_id == "hicode" else "l1_worker",
            provider=provider,
            model=model,
            auth_state="AUTHENTICATED" if authenticated else "MISSING",
            authenticated=authenticated,
            reachable=reachable,
            launcher=launcher,
            capabilities=frozenset(),
            runtime_source=source,
            updated_at=time.time(),
            status=status,
        )

    def identity(self, executor_id: str) -> ExecutorRuntimeIdentity:
        key = normalize_executor_id(executor_id)
        if key not in self._identities:
            self._identities[key] = self._discover(key)
        return self._identities[key]

    def register(self, identity: ExecutorRuntimeIdentity) -> None:
        self._identities[normalize_executor_id(identity.executor_id)] = identity

    def snapshot(self) -> dict[str, ExecutorRuntimeIdentity]:
        return dict(self._identities)


_DEFAULT_REGISTRY: ExecutorRegistry | None = None


def get_executor_registry() -> ExecutorRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ExecutorRegistry()
    return _DEFAULT_REGISTRY


def reset_executor_registry() -> None:
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None


__all__ = [
    "ExecutorRegistry",
    "ExecutorRuntimeIdentity",
    "get_executor_registry",
    "normalize_executor_id",
    "reset_executor_registry",
]
