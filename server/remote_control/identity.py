"""W13 machine identity — who is on the other end of this tailnet address.

Identity is resolved from the peer address with ``tailscale whois --json``.
The call itself lives in :mod:`server.remote_control.tailscale_cmd`; this
module owns the pure half — parsing, the policy file, and the decision — so the
test suite mocks a *function*, never a process and never a network.

Trust asymmetry (borrowed from Yobiru, MIT): being able to post a message never
implies permission to wake or execute. ``message`` is the only action the
tailnet default grants; ``wake`` and ``execute`` need an explicit rule.

Unknown identity → **deny**. There is no implicit-trust branch anywhere here.
"""

from __future__ import annotations

import fnmatch
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from server.remote_control import _ledger

#: Actions a peer may be permitted to take. ``message`` is deliberately weaker
#: than the other two: delivering a note to a mailbox grants nothing else.
ACTIONS: tuple[str, ...] = ("message", "wake", "execute")

#: The only action a bare ``default: "tailnet"`` grants.
TAILNET_DEFAULT_ACTION = "message"

#: An injectable command runner: argv in, stdout string out.
CommandRunner = Callable[[Sequence[str]], str]

_LEDGER_ENV = "VEYA_REMOTE_CONTROL_POLICY_PATH"
_LEDGER_FILE = "remote_control_policy.json"
_POLICY_KEY = "policy"


@dataclass(frozen=True)
class PeerIdentity:
    """A resolved tailnet peer. ``resolved=False`` means 'whoever, denied'."""

    node_name: str = ""
    fqdn: str = ""
    tags: tuple[str, ...] = ()
    user_login: str = ""
    tailnet: str = ""
    ip: str = ""
    online: bool = False
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def subject(self) -> str:
        """Stable display subject for logs and notifications."""

        if not self.resolved:
            return f"unknown({self.ip})" if self.ip else "unknown"
        return self.node_name or self.fqdn or self.ip or "peer"

    def matches(self, matcher: str) -> bool:
        """Does this peer satisfy one ``kind:value`` matcher?

        ``host:`` matches the node name, ``fqdn:`` the DNS name, ``tag:`` any
        tailnet tag, ``user:`` the login. A bare string is shorthand for
        ``host:``. Globs are allowed on ``host:`` / ``fqdn:``.
        """

        text = str(matcher or "").strip()
        if not text:
            return False
        kind, _, value = text.partition(":")
        if not value:
            kind, value = "host", kind
        value = value.lower()
        if kind == "host":
            return fnmatch.fnmatch(self.node_name.lower(), value)
        if kind == "fqdn":
            return fnmatch.fnmatch(self.fqdn.lower().rstrip("."), value.rstrip("."))
        if kind == "tag":
            # tailscale reports tags already prefixed ("tag:trusted"), and a
            # policy matcher is written the same way. Compare both sides with
            # the prefix stripped so the two spellings agree.
            wanted = value.removeprefix("tag:")
            return wanted in {tag.lower().removeprefix("tag:") for tag in self.tags}
        if kind == "user":
            return value in {self.user_login.lower(), self.user_login.lower().split("@")[0]}
        return False


@dataclass
class IdentityPolicy:
    """Local policy: who may do what. Deny always wins."""

    default: str = "deny"
    allowlist: list[str] = field(default_factory=list)
    denylist: list[str] = field(default_factory=list)
    matchers: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> IdentityPolicy:
        matchers = value.get("matchers")
        return cls(
            default=str(value.get("default") or "deny").lower(),
            allowlist=[str(x) for x in (value.get("allowlist") or [])],
            denylist=[str(x) for x in (value.get("denylist") or [])],
            matchers=[m for m in (matchers or []) if isinstance(m, dict)],
            updated_at=float(value.get("updated_at") or time.time()),
        )


