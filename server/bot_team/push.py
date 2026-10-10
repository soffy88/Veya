"""Web Push delivery for the Lorca bot team (P5).

A bot team is something you leave running: a turn, a group round or a routine
finishing while the browser tab is closed is exactly the case a home-screen
install is for. This module carries that notification out of the tab.

Three pieces live here, deliberately in one place because they share one root
directory and one failure story:

* **VAPID keys** — generated once, persisted under ``~/.veya/push/vapid.json``
  (``VEYA_PUSH_ROOT`` overrides), mode ``0600``, never committed. The public
  half is what the browser needs to subscribe.
* **Subscriptions** — one JSON file, one record per browser per user, so a
  user with a phone and a laptop gets two pushes and can drop either one.
* **Delivery** — :func:`default_sender` calls ``pywebpush`` when it is
  installed (``pip install 'veya[push]'``) and otherwise logs one clear line
  and skips. The sender is injectable, so the hook is testable without a push
  service.

The hook itself is *not* an edit to GoalRun: a bot turn only announces through
``server.notification_center`` (see ``WEB_PUSH_FLAG``), and ``server/routes/
push.py`` registers this module as an observer at import time. Anything that
must not become a push therefore only has to stop setting the flag.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from server.bot_team.conversations import atomic_write_json
from server.notification_center import WEB_PUSH_FLAG, global_notifier

_logg = logging.getLogger("veya.bot_team.push")

_VAPID_FILE = "vapid.json"
_SUBSCRIPTIONS_FILE = "subscriptions.json"

#: A push service requires a contact address in the VAPID subject; browsers and
#: push services silently drop requests that do not carry one.
DEFAULT_VAPID_SUBJECT = "mailto:veya@localhost"

#: How long a push request is valid at the push service. Short on purpose: a
#: notification nobody reads is not worth a retry.
_PUSH_TTL = 600

# ── NIST P-256 (secp256r1) constants ─────────────────────────────────────
# Only used by the dependency-free key generator below; they are the public
# curve parameters, not a secret.
_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_P256_A = -3
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
_P256_G = (_P256_GX, _P256_GY)


def default_push_root() -> Path:
    """Root of the push directory (``VEYA_PUSH_ROOT`` overrides it)."""

    raw = os.environ.get("VEYA_PUSH_ROOT") or str(Path.home() / ".veya" / "push")
    return Path(raw).expanduser()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padded = str(value) + "=" * (-len(str(value)) % 4)
    return base64.urlsafe_b64decode(padded)


# ── VAPID key pair ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class VapidKeys:
    """A VAPID key pair in the wire format browsers and pywebpush expect."""

    private_key: str
    public_key: str
    subject: str = DEFAULT_VAPID_SUBJECT
    created_at: float = 0.0

    def to_public_dict(self) -> dict[str, Any]:
        return {"public_key": self.public_key, "subject": self.subject}


def _inv_mod(value: int, modulus: int = _P256_P) -> int:
    return pow(int(value), -1, modulus)


def _point_add(
    left: tuple[int, int] | None, right: tuple[int, int] | None
) -> tuple[int, int] | None:
    """Add two affine points on P-256. ``None`` is the point at infinity."""

    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2 and (y1 + y2) % _P256_P == 0:
        return None
    if left == right:
        slope = (3 * x1 * x1 + _P256_A) * _inv_mod(2 * y1) % _P256_P
    else:
        slope = (y2 - y1) * _inv_mod(x2 - x1) % _P256_P
    x3 = (slope * slope - x1 - x2) % _P256_P
    y3 = (slope * (x1 - x3) - y1) % _P256_P
    return (x3, y3)


def _scalar_mult(scalar: int, point: tuple[int, int] | None = _P256_G) -> tuple[int, int] | None:
    result: tuple[int, int] | None = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def on_p256_curve(x: int, y: int) -> bool:
    """True when ``(x, y)`` satisfies the P-256 curve equation."""

    return (
        y * y
        - (
            x * x * x
            + _P256_A * x
            + 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
        )
    ) % _P256_P == 0


def _stdlib_vapid_pair() -> tuple[str, str]:
    """Generate a P-256 key pair with the standard library only.

    ``cryptography`` is the preferred path (it is in ``deploy/requirements.txt``),
    but generating a key pair is scalar multiplication on a public curve, so a
    deployment without it still gets a usable pair instead of a dead push
    surface. The private scalar comes from ``secrets`` (CSPRNG).
    """

    while True:
        private_value = secrets.randbelow(_P256_N - 1) + 1
        public = _scalar_mult(private_value)
        if public is None:  # pragma: no cover — negligibe for a random scalar
            continue
        x, y = public
        return (
            _b64url(private_value.to_bytes(32, "big")),
            _b64url(b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")),
        )


def generate_vapid_pair() -> tuple[str, str]:
    """A fresh VAPID key pair as ``(private_key, public_key)``."""

    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        return _stdlib_vapid_pair()
    key = ec.generate_private_key(ec.SECP256R1())
    numbers = key.private_numbers()
    private_value = numbers.private_value
    public_numbers = numbers.public_numbers
    return (
        _b64url(private_value.to_bytes(32, "big")),
        _b64url(
            b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
        ),
    )


def _write_private_json(path: Path, payload: Any) -> None:
    """Write a secret atomically with owner-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class VapidKeyManager:
    """Load the VAPID key pair, generating and storing it on first use.

    A root given here wins forever; without one the root is re-read from
    ``VEYA_PUSH_ROOT`` on every operation. Binding it at construction time would
    freeze whatever the environment happened to be when the manager was built —
    which for the process-wide notifier (see ``get_push_notifier``) means import
    time — so a deployment that points the env var at another directory would
    silently keep generating its pair in the first one it ever saw.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_push_root()

    @property
    def path(self) -> Path:
        return self.root / _VAPID_FILE

    def load(self) -> VapidKeys | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if not isinstance(raw, dict) or not raw.get("private_key") or not raw.get("public_key"):
            return None
        return VapidKeys(
            private_key=str(raw["private_key"]),
            public_key=str(raw["public_key"]),
            subject=str(raw.get("subject") or DEFAULT_VAPID_SUBJECT),
            created_at=float(raw.get("created_at") or 0.0),
        )

    def load_or_create(self) -> VapidKeys:
        """The stored pair, or a new one persisted before it is handed out."""

        with self._lock:
            existing = self.load()
            if existing is not None:
                return existing
            private_key, public_key = generate_vapid_pair()
            keys = VapidKeys(
                private_key=private_key,
                public_key=public_key,
                subject=str(os.environ.get("VEYA_VAPID_SUBJECT") or DEFAULT_VAPID_SUBJECT),
                created_at=time.time(),
            )
            _write_private_json(
                self.path,
                {
                    "private_key": keys.private_key,
                    "public_key": keys.public_key,
                    "subject": keys.subject,
                    "created_at": keys.created_at,
                },
            )
            return keys


# ── subscriptions ────────────────────────────────────────────────────────


def subscription_id(endpoint: str) -> str:
    """A stable, opaquely derived id for one push endpoint."""

    return hashlib.sha256(str(endpoint).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class PushSubscription:
    """One browser's push endpoint for one user."""

    id: str
    endpoint: str
    p256dh: str
    auth: str
    user_id: str
    created_at: float
    expiration_time: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "endpoint": self.endpoint,
            "p256dh": self.p256dh,
            "auth": self.auth,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "expiration_time": self.expiration_time,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, user_id: str = "") -> PushSubscription:
        endpoint = str(value.get("endpoint") or "")
        return cls(
            id=str(value.get("id") or subscription_id(endpoint)),
            endpoint=endpoint,
            p256dh=str(value.get("p256dh") or ""),
            auth=str(value.get("auth") or ""),
            user_id=str(value.get("user_id") or user_id),
            created_at=float(value.get("created_at") or 0.0),
            expiration_time=(
                int(value["expiration_time"]) if value.get("expiration_time") else None
            ),
        )


