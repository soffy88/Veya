"""W17 model-callable ssh host tools (flag ``VEYA_SSH_HOSTS`` + ``VEYA_REMOTE_CONTROL``).

Registered into the ONE ``master_tools`` registry, only when both flags are on
(registration-time gate, same shape as ``server/remote_control/tools.py``).

Identity is never a model argument: the owner comes from ``current_bot_id()`` →
``BotRegistry`` and a host is usable only when it is registered to that owner,
enabled, and the W13 identity policy allows the action for its tailnet peer.

Approval (spec §7.1–7.2) — the tool body is the primary gate, so it holds on
both ``execute`` branches (task governance / ActionGateway and tool_guard):

* write / exec / transfer / kill tools need ``approved=true``. The first such
  call (no ``approval_id``) only files a pending record through the existing
  ``ApprovalService`` (via ``server.remote_control.approval``) and returns its
  id; nothing touches the host. After a human resolves it, the model repeats
  the call with ``approval_id``; the record must be approved, be for the same
  tool, host and argument fingerprint, and a ``once`` decision is single-use.
* read tools (read/list/grep) follow the same flow unless the host has
  ``metadata.read_approved = true`` (owner opt-in per host).
* ``ssh_host_list``, ``ssh_host_probe`` and ``ssh_bg_status`` are read-only and
  need no approval.

A ``tool_guard`` policy (``install_w17_host_policy``) adds the secondary,
argument-level check on the non-governance branch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from server.remote_control import _ledger
from server.remote_control import approval as approval_mod
from server.remote_control import flags
from server.remote_control import hosts as hosts_mod
from server.remote_control import identity as identity_mod
from server.remote_control.backend import BackendError, SshBackend

GUARD_POLICY = "w17_ssh_host_gate"
_CONSUMED_ENV = "VEYA_SSH_HOSTS_CONSUMED_PATH"
_CONSUMED_FILE = "remote_control_ssh_consumed.json"

#: Test seams (``None`` → real ssh / real identity).
RUNNER: Any = None
KEY_LOOKUP: Any = None
IDENTITY_RESOLVER: Any = None

READ_TOOLS = frozenset({"ssh_read_file", "ssh_list_dir", "ssh_grep"})
MUTATING_TOOLS = frozenset(
    {"ssh_write_file", "ssh_edit_file", "ssh_run", "ssh_upload", "ssh_download", "ssh_bg_start", "ssh_bg_kill"}
)


class Refused(RuntimeError):
    pass


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _caller_owner_id() -> str:
    from server.remote_control.tools import _caller_owner_id as owner

    return owner()


def _host_for(host_id: str, action: str) -> Any:
    owner = _caller_owner_id()
    if not owner:
        raise Refused("无法确定调用者身份。")
    host = hosts_mod.get_host(str(host_id or ""))
    if host is None or host.owner_id != owner or not host.enabled:
        # Same answer for absent / foreign / disabled: existence never leaks.
        raise Refused(f"找不到主机 {host_id!r}。")
    resolver = IDENTITY_RESOLVER or identity_mod.resolve_identity
    peer = resolver(host.tailnet_ip)
    allowed, reason = identity_mod.evaluate(peer, action)
    if not allowed:
        raise Refused(f"身份策略拒绝该主机（{action}）：{reason}")
    return host


def _backend(host: Any) -> SshBackend:
    return SshBackend(host, runner=RUNNER, key_lookup=KEY_LOOKUP)


def fingerprint(tool: str, host_id: str, args: dict[str, Any]) -> str:
    blob = json.dumps({"tool": tool, "host": host_id, "args": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _summary(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        text = str(value)
        out[key] = text if len(text) <= 300 else f"{text[:300]}…[{len(text)} chars]"
    return out


def _consumed(approval_id: str) -> bool:
    return approval_id in _ledger.read_map(_ledger.default_path(_CONSUMED_ENV, _CONSUMED_FILE))


def _consume(approval_id: str) -> None:
    import time

    def _mutate(rows: dict[str, dict[str, Any]]):
        rows[approval_id] = {"at": time.time()}
        return rows, None

    _ledger.update(_ledger.default_path(_CONSUMED_ENV, _CONSUMED_FILE), _mutate)


async def _gate(tool: str, host: Any, args: dict[str, Any], approved: bool, approval_id: str) -> str | None:
    """``None`` → proceed. Otherwise the text to return (nothing was done)."""

    if not approved:
        return f"已拒绝：{tool} 指向远程主机，需要 approved=true（经既有审批通道人工批准）后才能执行。"
    fp = fingerprint(tool, host.host_id, args)
    if not approval_id:
        aid = await approval_mod.request_approval(
            tool=tool,
            user_id=host.owner_id,
            session_id=f"ssh:{host.host_id}",
            args={**_summary(args), "fingerprint": fp},
            reason=f"W17 ssh: {tool} on host {host.host_id}",
            host_id=host.host_id,
        )
        return _dump(
            {
                "status": "approval_pending",
                "approval_id": aid,
                "message": "已提交审批；用户批准后用同样参数并带上 approval_id 再调用一次。尚未对主机做任何操作。",
            }
        )
    record = approval_mod._service().get(approval_id) or {}
    targs = record.get("tool_args") or {}
    if (
        str(record.get("tool") or "") != tool
        or str(targs.get("host_id") or "") != host.host_id
        or str(targs.get("fingerprint") or "") != fp
    ):
        return "已拒绝：该 approval_id 不是为这次调用（同一工具、主机与参数）签发的。"
    try:
        approval_mod.require_approved(approval_id)
    except approval_mod.RemoteApprovalRequired as exc:
        return f"已拒绝：{exc}"
    _status, decision = approval_mod.approval_state(approval_id)
    if decision == "once":
        if _consumed(approval_id):
            return "已拒绝：该一次性审批已被使用。"
        _consume(approval_id)
    return None


def _read_needs_approval(host: Any) -> bool:
    return not bool((host.metadata or {}).get("read_approved"))


async def _guarded(tool: str, host_id: str, args: dict[str, Any], approved: bool, approval_id: str,
                   action, *, read: bool = False) -> str:
    try:
        host = _host_for(host_id, "execute")
        if not read or _read_needs_approval(host):
            blocked = await _gate(tool, host, args, approved, approval_id)
            if blocked is not None:
                return blocked
        return await asyncio.to_thread(action, _backend(host))
    except (Refused, BackendError) as exc:
        return f"已拒绝：{exc}"


# ── tools ──────────────────────────────────────────────────────────────────


async def ssh_host_list() -> str:
    owner = _caller_owner_id()
    if not owner:
        return "没有可用的主机：当前没有可识别的机器人身份。"
    keys = ("host_id", "display_name", "tailnet_ip", "ssh_user", "ssh_port", "os_family",
            "write_root", "default_cwd", "enabled")
    return _dump([{k: getattr(h, k) for k in keys} | {"read_approved": bool((h.metadata or {}).get("read_approved"))}
                  for h in hosts_mod.list_hosts(owner_id=owner)])


async def ssh_host_probe(host_id: str) -> str:
    from server.remote_control import ssh as ssh_mod

    try:
        host = _host_for(host_id, "wake")
    except Refused as exc:
        return f"已拒绝：{exc}"
    result = await asyncio.to_thread(ssh_mod.probe, host, runner=RUNNER, key_lookup=KEY_LOOKUP)
    if result.reachable:
        hosts_mod.touch_seen(host.host_id)
    return _dump({"host_id": host.host_id, **result.to_dict()})


async def ssh_read_file(host_id: str, path: str, max_bytes: int = 200000, approved: bool = False,
                        approval_id: str = "") -> str:
    args = {"path": path, "max_bytes": int(max_bytes)}
    return await _guarded("ssh_read_file", host_id, args, approved, approval_id,
                          lambda b: b.read_text(path, max_bytes=int(max_bytes)), read=True)


async def ssh_list_dir(host_id: str, path: str = "", approved: bool = False, approval_id: str = "") -> str:
    return await _guarded("ssh_list_dir", host_id, {"path": path}, approved, approval_id,
                          lambda b: _dump(b.list_dir(path)), read=True)


async def ssh_grep(host_id: str, pattern: str, root: str = "", glob: str = "", max_results: int = 100,
                   approved: bool = False, approval_id: str = "") -> str:
    args = {"pattern": pattern, "root": root, "glob": glob, "max_results": int(max_results)}
    return await _guarded("ssh_grep", host_id, args, approved, approval_id,
                          lambda b: b.grep(pattern, root=root, glob=glob, max_results=int(max_results)) or "(no matches)",
                          read=True)


async def ssh_write_file(host_id: str, path: str, content: str, overwrite: bool = True, approved: bool = False,
                         approval_id: str = "") -> str:
    args = {"path": path, "content_sha256": hashlib.sha256(str(content).encode()).hexdigest(),
            "content_preview": str(content)[:200], "bytes": len(str(content).encode()), "overwrite": bool(overwrite)}
    return await _guarded("ssh_write_file", host_id, args, approved, approval_id,
                          lambda b: b.write_text(path, content, overwrite=bool(overwrite)))


async def ssh_edit_file(host_id: str, path: str, old_text: str, new_text: str, approved: bool = False,
                        approval_id: str = "") -> str:
    args = {"path": path, "old_sha256": hashlib.sha256(old_text.encode()).hexdigest(),
            "new_sha256": hashlib.sha256(new_text.encode()).hexdigest(),
            "old_preview": old_text[:150], "new_preview": new_text[:150]}
    return await _guarded("ssh_edit_file", host_id, args, approved, approval_id,
                          lambda b: b.replace_text(path, old_text, new_text))


async def ssh_run(host_id: str, command: str, cwd: str = "", timeout_s: float = 60.0, approved: bool = False,
                  approval_id: str = "") -> str:
    args = {"command": command, "cwd": cwd, "timeout_s": float(timeout_s)}
    bounded = max(1.0, min(float(timeout_s), 900.0))
    return await _guarded("ssh_run", host_id, args, approved, approval_id,
                          lambda b: _dump(b.run(command, cwd=cwd, timeout_s=bounded).to_dict()))


async def ssh_upload(host_id: str, local_path: str, remote_path: str, approved: bool = False,
                     approval_id: str = "") -> str:
    args = {"local_path": local_path, "remote_path": remote_path}
    return await _guarded("ssh_upload", host_id, args, approved, approval_id,
                          lambda b: b.put_file(local_path, remote_path))


async def ssh_download(host_id: str, remote_path: str, local_path: str, approved: bool = False,
                       approval_id: str = "") -> str:
    args = {"remote_path": remote_path, "local_path": local_path}
    return await _guarded("ssh_download", host_id, args, approved, approval_id,
                          lambda b: b.get_file(remote_path, local_path))


async def ssh_bg_start(host_id: str, name: str, command: str, cwd: str = "", approved: bool = False,
                       approval_id: str = "") -> str:
    args = {"name": name, "command": command, "cwd": cwd}
    return await _guarded("ssh_bg_start", host_id, args, approved, approval_id,
                          lambda b: _dump(b.spawn_bg(command, name=name, cwd=cwd).to_dict()))


async def ssh_bg_status(host_id: str, name: str) -> str:
    from server.remote_control.sessions import evidence_from_bg_status

    try:
        host = _host_for(host_id, "execute")
        status = await asyncio.to_thread(_backend(host).bg_status, name)
    except (Refused, BackendError) as exc:
        return f"已拒绝：{exc}"
    evidence = evidence_from_bg_status(status)
    return _dump({**status, "evidence": evidence.to_dict(), "verified": evidence.usable})


async def ssh_bg_kill(host_id: str, name: str, approved: bool = False, approval_id: str = "") -> str:
    return await _guarded("ssh_bg_kill", host_id, {"name": name}, approved, approval_id,
                          lambda b: b.bg_kill(name))


# ── schemas / registration ─────────────────────────────────────────────────

_HOST = {"type": "string", "description": "已登记的 ssh 主机 id（见 ssh_host_list）。"}
_APPROVAL = {
    "approved": {"type": "boolean", "description": "必须为 true 才会提交/使用审批。"},
    "approval_id": {"type": "string", "description": "用户已批准的审批 id（首次调用留空以提交审批）。"},
}


def _schema(props: dict[str, Any], required: list[str], *, mutating: bool, approvable: bool = True) -> dict[str, Any]:
    properties = {"host_id": _HOST, **props}
    req = ["host_id", *required]
    if approvable:
        properties.update(_APPROVAL)
        if mutating:
            req.append("approved")
    return {"type": "object", "properties": properties, "required": req}


_S = {"type": "string"}
_TOOLS: list[tuple[str, str, dict[str, Any], Any, str]] = [
    ("ssh_host_list", "列出当前用户登记的 ssh 主机（地址、用户、OS、write_root）。只读。",
     {"type": "object", "properties": {}, "required": []}, ssh_host_list, "pure_read"),
    ("ssh_host_probe", "探测 ssh 主机连通性（ssh BatchMode 执行 uname -s）。只读，无副作用。",
     _schema({}, [], mutating=False, approvable=False), ssh_host_probe, "pure_read"),
    ("ssh_read_file", "读取远程主机上的文件（限 write_root/default_cwd 内）。除非主机配置 read_approved，需审批。",
     _schema({"path": _S, "max_bytes": {"type": "integer"}}, ["path"], mutating=False), ssh_read_file, "pure_read"),
    ("ssh_list_dir", "列出远程主机目录（限可读根内）。除非主机配置 read_approved，需审批。",
     _schema({"path": _S}, [], mutating=False), ssh_list_dir, "pure_read"),
    ("ssh_grep", "在远程主机上搜索文本（rg，回退 grep -E）。除非主机配置 read_approved，需审批。",
     _schema({"pattern": _S, "root": _S, "glob": _S, "max_results": {"type": "integer"}}, ["pattern"],
             mutating=False), ssh_grep, "pure_read"),
    ("ssh_write_file", "在远程主机 write_root 内写文件。外部变更：需 approved=true 且审批通过。",
     _schema({"path": _S, "content": _S, "overwrite": {"type": "boolean"}}, ["path", "content"], mutating=True),
     ssh_write_file, "external_mutation"),
    ("ssh_edit_file", "在远程主机 write_root 内做精确唯一替换（old_text→new_text）。外部变更：需审批。",
     _schema({"path": _S, "old_text": _S, "new_text": _S}, ["path", "old_text", "new_text"], mutating=True),
     ssh_edit_file, "external_mutation"),
    ("ssh_run", "在远程主机上执行一条命令并返回输出（脱敏、截断、超时）。外部变更：需审批；灾难性命令直接拒绝。",
     _schema({"command": _S, "cwd": _S, "timeout_s": {"type": "number"}}, ["command"], mutating=True),
     ssh_run, "external_mutation"),
    ("ssh_upload", "把本机写根内的文件上传到远程主机 write_root。外部变更：需审批。",
     _schema({"local_path": _S, "remote_path": _S}, ["local_path", "remote_path"], mutating=True),
     ssh_upload, "external_mutation"),
    ("ssh_download", "把远程主机文件下载到本机写根。外部变更：需审批。",
     _schema({"remote_path": _S, "local_path": _S}, ["remote_path", "local_path"], mutating=True),
     ssh_download, "external_mutation"),
    ("ssh_bg_start", "在远程主机后台启动长任务（nohup/setsid，日志在 write_root/.veya/run/<name>.log）。外部变更：需审批。",
     _schema({"name": _S, "command": _S, "cwd": _S}, ["name", "command"], mutating=True),
     ssh_bg_start, "external_mutation"),
    ("ssh_bg_status", "查询远程后台任务：是否结束、退出码、日志尾，并给出验收证据。只读。",
     _schema({"name": _S}, ["name"], mutating=False, approvable=False), ssh_bg_status, "pure_read"),
    ("ssh_bg_kill", "终止远程后台任务。外部变更：需审批。",
     _schema({"name": _S}, ["name"], mutating=True), ssh_bg_kill, "external_mutation"),
]


def host_policy(name: str, kwargs: dict, source: str) -> str | None:
    """tool_guard policy (secondary gate, non-governance branch only)."""

    if name in MUTATING_TOOLS and kwargs.get("approved") is not True:
        return f"{name} targets a remote host and needs approved=true plus an approved approval_id"
    return None


def install_w17_host_policy(guard: Any = None) -> None:
    """Idempotent; enforce mode (the body re-checks anyway)."""

    from server.tool_guard import global_tool_guard

    target = guard or global_tool_guard
    if not target.has_policy(GUARD_POLICY):
        target.register_policy(GUARD_POLICY, host_policy, enforce=True)


def register_tools(registry: Any = None) -> int:
    """Register the W17 tools when both flags are on. Returns how many were added."""

    if not flags.ssh_enabled():
        return 0
    from server.tool_registry import SideEffect, master_tools

    target = registry if (registry is not None and hasattr(registry, "has")) else master_tools
    added = 0
    for name, description, parameters, func, effect in _TOOLS:
        if target.has(name):
            continue
        read = effect == "pure_read"
        target.register(
            name,
            description,
            parameters,
            func,
            max_result_chars=8000,
            parallel_safe=read,
            side_effect=SideEffect.PURE_READ if read else SideEffect.EXTERNAL_MUTATION,
            effect_capability="none" if read else "manual_only",
        )
        added += 1
    install_w17_host_policy()
    return added


__all__ = [
    "GUARD_POLICY",
    "MUTATING_TOOLS",
    "READ_TOOLS",
    "fingerprint",
    "host_policy",
    "install_w17_host_policy",
    "register_tools",
]
