"""
cli/main.py — veya 交互入口(readline-based interactive loop)

用法:
    veya                      # 交互模式
    veya --help               # 帮助
    echo "改 foo.py" | veya   # stdin 单次执行
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from typing import Any


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="veya",
        description="veya — AI coding agent (interactive mode)",
    )
    p.add_argument(
        "--persona",
        default="build",
        choices=["build", "research", "plan", "execute"],
        help="Agent persona (default: build)",
    )
    p.add_argument("--config", default=None, help="Config file path")
    p.add_argument("--resume", metavar="SESSION_ID", help="Resume a checkpointed session")
    p.add_argument("--version", action="version", version="veya 0.5.1")
    return p


async def _run_once(text: str, *, persona: str) -> dict[str, Any]:
    from server.coordinator import coordinator

    return await coordinator.handle({"text": text, "persona": persona})


async def _interactive_loop(persona: str) -> None:
    from server.coordinator import coordinator

    print(f"veya 0.5.1 | persona={persona} | Ctrl-D or 'exit' to quit", file=sys.stderr)
    session_id: str | None = None

    with contextlib.suppress(ImportError):
        import readline  # noqa: F401 — enables arrow-key editing on supported platforms

    while True:
        try:
            line = input("veya> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.", file=sys.stderr)
            break

        if not line or line.startswith("#"):
            continue
        if line.lower() in ("exit", "quit", "q"):
            break

        result = await coordinator.handle(
            {"text": line, "persona": persona},
            session_id=session_id,
        )
        session_id = result.get("session_id") or session_id

        status = result.get("status", "?")
        cost = result.get("cost_usd", 0.0)
        print(f"\n[{status}] cost=${cost:.6f}")

        output = result.get("output") or result.get("squads")
        if output:
            import json

            if isinstance(output, str):
                print(output)
            else:
                print(json.dumps(output, ensure_ascii=False, indent=2))
        print()


async def _resume(session_id: str, persona: str) -> None:
    from server.checkpoint import load_checkpoint
    from veya.compat import checkpoint_to_run_state

    ckpt = await load_checkpoint(session_id)
    if not ckpt:
        print(f"No checkpoint found for session '{session_id}'", file=sys.stderr)
        sys.exit(1)

    state = checkpoint_to_run_state(ckpt)
    print(
        f"Resuming session {session_id} from step {state.step}, completed: {state.completed_steps}",
        file=sys.stderr,
    )

    from server.coordinator import coordinator

    result = await coordinator.resume(state)
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    from config.loader import load_config
    from server.assembly import Infra

    args = _build_parser().parse_args(argv)
    Infra.init(load_config(args.config if hasattr(args, "config") else None))

    if args.resume:
        asyncio.run(_resume(args.resume, args.persona))
        return 0

    # Non-interactive: read from stdin when piped
    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            result = asyncio.run(_run_once(text, persona=args.persona))
            import json

            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("status") == "success" else 1
        return 0

    # Launch TUI if in a TTY
    try:
        from tui.app import run_tui

        run_tui()
    except Exception:
        # Fallback to readline loop if TUI fails
        asyncio.run(_interactive_loop(args.persona))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