def _validate_subscription(endpoint: str, p256dh: str, auth: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("subscription endpoint must be an http(s) URL")
    for name, value in (("p256dh", p256dh), ("auth", auth)):
        try:
            raw = _b64url_decode(value)
        except Exception as exc:
            raise ValueError(f"{name} must be base64url") from exc
        if not raw:
            raise ValueError(f"{name} must not be empty")


class SubscriptionStore:
    """Push subscriptions, one JSON file, filtered per user."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root) if root is not None else None
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else default_push_root()

    @property
    def path(self) -> Path:
        return self.root / _SUBSCRIPTIONS_FILE

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        items = raw.get("subscriptions")
        if not isinstance(items, dict):
            return {}
        return {str(key): value for key, value in items.items() if isinstance(value, dict)}

    def _save(self, items: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(self.path, {"subscriptions": items})

    def list(self, user_id: str | None = None) -> list[PushSubscription]:
        """Stored subscriptions, optionally only one user's."""

        wanted = None if user_id is None else str(user_id)
        out: list[PushSubscription] = []
        for value in self._load().values():
            record = PushSubscription.from_dict(value)
            if wanted is not None and record.user_id != wanted:
                continue
            out.append(record)
        return out

    def get(self, subscription_id: str, *, user_id: str | None = None) -> PushSubscription | None:
        wanted = None if user_id is None else str(user_id)
        raw = self._load().get(str(subscription_id))
        if raw is None:
            return None
        record = PushSubscription.from_dict(raw)
        if wanted is not None and record.user_id != wanted:
            return None
        return record

    def upsert(
        self,
        *,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_id: str,
        expiration_time: int | None = None,
    ) -> PushSubscription:
        """Register (or refresh) one subscription. Same endpoint → same id."""

        _validate_subscription(endpoint, p256dh, auth)
        endpoint = str(endpoint).strip()
        record = PushSubscription(
            id=subscription_id(endpoint),
            endpoint=endpoint,
            p256dh=str(p256dh).strip(),
            auth=str(auth).strip(),
            user_id=str(user_id),
            created_at=time.time(),
            expiration_time=expiration_time,
        )
        with self._lock:
            items = self._load()
            items[record.id] = record.to_dict()
            self._save(items)
        return record

    def delete(self, subscription_id: str, *, user_id: str | None = None) -> bool:
        wanted = None if user_id is None else str(user_id)
        with self._lock:
            items = self._load()
            current = items.get(str(subscription_id))
            if current is None:
                return False
            if wanted is not None and str(current.get("user_id") or "") != wanted:
                return False
            del items[str(subscription_id)]
            self._save(items)
            return True

    def delete_endpoint(self, endpoint: str, *, user_id: str | None = None) -> bool:
        return self.delete(subscription_id(endpoint), user_id=user_id)


# ── payload + delivery ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PushPayload:
    """What a notification looks like on the device."""

    title: str
    body: str
    url: str = "/"
    tag: str = ""
    kind: str = "bot_turn"
    ttl: int = _PUSH_TTL

    def to_json(self) -> str:
        return json.dumps(
            {
                "title": self.title,
                "body": self.body,
                "url": self.url,
                "tag": self.tag,
                "kind": self.kind,
            },
            ensure_ascii=False,
        )


#: A sender turns one payload for one subscription into ``(delivered, detail)``.
PushSender = Callable[[PushPayload, PushSubscription, VapidKeys], "tuple[bool, str]"]


def _webpush_module() -> Any:
    try:
        from pywebpush import webpush
    except ImportError:
        return None
    return webpush


def webpush_available() -> bool:
    """True when ``pywebpush`` is importable, i.e. sending can actually happen."""

    return _webpush_module() is not None


def default_sender(
    payload: PushPayload, subscription: PushSubscription, vapid: VapidKeys
) -> tuple[bool, str]:
    """Send one payload through ``pywebpush``, or explain why it did not."""

    webpush = _webpush_module()
    if webpush is None:
        return False, (
            "pywebpush is not installed — install the 'veya[push]' extra to enable "
            "Web Push delivery; the notification was dropped"
        )
    try:
        from pywebpush import WebPushException
    except ImportError:  # pragma: no cover — pywebpush always ships this name
        WebPushException = Exception  # type: ignore[assignment,misc]
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=payload.to_json(),
            vapid_private_key=vapid.private_key,
            vapid_claims={"sub": vapid.subject},
            ttl=payload.ttl,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = str(getattr(exc, "message", exc)) or exc.__class__.__name__
        # 404/410 is the push service saying "this browser is gone": the caller
        # should drop the subscription rather than retry it forever.
        return False, f"push service refused (status={status}): {detail}"
    except Exception as exc:
        return False, f"push send failed: {exc}"
    return True, "delivered"


@dataclass
class PushNotifier:
    """Turns flagged notifications into device pushes.

    ``server/routes/push.py`` calls :meth:`install` once at import time. From
    then on every ``notification_center`` message carrying ``WEB_PUSH_FLAG`` is
    fanned out to that user's subscriptions; everything else is untouched, so
    the existing toast stream keeps its exact behaviour.
    """

    store: SubscriptionStore
    vapid: VapidKeyManager
    sender: PushSender = field(default=default_sender)
    log: logging.Logger = field(default=_logg)

    def __init__(
        self,
        *,
        store: SubscriptionStore | None = None,
        vapid: VapidKeyManager | None = None,
        sender: PushSender | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.store = store if store is not None else SubscriptionStore()
        self.vapid = vapid if vapid is not None else VapidKeyManager()
        self.sender = sender if sender is not None else default_sender
        self.log = log if log is not None else _logg
        self._installed = False

    # ── hook ─────────────────────────────────────────────────────────────
    def install(self) -> None:
        """Subscribe to the notification centre (idempotent)."""

        if self._installed:
            global_notifier.remove_push_observer(self.observe)
        global_notifier.add_push_observer(self.observe)
        self._installed = True

    def uninstall(self) -> None:
        if self._installed:
            global_notifier.remove_push_observer(self.observe)
        self._installed = False

    def observe(self, message: dict[str, Any]) -> None:
        """Observer contract: only flagged messages leave the browser."""

        if not isinstance(message, dict):
            return
        payload = message.get("payload") or {}
        if not isinstance(payload, dict) or not payload.get(WEB_PUSH_FLAG):
            return
        title = str(message.get("title") or "").strip()
        if not title:
            return
        push = PushPayload(
            title=title,
            body=str(message.get("content") or "").strip(),
            url=str(payload.get("url") or "/"),
            tag=str(payload.get("tag") or ""),
            kind=str(payload.get("kind") or "bot_turn"),
        )
        self.send(user_id=str(message.get("user_id") or ""), payload=push)

    # ── delivery ─────────────────────────────────────────────────────────
    def send(self, *, user_id: str, payload: PushPayload) -> dict[str, Any]:
        """Deliver ``payload`` to every subscription of ``user_id``."""

        subscriptions = self.store.list(user_id or None)
        if not subscriptions:
            return {"sent": 0, "skipped": 0, "subscriptions": 0, "results": []}
        if self.sender is default_sender and not webpush_available():
            # One line, naming the fix. This is the documented degradation, and
            # it is only reached through the real sender: an injected one is
            # the caller's own transport decision and must not be second-guessed.
            self.log.warning(
                "web push skipped for %d subscription(s): pywebpush is not installed "
                "(install the 'veya[push]' extra); notification=%r",
                len(subscriptions),
                payload.title,
            )
            return {
                "sent": 0,
                "skipped": len(subscriptions),
                "subscriptions": len(subscriptions),
                "results": [],
            }
        vapid = self.vapid.load_or_create()
        results: list[dict[str, Any]] = []
        sent = 0
        for subscription in subscriptions:
            delivered, detail = self.sender(payload, subscription, vapid)
            if delivered:
                sent += 1
            else:
                self.log.warning("web push to %s failed: %s", subscription.id, detail)
            results.append(
                {"subscription_id": subscription.id, "delivered": delivered, "detail": detail}
            )
            if not delivered and ("status=410" in detail or "status=404" in detail):
                # The browser is gone; never retry a dead endpoint.
                self.store.delete(subscription.id)
        return {
            "sent": sent,
            "skipped": len(subscriptions) - sent,
            "subscriptions": len(subscriptions),
            "results": results,
        }

    def describe(self) -> dict[str, Any]:
        """Status for the public endpoint: can this deployment push at all?"""

        return {
            "enabled": webpush_available(),
            "sender": "pywebpush" if webpush_available() else "skipped",
            "subscriptions": len(self.store.list()),
            "hint": (
                None
                if webpush_available()
                else "install the 'veya[push]' extra (pywebpush) to enable Web Push"
            ),
        }


_notifier: PushNotifier | None = None


def get_push_notifier() -> PushNotifier:
    """The process-wide notifier bound to the default push root."""

    global _notifier
    if _notifier is None:
        _notifier = PushNotifier()
    return _notifier


def install_push_hook(notifier: PushNotifier | None = None) -> PushNotifier:
    """Register the push hook on the notification centre."""

    target = notifier if notifier is not None else get_push_notifier()
    target.install()
    return target


# ── announcements ────────────────────────────────────────────────────────


def first_line(text: str, limit: int = 140) -> str:
    """The first non-empty line of a reply, clamped for a notification body."""

    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def announce_bot_reply(
    *,
    bot_name: str,
    reply: str,
    task_id: str,
    user_id: str,
    bot_id: str = "",
) -> str:
    """Announce one finished bot turn: ``<bot name>`` + its first line."""

    return global_notifier.push(
        "SUCCESS",
        bot_name or "Bot",
        first_line(reply) or "(no reply)",
        payload={
            "url": f"/team/bot/{bot_id}" if bot_id else "/team",
            "tag": f"bot:{bot_id}" if bot_id else "bot",
            "kind": "bot_turn",
            "bot_id": bot_id,
            "task_id": task_id,
            WEB_PUSH_FLAG: True,
        },
        user_id=user_id,
    )


def announce_group_round(
    *,
    group_name: str,
    group_id: str,
    replies: Iterable[dict[str, Any]],
    user_id: str,
) -> str | None:
    """Announce one finished group round, quoting the first member that spoke."""

    visible = [item for item in replies if item.get("reply")]
    if not visible:
        return None
    speaker = str(visible[0].get("bot_id") or "bot")
    body = f"{speaker}: {first_line(str(visible[0].get('reply') or ''))}"
    return global_notifier.push(
        "SUCCESS",
        group_name or "Group",
        body,
        payload={
            "url": f"/team/group/{group_id}",
            "tag": f"group:{group_id}",
            "kind": "group_round",
            "group_id": group_id,
            WEB_PUSH_FLAG: True,
        },
        user_id=user_id,
    )


__all__ = [
    "DEFAULT_VAPID_SUBJECT",
    "PushNotifier",
    "PushPayload",
    "PushSender",
    "PushSubscription",
    "SubscriptionStore",
    "VapidKeyManager",
    "VapidKeys",
    "announce_bot_reply",
    "announce_group_round",
    "default_push_root",
    "default_sender",
    "first_line",
    "generate_vapid_pair",
    "get_push_notifier",
    "install_push_hook",
    "on_p256_curve",
    "subscription_id",
    "webpush_available",
]