def parse_whois(raw: str, *, ip: str = "") -> PeerIdentity:
    """Parse ``tailscale whois --json`` output.

    Anything unparsable, or output without a node name, resolves to an
    **unresolved** identity — which policy evaluation denies.
    """

    try:
        data = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError, ValueError):
        return PeerIdentity(ip=ip, resolved=False)
    if not isinstance(data, dict):
        return PeerIdentity(ip=ip, resolved=False)
    node = data.get("Node") or data.get("node")
    if not isinstance(node, dict):
        return PeerIdentity(ip=ip, resolved=False)
    name = str(node.get("Name") or node.get("name") or "").strip()
    if not name:
        return PeerIdentity(ip=ip, resolved=False)
    tags = node.get("Tags") or node.get("tags") or []
    user = node.get("User") or node.get("user")
    login = ""
    if isinstance(user, dict):
        login = str(user.get("LoginName") or user.get("login_name") or "")
    elif isinstance(user, str):
        login = user
    return PeerIdentity(
        node_name=name,
        fqdn=str(node.get("FQDN") or node.get("fqdn") or "").rstrip("."),
        tags=tuple(str(tag) for tag in tags),
        user_login=login or str(node.get("User") or ""),
        tailnet=str(node.get("Tailnet") or node.get("tailnet") or ""),
        ip=str(node.get("TailscaleIPs", [ip])[0] if node.get("TailscaleIPs") else ip),
        online=bool(node.get("Online", node.get("online", False))),
        resolved=True,
    )


def resolve_identity(ip: str, *, runner: CommandRunner | None = None) -> PeerIdentity:
    """Resolve one peer address. Never raises; failure is an unknown identity."""

    address = str(ip or "").strip()
    if not address:
        return PeerIdentity(resolved=False)
    run: CommandRunner = runner or _default_runner
    try:
        raw = run(("tailscale", "whois", "--json", address))
    except Exception:
        return PeerIdentity(ip=address, resolved=False)
    return parse_whois(raw, ip=address)


def _default_runner(argv: Sequence[str]) -> str:
    from server.remote_control import process

    return process.run_tailscale(argv)


def load_policy() -> IdentityPolicy:
    """Read the local policy. Missing / corrupt → deny-by-default."""

    row = _ledger.read_map(_ledger.default_path(_LEDGER_ENV, _LEDGER_FILE)).get(_POLICY_KEY)
    if row is None:
        return IdentityPolicy()
    return IdentityPolicy.from_dict(row)


def save_policy(policy: IdentityPolicy) -> IdentityPolicy:
    """Persist the policy map. Validates before writing."""

    if policy.default not in {"tailnet", "deny"}:
        raise ValueError("policy.default must be 'tailnet' or 'deny'")
    for matcher in policy.matchers:
        action = str(matcher.get("action") or "").lower()
        if action not in {"allow", "deny"}:
            raise ValueError("each matcher needs action 'allow' or 'deny'")
    policy.updated_at = time.time()

    def _mutate(rows: dict[str, dict[str, Any]]):
        rows[_POLICY_KEY] = policy.to_dict()
        return rows, policy

    return _ledger.update(_ledger.default_path(_LEDGER_ENV, _LEDGER_FILE), _mutate)


def evaluate(
    identity: PeerIdentity,
    action: str,
    *,
    policy: IdentityPolicy | None = None,
) -> tuple[bool, str]:
    """Decide ``identity``'s permission for ``action``.

    Order: explicit matchers (first match wins, deny wins inside a rule) →
    denylist → allowlist → default. **Deny wins at every level.**
    """

    if action not in ACTIONS:
        return False, f"unknown action: {action}"
    active = policy if policy is not None else load_policy()
    if not identity.resolved:
        return False, "unknown identity"

    for matcher in active.matchers:
        subject = str(matcher.get("match") or "")
        if not identity.matches(subject):
            continue
        if str(matcher.get("action") or "").lower() == "deny":
            return False, f"denied by rule {subject!r}"
        allowed_actions = matcher.get("actions")
        if allowed_actions and action not in [str(x) for x in allowed_actions]:
            return False, f"rule {subject!r} does not allow {action}"
        return True, f"allowed by rule {subject!r}"

    if any(identity.matches(item) for item in active.denylist):
        return False, "denied by denylist"
    if any(identity.matches(item) for item in active.allowlist):
        return True, "allowed by allowlist"
    if active.default == "tailnet" and action == TAILNET_DEFAULT_ACTION:
        return True, "tailnet default (message only)"
    return False, f"no rule allows {action}"


def policy_allows(identity: PeerIdentity, action: str) -> bool:
    """Boolean form of :func:`evaluate` for the call sites that need one."""

    allowed, _reason = evaluate(identity, action)
    return allowed


__all__ = [
    "ACTIONS",
    "TAILNET_DEFAULT_ACTION",
    "CommandRunner",
    "IdentityPolicy",
    "PeerIdentity",
    "evaluate",
    "load_policy",
    "parse_whois",
    "policy_allows",
    "resolve_identity",
    "save_policy",
]
