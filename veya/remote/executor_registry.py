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
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from veya.executor_retirement import is_retired_executor as is_retired
from veya.remote import credential_structure as _credential_structure
from veya.remote.credential_probe import CredentialProbeCache, ProbeResult

if TYPE_CHECKING:  # provider state stays in ProviderRegistry; imported for typing only
    from veya.remote.provider_registry import ProviderRecord
from veya.obase import canonical_proxies as _cp

_ALIASES = {
    "agy": "antigravity",
    "antigravity": "antigravity",
    "open-code": "opencode",
    "opencode_go": "opencode",
    "claude-code": "claude_code",
    "claude_code": "claude_code",
}
# The canonical admission set. Every executor a runtime consumer may use belongs
# here, explicitly — it used to be reached instead by calling identity() on an
# undeclared name, which discovered it into the registry as a side effect of an
# unrelated lookup. grok was the case that leak was hiding.
_KNOWN = (
    "pi",
    "codex",
    "antigravity",
    "opencode",
    "claude_code",
    "dsh",
    "grok",
    "acp",
)
# Canonical routing preference for admitted executors. This is ordering only;
# membership stays with _KNOWN and register().
_CANONICAL_ORDER = (
    "antigravity",
    "opencode",
    "claude_code",
    "pi",
    "grok",
    "dsh",
    "codex",
)


def normalize_executor_id(value: str) -> str:
    key = str(value or "").strip().lower()
    return _ALIASES.get(key, key)


class ExecutorAvailability(StrEnum):
    """Lifecycle state of an executor as an *inventory* concern."""

    REGISTERED = "REGISTERED"
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    DISABLED = "DISABLED"
    LEGACY = "LEGACY"


#: The registry used to report READY / DEGRADED / UNKNOWN. Those words are
#: mapped here, once, onto the availability vocabulary the contract names, so
#: no caller has to know which generation of the enum it is reading.
_AVAILABILITY_FROM_STATUS: dict[str, str] = {
    "READY": str(ExecutorAvailability.AVAILABLE),
    "DEGRADED": str(ExecutorAvailability.DEGRADED),
    "UNAVAILABLE": str(ExecutorAvailability.UNAVAILABLE),
    "DISABLED": str(ExecutorAvailability.DISABLED),
    "REGISTERED": str(ExecutorAvailability.REGISTERED),
    "UNKNOWN": str(ExecutorAvailability.REGISTERED),
    "LEGACY": str(ExecutorAvailability.LEGACY),
}


def availability_for(status: str) -> str:
    """Normalise a discovery status into the availability vocabulary."""

    return _AVAILABILITY_FROM_STATUS.get(
        str(status or "UNKNOWN").upper(), str(ExecutorAvailability.REGISTERED)
    )


@dataclass(frozen=True)
class ExecutorIdentity:
    """Who the executor is. Static; says nothing about whether it may run."""

    name: str
    kind: str
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class ExecutorCapability:
    """What the executor can do.

    Kept separate from identity and runtime state on purpose: an executor can
    exist and be registered while having no capability record at all, which is
    exactly the state ``acp`` is in. Merging the two would make "unknown" and
    "none" indistinguishable.
    """

    supported_operations: frozenset[str] = frozenset()
    capabilities: frozenset[str] = frozenset()
    write_qualified: bool = False

    @property
    def selectable(self) -> bool:
        """An executor is selectable only once a capability record exists."""

        return bool(self.capabilities or self.supported_operations)


@dataclass(frozen=True)
class ExecutorRuntimeState:
    """How the executor is doing right now. Never holds credentials."""

    availability: str = str(ExecutorAvailability.REGISTERED)
    health: str = "UNKNOWN"
    failure_state: str | None = None
    last_seen: float = 0.0
    auth_state: str = "UNKNOWN"
    authenticated: bool = False
    reachable: bool = False
    launcher: str | None = None
    runtime_source: str = "unknown"
    #: Projected from the identity; see ExecutorRuntimeIdentity for why these
    #: are separate from ``authenticated``.
    credential_present: bool = False
    credential_valid: bool | None = None


@dataclass(frozen=True)
class ExecutorPolicy:
    """Routing preference. An input to selection, never an override of it."""

    priority: int = 0


