"""Qualify the DSH execution plane over the Remote MCP supervision surface.

DSH is a Veya *executor*, not a second model router: it runs through the canonical
project dispatch (``server.project_ask``) with a placeholder credential pointed at
the Veya LLM gateway. This client drives the real MCP server, so what it proves is
what a remote supervisor (ChatGPT) gets.

Usage::

    python scripts/qualify_remote_dsh.py create <mode> <workspace> <goal...>
    python scripts/qualify_remote_dsh.py run <workspace> <mission_id>
    python scripts/qualify_remote_dsh.py inspect <workspace> <mission_id>
    python scripts/qualify_remote_dsh.py latest <workspace> <mission_id>
    python scripts/qualify_remote_dsh.py review <workspace> <mission_id> <decision> [note...]
    python scripts/qualify_remote_dsh.py continue <workspace> <mission_id>

Token comes from ``VEYA_REMOTE_TOKEN`` or ``~/.veya/remote_mcp_token.secret``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


def _token() -> str:
    token = os.environ.get("VEYA_REMOTE_TOKEN", "").strip()
    if token:
        return token
    secret = Path("~/.veya/remote_mcp_token.secret").expanduser()
    return secret.read_text(encoding="utf-8").strip() if secret.is_file() else ""


def _text(result: Any) -> str:
    parts = []
    for item in getattr(result, "content", []) or []:
        parts.append(getattr(item, "text", "") or "")
    return "".join(parts)


async def _call(session: Any, name: str, arguments: dict[str, Any]) -> Any:
    result = await session.call_tool(name, arguments)
    raw = _text(result)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw[:2000]}


async def _poll(session: Any, job: dict[str, Any], timeout_s: float = 1800.0) -> dict[str, Any]:
    """Wait for a long-running MCP job to finish (jobs are session scoped)."""
    inner = job.get("result") if isinstance(job.get("result"), dict) else {}
    execution_id = job.get("execution_id") or inner.get("execution_id") or job.get("job_id")
    if not execution_id:
        return job
    waited = 0.0
    while waited < timeout_s:
        await asyncio.sleep(5)
        waited += 5
        status = await _call(session, "process.status", {"execution_id": execution_id})
        payload = status.get("result") if isinstance(status.get("result"), dict) else status
        state = str(payload.get("state") or payload.get("status") or "")
        if state.lower() in {"succeeded", "failed", "cancelled", "timeout"}:
            return payload
    return {"state": "timeout", "execution_id": execution_id}


async def _session(workspace: str):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ.get("VEYA_REMOTE_MCP_URL", "http://127.0.0.1:8790/mcp")
    headers = {"Authorization": f"Bearer {_token()}"}
    return streamablehttp_client(url, headers=headers), ClientSession


async def _run(action: str, argv: list[str]) -> int:
    transport, client_cls = await _session(argv[0] if argv else "")
    out: dict[str, Any] = {"action": action}
    async with transport as (read, write, _), client_cls(read, write) as session:
        await session.initialize()
        mode = argv[0] if action == "create" else ""
        workspace = argv[1] if action == "create" else argv[0]
        goal = " ".join(argv[2:]) if action == "create" else ""
        if action == "create":
            out = await _call(
                session,
                "veya.mission.create",
                {
                    "workspace": workspace,
                    "goal": goal,
                    "supervision_mode": mode,
                    "executor": "dsh",
                },
            )
        elif action == "run":
            job = await _call(
                session, "veya.mission.run", {"workspace": workspace, "mission_id": argv[1]}
            )
            out = {"job": job, "final": await _poll(session, job)}
        elif action == "inspect":
            out = await _call(
                session, "veya.mission.inspect", {"workspace": workspace, "mission_id": argv[1]}
            )
        elif action == "latest":
            out = await _call(
                session, "veya.report.latest", {"workspace": workspace, "mission_id": argv[1]}
            )
        elif action == "review":
            review = {"decision": argv[2], "note": " ".join(argv[3:])}
            out = await _call(
                session,
                "veya.review.apply",
                {"workspace": workspace, "mission_id": argv[1], "review": review},
            )
        elif action == "continue":
            out = await _call(
                session, "veya.mission.continue", {"workspace": workspace, "mission_id": argv[1]}
            )
        else:
            print(f"unknown action: {action}", file=sys.stderr)
            return 2
    print(json.dumps(out, ensure_ascii=False, default=str)[:4000])
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    return asyncio.run(_run(sys.argv[1], sys.argv[2:]))


if __name__ == "__main__":
    raise SystemExit(main())
