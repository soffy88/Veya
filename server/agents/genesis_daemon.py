"""Veya Genesis: 常驻守护进程 (Daemon Mode)。

Genesis 是后台的一个常驻守护进程: 只监听和处理 3O 架构级指令。

任务通道: inbox 目录轮询 (~/.veya/genesis/inbox/*.json)
  {"mission": "...", "session_id": "..."}

处理结果写入 results/{session_id}.json,任务文件移入 done/。

CLI:
  python -m server.agents.genesis_daemon --daemon --library-root platform/3O
  python -m server.agents.genesis_daemon --one-shot "检查 oskill 层有没有双均线交叉算子"
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

from server.agents.genesis_agent import GenesisAgent

logging.basicConfig(level=logging.INFO, format="[Genesis] %(message)s")
logger = logging.getLogger("genesis.daemon")


class GenesisDaemon:
    """常驻守护进程: 轮询 inbox,把任务喂给 Genesis,结果落盘。"""

    def __init__(
        self,
        agent: GenesisAgent,
        work_dir: str | Path | None = None,
        *,
        interval: float = 5.0,
    ):
        self.agent = agent
        self.work_dir = Path(work_dir or (Path.home() / ".veya" / "genesis"))
        self.inbox_dir = self.work_dir / "inbox"
        self.done_dir = self.work_dir / "done"
        self.results_dir = self.work_dir / "results"
        for d in (self.inbox_dir, self.done_dir, self.results_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self._stop = asyncio.Event()

    # ── 任务处理 ─────────────────────────────────────────────────────
    def _pending_tasks(self) -> list[tuple[Path, dict[str, Any]]]:
        tasks = []
        for task_file in sorted(self.inbox_dir.glob("*.json")):
            try:
                task = json.loads(task_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("跳过损坏任务 %s: %s", task_file.name, exc)
                continue
            if isinstance(task, dict) and task.get("mission"):
                tasks.append((task_file, task))
        return tasks

    async def run_once(self) -> int:
        """处理所有 inbox 任务,返回处理数量。"""
        tasks = self._pending_tasks()
        for task_file, task in tasks:
            mission = str(task["mission"])
            session_id = str(task.get("session_id") or uuid.uuid4().hex[:12])
            logger.info("接受任务 %s: %s", session_id, mission[:80])
            try:
                result = await self.agent.handle_mission(mission)
            except Exception as exc:  # 守护进程永不因单任务崩溃
                result = {"status": "failed", "error": str(exc), "steps": 0}
            result["session_id"] = session_id
            result["mission"] = mission
            (self.results_dir / f"{session_id}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            task_file.rename(self.done_dir / task_file.name)
            logger.info("任务完成 %s: %s", session_id, result.get("status"))
        return len(tasks)

    # ── 常驻循环 ─────────────────────────────────────────────────────
    async def serve_forever(self) -> None:
        """轮询 inbox,直到收到停止信号。"""
        logger.info(
            "Genesis Daemon online. Watching %s (interval=%ss)", self.inbox_dir, self.interval
        )
        while not self._stop.is_set():
            try:
                n = await self.run_once()
                if n:
                    logger.info("本轮处理 %d 个任务", n)
            except Exception as exc:
                logger.error("守护循环异常: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except TimeoutError:
                continue
        logger.info("Genesis Daemon offline. Goodbye.")

    def request_stop(self) -> None:
        self._stop.set()


def _build_agent(args: argparse.Namespace) -> GenesisAgent:
    return GenesisAgent(
        model=args.model,
        provider=args.provider,
        endpoint=args.endpoint,
        library_root=args.library_root,
        max_steps=args.max_steps,
    )


def _print_online(agent: GenesisAgent) -> None:
    state = agent.wake_up()
    print("Genesis Agent Online. Loading persistent memory...")
    print(f"Current elements managed: {state['elements_managed']}")
    print(f"Lessons learned: {state['lessons_learned']}")
    print(f"Model: {state['model']} ({state['provider']}, temperature=0)")


async def _run(args: argparse.Namespace) -> int:
    agent = _build_agent(args)
    _print_online(agent)

    if args.one_shot:
        result = await agent.handle_mission(args.one_shot)
        if result.get("status") == "success":
            print("\n" + (result.get("response") or "(no response)"))
        else:
            print("\n[MISSION FAILED] " + result.get("error", "unknown error"), file=sys.stderr)
            return 1
        return 0

    daemon = GenesisDaemon(agent, interval=args.interval)
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, daemon.request_stop)
    await daemon.serve_forever()
    agent.sleep()
    return 0


def main(argv: list[str] | None = None) -> int:
    # 自动加载项目 .env(与主服务一致): GENESIS_API_KEY/MODEL/PROVIDER/ENDPOINT 均可写在 .env
    try:
        from config.loader import _load_dotenv

        _load_dotenv()
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        prog="genesis", description="Veya Genesis — 3O 护库智能体守护进程"
    )
    parser.add_argument("--daemon", action="store_true", help="常驻模式: 轮询 inbox 任务目录")
    parser.add_argument("--one-shot", metavar="MISSION", help="单次模式: 直接执行一条 3O 架构指令")
    parser.add_argument(
        "--library-root",
        default=str(Path(__file__).resolve().parent.parent.parent / "platform" / "3O"),
    )
    parser.add_argument(
        "--model", default=None, help="专属模型(默认读 GENESIS_MODEL env, 再默认 gpt-4o)"
    )
    parser.add_argument(
        "--provider", default=None, help="专属 provider(默认读 GENESIS_PROVIDER env, 再默认 openai)"
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="OpenAI 兼容端点覆盖, 如 NVIDIA NIM https://integrate.api.nvidia.com/v1/chat/completions(默认读 GENESIS_ENDPOINT env)",
    )
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--interval", type=float, default=5.0, help="daemon 轮询间隔(秒)")
    args = parser.parse_args(argv)

    if not args.daemon and not args.one_shot:
        parser.error("请指定 --daemon 或 --one-shot")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