@dataclass(frozen=True)
class ExecutorRuntimeIdentity:
    """Composed view of an executor across the four concerns.

    The components stay separate so that no single field can quietly become an
    authority for a question it does not answer: identity never decides
    availability, and policy never decides capability. This object only
    *presents* them together for callers that need a flat record.
    """

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
    supported_operations: frozenset[str] = frozenset()
    write_qualified: bool = False
    health: str = "UNKNOWN"
    failure_state: str | None = None
    priority: int = 0
    provider_capabilities: frozenset[str] = frozenset()
    #: Whether credential *material* exists — an env var or a credentials file.
    #: This is what ``_credential_present`` answers and it is reliable, but it
    #: says nothing about whether the credential still works.
    credential_present: bool = False
    #: Whether the credential was *proven* by a real authenticated call.
    #: ``None`` means not probed, which is not the same as False and not the
    #: same as True. Measured on 2026-10-04: three of the four executors
    #: reporting ``authenticated=True`` failed on real contact, so a presence
    #: check presented as authentication is a lie this field exists to prevent.
    credential_valid: bool | None = None

    # ── provider reference, kept as a request rather than as state ──
    def provider_record(self) -> ProviderRecord:
        """The provider this executor names, as a ProviderRegistry record.

        A *name* is a reference and belongs here. The provider's health, quota
        and auth state do not: they are reached through ``request()`` so a
        caller cannot read a stale copy off the executor instead.
        """

        from veya.remote.provider_registry import get_provider_registry

        if not self.provider:
            raise ValueError(f"executor {self.executor_id!r} names no provider")
        return get_provider_registry().identity(self.provider)

    def provider_request(self) -> tuple[ProviderRecord, ...]:
        """Providers satisfying what this executor needs, in canonical order.

        An empty ``provider_capabilities`` means "no requirement recorded", not
        "anything goes": an executor that has not declared a requirement must
        not be handed a provider list that implies one.
        """

        from veya.remote.provider_registry import get_provider_registry

        registry = get_provider_registry()
        if not self.provider_capabilities:
            return ()
        matched: dict[str, Any] = {}
        for capability in sorted(self.provider_capabilities):
            for record in registry.request(capability):
                matched.setdefault(record.name, record)
        return tuple(matched[name] for name in registry.ordered_ids() if name in matched)

    # ── layered projections ──
    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            name=self.executor_id,
            kind=self.executor_kind,
            provider=self.provider,
            model=self.model,
        )

    @property
    def credential_proven(self) -> bool:
        """True only when a real authenticated call proved the credential.

        The distinction matters because ``authenticated`` is currently a
        presence projection: on 2026-10-04, pi, codex and claude_code all
        reported authenticated while failing on real contact. Consumers that
        need "can this actually authenticate" must ask this instead.
        """
        return self.credential_valid is True

    @property
    def capability(self) -> ExecutorCapability:
        return ExecutorCapability(
            supported_operations=self.supported_operations,
            capabilities=self.capabilities,
            write_qualified=self.write_qualified,
        )

    @property
    def runtime(self) -> ExecutorRuntimeState:
        return ExecutorRuntimeState(
            availability=self.status,
            health=self.health,
            failure_state=self.failure_state,
            last_seen=self.updated_at,
            auth_state=self.auth_state,
            authenticated=self.authenticated,
            reachable=self.reachable,
            launcher=self.launcher,
            runtime_source=self.runtime_source,
            credential_present=self.credential_present,
            credential_valid=self.credential_valid,
        )

    @property
    def policy(self) -> ExecutorPolicy:
        return ExecutorPolicy(priority=self.priority)

    @property
    def availability(self) -> str:
        """Availability in the contract vocabulary.

        Normalised on read rather than stored, so the registry keeps the status
        wording the rest of the system already compares against while callers
        that want REGISTERED/AVAILABLE/DEGRADED/... get one stable vocabulary.
        """

        return availability_for(self.status)

    @property
    def selectable(self) -> bool:
        """Capability recorded *and* availability permits selection.

        Registered is not selectable, and available is not selectable without a
        capability record: ``acp`` is the case that keeps these apart. Runtime
        health is deliberately not consulted here — that is the health
        registry's input to selection, not a property of the identity.
        """

        if not self.capability.selectable:
            return False
        return self.availability not in {
            str(ExecutorAvailability.UNAVAILABLE),
            str(ExecutorAvailability.DISABLED),
            str(ExecutorAvailability.LEGACY),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "executor_kind": self.executor_kind,
            "provider": self.provider,
            "model": self.model,
            "auth_state": self.auth_state,
            "credential_present": self.credential_present,
            "credential_valid": self.credential_valid,
            "credential_proven": self.credential_proven,
            "authenticated": self.authenticated,
            "reachable": self.reachable,
            "launcher": self.launcher,
            "capabilities": sorted(self.capabilities),
            "supported_operations": sorted(self.supported_operations),
            "runtime_source": self.runtime_source,
            "updated_at": self.updated_at,
            "status": self.status,
            "health": self.health,
            "failure_state": self.failure_state,
            "priority": self.priority,
            "write_qualified": self.write_qualified,
            "selectable": self.selectable,
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
        "claude_code": "VEYA_CLAUDE_BIN",
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


# What each executor needs FROM a provider. Deliberately a requirement and not
# an observation: nothing here records whether the provider answered, is
# authenticated, or has quota left. Those live in ProviderRegistry (P5.1).
_PROVIDER_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "opencode": ("text", "stream"),
    "claude_code": ("text", "stream", "tool_use"),
    "codex": ("text", "stream", "tool_use"),
    "antigravity": ("text", "stream", "tool_use"),
    "pi": ("text", "stream"),
    "dsh": ("text",),
    "grok": ("text", "stream"),
    "acp": (),
}


