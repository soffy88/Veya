"""
cli/main.py — veya 交互入口(readline-based interactive loop)

用法:
    veya                      # 交互模式
    veya init                 # 首次运行向导 (接模型/选工作目录)
    veya start                # 一键启动本地服务 + 打开浏览器
    veya doctor               # 环境自检
    veya --help               # 帮助
    echo "改 foo.py" | veya   # stdin 单次执行
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from typing import Any

_PRODUCT_COMMANDS = {"init", "start", "doctor"}
_VERSION = "0.6.0"


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
    p.add_argument("--version", action="version", version=f"veya {_VERSION}")
    return p


# 整改批次 C: CLI 统一到主脑单一大模型 (与 Web 同一 coordinator_master),
# 消灭「Web=单 LLM / CLI=旧 DAG」双头脑。persona 在主脑下无意义 (无分类/无分队),
# 保留参数仅为兼容旧命令行, 不再影响行为。
async def _run_once(text: str, *, persona: str, session_id: str | None = None) -> dict[str, Any]:
    from server.coordinator_master import master_coordinator

    return await master_coordinator.chat_stream(text, session_id=session_id)


async def _interactive_loop(persona: str, session_id: str | None = None) -> None:
    from server.coordinator_master import master_coordinator

    print(f"veya {_VERSION} | 主脑单一大模型 | Ctrl-D or 'exit' to quit", file=sys.stderr)

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

        result = await master_coordinator.chat_stream(line, session_id=session_id)
        session_id = result.get("session_id") or session_id

        answer = result.get("final_answer") or ""
        print(f"\n{answer}")
        cost = result.get("cost_usd") or result.get("cost") or 0.0
        if cost:
            print(f"[cost=${cost:.6f}]", file=sys.stderr)
        print()


async def _resume(session_id: str, persona: str) -> None:
    """Resume = continue the master session (same history store as Web)."""
    print(f"Resuming master session {session_id}", file=sys.stderr)
    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            result = await _run_once(text, persona=persona, session_id=session_id)
            print(result.get("final_answer") or "")
        return
    await _interactive_loop(persona, session_id=session_id)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 产品化子命令: veya init / start / doctor
    if argv and argv[0] in _PRODUCT_COMMANDS:
        from cli import product

        cmd = argv.pop(0)
        if cmd == "init":
            return product.run_init(argv)
        if cmd == "start":
            return product.run_start(argv)
        return product.run_doctor(argv)

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
            answer = result.get("final_answer") or ""
            print(answer)
            return 0 if answer else 1
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
