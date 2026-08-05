"""veya.cli.main — interactive / one-shot CLI on top of the assembled engines.

Usage::

    python -m veya.cli.main run "<task description>"          # run a task
    python -m veya.cli.main run "<task>" --dry-run            # print manifest plan
    python -m veya.cli.main run "<task>" --resume <session>   # resume a session
    python -m veya.cli.main history <session_id>              # show decision trail

The CLI instantiates the ``oservi.agentic_loop`` engine through the
``ServiceManifest`` assembly (``veya.server.manifests``), streams every
``decision_trail`` step to the terminal via ``rich`` (when available), and
supports memory resume through the long-task memory workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from veya.server.manifests import load_decision_trail, save_decision_trail

# Optional rich dependency (pyproject interactive extras) — degrade to print.
try:  # pragma: no cover - exercised in real terminal
    from rich.console import Console
    from rich.panel import Panel

    _HAS_RICH = True
except Exception:  # pragma: no cover - fallback path
    _HAS_RICH = False

_console: Any = None


def _get_console() -> Any:
    global _console
    if _console is None:
        _console = Console() if _HAS_RICH else None
    return _console


# ---------------------------------------------------------------------------
# on_step streaming callback (decision trail → terminal)
# ---------------------------------------------------------------------------


def _format_step(step: dict) -> str:
    """Render a single decision-trail step as a terminal line (rich or plain)."""
    event = step.get("event", "step")
    step_no = step.get("step_no")
    label = f"[step {step_no}] {event}" if step_no is not None else event
    detail = step.get("detail") or step.get("data") or step.get("tool") or ""
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)[:160]
    return f"{label}: {detail}" if detail else label


def make_on_step(console: Any | None = None) -> Any:
    """Return an ``on_step`` callback that streams steps to the terminal.

    Supports both sync and async consumers: the callback itself is a plain
    function (engines may call it from sync or async contexts).
    """

    def on_step(step: dict) -> None:
        line = _format_step(step)
        ts = step.get("ts", "")
        stamp = f"{ts:.3f}" if isinstance(ts, float) else ""
        if console is not None:
            if step.get("event") == "session_done":
                console.print(Panel.fit(line, style="bold green"))
            elif step.get("event") in ("llm_call", "thinking"):
                console.print(
                    f"[dim]{stamp}[/dim] [cyan]{line}[/cyan]" if stamp else f"[cyan]{line}[/cyan]"
                )
            elif step.get("event") in ("tool_call", "tool_result"):
                console.print(
                    f"[dim]{stamp}[/dim] [yellow]{line}[/yellow]"
                    if stamp
                    else f"[yellow]{line}[/yellow]"
                )
            else:
                console.print(f"[dim]{stamp}[/dim] {line}" if stamp else line)
        else:
            print(line, flush=True)

    return on_step


# ---------------------------------------------------------------------------
# Task runner (agentic loop)
# ---------------------------------------------------------------------------


async def run_task(
    task: str,
    *,
    resume: str | None = None,
    dry_run: bool = False,
    config: dict[str, Any] | None = None,
    on_step: Any | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Execute a task through the Agent OS master brain.

    Returns {result / status / cost / session_id / events}. In ``dry_run``
    mode returns a plan stub without executing anything.
    """
    if dry_run:
        return {"status": "dry_run", "plan": "Agent OS master brain (dry run)", "session_id": session_id}

    # memory restore (decision trail) — resume path
    if resume:
        steps = load_decision_trail(resume)
        if steps:
            print(
                f"◆ Resumed session {resume}: {len(steps)} decision steps loaded", file=sys.stderr
            )

    from server.coordinator_master import master_coordinator

    if on_step is None:
        on_step = make_on_step(_get_console())

    on_step({"event": "session_start", "session_id": session_id or "cli", "ts": time.time()})

    result = await master_coordinator.chat_stream(task, session_id=session_id, max_rounds=3)

    events = [{"event": "session_done", "session_id": result.get("session_id", "cli"), "ts": time.time()}]
    on_step(events[0])

    # persist decision trail
    sid = result.get("session_id") or session_id or "cli"
    trail = load_decision_trail(sid)
    trail.extend(events)
    save_decision_trail(sid, trail)

    return {
        "result": result.get("final_answer") or result.get("error", ""),
        "status": result.get("status", "failed"),
        "cost_usd": result.get("cost_usd", 0.0),
        "session_id": sid,
        "events": events,
    }


# ---------------------------------------------------------------------------
# argparse CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veya",
        description="veya — Layer 4 CLI on the assembled 3O agentic engines",
    )
    p.add_argument("--version", action="version", version="veya 0.5.1 (Layer 4)")
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run an agentic task")
    run_p.add_argument("task", nargs="+", help="Task description (quote it)")
    run_p.add_argument("--resume", metavar="SESSION_ID", default=None, help="Resume a session")
    run_p.add_argument(
        "--dry-run", action="store_true", help="Print the assembly plan, do not execute"
    )
    run_p.add_argument("--config", default=None, help="Path to a JSON config file")
    run_p.add_argument("--session-id", default=None, help="Explicit session id")

    hist_p = sub.add_parser("history", help="Show a session's decision trail")
    hist_p.add_argument("session_id")
    return p


def _load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        print(f"config not found: {path}", file=sys.stderr)
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"invalid config JSON: {exc}", file=sys.stderr)
        return {}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _load_config(getattr(args, "config", None))

    if args.command == "run":
        task = " ".join(args.task)
        result = asyncio.run(
            run_task(
                task,
                resume=args.resume,
                dry_run=args.dry_run,
                config=config,
                session_id=args.session_id,
            )
        )
        if args.dry_run:
            plan = result["plan"]
            console = _get_console()
            if console is not None:
                console.print(Panel(str(plan), title="Agent OS master brain (dry run)"))
            else:
                print(str(plan))
            print("dry-run: OK — nothing executed")
            return 0

        print(f"\n[{result['status']}] cost=${result.get('cost_usd', 0.0):.6f}")
        if isinstance(result.get("result"), dict):
            print(json.dumps(result["result"], ensure_ascii=False, indent=2)[:4000])
        else:
            print(str(result.get("result", ""))[:4000])
        return 0 if result.get("status") == "completed" else 1

    if args.command == "history":
        steps = load_decision_trail(args.session_id)
        if not steps:
            print(f"no decision trail for session {args.session_id}")
            return 1
        for step in steps:
            print(_format_step(step))
        return 0

    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