#: auth_state as a function of probe validity. None leaves whatever local
#: evidence already established untouched, because "no probe" is not a finding.
_AUTH_STATE_FOR_VALIDITY: dict[bool | None, str] = {
    True: "AUTHENTICATED",
    False: "INVALID",
}


@dataclass
class ExecutorRegistry:
    """Canonical identity authority; adapters receive projections from here."""

    overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    _identities: dict[str, ExecutorRuntimeIdentity] = field(default_factory=dict, init=False)
    _probes: CredentialProbeCache = field(default_factory=CredentialProbeCache, init=False)

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
        if is_retired(executor_id):
            raise ValueError(f"Executor retired: {executor_id!r}")
        if executor_id == "pi":
            provider, model, _configured, source = _pi_config()
            auth_env = ["PI_API_KEY", "ANTHROPIC_API_KEY"]
            auth = _credential_present(
                auth_env,
                [Path("~/.pi/agent/auth.json").expanduser()],
            )
            launcher = _configured_launcher(executor_id) or _launcher(["pi"])
        else:
            contract = _cp.executor_contract(executor_id)
            env_prefix = {
                "antigravity": "ANTIGRAVITY",
                "codex": "CODEX",
                "dsh": "DSH",
                "opencode": "OPENCODE",
                "claude_code": "CLAUDE_CODE",
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
            auth_env = [str(item) for item in contract.get("auth_env", ())]
            auth = _credential_present(
                auth_env,
                _credential_structure.CREDENTIAL_FILES.get(executor_id, []),
            )
            bins = [str(item) for item in contract.get("bins", ())]
            bins.extend(
                {
                    "codex": ["codex"],
                    "opencode": ["opencode"],
                    "claude_code": ["claude"],
                    "dsh": ["dsh"],
                }.get(executor_id, [])
            )
            launcher = _configured_launcher(executor_id) or _launcher(bins)
        reachable = launcher is not None
        present = bool(auth)
        authenticated = present
        status = (
            "READY" if reachable and authenticated else "DEGRADED" if reachable else "UNAVAILABLE"
        )
        # capability and priority are *inputs* read from their own authorities;
        # this function only projects them, it never decides them.
        from veya.remote.worker_runtime import WORKER_CAPABILITIES

        record = WORKER_CAPABILITIES.get(executor_id)
        # WorkerCapabilities is a set of booleans; the operation vocabulary the
        # registry publishes is derived from it rather than restated, so the
        # two cannot disagree about what an executor supports.
        supported = (
            frozenset(name for name, value in asdict(record).items() if value is True)
            if record is not None
            else frozenset()
        )
        capabilities = supported
        write_qualified = bool(record.supports_write_task) if record is not None else False
        order = (
            _CANONICAL_ORDER.index(executor_id)
            if executor_id in _CANONICAL_ORDER
            else len(_CANONICAL_ORDER)
        )
        inspection = _credential_structure.inspect(executor_id, auth_env)
        present = inspection.present
        authenticated = present
        # Settled locally, no network. A declared source that exists but holds
        # nothing usable cannot authenticate, so validity is False on evidence.
        # Everything else stays None: material never probed needs a real call,
        # and no declared source at all means the path is simply unmodelled —
        # antigravity completes real tasks with no credential source this
        # inspection knows about, so absent is not evidence of anything.
        credential_valid = False if inspection.structurally_unusable else None
        return ExecutorRuntimeIdentity(
            executor_id=executor_id,
            executor_kind="l1_worker",
            provider=provider,
            model=model,
            provider_capabilities=frozenset(_PROVIDER_REQUIREMENTS.get(executor_id, ())),
            auth_state="AUTHENTICATED" if authenticated else "MISSING",
            # Present is observed; valid is settled only when local evidence is
            # decisive. None means material exists but no probe has run.
            credential_present=present,
            credential_valid=credential_valid,
            authenticated=authenticated,
            reachable=reachable,
            launcher=launcher,
            capabilities=capabilities,
            runtime_source=source,
            updated_at=time.time(),
            status=status,
            supported_operations=supported,
            write_qualified=write_qualified,
            # health is UNKNOWN until evidence arrives; a fresh discovery must
            # not look healthy just because it exists.
            health="UNKNOWN",
            failure_state=None,
            priority=order,
        )

    def identity(self, executor_id: str) -> ExecutorRuntimeIdentity:
        """Look up an admitted executor identity. Read-only.

        This is an admission *lookup*, never a discovery hook. An unregistered
        name raises instead of being discovered into ``_identities``, so reading
        an identity can never widen the admission surface that ``snapshot()``
        reports. ``register()`` is the only way to add an executor.
        """
        key = normalize_executor_id(executor_id)
        if is_retired(key):
            raise ValueError(f"Executor retired: {key}")
        identity = self._identities.get(key)
        if identity is None:
            raise ValueError(f"unknown executor: {key!r} is not registered")
        return identity

    def probe_credential(self, executor_id: str, *, force: bool = False) -> ProbeResult:
        """Establish a credential verdict by real probe, or report why not.

        Deliberately not called during discovery: the real calls measured on
        2026-10-04 run 12.7s to 217.7s, and an import-time probe would make
        process start depend on network weather. Callers that need certainty —
        selection before failover, a preflight before a long run — ask for it
        here and get a cached answer on every call after the first.

        A provider with no registered probe returns UNPROBABLE and leaves
        ``credential_valid`` at None, so an unverified endpoint can never mark a
        working credential invalid.
        """
        key = normalize_executor_id(executor_id)
        identity = self._identities.get(key)
        if identity is None:
            raise ValueError(f"unknown executor: {key!r} is not registered")
        result = self._probes.probe(key, force=force)
        # Only a finding may move the verdict. UNPROBABLE and UNKNOWN carry no
        # information, so applying them would erase a local refutation: probing
        # pi and learning nothing would have turned a structural False back into
        # the unknown that P2a just removed. A completed successful call does
        # override it, because that is stronger evidence than reading a file.
        if result.is_valid or result.is_negative:
            validity = self._probes.credential_valid(key)
            self._identities[key] = replace(
                identity,
                credential_valid=validity,
                auth_state=_AUTH_STATE_FOR_VALIDITY.get(validity, identity.auth_state),
            )
        return result

    def register(self, identity: ExecutorRuntimeIdentity) -> None:
        """Admit an executor.

        This is the only way an executor enters the registry, so the retirement
        guard belongs here. ``_discover`` and ``identity()`` also refuse retired
        names, but without this check a retired executor could be injected
        through the very path that is supposed to be authoritative.
        """

        key = normalize_executor_id(identity.executor_id)
        if is_retired(key):
            raise ValueError(f"Executor retired: {key}")
        self._identities[key] = identity

    def snapshot(self) -> dict[str, ExecutorRuntimeIdentity]:
        return dict(self._identities)

    def ordered_ids(self) -> tuple[str, ...]:
        """Canonical routing order for admitted executors.

        Admission membership comes from ``_KNOWN``/``register()``; the *order*
        is explicit here because discovery sequence is not routing preference.
        Executors absent from ``_CANONICAL_ORDER`` follow in admission sequence,
        so newly registered executors remain visible to consumers.
        """
        admitted = self.snapshot()
        ordered = [x for x in _CANONICAL_ORDER if x in admitted]
        ordered.extend(x for x in admitted if x not in _CANONICAL_ORDER)
        return tuple(ordered)


_DEFAULT_REGISTRY: ExecutorRegistry | None = None


def get_executor_registry() -> ExecutorRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ExecutorRegistry()
    return _DEFAULT_REGISTRY


def reset_executor_registry() -> None:
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None


def is_retired_executor(executor_id: str) -> bool:
    """Delegate to the neutral retirement policy, normalizing aliases first."""

    return is_retired(normalize_executor_id(executor_id))


__all__ = [
    "ExecutorRegistry",
    "ExecutorRuntimeIdentity",
    "get_executor_registry",
    "is_retired_executor",
    "normalize_executor_id",
    "reset_executor_registry",
]
