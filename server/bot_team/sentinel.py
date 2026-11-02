"""server/bot_team/sentinel.py — Sentinel gate: one deterministic decision order
for every tool call (nanoMuse Sentinel method, clean rewrite; no upstream code).

The gate is a single async ToolGuard policy. It runs at both execution funnels
(MasterToolRegistry.execute and VeyaSkillHub.execute, via global_tool_guard)
because both call ``acheck``. When the flag is off it returns None and changes
nothing. When on, every call is decided in this order (steps 5-6 can only
escalate to ASK):

1. deny_tools (owner glob list) -> deny
2. explicit rules (tool glob + argument globs) -> allow/ask/deny
3. always_allow_tools / always_ask_tools -> allow/ask
4. risk x mode (ask: sensitive asks; strict: moderate+ asks)
5. taint: private data read earlier in this conversation + egress to a host
   outside the allowlist -> ask (a grant may cover it; auto mode may not wave
   it through — auto is not offered in phase 1, unknown modes read as ask)
6. warnings from the per-call assessment -> ask (only an explicit allow rule
   from step 2 may cover a warning; grants and modes may not)

Approvals are scoped capabilities bound to a tool and a target
(``web_fetch:example.com``), with five lifetimes: once / conversation /
session / 24h / always. Only 24h/always persist to disk.

Design: ~/workspace/veya-sentinel-design.md. Flag VEYA_SENTINEL, default OFF.
"""

from __future__ import annotations

import contextlib
import contextvars
import fnmatch
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_log = logging.getLogger("veya.sentinel")

SENTINEL_FLAG = "VEYA_SENTINEL"
SENTINEL_POLICY_NAME = "sentinel"

SCOPES = ("once", "conversation", "session", "24h", "always")
PERSISTED_SCOPES = ("24h", "always")
DAY_S = 24 * 3600.0

_DEFAULT_EGRESS_ALLOWLIST = (
    "github.com",
    "*.github.com",
    "pypi.org",
    "*.pypi.org",
    "wikipedia.org",
    "*.wikipedia.org",
    "duckduckgo.com",
    "*.duckduckgo.com",
)


class Risk3(StrEnum):
    SAFE = "safe"
    MODERATE = "moderate"
    SENSITIVE = "sensitive"


def _static_risk(tool: str, kwargs: dict[str, Any] | None) -> Risk3:
    """Static risk from the existing tool-name classification (R0-R4 -> 3 levels)."""
    try:
        from server.permission_profiles import RiskLevel, classify_risk

        level = classify_risk(tool, kwargs)
    except Exception:
        return Risk3.SAFE
    if level == RiskLevel.R0:
        return Risk3.SAFE
    if level in (RiskLevel.R1, RiskLevel.R2):
        return Risk3.MODERATE
    return Risk3.SENSITIVE


@dataclass
class CallAssessment:
    """Per-call risk assessment handed to the decision engine."""

    risk: Risk3 = Risk3.SAFE
    reads_private_data: bool = False
    egress: bool = False
    egress_target: str | None = None
    egress_configured: bool = False
    target: str | None = None
    summary: str = ""
    warnings: list[str] = field(default_factory=list)


# ── assessors: static risk + per-call escalation (pure, fail-open) ───────────

_SHELL_TOOLS = frozenset(
    {
        "bash",
        "run",
        "run_in_sandbox",
        "hicode_run",
        "coding_run_command",
        "coding_run_tests",
        "coding_run_lint",
        "coding_run_typecheck",
        "coding_build",
        "evolve_solution",
        "automation_run",
        "test_run",
        "system_spawn_swarm",
        "harness_sensor_run",
    }
)
_CODE_TOOLS = frozenset({"python_execute", "run_python", "code_run", "code_execute"})
_EGRESS_TOOLS = frozenset(
    {
        "fetch_url",
        "network_post",
        "http_post",
        "webhook_send",
        "send_email",
        "post_message",
        "team_send_message",
        "produce_wechat_article",
        "publish_article",
        "system_dispatch_omni_channel",
    }
)
_PRIVATE_READ_TOOLS = frozenset(
    {
        "memory_search",
        "memory_recall_project_lessons",
        "memory_get",
        "memory_explain",
        "memory_show_source",
    }
)
_PRIVATE_READ_MARKERS = ("gmail", "read_email", "contacts", "calendar", "address_book")
_BROWSER_SUBMIT_ACTIONS = frozenset({"click", "type", "upload", "download", "submit"})

_DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f\b"), "command contains rm -rf"),
    (re.compile(r"\bsudo\b"), "command uses sudo"),
    (re.compile(r"\|\s*(sh|bash|zsh|dash)\b"), "command pipes into a shell"),
    (re.compile(r"\b(env|printenv)\b"), "command reads environment variables"),
    (re.compile(r"\b(mkfs|dd\s+[^|]*\bof=/dev/)"), "destructive disk command"),
    (re.compile(r"\bchmod\s+(-R\s+)?0?777\b"), "command opens permissions wide"),
)
_NETWORK_PROGRAMS = re.compile(
    r"\b(curl|wget|ssh|scp|rsync|git|pip|pip3|npm|npx|docker|apt|apt-get)\b"
)
_CODE_RISKY = re.compile(
    r"import\s+(requests|httpx|socket|urllib|subprocess)|"
    r"from\s+(requests|httpx|socket|urllib|subprocess)\s+import|"
    r"os\.environ|os\.remove|os\.unlink|shutil\.rmtree|eval\s*\(|exec\s*\("
)


def _first_str(kwargs: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _host_of(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if "://" not in text:
        text = "https://" + text
    try:
        host = urlsplit(text).hostname
    except Exception:
        return None
    return host.lower() if host else None


def _assess_shell(tool: str, kwargs: dict[str, Any], base: CallAssessment) -> None:
    command = _first_str(kwargs, ("command", "cmd", "script", "code"))
    if command:
        first = command.strip().split()[0] if command.strip() else ""
        base.target = first or None
        for pattern, warning in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                base.warnings.append(warning)
        if _NETWORK_PROGRAMS.search(command):
            base.egress = True
            base.egress_target = None  # shell: destination unknown
            if base.risk == Risk3.SAFE:
                base.risk = Risk3.MODERATE
    if base.warnings:
        base.risk = Risk3.SENSITIVE


def _assess_code(tool: str, kwargs: dict[str, Any], base: CallAssessment) -> None:
    code = _first_str(kwargs, ("code", "script", "source"))
    if code and _CODE_RISKY.search(code):
        base.warnings.append("code touches network, environment, or deletes files")
        base.risk = Risk3.SENSITIVE
        base.egress = True
        base.egress_target = None
    elif base.risk == Risk3.SAFE:
        base.risk = Risk3.MODERATE


def _assess_egress(tool: str, kwargs: dict[str, Any], base: CallAssessment) -> None:
    base.egress = True
    url = kwargs.get("url") or kwargs.get("href") or kwargs.get("endpoint") or kwargs.get("webhook_url")
    host = _host_of(url)
    recipient = kwargs.get("to") or kwargs.get("recipient") or kwargs.get("address")
    if isinstance(recipient, (list, tuple)):
        recipient = recipient[0] if recipient else None
    if isinstance(recipient, str) and recipient.strip():
        base.target = recipient.strip()
        if "@" in recipient and host is None:
            host = recipient.strip().split("@", 1)[1].lower() or None
    channel = kwargs.get("channel")
    if base.target is None and isinstance(channel, str) and channel.strip():
        base.target = channel.strip()
    if base.target is None:
        base.target = host
    base.egress_target = host
    if tool.startswith("browser_"):
        action = str(kwargs.get("action") or "").strip().lower()
        if action and action not in _BROWSER_SUBMIT_ACTIONS:
            # Read-only browsing still fetches, but is not a submit: keep the
            # egress flag (taint still applies) without a submit target.
            base.target = host


def assess_call(tool: str, kwargs: dict[str, Any] | None) -> CallAssessment:
    """Assess one call. Never raises: on any error the static risk stands."""
    args = kwargs or {}
    try:
        base = CallAssessment(risk=_static_risk(tool, args))
        name = str(tool or "")
        if name in _SHELL_TOOLS or "shell" in name:
            _assess_shell(name, args, base)
        elif name in _CODE_TOOLS or "python" in name:
            _assess_code(name, args, base)
        if (
            name in _EGRESS_TOOLS
            or name.startswith("browser_")
            or "notify" in name
            or "webhook" in name
            or "post" in name
        ):
            _assess_egress(name, args, base)
        if name in _PRIVATE_READ_TOOLS or any(m in name for m in _PRIVATE_READ_MARKERS):
            base.reads_private_data = True
        if base.target is None:
            base.target = base.egress_target
        summary = name
        if base.target:
            summary += f" -> {base.target}"
        elif base.egress_target:
            summary += f" -> {base.egress_target}"
        base.summary = summary
        return base
    except Exception as exc:  # assessors are advisory; never fail the call
        _log.warning("sentinel assess_call failed for %r: %s", tool, exc)
        return CallAssessment(risk=Risk3.SAFE, summary=str(tool or ""))


# ── configuration (file layer, read per call like the flag chain) ────────────

@dataclass
class SentinelRule:
    tool: str
    match: dict[str, str] = field(default_factory=dict)
    action: str = "ask"
    reason: str = ""


@dataclass
class SentinelConfig:
    mode: str = "ask"
    shadow: bool = False
    deny_tools: tuple[str, ...] = ()
    rules: tuple[SentinelRule, ...] = ()
    always_allow_tools: tuple[str, ...] = ()
    always_ask_tools: tuple[str, ...] = ()
    egress_allowlist: tuple[str, ...] = _DEFAULT_EGRESS_ALLOWLIST
    taint_tracking: bool = True


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if isinstance(v, (str, int, float)))
    return ()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def load_sentinel_config(file_config: dict[str, Any] | None = None) -> SentinelConfig:
    """Parse the ``sentinel`` section of the file config layer. Defaults win
    on any malformed input: a config mistake must never lock the tool chain."""
    try:
        if file_config is None:
            file_config = _read_file_layer()
        section = file_config.get("sentinel") if isinstance(file_config, dict) else None
        if not isinstance(section, dict):
            return SentinelConfig()
        rules: list[SentinelRule] = []
        for raw in section.get("rules") or []:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action") or "ask").strip().lower()
            if action not in ("allow", "ask", "deny"):
                continue
            match = raw.get("match") or {}
            if not isinstance(match, dict):
                continue
            rules.append(
                SentinelRule(
                    tool=str(raw.get("tool") or "*"),
                    match={str(k): str(v) for k, v in match.items()},
                    action=action,
                    reason=str(raw.get("reason") or ""),
                )
            )
        return SentinelConfig(
            mode=str(section.get("mode") or "ask").strip().lower(),
            shadow=_as_bool(section.get("shadow"), False),
            deny_tools=_as_str_tuple(section.get("deny_tools")),
            rules=tuple(rules),
            always_allow_tools=_as_str_tuple(section.get("always_allow_tools")),
            always_ask_tools=_as_str_tuple(section.get("always_ask_tools")),
            egress_allowlist=_as_str_tuple(section.get("egress_allowlist"))
            or _DEFAULT_EGRESS_ALLOWLIST,
            taint_tracking=_as_bool(section.get("taint_tracking"), True),
        )
    except Exception as exc:
        _log.warning("sentinel config parse failed, using defaults: %s", exc)
        return SentinelConfig()


def _read_file_layer() -> dict[str, Any]:
    try:
        from config.authority import read_file_config

        data, _path = read_file_config()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ── decision engine (pure) ───────────────────────────────────────────────────

@dataclass
class PolicyResult:
    decision: str  # allow | ask | deny
    reasons: list[str] = field(default_factory=list)
    tainted_ask: bool = False


def _match_any(name: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def host_allowed(host: str | None, allowlist: tuple[str, ...] | list[str]) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    for pattern in allowlist:
        pattern = pattern.lower()
        if pattern == "*" or fnmatch.fnmatch(host, pattern):
            return True
        if not pattern.startswith("*") and host.endswith("." + pattern):
            return True
    return False


def _rule_matches(rule: SentinelRule, tool: str, args: dict[str, Any]) -> bool:
    if not fnmatch.fnmatch(tool, rule.tool):
        return False
    for key, pattern in rule.match.items():
        value = args.get(key, "")
        if isinstance(value, (dict, list)):
            value = str(value)
        if not fnmatch.fnmatch(str(value), pattern):
            return False
    return True


def evaluate(
    tool: str,
    kwargs: dict[str, Any] | None,
    assessment: CallAssessment,
    *,
    tainted: bool = False,
    config: SentinelConfig | None = None,
) -> PolicyResult:
    """The decision order. First hit wins for steps 1-4; steps 5-6 escalate."""
    cfg = config or SentinelConfig()
    args = kwargs or {}
    reasons: list[str] = []

    # 1. hard deny
    if _match_any(tool, cfg.deny_tools):
        return PolicyResult("deny", [f"'{tool}' is in sentinel.deny_tools"])

    decision: str | None = None
    from_rule = False
    tainted_ask = False

    # 2. explicit rules
    for rule in cfg.rules:
        if _rule_matches(rule, tool, args):
            reason = rule.reason or f"matched rule tool='{rule.tool}' match={rule.match}"
            if rule.action == "deny":
                return PolicyResult("deny", [reason])
            decision = rule.action
            from_rule = True
            reasons.append(reason)
            break

    # 3. per-tool overrides
    if decision is None:
        if _match_any(tool, cfg.always_allow_tools):
            decision, reasons = "allow", [f"'{tool}' is in always_allow_tools"]
        elif _match_any(tool, cfg.always_ask_tools):
            decision, reasons = "ask", [f"'{tool}' is in always_ask_tools"]

    # 4. risk x mode (auto is not offered in phase 1: unknown modes read as ask)
    if decision is None:
        risk = assessment.risk
        if cfg.mode == "strict":
            decision = "ask" if risk in (Risk3.MODERATE, Risk3.SENSITIVE) else "allow"
        else:
            if cfg.mode not in ("ask", "strict"):
                reasons.append(f"unknown sentinel mode '{cfg.mode}'; treated as ask")
            decision = "ask" if risk == Risk3.SENSITIVE else "allow"
        if decision == "ask":
            reasons.append(f"risk level is '{risk.value}' (mode={cfg.mode})")

    # 5. taint: escalate only
    if (
        cfg.taint_tracking
        and tainted
        and assessment.egress
        and not assessment.egress_configured
        and not host_allowed(assessment.egress_target, cfg.egress_allowlist)
    ):
        target = assessment.egress_target or "an unknown destination"
        decision = "ask"
        tainted_ask = True
        reasons.append(
            "private data was read earlier in this conversation and "
            f"'{tool}' can send data to {target}, which is not on sentinel.egress_allowlist"
        )

    # 6. warnings: escalate only; only an explicit allow rule covers them
    if assessment.warnings:
        if decision == "allow" and not from_rule:
            decision = "ask"
        reasons.extend(assessment.warnings)

    return PolicyResult(decision or "allow", reasons, tainted_ask)


# ── grants: scoped capabilities (tool + target, with lifetimes) ─────────────

def grant_key(tool: str, target: str | None) -> str:
    return f"{tool}:{target}" if target else tool


@dataclass
class Grant:
    key: str
    tool: str
    target: str | None
    scope: str
    granted_at: float
    expires_at: float | None = None
    conversation_id: str | None = None
    owner_scope: str = ""

    def active(self, now: float, conversation_id: str | None) -> bool:
        if self.scope == "conversation":
            return (
                self.conversation_id is not None
                and conversation_id is not None
                and self.conversation_id == conversation_id
            )
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return self.expires_at is None or now < self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tool": self.tool,
            "target": self.target,
            "scope": self.scope,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "conversation_id": self.conversation_id,
            "owner_scope": self.owner_scope,
        }


def _default_grants_path() -> Path:
    import os

    override = os.environ.get("VEYA_SENTINEL_GRANTS_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".veya" / "sentinel_grants.json"


class GrantStore:
    """In-memory grants plus a JSON file for the ones that outlive the process."""

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else _default_grants_path()
        self._grants: list[Grant] = []
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        now = time.time()
        loaded: list[Grant] = []
        if isinstance(data, list):
            # v1 file: a bare list of tool names that were "always allowed".
            for item in data:
                if isinstance(item, str):
                    loaded.append(
                        Grant(
                            key=item,
                            tool=item,
                            target=None,
                            scope="always",
                            granted_at=now,
                        )
                    )
        elif isinstance(data, dict):
            for item in data.get("grants", []):
                try:
                    grant = Grant(
                        key=str(item["key"]),
                        tool=str(item.get("tool") or str(item["key"]).split(":", 1)[0]),
                        target=item.get("target"),
                        scope=str(item.get("scope", "always")),
                        granted_at=float(item.get("granted_at") or now),
                        expires_at=(
                            float(item["expires_at"]) if item.get("expires_at") else None
                        ),
                        conversation_id=item.get("conversation_id"),
                        owner_scope=str(item.get("owner_scope") or ""),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if grant.scope in SCOPES:
                    loaded.append(grant)
        with self._lock:
            self._grants = [g for g in loaded if g.active(now, g.conversation_id)]

    def _save(self) -> None:
        with self._lock:
            keep = [
                g.to_dict()
                for g in self._grants
                if g.scope in PERSISTED_SCOPES and g.active(time.time(), g.conversation_id)
            ]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"version": 2, "grants": keep}, indent=1, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            _log.warning("sentinel grants save failed: %s", exc)

    def match(
        self,
        key: str,
        *,
        conversation_id: str | None,
        owner_scope: str,
        now: float | None = None,
    ) -> Grant | None:
        """Exact key first (tool:target), then the bare tool key (whole tool)."""
        now = time.time() if now is None else now
        tool = key.split(":", 1)[0]
        with self._lock:
            grants = list(self._grants)
        for candidate in (key, tool):
            for grant in grants:
                if grant.key != candidate:
                    continue
                if grant.owner_scope and grant.owner_scope != owner_scope:
                    continue
                if grant.active(now, conversation_id):
                    return grant
        return None

    def record(
        self,
        *,
        key: str,
        tool: str,
        target: str | None,
        scope: str,
        conversation_id: str | None,
        owner_scope: str,
        now: float | None = None,
    ) -> Grant | None:
        """Record an approval. ``once`` is not stored: it covers this call only."""
        if scope not in SCOPES or scope == "once":
            return None
        now = time.time() if now is None else now
        expires_at = now + DAY_S if scope == "24h" else None
        grant = Grant(
            key=key,
            tool=tool,
            target=target,
            scope=scope,
            granted_at=now,
            expires_at=expires_at,
            conversation_id=conversation_id if scope == "conversation" else None,
            owner_scope=owner_scope,
        )
        with self._lock:
            self._grants = [
                g
                for g in self._grants
                if not (
                    g.key == key and g.scope == scope and g.owner_scope == owner_scope
                )
            ]
            self._grants.append(grant)
        if scope in PERSISTED_SCOPES:
            self._save()
        return grant

    def revoke(self, key: str, *, owner_scope: str) -> bool:
        with self._lock:
            before = len(self._grants)
            self._grants = [
                g
                for g in self._grants
                if not (g.key == key and (not g.owner_scope or g.owner_scope == owner_scope))
            ]
            removed = len(self._grants) != before
        if removed:
            self._save()
        return removed

    def revoke_all(self, *, owner_scope: str) -> int:
        with self._lock:
            before = len(self._grants)
            self._grants = [
                g
                for g in self._grants
                if g.owner_scope and g.owner_scope != owner_scope
            ]
            removed = before - len(self._grants)
        if removed:
            self._save()
        return removed

    def list_grants(self, *, owner_scope: str) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            grants = list(self._grants)
        return [
            g.to_dict()
            for g in grants
            if (not g.owner_scope or g.owner_scope == owner_scope)
            and g.active(now, g.conversation_id)
        ]


_STORE_OVERRIDE: GrantStore | None = None
_GRANT_STORE_SINGLETON: GrantStore | None = None


def set_grant_store_override(store: GrantStore | None) -> None:
    global _STORE_OVERRIDE
    _STORE_OVERRIDE = store


def get_grant_store() -> GrantStore:
    global _GRANT_STORE_SINGLETON
    if _STORE_OVERRIDE is not None:
        return _STORE_OVERRIDE
    if _GRANT_STORE_SINGLETON is None:
        _GRANT_STORE_SINGLETON = GrantStore()
    return _GRANT_STORE_SINGLETON


# ── taint: per-conversation, memory only ─────────────────────────────────────

_TAINT_KEY_CTX: contextvars.ContextVar[str] = contextvars.ContextVar(
    "veya_sentinel_taint_key", default=""
)
_TAINTED: set[str] = set()
_TAINT_LOCK = threading.Lock()

_EVALUATED_CTX: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "veya_sentinel_evaluated", default=False
)


def bind_taint_context(key: str) -> contextvars.Token:
    return _TAINT_KEY_CTX.set(str(key or ""))


def reset_taint_context(token: contextvars.Token) -> None:
    _TAINT_KEY_CTX.reset(token)


def current_taint_key() -> str:
    return _TAINT_KEY_CTX.get()


def is_tainted(key: str | None = None) -> bool:
    target = key if key is not None else _TAINT_KEY_CTX.get()
    if not target:
        return False
    with _TAINT_LOCK:
        return target in _TAINTED


def mark_tainted(key: str | None = None) -> None:
    target = key if key is not None else _TAINT_KEY_CTX.get()
    if not target:
        return
    with _TAINT_LOCK:
        _TAINTED.add(target)


def clear_taint(key: str | None = None) -> None:
    target = key if key is not None else _TAINT_KEY_CTX.get()
    if not target:
        return
    with _TAINT_LOCK:
        _TAINTED.discard(target)


def sentinel_evaluated() -> bool:
    return bool(_EVALUATED_CTX.get())


def _mark_evaluated() -> None:
    _EVALUATED_CTX.set(True)


def note_execution(tool: str, kwargs: dict[str, Any] | None, *, ok: bool = True) -> None:
    """Post-execution hook: a successful private-data read taints the
    conversation. Never raises; the funnels call it best-effort."""
    if not ok:
        return
    try:
        key = _TAINT_KEY_CTX.get()
        if not key:
            return
        if assess_call(tool, kwargs).reads_private_data:
            mark_tainted(key)
    except Exception:
        pass


# ── flag + owner scope ───────────────────────────────────────────────────────

def sentinel_enabled() -> bool:
    try:
        from server.feature_flags import enabled as flag_enabled

        return bool(flag_enabled(SENTINEL_FLAG))
    except Exception:
        return False


def _owner_scope() -> str:
    try:
        from server.approvals import ApprovalService
        from server.auth import current_user

        user_id = str((current_user() or {}).get("user_id") or "anonymous")
        return ApprovalService.scope_for(user_id)
    except Exception:
        return hashlib.sha256(b"anonymous").hexdigest()[:16]


# ── the guard policy ─────────────────────────────────────────────────────────

def _grant_options(assessment: CallAssessment) -> list[str]:
    # Phase 1: only the verdicts the existing approval UI offers. The engine
    # already supports conversation/24h; they become choosable with the D6 UI.
    return ["once", "session", "always"]


def _trail_decision(
    *,
    tool: str,
    source: str,
    decision: str,
    settled_by: str,
    assessment: CallAssessment,
    reasons: list[str],
    tainted: bool,
    shadow: bool,
    scope: str = "",
    grant_key_value: str = "",
) -> None:
    with contextlib.suppress(Exception):
        from server.events import append_canonical_event

        append_canonical_event(
            "sentinel.decision",
            {
                "tool": tool,
                "source": source,
                "decision": decision,
                "settled_by": settled_by,
                "risk": assessment.risk.value,
                "reasons": [str(r)[:200] for r in reasons][:4],
                "warnings_count": len(assessment.warnings),
                "scope": scope,
                "grant_key": grant_key_value,
                "target": assessment.target,
                "tainted": bool(tainted),
                "shadow": bool(shadow),
            },
            actor="system",
        )


async def sentinel_policy(name: str, kwargs: dict, source: str) -> str | None:
    """ToolGuard policy. None = allow; non-empty str = deny reason (the guard
    turns it into ToolDenied). An ASK awaits the existing approval wait inside
    this policy, exactly like user_control_policy does today."""
    try:
        if not sentinel_enabled():
            return None
        config = load_sentinel_config()
        assessment = assess_call(name, kwargs)
        tainted = is_tainted()
        result = evaluate(name, kwargs, assessment, tainted=tainted, config=config)
        if config.shadow:
            _trail_decision(
                tool=name,
                source=source,
                decision=result.decision,
                settled_by="shadow",
                assessment=assessment,
                reasons=result.reasons,
                tainted=tainted,
                shadow=True,
            )
            return None
        _mark_evaluated()
        if result.decision == "deny":
            _trail_decision(
                tool=name,
                source=source,
                decision="deny",
                settled_by="policy",
                assessment=assessment,
                reasons=result.reasons,
                tainted=tainted,
                shadow=False,
            )
            return "; ".join(result.reasons) or f"sentinel denied '{name}'"
        if result.decision == "ask":
            key = grant_key(name, assessment.target)
            grant = None
            if not assessment.warnings:
                grant = get_grant_store().match(
                    key,
                    conversation_id=current_taint_key() or None,
                    owner_scope=_owner_scope(),
                )
            if grant is not None:
                _trail_decision(
                    tool=name,
                    source=source,
                    decision="allow",
                    settled_by="grant",
                    assessment=assessment,
                    reasons=[f"covered by your '{grant.scope}' permission for {grant.key}"],
                    tainted=tainted,
                    shadow=False,
                    scope=grant.scope,
                    grant_key_value=grant.key,
                )
                return None
            verdict_holder: dict[str, str] = {}

            def _on_verdict(verdict: str) -> None:
                verdict_holder["verdict"] = str(verdict)

            from server.user_control import _wait_approval

            sentinel_info = {
                "tool": name,
                "summary": assessment.summary,
                "risk": assessment.risk.value,
                "reasons": list(result.reasons),
                "warnings": list(assessment.warnings),
                "egress_target": assessment.egress_target,
                "target": assessment.target,
                "grant_key": key,
                "grant_options": _grant_options(assessment),
                "tainted_ask": result.tainted_ask,
            }
            wait_err = await _wait_approval(
                name, kwargs, on_verdict=_on_verdict, sentinel=sentinel_info
            )
            if wait_err is not None:
                _trail_decision(
                    tool=name,
                    source=source,
                    decision="deny",
                    settled_by="approval",
                    assessment=assessment,
                    reasons=[str(wait_err)[:200]],
                    tainted=tainted,
                    shadow=False,
                )
                return str(wait_err)
            verdict = verdict_holder.get("verdict", "")
            scope = verdict if verdict in ("session", "always") else ""
            if scope:
                get_grant_store().record(
                    key=key,
                    tool=name,
                    target=assessment.target,
                    scope=scope,
                    conversation_id=current_taint_key() or None,
                    owner_scope=_owner_scope(),
                )
            _trail_decision(
                tool=name,
                source=source,
                decision="allow",
                settled_by="approval",
                assessment=assessment,
                reasons=result.reasons,
                tainted=tainted,
                shadow=False,
                scope=scope,
                grant_key_value=key,
            )
            return None
        _trail_decision(
            tool=name,
            source=source,
            decision="allow",
            settled_by="policy",
            assessment=assessment,
            reasons=result.reasons,
            tainted=tainted,
            shadow=False,
        )
        return None
    except Exception as exc:
        # A gate bug must never break the tool chain: fail open, loudly logged.
        _log.warning("sentinel policy errored on tool %r: %s", name, exc)
        return None


def install_sentinel_policy(guard: Any = None) -> None:
    """Idempotent. Registered first so its deny/ask verdicts are authoritative;
    later policies (plan/freeze) still apply after an allow."""
    from server.tool_guard import global_tool_guard

    guard = guard or global_tool_guard
    if guard.has_policy(SENTINEL_POLICY_NAME):
        return
    guard.register_policy(SENTINEL_POLICY_NAME, sentinel_policy, enforce=True)
