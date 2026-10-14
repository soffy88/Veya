"""Veya Tailnet channel: tailmsg v1 transport, policy and request receipts.

The receiver is opt-in and binds only to an address returned by Tailscale.
Message bodies are untrusted channel input; this module never executes them.
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import unicodedata
import zoneinfo
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from veya.oprim.fs import (
    local_atomic_write,
    local_fsync,
    local_path_chmod,
    local_path_exists,
    local_path_is_symlink,
    local_path_is_file,
    local_path_is_dir,
    local_path_rglob,
    local_path_rmdir,
    local_path_read_bytes,
    local_path_mkdir,
    local_path_open,
    local_path_replace,
    local_path_resolve,
    local_path_size,
    local_path_unlink,
    local_read_bytes_limited,
)
from veya.oprim.shell import HostCommandError, host_exec_args

log = logging.getLogger("veya.tailnet")
PROTOCOL_VERSION = 1
REQUEST_CAPABILITY = "request-lifecycle-v1"
DEFAULT_PORT = 7767  # Veya receiver; never bind Yobiru's default port
DEFAULT_PEER_PORT = 7766  # tailmsg/Yobiru receiver compatibility
MAX_MESSAGE_BYTES = 256 * 1024
DEFAULT_MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
DEFAULT_TTL_SECONDS = 24 * 60 * 60
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}\Z")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
WAKE_LEVELS = {"none": 0, "batch": 1, "now": 2}
WAKE_ORDER = ("none", "batch", "now")
REQUEST_STATES = {"received", "running", "completed", "failed", "rejected", "blocked", "timed_out"}
REQUEST_FINAL = {"completed", "failed", "rejected", "blocked"}
ENVELOPE_REQUIRED = {"v", "id", "from", "to", "ts"}
ENVELOPE_ALLOWED = ENVELOPE_REQUIRED | {"kind", "text", "reply_to", "fetch", "wake", "agent", "from_agent", "request", "response"}


def enabled(value: str | None = None) -> bool:
    raw = os.getenv("VEYA_TAILNET_CHANNEL", "") if value is None else value
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not UTC_RE.fullmatch(value):
        raise ValueError("timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def normalize_host(value: str) -> str:
    return str(value or "").strip().rstrip(".").lower()


@dataclass(frozen=True)
class _URLParts:
    scheme: str
    netloc: str
    path: str
    query: str
    fragment: str
    username: str | None
    password: str | None
    hostname: str | None
    port_text: str | None


def _split_url(value: str) -> _URLParts:
    """Parse the HTTP URL forms used by this protocol without any network I/O."""
    without_fragment, _, fragment = str(value or "").partition("#")
    before_query, query_sep, query = without_fragment.partition("?")
    rest = before_query
    scheme = ""
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", rest)
    if match:
        scheme = match.group(1).lower()
        rest = rest[match.end():]
    netloc = ""
    if rest.startswith("//"):
        authority_and_path = rest[2:]
        slash = authority_and_path.find("/")
        if slash < 0:
            netloc, path = authority_and_path, ""
        else:
            netloc, path = authority_and_path[:slash], authority_and_path[slash:]
    else:
        path = rest
    username = password = None
    hostport = netloc
    if "@" in hostport:
        userinfo, hostport = hostport.rsplit("@", 1)
        username, colon, secret = userinfo.partition(":")
        password = secret if colon else None
    port_text = None
    if hostport.startswith("["):
        closing = hostport.find("]")
        if closing < 0:
            hostname = None
        else:
            hostname = hostport[1:closing].lower()
            suffix = hostport[closing + 1:]
            if suffix.startswith(":"):
                port_text = suffix[1:]
            elif suffix:
                hostname = None
    elif hostport.count(":") == 1:
        hostname, _, port_text = hostport.rpartition(":")
        hostname = hostname.lower()
    elif ":" in hostport:
        hostname = None
    else:
        hostname = hostport.lower() or None
    return _URLParts(scheme, netloc, path, query if query_sep else "", fragment, username, password, hostname, port_text)


def _url_port(parts: _URLParts) -> int | None:
    if parts.port_text is None or parts.port_text == "":
        return None
    if not parts.port_text.isascii() or not parts.port_text.isdigit():
        raise ValueError("invalid URL port")
    port = int(parts.port_text)
    if not 0 <= port <= 65535:
        raise ValueError("invalid URL port")
    return port


def _url_quote(value: str, *, safe: str = "/") -> str:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~" + safe
    encoded: list[str] = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        encoded.append(char if byte < 128 and char in allowed else f"%{byte:02X}")
    return "".join(encoded)


def _url_unquote(value: str) -> str:
    decoded = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%" and index + 2 < len(value) and re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 1:index + 3]):
            decoded.append(int(value[index + 1:index + 3], 16))
            index += 3
        else:
            decoded.extend(char.encode("utf-8"))
            index += 1
    return decoded.decode("utf-8", errors="replace")


def is_tailnet_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv4Address):
            return ip in ipaddress.ip_network("100.64.0.0/10")
        return ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")
    except ValueError:
        return False


def validate_envelope(env: Any) -> str | None:
    if not isinstance(env, dict):
        return "envelope must be an object"
    if ENVELOPE_REQUIRED - set(env):
        return "missing required envelope fields"
    if set(env) - ENVELOPE_ALLOWED:
        return "unknown envelope fields"
    if env.get("v") != PROTOCOL_VERSION:
        return "unsupported protocol version"
    if not isinstance(env.get("id"), str) or not ID_RE.fullmatch(env["id"]):
        return "invalid message id"
    for field in ("from", "to"):
        value = env.get(field)
        if not isinstance(value, str) or not value or len(value) > 253 or any(ord(c) < 32 for c in value):
            return f"invalid {field} host"
    try:
        parse_utc(env.get("ts"))
    except (TypeError, ValueError, OverflowError):
        return "invalid timestamp"
    kind = env.get("kind", "text")
    if not isinstance(kind, str) or not kind or len(kind) > 32:
        return "invalid kind"
    text = env.get("text", "")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        return "invalid or oversized text"
    wake = env.get("wake", "batch")
    if not isinstance(wake, str) or wake not in WAKE_LEVELS:
        return "invalid wake level"
    for field in ("agent", "from_agent"):
        if field in env:
            name = env[field]
            if not isinstance(name, str) or not name or len(name) > 64 or name.strip() != name or "/" in name or any(unicodedata.category(c).startswith("C") for c in name):
                return f"invalid {field}"
            env[field] = unicodedata.normalize("NFC", name)
    if "reply_to" in env and (not isinstance(env["reply_to"], str) or not ID_RE.fullmatch(env["reply_to"])):
        return "invalid reply_to"
    fetch = env.get("fetch", [])
    if not isinstance(fetch, list) or len(fetch) > 100:
        return "invalid fetch list"
    sender = normalize_host(env["from"])
    for item in fetch:
        if not isinstance(item, dict) or set(item) - {"name", "url", "sha256", "size", "mime", "expires"} or not {"url", "sha256", "size"} <= set(item):
            return "invalid fetch item"
        if not isinstance(item["size"], int) or isinstance(item["size"], bool) or item["size"] < 0:
            return "invalid fetch size"
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            return "invalid fetch hash"
        url = _split_url(str(item["url"]))
        if url.scheme != "http" or normalize_host(url.hostname or "") != sender or not url.path.startswith("/f/") or url.username or url.password:
            return "fetch URL must be on the authenticated sender host"
        name = item.get("name", "attachment")
        if not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
            return "invalid attachment name"
        if "expires" in item:
            try:
                parse_utc(item["expires"])
            except (TypeError, ValueError, OverflowError):
                return "invalid attachment expiry"
    if "request" in env:
        req = env["request"]
        if not isinstance(req, dict) or set(req) - {"timeout_seconds", "reply_port"} or not {"timeout_seconds", "reply_port"} <= set(req):
            return "invalid request metadata"
        timeout = req["timeout_seconds"]
        port = req["reply_port"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 604800:
            return "invalid request timeout"
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            return "invalid reply port"
        if "response" in env or env.get("kind") != "request" or "reply_to" in env:
            return "request metadata requires kind=request and no reply_to/response"
        try:
            parse_utc(env["ts"]) + timedelta(seconds=timeout)
        except (ValueError, OverflowError):
            return "request deadline outside supported timestamp range"
    if "response" in env:
        response = env["response"]
        if not isinstance(response, dict) or set(response) != {"state", "seq"} or not isinstance(response.get("state"), str) or response["state"] not in REQUEST_STATES or not isinstance(response.get("seq"), int) or isinstance(response.get("seq"), bool) or not 1 <= response["seq"] <= 9223372036854775807:
            return "invalid response metadata"
        expected = "request-result" if response["state"] in REQUEST_FINAL | {"timed_out"} else "request-receipt"
        if kind != expected or "reply_to" not in env or wake != "none":
            return "response kind, correlation, or wake is invalid"
    elif kind in {"request-receipt", "request-result"}:
        return "response metadata required"
    return None


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    local_atomic_write(path, temp, payload, parent_mode=0o700, file_mode=0o600)


def _json_read(path: Path, *, limit: int = MAX_MESSAGE_BYTES * 2) -> dict[str, Any] | None:
    try:
        raw = local_read_bytes_limited(path, limit)
        if raw is None:
            return None
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _whois(ip: str, command: str = "tailscale") -> dict[str, Any] | None:
    try:
        completed = host_exec_args([command, "whois", "--json", ip], timeout=5)
        if completed.returncode != 0:
            return None
        raw = json.loads(completed.stdout)
        node = raw.get("Node") if isinstance(raw, dict) else None
        if not isinstance(node, dict):
            return None
        node_name = normalize_host(node.get("DNSName") or node.get("Name") or node.get("HostName"))
        hostname = normalize_host(node.get("HostName") or node_name)
        tags = node.get("Tags") or []
        profile = raw.get("UserProfile") or raw.get("User") or {}
        user = str(profile.get("LoginName") or profile.get("DisplayName") or "") if isinstance(profile, dict) else ""
        aliases = {node_name, hostname, normalize_host(node.get("Name")), normalize_host(node.get("DNSName"))} - {""}
        return {"node": node_name, "hostname": hostname, "aliases": sorted(aliases), "tags": [str(t).lower() for t in tags if isinstance(t, str)], "user": user}
    except (OSError, ValueError, HostCommandError, TypeError):
        return None


def _identity_matches(rule: str, ident: Mapping[str, Any]) -> bool:
    rule = str(rule or "").strip()
    if not rule:
        return False
    low = rule.lower()
    if low.startswith("tag:"):
        wanted = low[4:]
        return wanted in {str(tag).lower().removeprefix("tag:") for tag in ident.get("tags", [])}
    if low.startswith("user:"):
        return normalize_host(str(ident.get("user", ""))) == normalize_host(rule[5:])
    return normalize_host(rule) in {normalize_host(x) for x in ident.get("aliases", [])} or normalize_host(rule) == normalize_host(str(ident.get("node", "")))


def authorize_peer(ident: Mapping[str, Any] | None, policy: Mapping[str, Any], *, recipient_token: bool = False) -> tuple[bool, str]:
    if not ident or not normalize_host(str(ident.get("node", ""))):
        return False, "unknown_peer"
    deny = policy.get("deny", []) if isinstance(policy.get("deny", []), list) else []
    if any(_identity_matches(str(rule), ident) for rule in deny):
        return False, "denied"
    if recipient_token:
        return True, "recipient_token"
    mode = policy.get("mode", "allowlist")
    if mode == "tailnet":
        return True, "tailnet"
    allow = policy.get("allow", []) if isinstance(policy.get("allow", []), list) else []
    if not allow or not any(_identity_matches(str(rule), ident) for rule in allow):
        return False, "not_allowed"
    return True, "allowlist"


class TailnetService:
    """Protocol handler and local policy façade. Inbox JSON is protocol storage; tracked execution remains in the existing bot/GoalRun path."""
    def __init__(self, home: str | Path | None = None, *, tailscale_command: str = "tailscale", whois_fn: Callable[[str], dict[str, Any] | None] | None = None, allow_loopback: bool = False, port: int | None = None, peer_port: int | None = None):
        self.home = local_path_resolve(Path(home or os.getenv("VEYA_TAILNET_HOME") or (Path.home() / ".veya" / "tailnet-channel")).expanduser())
        self.inbox = self.home / "inbox"
        self.outbox = self.home / "outbox"
        self.policy_file = self.home / "policy.json"
        self.tailscale_command = tailscale_command
        self.whois_fn = whois_fn or (lambda ip: _whois(ip, self.tailscale_command))
        self.allow_loopback = allow_loopback
        self.port = port if port is not None else int(os.getenv("VEYA_TAILNET_PORT", str(DEFAULT_PORT)))
        self.peer_port = peer_port if peer_port is not None else int(os.getenv("VEYA_TAILNET_PEER_PORT", str(DEFAULT_PEER_PORT)))
        if not 1 <= self.port <= 65535 or not 1 <= self.peer_port <= 65535:
            raise ValueError("Tailnet receiver and peer ports must be within 1..65535")
        self._lock = threading.RLock()
        self._wake_lock = threading.Lock()
        self._now_hits: dict[tuple[str, str | None], list[float]] = {}
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def load_policy(self) -> dict[str, Any]:
        raw = _json_read(self.policy_file, limit=1024 * 1024)
        default = {"mode": "allowlist", "allow": [], "deny": [], "bindings": {}, "wake_policy": {"default_max": "batch", "peers": {}}, "quiet_hours": None, "wake_rate": 6, "agents": {}, "max_message_bytes": MAX_MESSAGE_BYTES, "max_attachment_bytes": DEFAULT_MAX_ATTACHMENT_BYTES, "attachment_ttl_seconds": DEFAULT_TTL_SECONDS}
        if not raw:
            return default
        for key in ("allow", "deny"):
            if not isinstance(raw.get(key, []), list) or len(raw.get(key, [])) > 500:
                raw[key] = []
        if raw.get("mode") not in ("allowlist", "tailnet"):
            raw["mode"] = "allowlist"
        if not isinstance(raw.get("bindings"), dict):
            raw["bindings"] = {}
        wake = raw.get("wake_policy") if isinstance(raw.get("wake_policy"), dict) else {}
        cap = wake.get("default_max", "batch")
        peers = wake.get("peers", {})
        if cap not in WAKE_LEVELS:
            cap = "batch"
        if not isinstance(peers, dict) or len(peers) > 500 or any(not isinstance(k, str) or not k.strip() or not isinstance(v, str) or v not in WAKE_LEVELS for k, v in peers.items()):
            peers = {}
        raw["wake_policy"] = {"default_max": cap, "peers": peers}
        quiet = raw.get("quiet_hours")
        if not isinstance(quiet, dict) or not isinstance(quiet.get("start"), str) or not isinstance(quiet.get("end"), str):
            raw["quiet_hours"] = None
        if not isinstance(raw.get("agents"), dict) or len(raw.get("agents", {})) > 500:
            raw["agents"] = {}
        if not isinstance(raw.get("wake_rate"), int) or isinstance(raw.get("wake_rate"), bool):
            raw["wake_rate"] = 6
        raw["wake_rate"] = min(1000, max(0, raw["wake_rate"] ))
        try:
            raw["max_message_bytes"] = min(MAX_MESSAGE_BYTES, max(1024, int(raw.get("max_message_bytes", MAX_MESSAGE_BYTES))))
            raw["max_attachment_bytes"] = min(256 * 1024 * 1024, max(1, int(raw.get("max_attachment_bytes", DEFAULT_MAX_ATTACHMENT_BYTES))))
            raw["attachment_ttl_seconds"] = min(7 * 24 * 3600, max(60, int(raw.get("attachment_ttl_seconds", DEFAULT_TTL_SECONDS))))
        except (TypeError, ValueError):
            raw["max_message_bytes"] = MAX_MESSAGE_BYTES
            raw["max_attachment_bytes"] = DEFAULT_MAX_ATTACHMENT_BYTES
            raw["attachment_ttl_seconds"] = DEFAULT_TTL_SECONDS
        return {**default, **raw}

    def save_policy(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValueError("policy must be an object")
        mode = raw.get("mode", "allowlist")
        if mode not in ("allowlist", "tailnet"):
            raise ValueError("mode must be allowlist or tailnet")
        allow, deny = raw.get("allow", []), raw.get("deny", [])
        if not isinstance(allow, list) or not isinstance(deny, list) or len(allow) > 500 or len(deny) > 500:
            raise ValueError("allow and deny must be lists of at most 500 rules")
        rules: list[str] = []
        for value in [*allow, *deny]:
            if not isinstance(value, str) or not value.strip() or len(value) > 253 or any(ord(c) < 32 for c in value):
                raise ValueError("invalid peer rule")
            rules.append(value.strip())
        bindings = raw.get("bindings", {})
        if not isinstance(bindings, dict) or len(bindings) > 500:
            raise ValueError("bindings must be an object")
        normalized_bindings: dict[str, dict[str, str]] = {}
        for host, binding in bindings.items():
            host = normalize_host(host)
            if not host or not isinstance(binding, dict) or binding.get("target_type") not in {"bot", "group"} or not isinstance(binding.get("target_id"), str) or not binding["target_id"].strip():
                raise ValueError("each binding needs host, target_type bot/group, and target_id")
            normalized_bindings[host] = {"target_type": binding["target_type"], "target_id": binding["target_id"].strip(), "owner_ref": ""}
        old = self.load_policy()
        wake_policy = raw.get("wake_policy", old["wake_policy"])
        if not isinstance(wake_policy, dict) or set(wake_policy) - {"default_max", "peers"}:
            raise ValueError("wake_policy must contain default_max and peers")
        wake_cap = wake_policy.get("default_max", "batch")
        peer_caps = wake_policy.get("peers", {})
        if not isinstance(wake_cap, str) or wake_cap not in WAKE_LEVELS or not isinstance(peer_caps, dict) or len(peer_caps) > 500:
            raise ValueError("invalid wake_policy")
        normalized_caps: dict[str, str] = {}
        for rule, level in peer_caps.items():
            if not isinstance(rule, str) or not rule.strip() or len(rule) > 253 or not isinstance(level, str) or level not in WAKE_LEVELS:
                raise ValueError("wake_policy peer rules require valid names and now/batch/none levels")
            normalized_caps[rule.strip()] = level
        quiet = raw.get("quiet_hours", old["quiet_hours"])
        if quiet is not None:
            if not isinstance(quiet, dict) or set(quiet) - {"start", "end", "tz"} or not {"start", "end"} <= set(quiet):
                raise ValueError("quiet_hours requires start/end and optional tz")
            for key in ("start", "end"):
                if not isinstance(quiet[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", quiet[key]):
                    raise ValueError("quiet_hours times must be HH:MM")
            if quiet.get("tz"):
                try:
                    zoneinfo.ZoneInfo(quiet["tz"])
                except (zoneinfo.ZoneInfoNotFoundError, TypeError):
                    raise ValueError("quiet_hours tz must be a valid IANA timezone")
        wake_rate = raw.get("wake_rate", old["wake_rate"])
        if not isinstance(wake_rate, int) or isinstance(wake_rate, bool) or not 0 <= wake_rate <= 1000:
            raise ValueError("wake_rate must be an integer from 0 to 1000")
        agents = raw.get("agents", old["agents"])
        if not isinstance(agents, dict) or len(agents) > 500:
            raise ValueError("agents must be an object with at most 500 entries")
        normalized_agents: dict[str, dict[str, str]] = {}
        for name, spec in agents.items():
            if not isinstance(name, str) or not name or len(name) > 64 or name.strip() != name or "/" in name or not isinstance(spec, dict) or set(spec) - {"wake_max", "target_type", "target_id"}:
                raise ValueError("invalid agent policy")
            if spec.get("wake_max") not in (None, *WAKE_LEVELS):
                raise ValueError("agent wake_max must be now/batch/none")
            entry = {k: v for k, v in spec.items() if v is not None}
            if ("target_type" in entry or "target_id" in entry) and (
                entry.get("target_type") not in {"bot", "group"}
                or not isinstance(entry.get("target_id"), str)
                or not entry["target_id"].strip()
            ):
                raise ValueError("agent target requires target_type bot/group and target_id")
            normalized_agents[name] = entry
        policy = {**old, "mode": mode, "allow": list(allow), "deny": list(deny), "bindings": normalized_bindings, "wake_policy": {"default_max": wake_cap, "peers": normalized_caps}, "quiet_hours": quiet, "wake_rate": wake_rate, "agents": normalized_agents}
        _atomic_json(self.policy_file, policy)
        return policy

    def identify(self, ip: str) -> dict[str, Any] | None:
        if self.allow_loopback and ip in {"127.0.0.1", "::1"}:
            return self.whois_fn(ip)
        if not is_tailnet_ip(ip):
            return None
        ident = self.whois_fn(ip)
        if not ident or not normalize_host(str(ident.get("node", ""))):
            return None
        return ident

    def self_node(self) -> str:
        value = os.getenv("VEYA_TAILNET_NODE", "").strip()
        if value:
            return normalize_host(value)
        try:
            proc = host_exec_args([self.tailscale_command, "status", "--json"], timeout=5)
            body = json.loads(proc.stdout) if proc.returncode == 0 else {}
            me = body.get("Self", {}) if isinstance(body, dict) else {}
            return normalize_host(me.get("DNSName") or me.get("HostName") or me.get("Name") or "")
        except (OSError, ValueError, HostCommandError):
            return ""

    def tailscale_ip(self) -> str | None:
        try:
            proc = host_exec_args([self.tailscale_command, "ip"], timeout=5)
            if proc.returncode != 0:
                return None
            for line in proc.stdout.splitlines():
                value = line.strip()
                if is_tailnet_ip(value):
                    return value
        except (OSError, HostCommandError):
            pass
        return None

    def list_peers(self) -> list[dict[str, Any]]:
        try:
            proc = host_exec_args([self.tailscale_command, "status", "--json"], timeout=5)
            body = json.loads(proc.stdout) if proc.returncode == 0 else {}
        except (OSError, ValueError, HostCommandError):
            return []
        rows = []
        peers = body.get("Peer", {}) if isinstance(body, dict) else {}
        if not isinstance(peers, dict):
            return []
        for peer in peers.values():
            if not isinstance(peer, dict):
                continue
            dns = normalize_host(peer.get("DNSName", ""))
            host = normalize_host(peer.get("HostName", ""))
            ips = [ip for ip in peer.get("TailscaleIPs", []) if isinstance(ip, str) and is_tailnet_ip(ip)]
            rows.append({"hostname": host, "dns_name": dns, "online": bool(peer.get("Online")), "ips": ips, "tags": [str(t) for t in peer.get("Tags", [])]})
        return sorted(rows, key=lambda item: (not item["online"], item["hostname"]))

    def resolve_peer(self, address: str) -> tuple[str, str, int, str | None]:
        hostpart, slash, agent = str(address).partition("/")
        hostpart = hostpart.strip()
        if not hostpart or len(hostpart) > 300:
            raise ValueError("peer must be hostname[/agent] or Tailscale IP")
        port = self.peer_port
        hostname = hostpart
        if hostpart.startswith("["):
            closing = hostpart.find("]")
            if closing < 0:
                raise ValueError("invalid bracketed peer address")
            hostname = hostpart[1:closing]
            suffix = hostpart[closing + 1:]
            if suffix:
                if not suffix.startswith(":") or not suffix[1:].isdigit():
                    raise ValueError("invalid peer port")
                port = int(suffix[1:])
        elif hostpart.count(":") == 1:
            possible_host, possible_port = hostpart.rsplit(":", 1)
            if possible_port.isdigit():
                hostname, port = possible_host, int(possible_port)
        if not 1 <= port <= 65535:
            raise ValueError("invalid peer port")
        try:
            ip = ipaddress.ip_address(hostname.strip("[]"))
            if not is_tailnet_ip(str(ip)):
                raise ValueError("peer IP must be on the Tailscale network")
            return normalize_host(str(ip)), str(ip), port, agent.strip() or None if slash else None
        except ValueError as exc:
            if "peer IP must" in str(exc):
                raise
        wanted = normalize_host(hostname)
        matches = []
        for peer in self.list_peers():
            dns, short = peer["dns_name"], peer["hostname"]
            is_match = wanted in (dns, short) or ("." not in wanted and (short == wanted or dns.split(".", 1)[0] == wanted))
            if is_match and peer["online"] and peer["ips"]:
                matches.append(peer)
        if len(matches) != 1:
            if len(matches) > 1:
                raise ValueError("peer hostname is ambiguous; use the full Tailscale DNS name")
            raise ValueError("peer is not an online Tailscale node")
        return matches[0]["dns_name"] or matches[0]["hostname"], matches[0]["ips"][0], port, agent.strip() or None if slash else None

    def authorize_outbound_peer(self, recipient: str, ip: str) -> None:
        policy = self.load_policy()
        peer_info = next((peer for peer in self.list_peers() if ip in peer.get("ips", [])), None)
        aliases = {normalize_host(recipient), normalize_host(ip)}
        tags: list[str] = []
        if peer_info:
            aliases.update({normalize_host(peer_info.get("dns_name", "")), normalize_host(peer_info.get("hostname", ""))})
            tags = [str(tag).lower() for tag in peer_info.get("tags", [])]
        aliases.discard("")
        ident = {"node": normalize_host(recipient), "hostname": normalize_host(recipient), "aliases": sorted(aliases), "tags": tags, "user": ""}
        allowed, reason = authorize_peer(ident, policy)
        if not allowed:
            raise PermissionError(f"outbound Tailnet peer denied by local policy: {reason}")

    def send_json(self, host: str, ip: str, port: int, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15) -> tuple[int, dict[str, Any]]:
        conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        headers = {"Host": f"{host}:{port}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read(1024 * 1024)
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                decoded = {"raw": raw[:512].decode("utf-8", "replace")}
            return response.status, decoded if isinstance(decoded, dict) else {"data": decoded}
        finally:
            conn.close()

    def stage_attachments(self, files: list[str | Path], *, recipient: str, message_id: str, node: str, port: int) -> list[dict[str, Any]]:
        if not files:
            return []
        policy = self.load_policy()
        token = secrets.token_urlsafe(32)
        token = token.replace("=", "")
        if len(token) < 32:
            token = secrets.token_urlsafe(36).replace("=", "")
        token_dir = self.outbox / token
        files_dir = token_dir / "files"
        local_path_mkdir(files_dir, parents=True, exist_ok=False, mode=0o700)
        try:
            local_path_chmod(token_dir, 0o700)
            local_path_chmod(files_dir, 0o700)
        except OSError:
            pass
        expires = utc_iso(utc_now() + timedelta(seconds=policy["attachment_ttl_seconds"]))
        meta_files: dict[str, Any] = {}
        result: list[dict[str, Any]] = []
        total = 0
        try:
            for source_value in files:
                source = local_path_resolve(Path(source_value).expanduser(), strict=True)
                if not local_path_is_file(source):
                    raise ValueError("attachment must be a regular file")
                name = source.name
                if not name or name in {".", ".."} or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
                    raise ValueError("invalid attachment filename")
                destination = files_dir / name
                if local_path_exists(destination):
                    raise ValueError("duplicate attachment filename")
                digest = hashlib.sha256()
                size = 0
                with local_path_open(source, "rb") as reader, local_path_open(destination, "xb") as writer:
                    with suppress(OSError):
                        local_path_chmod(destination, 0o600)
                    while True:
                        block = reader.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        total += len(block)
                        if size > policy["max_attachment_bytes"] or total > policy["max_attachment_bytes"]:
                            raise ValueError("attachment size limit exceeded")
                        digest.update(block)
                        writer.write(block)
                mime = "application/octet-stream"
                item = {"name": name, "url": f"http://{node}:{port}/f/{token}/{_url_quote(name)}", "sha256": digest.hexdigest(), "size": size, "mime": mime, "expires": expires}
                result.append(item)
                meta_files[name] = {"size": size, "sha256": digest.hexdigest(), "mime": mime}
            _atomic_json(token_dir / "meta.json", {"id": message_id, "to": normalize_host(recipient), "expires": expires, "files": meta_files})
            return result
        except Exception:
            # Bounded cleanup is explicit and confined to the newly-created task directory.
            for path in sorted(local_path_rglob(token_dir, "*"), reverse=True):
                if local_path_is_file(path) or local_path_is_symlink(path):
                    local_path_unlink(path, missing_ok=True)
                elif local_path_is_dir(path):
                    local_path_rmdir(path)
            local_path_rmdir(token_dir)
            raise

    def fetch_file(self, token: str, name: str, ident: Mapping[str, Any]) -> tuple[Path, str] | None:
        if not TOKEN_RE.fullmatch(token) or not name or Path(name).name != name or name in {".", ".."}:
            return None
        token_dir = self.outbox / token
        meta = _json_read(token_dir / "meta.json", limit=1024 * 1024)
        if not meta or normalize_host(str(meta.get("to", ""))) != normalize_host(str(ident.get("node", ""))):
            return None
        try:
            if parse_utc(meta["expires"]) <= utc_now():
                return None
        except (KeyError, ValueError, TypeError):
            return None
        item = meta.get("files", {}).get(name)
        path = token_dir / "files" / name
        if not isinstance(item, dict) or not local_path_is_file(path) or local_path_is_symlink(path):
            return None
        if local_path_size(path) != item.get("size"):
            return None
        digest = hashlib.sha256(local_path_read_bytes(path)).hexdigest()
        if digest != item.get("sha256"):
            return None
        return path, str(item.get("mime") or "application/octet-stream")

    def _agent_is_known(self, agent: str | None, policy: Mapping[str, Any]) -> bool:
        if not agent:
            return True
        configured = policy.get("agents", {}).get(agent) if isinstance(policy.get("agents"), dict) else None
        if isinstance(configured, dict) and configured.get("target_type") in {"bot", "group"} and configured.get("target_id"):
            return True
        try:
            from server.goal_run.bot_identity import BotRegistry
            return BotRegistry().get(agent) is not None
        except Exception:
            return False

    @staticmethod
    def _in_quiet_hours(spec: Mapping[str, Any] | None, now: datetime | None = None) -> bool:
        if not isinstance(spec, Mapping):
            return False
        try:
            current = now or utc_now()
            tz = zoneinfo.ZoneInfo(str(spec["tz"])) if spec.get("tz") else UTC
            current = current.astimezone(tz)
            minute = current.hour * 60 + current.minute
            start_h, start_m = (int(x) for x in str(spec["start"]).split(":"))
            end_h, end_m = (int(x) for x in str(spec["end"]).split(":"))
            start, end = start_h * 60 + start_m, end_h * 60 + end_m
        except (KeyError, ValueError, TypeError, zoneinfo.ZoneInfoNotFoundError):
            return False
        if start == end:
            return False
        return start <= minute < end if start < end else minute >= start or minute < end

    def wake_decision(self, requested: str, ident: Mapping[str, Any], agent: str | None, *, agent_known: bool = True, now: datetime | None = None) -> tuple[str, str]:
        policy = self.load_policy()
        wake_policy = policy["wake_policy"]
        cap = wake_policy["default_max"]
        for rule, level in wake_policy["peers"].items():
            if _identity_matches(rule, ident):
                cap = level
                break
        effective = requested if requested in WAKE_LEVELS else "batch"
        reasons: list[str] = []
        if WAKE_ORDER.index(effective) > WAKE_ORDER.index(cap):
            effective = cap
            reasons.append(f"capped by wake_policy (max {cap})")
        agent_policy = policy["agents"].get(agent, {}) if agent else {}
        agent_max = agent_policy.get("wake_max") if isinstance(agent_policy, dict) else None
        if agent_max in WAKE_LEVELS and WAKE_ORDER.index(effective) > WAKE_ORDER.index(agent_max):
            effective = agent_max
            reasons.append(f"capped by agents[{agent}].wake_max ({agent_max})")
        if not agent_known and effective == "now":
            effective = "batch"
            reasons.append(f"unknown agent {agent!r}: nobody woken")
        if effective == "now" and self._in_quiet_hours(policy.get("quiet_hours"), now):
            effective = "batch"
            reasons.append("quiet hours")
        if effective == "now":
            stamp = time.time()
            key = (normalize_host(str(ident.get("node", ""))), agent)
            limit = policy["wake_rate"]
            with self._wake_lock:
                hits = [value for value in self._now_hits.get(key, []) if value > stamp - 3600]
                if len(hits) >= limit:
                    effective = "batch"
                    reasons.append(f"rate limit ({limit} now/hour)")
                else:
                    hits.append(stamp)
                self._now_hits[key] = hits
        return effective, "; ".join(reasons) or "as requested"

    def fetch_attachments(self, message_id: str, *, timeout: float = 15) -> dict[str, Any]:
        """Pull and verify files declared by one inbox message; never execute them."""
        if not ID_RE.fullmatch(str(message_id)):
            raise ValueError("invalid message id")
        record_path = self.inbox / f"{message_id}.json"
        record = _json_read(record_path)
        if not record or not isinstance(record.get("envelope"), dict):
            raise FileNotFoundError("Tailnet message not found")
        envelope = record["envelope"]
        if "response" in envelope:
            raise ValueError("protocol response messages cannot contain fetchable work attachments")
        items = envelope.get("fetch", [])
        if not items:
            return {"ok": True, "id": message_id, "files": []}
        sender = normalize_host(str(record.get("received", {}).get("peer", {}).get("node", "")))
        declared_sender = normalize_host(str(envelope.get("from", "")))
        if not sender or declared_sender not in {sender, *[normalize_host(x) for x in record.get("received", {}).get("peer", {}).get("aliases", [])]}:
            raise PermissionError("message sender does not match the authenticated stored identity")
        policy = self.load_policy()
        if len(items) > 100:
            raise ValueError("attachment count limit exceeded")
        cap = policy["max_attachment_bytes"]
        total_declared = 0
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("size"), int) or item["size"] < 0:
                raise ValueError("invalid attachment metadata")
            total_declared += item["size"]
            if total_declared > cap:
                raise ValueError("aggregate attachment size limit exceeded")
        target_dir = self.inbox / message_id / "files"
        local_path_mkdir(target_dir, parents=True, exist_ok=True, mode=0o700)
        try:
            local_path_chmod(self.inbox / message_id, 0o700)
            local_path_chmod(target_dir, 0o700)
        except OSError:
            pass
        target_root = local_path_resolve(target_dir)
        saved: list[dict[str, Any]] = []
        total_actual = 0
        for item in items:
            raw_url = str(item.get("url", ""))
            parsed = _split_url(raw_url)
            if parsed.scheme != "http" or normalize_host(parsed.hostname or "") != declared_sender or parsed.username or parsed.password or not parsed.path.startswith("/f/"):
                raise ValueError("attachment URL does not match the authenticated sender")
            name = item.get("name", "attachment")
            if not isinstance(name, str) or not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name or any(unicodedata.category(c).startswith("C") for c in name):
                raise ValueError("invalid attachment name")
            if "expires" in item and parse_utc(item["expires"]) <= utc_now():
                raise ValueError(f"attachment link expired: {name}")
            size = item["size"]
            if size > cap or total_actual + size > cap:
                raise ValueError("attachment size limit exceeded")
            wanted_hash = item.get("sha256")
            if not isinstance(wanted_hash, str) or not SHA256_RE.fullmatch(wanted_hash):
                raise ValueError("invalid attachment SHA-256")
            port = _url_port(parsed) or self.port
            address = f"{parsed.hostname}:{port}"
            peer_host, peer_ip, peer_port, _ = self.resolve_peer(address)
            self.authorize_outbound_peer(peer_host, peer_ip)
            destination = local_path_resolve(target_root / name)
            if destination.parent != target_root:
                raise ValueError("attachment destination escaped inbox")
            if local_path_exists(destination) or local_path_is_symlink(destination):
                raise FileExistsError(f"attachment already exists: {name}")
            temp_path = target_root / ("." + name + "." + secrets.token_hex(6) + ".part")
            conn = http.client.HTTPConnection(peer_ip, peer_port, timeout=timeout)
            digest = hashlib.sha256()
            received = bytearray()
            try:
                conn.request("GET", parsed.path + (("?" + parsed.query) if parsed.query else ""), headers={"Host": f"{peer_host}:{peer_port}", "Accept": "application/octet-stream"})
                response = conn.getresponse()
                if response.status != 200:
                    response.read(4096)
                    raise RuntimeError(f"attachment fetch rejected with HTTP {response.status}")
                declared_length = response.getheader("Content-Length")
                if declared_length is not None and int(declared_length) != size:
                    raise ValueError("attachment Content-Length mismatch")
                with local_path_open(temp_path, "xb") as writer:
                    with suppress(OSError):
                        local_path_chmod(temp_path, 0o600)
                    while True:
                        block = response.read(min(1024 * 1024, size - len(received) + 1))
                        if not block:
                            break
                        received.extend(block)
                        if len(received) > size:
                            raise ValueError("attachment exceeded declared size")
                        digest.update(block)
                        writer.write(block)
                    writer.flush()
                    local_fsync(writer)
                if len(received) != size or digest.hexdigest() != wanted_hash:
                    raise ValueError(f"attachment size/SHA-256 mismatch: {name}")
                local_path_replace(temp_path, destination)
                total_actual += len(received)
                saved.append({"name": name, "path": str(destination.relative_to(self.inbox)), "size": size, "sha256": digest.hexdigest(), "mime": item.get("mime", "application/octet-stream")})
            finally:
                conn.close()
                if local_path_exists(temp_path):
                    local_path_unlink(temp_path)
        self._update_record(record_path, attachments_local=saved)
        return {"ok": True, "id": message_id, "files": saved}

    def _store_inbound(self, envelope: dict[str, Any], ident: dict[str, Any], peer_ip: str) -> tuple[Path, bool]:
        path = self.inbox / f"{envelope['id']}.json"
        with self._lock:
            previous = _json_read(path)
            if local_path_exists(path) and previous is None:
                raise FileExistsError("message ID collides with an unreadable existing record")
            if previous is not None:
                same_peer = normalize_host(str(previous.get("received", {}).get("peer", {}).get("node", ""))) == normalize_host(str(ident.get("node", "")))
                if not same_peer or previous.get("envelope") != envelope:
                    raise FileExistsError("message ID conflicts with an existing message")
                return path, False
            record = {"envelope": envelope, "received": {"at": utc_iso(), "peer": dict(ident), "peer_ip": peer_ip, "request_state": "received" if "request" in envelope else None, "seq": 1, "outcome_known": False}}
            _atomic_json(path, record)
        return path, True

    def _update_record(self, path: Path, **updates: Any) -> dict[str, Any]:
        with self._lock:
            record = _json_read(path)
            if record is None:
                raise ValueError("message record unavailable")
            record.setdefault("received", {}).update(updates)
            _atomic_json(path, record)
            return record

    def _response_envelope(self, original: Mapping[str, Any], self_node: str, state: str, seq: int, text: str) -> dict[str, Any]:
        env: dict[str, Any] = {"v": 1, "id": secrets.token_hex(8), "from": self_node, "to": normalize_host(str(original["from"])), "ts": utc_iso(), "kind": "request-result" if state in REQUEST_FINAL | {"timed_out"} else "request-receipt", "text": text, "reply_to": original["id"], "wake": "none", "response": {"state": state, "seq": seq}}
        if original.get("agent") is not None:
            env["from_agent"] = original["agent"]
        if original.get("from_agent") is not None:
            env["agent"] = original["from_agent"]
        return env

    def send_lifecycle_response(self, original: Mapping[str, Any], ident: Mapping[str, Any], state: str, seq: int, text: str, *, peer_ip: str, reply_port: int) -> None:
        node = self.self_node()
        if not node:
            return
        encoded = str(text).encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            encoded = encoded[: MAX_MESSAGE_BYTES - 32]
            while encoded:
                try:
                    text = encoded.decode("utf-8") + "\n[reply truncated]"
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]
        response = self._response_envelope(original, node, state, seq, text)
        if validate_envelope(response):
            return
        try:
            self.send_json(normalize_host(str(ident.get("node", ""))), peer_ip, reply_port, "POST", "/msg", response, timeout=3)
        except (OSError, http.client.HTTPException, TimeoutError, ValueError):
            # Status is durably queryable from the receiving peer; failed notification is not replayed blindly.
            log.info("tailnet lifecycle notification delivery uncertain for %s", original.get("id"))

    def send_text_reply(self, original: Mapping[str, Any], ident: Mapping[str, Any], text: str, *, peer_ip: str, reply_port: int) -> None:
        """Deliver a plain-text bot reply through tailmsg v1 without echo loops."""
        if not text:
            return
        payload = text.encode("utf-8")
        if len(payload) > MAX_MESSAGE_BYTES:
            payload = payload[: MAX_MESSAGE_BYTES - 32]
            while payload:
                try:
                    text = payload.decode("utf-8") + "\n[reply truncated]"
                    break
                except UnicodeDecodeError:
                    payload = payload[:-1]
        self_node = self.self_node()
        if not self_node:
            return
        envelope: dict[str, Any] = {
            "v": 1,
            "id": secrets.token_hex(8),
            "from": self_node,
            "to": normalize_host(str(original["from"])),
            "ts": utc_iso(),
            "kind": "text",
            "text": text,
            "reply_to": original["id"],
            "wake": "none",
        }
        if original.get("agent") is not None:
            envelope["from_agent"] = original["agent"]
        if original.get("from_agent") is not None:
            envelope["agent"] = original["from_agent"]
        if validate_envelope(envelope):
            return
        try:
            code, _ = self.send_json(
                normalize_host(str(ident.get("node", ""))), peer_ip, reply_port,
                "POST", "/msg", envelope, timeout=3,
            )
            if code not in {200, 202}:
                log.info("tailnet text reply rejected for message id=%s (HTTP %d)", original.get("id"), code)
        except (OSError, http.client.HTTPException, TimeoutError, ValueError):
            log.info("tailnet text reply delivery uncertain for message id=%s", original.get("id"))

    def request_status(self, request_id: str, *, peer: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        if not ID_RE.fullmatch(request_id):
            return None
        record = _json_read(self.inbox / f"{request_id}.json")
        if not record or "request" not in record.get("envelope", {}):
            return None
        received = record.get("received", {})
        if peer is not None and normalize_host(str(received.get("peer", {}).get("node", ""))) != normalize_host(str(peer.get("node", ""))):
            return None
        env = record["envelope"]
        state = received.get("request_state") or "received"
        deadline = parse_utc(env["ts"]) + timedelta(seconds=env["request"]["timeout_seconds"])
        overdue = state not in REQUEST_FINAL and utc_now() >= deadline
        if overdue:
            state = "timed_out"
        known = state in REQUEST_FINAL
        return {"id": request_id, "direction": "incoming", "peer": received.get("peer", {}).get("node"), "agent": env.get("agent"), "from_agent": env.get("from_agent"), "state": state, "last_state": received.get("request_state", "received"), "deadline": utc_iso(deadline), "overdue": overdue, "outcome_known": known, "timed_out": state == "timed_out", "late": bool(received.get("late", False)), "seq": int(received.get("seq", 1)), "task_id": received.get("task_id"), "trace_id": received.get("trace_id"), "turn_status": received.get("turn_status"), "requested_wake": received.get("requested_wake"), "effective_wake": received.get("effective_wake"), "wake_reason": received.get("wake_reason"), "result": received.get("result", "")}

    def handle_envelope(self, envelope: dict[str, Any], ident: dict[str, Any], peer_ip: str, server_port: int) -> tuple[int, dict[str, Any]]:
        error = validate_envelope(envelope)
        if error:
            return 400, {"ok": False, "error": error}
        if normalize_host(envelope["from"]) not in {normalize_host(str(x)) for x in ident.get("aliases", [])} | {normalize_host(str(ident.get("node", ""))), normalize_host(str(ident.get("hostname", "")))}:
            return 403, {"ok": False, "error": "from does not match authenticated Tailscale identity"}
        self_node = self.self_node()
        if self_node and normalize_host(envelope["to"]) not in {self_node, self_node.split(".", 1)[0]}:
            return 400, {"ok": False, "error": "misdirected message"}
        try:
            path, fresh = self._store_inbound(envelope, ident, peer_ip)
        except FileExistsError as exc:
            return 409, {"ok": False, "error": str(exc)}
        if not fresh:
            return 200, {"ok": True, "id": envelope["id"], "duplicate": True}
        if "response" in envelope:
            # Lifecycle responses are records, never agent instructions.
            self._update_record(path, response_for=envelope.get("reply_to"), request_state=envelope["response"]["state"], seq=envelope["response"]["seq"], result=envelope.get("text", ""), outcome_known=envelope["response"]["state"] in REQUEST_FINAL, is_protocol_response=True)
            return 202, {"ok": True, "id": envelope["id"]}
        if envelope.get("reply_to") and "request" not in envelope:
            # A plain tailmsg reply is an inbound response to a previous message;
            # keep it in the inbox but do not dispatch/reply to it again.
            self._update_record(path, response_for=envelope.get("reply_to"), result=envelope.get("text", ""), is_protocol_response=True)
            return 202, {"ok": True, "id": envelope["id"], "reply": True}
        if "request" in envelope:
            self._update_record(path, request_state="received", seq=1)
            self.send_lifecycle_response(envelope, ident, "received", 1, "Request durably received; execution has not been confirmed.", peer_ip=peer_ip, reply_port=envelope["request"]["reply_port"])
        # Validate declared links at receipt time; bytes are pulled only by the
        # explicit attachment-fetch operation, never executed automatically.
        for item in envelope.get("fetch", []):
            expires = item.get("expires")
            if expires and parse_utc(expires) <= utc_now():
                self._update_record(path, delivery_state="rejected", delivery_error="attachment link expired")
                if "request" in envelope:
                    self.send_lifecycle_response(envelope, ident, "rejected", 2, "Attachment link expired; request was not dispatched.", peer_ip=peer_ip, reply_port=envelope["request"]["reply_port"])
                return 400, {"ok": False, "error": "attachment link expired"}
        if "request" in envelope:
            deadline = parse_utc(envelope["ts"]) + timedelta(seconds=envelope["request"]["timeout_seconds"])
            if deadline <= utc_now():
                self._update_record(path, request_state="timed_out", seq=2, timed_out_at=utc_iso(), outcome_known=False, result="Execution outcome is unknown; do not automatically repeat the task.")
                self.send_lifecycle_response(envelope, ident, "timed_out", 2, "Deadline passed before dispatch; outcome is unknown and must not be retried automatically.", peer_ip=peer_ip, reply_port=envelope["request"]["reply_port"])
                return 202, {"ok": True, "id": envelope["id"], "expired": True, "outcome_known": False}
        policy = self.load_policy()
        agent = envelope.get("agent")
        agent_known = self._agent_is_known(agent, policy)
        requested_wake = envelope.get("wake", "batch")
        effective_wake, wake_reason = self.wake_decision(requested_wake, ident, agent, agent_known=agent_known)
        self._update_record(path, requested_wake=requested_wake, effective_wake=effective_wake, wake_reason=wake_reason, agent_known=agent_known)
        if effective_wake != "now":
            # batch/none are durably stored but do not trigger any code or create
            # a second scheduler. A tracked request remains received until the
            # established Veya turn path is actually entered.
            return 202, {"ok": True, "id": envelope["id"], "queued": effective_wake == "batch", "effective_wake": effective_wake, "wake_reason": wake_reason}
        if "request" in envelope:
            self._update_record(path, request_state="running", seq=2)
            self.send_lifecycle_response(envelope, ident, "running", 2, "Request routed to the existing Veya channel/turn path.", peer_ip=peer_ip, reply_port=envelope["request"]["reply_port"])
        try:
            outcome = self._dispatch(envelope, ident)
            if not getattr(outcome, "ok", True):
                raise PermissionError("existing channel dispatcher rejected the message")
            turn_status = str(getattr(outcome, "turn_status", "completed") or "completed").lower()
            task_id = str(getattr(outcome, "task_id", "") or "")
            trace_id = str(getattr(outcome, "trace_id", "") or "")
            if turn_status in {"blocked", "rejected", "denied", "approval_required"}:
                final_state = "blocked"
            elif turn_status in {"error", "failed", "cancelled", "canceled", "unknown"}:
                final_state = "failed"
            else:
                final_state = "completed"
            current = _json_read(path) or {}
            received = current.get("received", {})
            current_seq = int(received.get("seq", 2))
            final_seq = max(3, current_seq + 1) if "request" in envelope else current_seq
            response_text = str(getattr(outcome, "reply", "") or "").strip()
            result_text = response_text or f"Existing Veya turn returned status={turn_status or 'unknown'}"
            self._update_record(path, request_state=final_state if "request" in envelope else None, seq=final_seq, outcome_known=final_state in REQUEST_FINAL, task_id=task_id, trace_id=trace_id, turn_status=turn_status, result=result_text)
            if "request" in envelope:
                late = utc_now() > deadline
                self._update_record(path, late=late)
                self.send_lifecycle_response(envelope, ident, final_state, final_seq, result_text, peer_ip=peer_ip, reply_port=envelope["request"]["reply_port"])
            elif envelope.get("kind", "text") == "text":
                self.send_text_reply(envelope, ident, str(getattr(outcome, "reply", "") or "").strip(), peer_ip=peer_ip, reply_port=self.peer_port)
        except Exception as exc:
            log.warning("tailnet dispatch failed for id=%s (%s)", envelope.get("id"), type(exc).__name__)
            if "request" in envelope:
                current = _json_read(path) or {}
                received = current.get("received", {})
                state = "blocked" if isinstance(exc, PermissionError) else "failed"
                seq = max(3, int(received.get("seq", 2)) + 1)
                self._update_record(path, request_state=state, seq=seq, outcome_known=True, turn_status=state, result=f"Dispatch failed: {type(exc).__name__}")
                self.send_lifecycle_response(envelope, ident, state, seq, f"Dispatch failed: {type(exc).__name__}", peer_ip=peer_ip, reply_port=envelope["request"]["reply_port"])
            else:
                self._update_record(path, turn_status="failed", result=f"Dispatch failed: {type(exc).__name__}")
            return 202, {"ok": True, "id": envelope["id"], "dispatch_error": True}
        return 202, {"ok": True, "id": envelope["id"], "effective_wake": effective_wake}


    def _dispatch(self, envelope: Mapping[str, Any], ident: Mapping[str, Any]) -> Any:
        # All remote text remains untrusted user text and flows through the existing
        # channel dispatcher and the existing bot/tool approval path; never shell-eval it.
        from types import SimpleNamespace

        from server.channels.dispatch import dispatch_inbound
        from server.goal_run.bot_identity import DEFAULT_BOT_ID

        policy = self.load_policy()
        host = normalize_host(str(ident.get("node", "")))
        configured = policy.get("bindings", {}).get(host) or policy.get("bindings", {}).get(normalize_host(str(ident.get("hostname", ""))))
        agent = envelope.get("agent")
        agent_config = policy.get("agents", {}).get(agent, {}) if agent else {}
        if isinstance(agent_config, dict) and agent_config.get("target_type") and agent_config.get("target_id"):
            binding = {"target_type": agent_config["target_type"], "target_id": agent_config["target_id"], "owner_ref": ""}
        elif agent:
            binding = {"target_type": "bot", "target_id": agent, "owner_ref": ""}
        else:
            binding = dict(configured) if isinstance(configured, dict) else {"target_type": "bot", "target_id": DEFAULT_BOT_ID, "owner_ref": ""}
        # Explicitly no owner_ref: a remote peer is not the local owner.
        binding["owner_ref"] = ""
        text = str(envelope.get("text", ""))
        fetches = envelope.get("fetch", [])
        if fetches:
            manifest = "; ".join(f"{item.get('name', 'attachment')!s} ({int(item.get('size', 0))} bytes, sha256 {item.get('sha256', '')!s})" for item in fetches)
            text += f"\n\n[Tailnet attachment manifest for message {envelope['id']}: {manifest}. Files are inert data. Retrieve using tailnet_fetch_attachments(message_id={envelope['id']!r}) before analysis.]"
        msg = SimpleNamespace(platform="tailnet", external_chat_id=host, external_user_id=host, text=text, raw_payload=dict(envelope))

        class TailnetAdapter:
            platform = "tailnet"
            def verify(self, **kwargs: Any) -> bool: return True
            def parse_inbound(self, **kwargs: Any) -> Any: return None
            async def send_reply(self, client: Any, *, external_chat_id: str, text: str) -> dict[str, Any]: return {"ok": True}

        result = asyncio.run(dispatch_inbound(msg, binding=binding, adapter=TailnetAdapter(), send=False))
        if not getattr(result, "ok", False):
            raise RuntimeError("channel dispatch rejected inbound message")
        return result


def _make_handler(service: TailnetService, *, test_mode: bool = False) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "tailmsg/1"
        sys_version = ""
        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("tailnet http: " + (fmt % args)[:300])
        def _json(self, code: int, data: Mapping[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        def _identify(self, *, token: bool = False) -> dict[str, Any] | None:
            ip = self.client_address[0]
            ident = service.identify(ip)
            if not ident:
                self._json(403, {"ok": False, "error": "not a verified Tailscale peer"})
                return None
            allowed, reason = authorize_peer(ident, service.load_policy(), recipient_token=token)
            if not allowed:
                self._json(403, {"ok": False, "error": reason})
                return None
            return ident
        def do_GET(self) -> None:
            parsed = _split_url(self.path)
            path = parsed.path
            if path == "/health":
                if not (service.allow_loopback and self.client_address[0] in {"127.0.0.1", "::1"}) and not is_tailnet_ip(self.client_address[0]):
                    return self._json(403, {"ok": False, "error": "not a Tailscale address"})
                return self._json(200, {"ok": True, "v": 1, "node": service.self_node() or "unknown"})
            if path == "/info":
                ident = self._identify()
                if ident is None:
                    return
                return self._json(200, {"v": 1, "node": service.self_node() or "unknown", "capabilities": [REQUEST_CAPABILITY], "default_agent": "veya-default", "agents": []})
            if path.startswith("/requests/"):
                ident = self._identify()
                if ident is None:
                    return
                request_id = path[len("/requests/"):]
                result = service.request_status(request_id, peer=ident)
                if not result:
                    return self._json(404, {"ok": False, "error": "request not found"})
                return self._json(200, {"ok": True, "request": result})
            match = re.fullmatch(r"/f/([^/]+)/([^/]+)", path)
            if match:
                ident = self._identify(token=True)
                if ident is None:
                    return
                token, name = match.group(1), _url_unquote(match.group(2))
                found = service.fetch_file(token, name, ident)
                if not found:
                    return self._json(404, {"ok": False, "error": "not found"})
                file_path, mime = found
                size = local_path_size(file_path)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(size))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with local_path_open(file_path, "rb") as source:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        self.wfile.write(block)
                return
            return self._json(404, {"ok": False, "error": "not found"})
        def do_POST(self) -> None:
            if _split_url(self.path).path != "/msg":
                return self._json(404, {"ok": False, "error": "not found"})
            ident = self._identify()
            if ident is None:
                return
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return self._json(415, {"ok": False, "error": "need application/json"})
            try:
                size = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                size = -1
            cap = service.load_policy()["max_message_bytes"]
            if size < 0:
                return self._json(411, {"ok": False, "error": "Content-Length required"})
            if size > cap:
                self.close_connection = True
                return self._json(413, {"ok": False, "error": "message too large"})
            raw = self.rfile.read(size)
            try:
                envelope = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"ok": False, "error": "invalid JSON"})
            code, result = service.handle_envelope(envelope, ident, self.client_address[0], service.port)
            return self._json(code, result)
    return Handler


def create_test_server(service: TailnetService) -> ThreadingHTTPServer:
    """Ephemeral loopback listener for tests only; production uses start_receiver_if_enabled."""
    service.allow_loopback = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(service, test_mode=True))
    server.daemon_threads = True
    service.server = server
    return server


def start_receiver_if_enabled(service: TailnetService | None = None) -> bool:
    """Start an opt-in daemon listener only on a confirmed Tailscale interface."""
    if not enabled():
        return False
    service = service or TailnetService()
    bind_ip = service.tailscale_ip()
    if not bind_ip or not is_tailnet_ip(bind_ip):
        log.error("VEYA_TAILNET_CHANNEL is ON but no Tailscale IP was available; receiver remains off")
        return False
    node = service.self_node()
    if not node:
        log.error("VEYA_TAILNET_CHANNEL is ON but Tailscale node identity is unavailable; receiver remains off")
        return False
    try:
        server = ThreadingHTTPServer((bind_ip, service.port), _make_handler(service))
    except OSError as exc:
        log.error("Tailnet receiver did not start (%s)", type(exc).__name__)
        return False
    server.daemon_threads = True
    service.server = server
    service.thread = threading.Thread(target=server.serve_forever, name="veya-tailnet-receiver", daemon=True)
    service.thread.start()
    _runtime_service_lock.acquire()
    try:
        _runtime_service_holder["service"] = service
    finally:
        _runtime_service_lock.release()
    log.info("Veya Tailnet channel listening on %s:%d", bind_ip, service.port)
    return True


_runtime_service_lock = threading.Lock()
_runtime_service_holder: dict[str, TailnetService] = {}


def stop_receiver() -> None:
    with _runtime_service_lock:
        service = _runtime_service_holder.pop("service", None)
    if service and service.server:
        service.server.shutdown()
        service.server.server_close()
        if service.thread:
            service.thread.join(timeout=5)
        service.server = None
        service.thread = None


def get_service() -> TailnetService:
    with _runtime_service_lock:
        instance = _runtime_service_holder.get("service") or _runtime_service_holder.get("api_service")
        if instance is None:
            instance = TailnetService()
            _runtime_service_holder["api_service"] = instance
        return instance


def send_message(to: str, text: str, *, kind: str = "text", files: list[str] | None = None, wake: str = "batch", timeout_seconds: int = 900, timeout: float = 15) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("VEYA_TAILNET_CHANNEL is OFF")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError("text must be a string no larger than 256 KiB")
    if files is not None and (not isinstance(files, list) or len(files) > 100):
        raise ValueError("files must be a list of at most 100 local paths")
    if kind not in {"text", "notice", "request"}:
        raise ValueError("kind must be text, notice, or request")
    if wake not in WAKE_LEVELS:
        raise ValueError("wake must be now, batch, or none")
    service = get_service()
    recipient, ip, port, agent = service.resolve_peer(to)
    service.authorize_outbound_peer(recipient, ip)
    node = service.self_node()
    if not node:
        raise RuntimeError("local Tailscale identity is unavailable")
    message_id = secrets.token_hex(8)
    envelope: dict[str, Any] = {"v": 1, "id": message_id, "from": node, "to": recipient, "ts": utc_iso(), "kind": kind, "text": text, "wake": wake}
    if agent:
        envelope["agent"] = agent
    if kind == "request":
        if not 1 <= int(timeout_seconds) <= 604800:
            raise ValueError("timeout_seconds must be 1..604800")
        envelope["request"] = {"timeout_seconds": int(timeout_seconds), "reply_port": service.port}
    if files:
        envelope["fetch"] = service.stage_attachments(files, recipient=recipient, message_id=message_id, node=node, port=service.port)
    err = validate_envelope(envelope)
    if err:
        raise ValueError(err)
    try:
        code, response = service.send_json(recipient, ip, port, "POST", "/msg", envelope, timeout=timeout)
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        # Don't retry an uncertain POST: the peer may already have durably stored it.
        raise RuntimeError(f"delivery outcome unknown for message {message_id}; query request status before sending again") from exc
    if code not in {200, 202}:
        raise RuntimeError(f"Tailnet peer rejected message with HTTP {code}: {response.get('error', 'request rejected')}")
    return {"ok": True, "id": message_id, "peer": recipient, "http_status": code, "delivery": "duplicate" if response.get("duplicate") else "accepted", "request_status_url": f"/requests/{message_id}" if kind == "request" else None, "warning": "accepted means stored, not completed" if kind == "request" else None}


def query_request_status(peer: str, request_id: str, *, timeout: float = 10) -> dict[str, Any]:
    if not enabled():
        raise RuntimeError("VEYA_TAILNET_CHANNEL is OFF")
    service = get_service()
    hostname, ip, port, _ = service.resolve_peer(peer)
    service.authorize_outbound_peer(hostname, ip)
    code, response = service.send_json(hostname, ip, port, "GET", "/requests/" + _url_quote(request_id, safe=""), timeout=timeout)
    if code != 200:
        raise RuntimeError(f"Tailnet status query failed with HTTP {code}: {response.get('error', 'request status unavailable')}")
    return response


def register_tools(registry: Any) -> int:
    """Add outbound send/status tools to the one existing master registry."""
    from server.tool_registry import SideEffect
    definitions = [
        ("tailnet_send", "Send a plain-text message or tracked request to an allowlisted Tailscale peer; sending is a network side effect and remains subject to existing tool policy/approval.", {"type": "object", "properties": {"to": {"type": "string", "description": "Tailscale hostname[/agent] or tailnet IP[:port][/agent]; an omitted port targets the Yobiru/tailmsg default 7766. Use :7767 for another Veya receiver unless VEYA_TAILNET_PEER_PORT overrides it."}, "text": {"type": "string"}, "kind": {"type": "string", "enum": ["text", "notice", "request"]}, "wake": {"type": "string", "enum": ["now", "batch", "none"], "default": "batch"}, "files": {"type": "array", "items": {"type": "string"}, "description": "Optional local file paths to stage as SHA-256 verified attachments."}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 604800}}, "required": ["to", "text"]}, lambda **kwargs: send_message(**kwargs), SideEffect.NETWORK_WRITE),
        ("tailnet_request_status", "Query the authenticated remote receiver for the latest receipt/result of a tracked Tailnet request. A timed_out state means the outcome is unknown and is not a safe retry signal.", {"type": "object", "properties": {"peer": {"type": "string"}, "request_id": {"type": "string"}}, "required": ["peer", "request_id"]}, query_request_status, SideEffect.PURE_READ),
        ("tailnet_fetch_attachments", "Fetch the attachments of one received Tailnet message from its authenticated sender, verify declared sizes and SHA-256 hashes, and save inert files only under the Tailnet inbox. This writes local files and remains subject to the normal side-effect policy.", {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]}, lambda message_id: get_service().fetch_attachments(message_id), SideEffect.LOCAL_WRITE),
    ]
    added = 0
    for name, description, schema, fn, effect in definitions:
        if getattr(registry, "has", None) and registry.has(name):
            continue
        registry.register(name=name, description=description, parameters=schema, func=fn, side_effect=effect, effect_capability="none" if effect is SideEffect.PURE_READ else "manual_only")
        added += 1
    return added


__all__ = ["TailnetService", "authorize_peer", "create_test_server", "enabled", "get_service", "is_tailnet_ip", "query_request_status", "register_tools", "send_message", "start_receiver_if_enabled", "stop_receiver", "validate_envelope"]
