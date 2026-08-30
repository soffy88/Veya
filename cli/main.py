"""
cli/main.py — veya 交互入口(readline-based interactive loop)

用法:
    veya                      # 交互模式
    veya init                 # 首次运行向导 (接模型/选工作目录)
    veya start                # 一键启动本地服务 + 打开浏览器
    veya doctor               # 环境自检
    veya harness doctor      # coding harness 自检
    veya workspace doctor    # harness 自检别名
    veya --help               # 帮助
    echo "改 foo.py" | veya   # stdin 单次执行
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from datetime import UTC, datetime
from typing import Any

_PRODUCT_COMMANDS = {"init", "start", "doctor", "upgrade", "migrate", "code"}
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
    p.add_argument("prompt", nargs="?", help="One-shot prompt; omit for interactive mode")
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


async def _session_listing() -> int:
    from veya.history_store import default_history_store

    sessions = await default_history_store().list_sessions(limit=50)
    for item in sessions:
        sid = item.get("sid", "")
        title = item.get("title") or "Untitled"
        print(f"{sid}\t{title}\t{item.get('msg_count', 0)}")
    return 0


async def _attach_session(session_id: str) -> int:
    from server.coordinator_master import _active_streams
    from veya.history_store import default_history_store

    store = default_history_store()
    messages = await store.load(session_id)
    known = await store.list_sessions(limit=5000)
    if not any(item.get("sid") == session_id for item in known):
        print(f"session not found: {session_id}", file=sys.stderr)
        return 1
    active = _active_streams.get(session_id)
    print(
        json.dumps(
            {
                "session_id": session_id,
                "messages": messages,
                "active": active is not None and not active.done(),
            },
            ensure_ascii=False,
        )
    )
    return 0


async def _resume_command(session_id: str | None, persona: str) -> int:
    if not session_id:
        from veya.history_store import default_history_store

        sessions = await default_history_store().list_sessions(limit=1)
        session_id = sessions[0]["sid"] if sessions else None
    if not session_id:
        print("no session to resume", file=sys.stderr)
        return 1
    await _resume(session_id, persona)
    return 0


def _skill_command(argv: list[str]) -> int:
    """Manage user-taught SkillSpec records without creating another executor."""
    from server.capability_model import SkillRegistry

    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: veya skill list|show <id>|edit <id> --description <text>|delete <id>")
        return 0 if argv else 2
    registry = SkillRegistry()
    action = argv[0]
    if action == "list" and len(argv) == 1:
        for spec in registry.search():
            print(f"{spec.skill_id}\t{spec.status}\tv{spec.version}\t{spec.instructions}")
        return 0
    if action == "show" and len(argv) == 2:
        spec = registry.get_version(argv[1])
        if spec is None:
            print(f"skill not found: {argv[1]}", file=sys.stderr)
            return 1
        print(json.dumps(spec.__dict__, ensure_ascii=False, indent=2))
        return 0
    if action == "delete" and len(argv) == 2:
        if registry.get_version(argv[1]) is None:
            print(f"skill not found: {argv[1]}", file=sys.stderr)
            return 1
        registry.rollback(argv[1])
        return 0
    if action == "edit" and len(argv) >= 4 and argv[2] == "--description":
        spec = registry.get_version(argv[1])
        if spec is None:
            print(f"skill not found: {argv[1]}", file=sys.stderr)
            return 1
        spec.instructions = " ".join(argv[3:])
        spec.updated_at = datetime.now(UTC).isoformat()
        registry._store.put("skill", spec.skill_id, spec.__dict__)
        return 0
    print("invalid skill command; run `veya skill --help`", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if len(argv) >= 2 and argv[0] in {"harness", "workspace"} and argv[1] == "doctor":
        from cli.harness import run_harness_doctor_cli

        return run_harness_doctor_cli(argv[2:])

    # 只读 state authority 诊断: veya state doctor [--json]
    if argv and argv[0] == "state" and len(argv) >= 2 and argv[1] == "doctor":
        from runtime.state_authority.doctor import run_cli

        return run_cli(argv[2:])

    # 产品化子命令: veya init / start / doctor / upgrade / migrate / code
    if argv and argv[0] in _PRODUCT_COMMANDS:
        from cli import product
        from commands import upgrade

        cmd = argv.pop(0)
        if cmd == "init":
            return product.run_init(argv)
        if cmd == "start":
            return product.run_start(argv)
        if cmd == "doctor":
            return product.run_doctor(argv)
        if cmd == "upgrade":
            return upgrade.run_upgrade(argv)
        if cmd == "migrate":
            return upgrade.run_migrate(argv)
        if cmd == "code":
            from cli.coding import run_coding_cli

            return run_coding_cli(argv)

    # Unified Session API CLI (P1-06). These commands use the same durable
    # history store as Web/MasterAgent; they do not maintain a CLI-only history.
    if argv and argv[0] == "sessions":
        return asyncio.run(_session_listing())
    if argv and argv[0] == "attach":
        if len(argv) != 2:
            print("usage: veya attach <session_id>", file=sys.stderr)
            return 2
        return asyncio.run(_attach_session(argv[1]))
    if argv and argv[0] == "resume":
        sid = argv[1] if len(argv) > 1 else None
        if len(argv) > 2:
            print("usage: veya resume [session_id]", file=sys.stderr)
            return 2
        return asyncio.run(_resume_command(sid, "build"))
    if argv and argv[0] == "skill":
        return _skill_command(argv[1:])

    from config.loader import load_config
    from server.assembly import Infra

    args = _build_parser().parse_args(argv)
    Infra.init(load_config(args.config if hasattr(args, "config") else None))

    if args.resume:
        asyncio.run(_resume(args.resume, args.persona))
        return 0

    if args.prompt:
        result = asyncio.run(_run_once(args.prompt, persona=args.persona))
        answer = result.get("final_answer") or ""
        print(answer)
        return 0 if answer else 1

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
