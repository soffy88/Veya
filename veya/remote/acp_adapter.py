"""ACP (Agent Client Protocol) L1 Executor Adapter.

Connects external ACP-compatible agents (e.g. OpenHands) via ACPBackend
into the canonical Veya L1 Executor interface.
"""

from __future__ import annotations

import asyncio
import os
import shutil

from server.acp_client import ACPBackend, ACPError
from veya.remote.execution import ProgressReporter


def resolve_acp_command(
    custom_command: list[str] | None = None,
) -> list[str] | None:
    """Resolve configured ACP agent executable command."""
    if custom_command:
        return list(custom_command)
    env_cmd = os.environ.get("VEYA_ACP_COMMAND")
    if env_cmd:
        return env_cmd.strip().split()
    # Check default known ACP agent commands
    for candidate in ("openhands", "acp-agent", "agents-cli"):
        if shutil.which(candidate):
            return [candidate]
    return None


async def run_acp_executor(
    reporter: ProgressReporter,
    *,
    prompt: str,
    command: list[str] | None = None,
    cwd: str | None = None,
    agent: str = "general",
    timeout_s: float = 600.0,
    task_id: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Execute an ACP task conforming to the canonical L1 Runner interface."""
    cmd = resolve_acp_command(command)
    if not cmd:
        reporter.finish_command(exit_code=1, status="failed")
        raise ACPError("No ACP agent command configured or found in PATH")

    backend = ACPBackend(command=cmd, agent=agent, cwd=cwd, env=env)
    reporter.update_message(f"ACP agent starting: {' '.join(cmd)}")

    try:
        sid = await backend.start_session(timeout_s=30.0)
        reporter.update_message(f"ACP session initialized: {sid}")

        tid = task_id or f"acp-{getattr(reporter, '_execution_id', 'task')}"
        run_task = asyncio.create_task(backend.run(prompt, timeout_s=timeout_s, task_id=tid))

        # Stream events to reporter while running
        last_event_idx = 0
        while not run_task.done():
            await asyncio.sleep(0.2)
            events = backend._event_log
            if len(events) > last_event_idx:
                for evt in events[last_event_idx:]:
                    ev_type = evt.get("event", {}).get("type", "activity")
                    msg = evt.get("event", {}).get("message") or f"ACP event: {ev_type}"
                    reporter.update_message(str(msg))
                last_event_idx = len(events)

        result = await run_task
        out_text = result.get("output", "")
        reporter.finish_command(exit_code=0, status="completed")
        return out_text
    except asyncio.CancelledError:
        reporter.update_message("ACP task cancelled")
        await backend.cancel(task_id)
        reporter.finish_command(exit_code=130, status="cancelled")
        raise
    except Exception as exc:
        reporter.finish_command(exit_code=1, status="failed")
        raise ACPError(f"ACP execution failed: {exc}") from exc
    finally:
        await backend.close()


__all__ = ["resolve_acp_command", "run_acp_executor"]
