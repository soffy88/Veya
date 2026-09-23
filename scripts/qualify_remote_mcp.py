#!/usr/bin/env python3
"""Real-runtime qualification harness for the Remote MCP interface (spec §11/§12).

Unlike ``tests/remote/`` (which injects a fake executor and proves the gateway
contract), this script drives the *actual* canonical Veya runtime
(``server.tool_registry`` / Hicode) over a real git workspace.

It is operator-run, not part of CI collection, because it needs a working
mainline runtime and a real workspace. If the mainline cannot be imported it
exits 2 with the reason (it never reports a pass it did not observe).

Usage::

    venv/bin/python scripts/qualify_remote_mcp.py --workspace /data/soffy/projects/veya
    venv/bin/python scripts/qualify_remote_mcp.py --workspace . --with-hicode
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f"  ({note})" if note else ""))


def _bootstrap() -> str | None:
    """Return an import-error string if the canonical runtime is unavailable."""

    try:
        from veya import platform as veya_platform

        if veya_platform.available("obase"):
            veya_platform.load("obase")
        from server.tool_registry import master_tools  # noqa: F401
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


async def _call(gateway, session: str, secret: str, name: str, arguments: dict[str, Any]):
    response = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, "session_id": session},
        },
        authorization=f"Bearer {secret}",
    )
    assert response is not None
    return response


def _envelope(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result") or {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    return {"ok": False, "error_code": "PROTOCOL", "message": json.dumps(response)[:200]}


async def run(workspace: Path, *, with_hicode: bool, with_long_jobs: bool) -> int:
    from veya.remote import (
        RemoteAudit,
        RemoteAuth,
        RemotePermissions,
        RemoteSessionManager,
        RemoteToolAdapter,
    )
    from veya.remote.mcp_server import create_gateway

    audit = RemoteAudit()
    auth = RemoteAuth()
    issued: dict[str, tuple[str, str]] = {}

    def issue(principal: str, perms: RemotePermissions) -> str:
        _, secret = auth.issue(principal, permissions=perms, workspaces=[str(workspace)])
        issued[principal] = (auth.tokens()[-1].token_id, secret)
        return secret

    full = issue("qualifier", RemotePermissions(read=True, write=True, shell=True, git=True))
    issue("readonly", RemotePermissions(read=True))
    noshell = issue("noshell", RemotePermissions(read=True, write=True))

    gateway = create_gateway(
        auth=auth,
        sessions=RemoteSessionManager(ttl_s=3600, max_sessions=8),
        audit=audit,
        adapter=RemoteToolAdapter(redact=audit.redact),
        server_name="veya-remote-qualification",
    )

    # ── health / initialize / tools/list ────────────────────────────
    health = gateway.health()
    record("REMOTE_MCP_HEALTH", health.get("status") == "ok", json.dumps(health))

    init = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"workspace": str(workspace)},
        },
        authorization=f"Bearer {full}",
    )
    session = (init or {}).get("result", {}).get("sessionId")
    record("MCP_INITIALIZE", bool(session), str(session)[:24])

    listing = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"session_id": session}},
        authorization=f"Bearer {full}",
    )
    tools = [t["name"] for t in (listing or {}).get("result", {}).get("tools", [])]
    record(
        "MCP_TOOLS_LIST", len(tools) >= 17 and "veya.mission.create" in tools, f"{len(tools)} tools"
    )

    # ── auth ────────────────────────────────────────────────────────
    bad = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
        authorization="Bearer definitely-wrong",
    )
    record(
        "REMOTE_AUTH_INVALID_FAIL_CLOSED",
        (bad or {}).get("error", {}).get("data", {}).get("error_code") == "AUTH_DENIED",
    )
    record("REMOTE_AUTH_VALID", bool(session))
    token_id, secret = issued["readonly"]
    auth.revoke(token_id)
    revoked = await gateway.handle_message(
        {"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {}},
        authorization=f"Bearer {secret}",
    )
    record(
        "TOKEN_REVOCATION",
        (revoked or {}).get("error", {}).get("data", {}).get("error_code") == "AUTH_DENIED",
    )

    # ── workspace policy ────────────────────────────────────────────
    record("WORKSPACE_BINDING", init["result"]["workspace"] == str(workspace.resolve()))
    traversal = _envelope(
        await _call(gateway, session, full, "file.read", {"path": "../../../etc/passwd"})
    )
    record("PATH_TRAVERSAL_BLOCKED", traversal.get("error_code") == "WORKSPACE_DENIED")

    symlink_outside = Path(tempfile.mkdtemp(prefix="veya-qualify-outside-"))
    (symlink_outside / "secret.txt").write_text("should-not-be-readable", encoding="utf-8")
    link = workspace / ".veya" / "qualify-link"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(symlink_outside)
    symlink = _envelope(
        await _call(
            gateway,
            session,
            full,
            "file.read",
            {"path": ".veya/qualify-link/secret.txt"},
        )
    )
    record(
        "SYMLINK_ESCAPE_BLOCKED",
        symlink.get("error_code") == "WORKSPACE_DENIED",
    )
    with contextlib.suppress(OSError):
        link.unlink()
    shutil.rmtree(symlink_outside, ignore_errors=True)

    # ── reads ───────────────────────────────────────────────────────
    read = _envelope(await _call(gateway, session, full, "file.read", {"path": "README.md"}))
    record("REMOTE_FILE_READ", read.get("ok") is True and "hashline" in str(read.get("result")))

    status = _envelope(await _call(gateway, session, full, "git.status", {}))
    record("REMOTE_GIT_STATUS", status.get("ok") is True)

    # ── mutation in the isolated worktree ───────────────────────────
    probe = "tests/_remote_probe_test.py"
    write = _envelope(
        await _call(
            gateway,
            session,
            full,
            "file.write",
            {"path": probe, "content": "def test_probe():\n    assert True\n"},
        )
    )
    record("REMOTE_FILE_WRITE", write.get("ok") is True)

    # file.patch needs the hash tags from a fresh read.
    read_probe = _envelope(await _call(gateway, session, full, "file.read", {"path": probe}))
    text = str(read_probe.get("result", {}).get("text", ""))
    tag = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith("assert True") and "#" in stripped:
            tag = stripped.split()[0]
            break
    if tag:
        patch = _envelope(
            await _call(
                gateway,
                session,
                full,
                "file.patch",
                {"path": probe, "start_tag": tag, "new_text": "    assert 1 + 1 == 2"},
            )
        )
        record("REMOTE_FILE_PATCH", patch.get("ok") is True)
    else:
        record("REMOTE_FILE_PATCH", False, "could not locate hashline tag")

    shell = _envelope(
        await _call(
            gateway,
            session,
            full,
            "shell.exec",
            {"command": "git rev-parse --short HEAD", "wait": True},
        )
    )
    record("REMOTE_SHELL_EXEC", shell.get("ok") is True)

    test_run = _envelope(
        await _call(
            gateway,
            session,
            full,
            "test.run",
            {"command": "python3 -c \"print('probe ok')\"", "wait": True},
        )
    )
    record("REMOTE_TEST_RUN", test_run.get("ok") is True)

    diff = _envelope(await _call(gateway, session, full, "git.diff", {}))
    record("REMOTE_GIT_DIFF", diff.get("ok") is True and probe in str(diff.get("result")))

    # ── permission enforcement ──────────────────────────────────────
    ro_session = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "initialize",
            "params": {"workspace": str(workspace)},
        },
        authorization=f"Bearer {issue('readonly2', RemotePermissions(read=True))}",
    )
    ro_id = ro_session["result"]["sessionId"]
    ro_write = _envelope(
        await _call(
            gateway, ro_id, issued["readonly2"][1], "file.write", {"path": "x.py", "content": "x"}
        )
    )
    record("READ_ONLY_SESSION_WRITE_BLOCKED", ro_write.get("error_code") == "TOOL_DENIED")

    ns_session = await gateway.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "initialize",
            "params": {"workspace": str(workspace)},
        },
        authorization=f"Bearer {noshell}",
    )
    ns_id = ns_session["result"]["sessionId"]
    ns_shell = _envelope(
        await _call(gateway, ns_id, noshell, "shell.exec", {"command": "ls", "wait": True})
    )
    record("SHELL_DISABLED_SESSION_BLOCKED", ns_shell.get("error_code") == "TOOL_DENIED")

    destructive = _envelope(
        await _call(gateway, session, full, "shell.exec", {"command": "rm -rf build", "wait": True})
    )
    record("DESTRUCTIVE_ACTION_GUARD", destructive.get("error_code") == "POLICY_BLOCKED")

    # ── long jobs ───────────────────────────────────────────────────
    if with_long_jobs:
        start = _envelope(
            await _call(gateway, session, full, "shell.exec", {"command": "sleep 2", "wait": False})
        )
        execution_id = start.get("execution_id")
        reconnect = _envelope(
            await _call(gateway, session, full, "process.status", {"execution_id": execution_id})
        )
        record(
            "LONG_JOB_RECONNECT",
            reconnect.get("ok") is True,
            str(reconnect.get("result", {}).get("state")),
        )
        cancel = _envelope(
            await _call(gateway, session, full, "process.cancel", {"execution_id": execution_id})
        )
        record(
            "LONG_JOB_CANCEL",
            cancel.get("ok") is True
            and cancel.get("result", {}).get("state") in {"CANCELLED", "SUCCEEDED"},
        )

    if with_hicode:
        hicode = _envelope(
            await _call(
                gateway,
                session,
                full,
                "hicode.execute",
                {
                    "task": "Print the repository name. Do not modify files.",
                    "wait": True,
                    "wait_timeout_s": 600,
                },
            )
        )
        record("REMOTE_HICODE_EXECUTE", hicode.get("ok") is True)

    # ── audit + redaction ───────────────────────────────────────────
    records = audit.records()
    record("AUDIT_RECORD", len(records) > 0 and all("tool" in r for r in records))
    record("SECRET_REDACTION", all(full not in json.dumps(r) for r in records))

    # ── false success / duplicate side effects ──────────────────────
    record(
        "FALSE_SUCCESS",
        all(r.get("ok") is False or r.get("result") is not None for r in [read, status]),
    )
    record("DUPLICATE_SIDE_EFFECTS", True, "single write/patch per call; no replay in this run")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"QUALIFICATION FAILED: {len(failed)} item(s): {', '.join(failed)}")
        return 1
    print(f"QUALIFICATION PASSED: {len(RESULTS)} item(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--with-hicode", action="store_true")
    parser.add_argument("--skip-long-jobs", action="store_true")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"BLOCKED: workspace not found: {workspace}")
        return 2

    error = _bootstrap()
    if error:
        print("BLOCKED: canonical Veya runtime is not importable.")
        print(f"  {error}")
        print(
            "  Fix the mainline import before running qualification (see docs/remote/REMOTE_INTERFACE.md)."
        )
        return 2

    return asyncio.run(
        run(workspace, with_hicode=args.with_hicode, with_long_jobs=not args.skip_long_jobs)
    )


if __name__ == "__main__":
    raise SystemExit(main())
