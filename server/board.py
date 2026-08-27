"""server/board.py — Kanban 式多 Agent 编排 (worktree 隔离 + 看板状态机 + 依赖链)。

对标 Cline Kanban 的三个机制:
  1. **隔离**: 每卡一个独立 git worktree + 独立工作目录 —— 并行 agent 互不踩踏
  2. **收敛**: 卡片完成后 auto-commit 到独立分支, 产出可审查的 diff
  3. **流水线**: depends_on 依赖链 —— 依赖卡完成并 trash 时, 下游卡自动启动

状态机: todo → running → done → trash (trash 触发依赖)
依赖检查: 卡只能在其 depends_on 全部 done 后才能 start。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# =========================================================================
# 卡片/看板数据模型
# =========================================================================

CARD_STATUSES = ("todo", "running", "done", "trash")


@dataclass
class Card:
    id: str = field(default_factory=lambda: f"c_{uuid.uuid4().hex[:8]}")
    title: str = ""
    prompt: str = ""
    status: str = "todo"  # todo | running | done | trash
    depends_on: list[str] = field(default_factory=list)  # 前置卡 id (全部 done 才能 start)
    branch: str = ""  # 独立分支 card-<id>
    worktree: str = ""  # worktree 绝对路径
    engine: str = "claude"  # 执行引擎 (claude/codex/pi; master 走主脑)
    model: str = ""
    exit_code: int | None = None
    result: str = ""  # 输出摘要
    error: str = ""
    commit_sha: str = ""  # auto-commit 产物
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Board:
    name: str
    repo: str = ""  # 主仓库绝对路径 (worktree 的源)
    cards: dict[str, Card] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repo": self.repo,
            "cards": {cid: c.to_dict() for cid, c in self.cards.items()},
            "created_at": self.created_at,
        }


# =========================================================================
# BoardStore — JSON 持久化
# =========================================================================


class BoardStore:
    """看板存储 (~/.veya/boards.json)。单文件全看板, 无外部依赖。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path or Path.home() / ".veya" / "boards.json")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._boards: dict[str, Board] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for name, bd in data.items():
                board = Board(
                    name=name, repo=bd.get("repo", ""), created_at=bd.get("created_at", time.time())
                )
                for cid, cd in (bd.get("cards") or {}).items():
                    board.cards[cid] = Card(
                        **{k: v for k, v in cd.items() if k in Card.__dataclass_fields__}
                    )
                self._boards[name] = board
        except (json.JSONDecodeError, OSError):  # pragma: no cover
            pass

    def save(self) -> None:
        self._path.write_text(
            json.dumps(
                {n: b.to_dict() for n, b in self._boards.items()}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )

    def get(self, name: str) -> Board | None:
        return self._boards.get(name)

    def create(self, name: str, repo: str = "") -> Board:
        if name in self._boards:
            raise ValueError(f"看板已存在: {name}")
        board = Board(name=name, repo=repo)
        self._boards[name] = board
        self.save()
        return board

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": b.name,
                "repo": b.repo,
                "cards": len(b.cards),
                "todo": sum(1 for c in b.cards.values() if c.status == "todo"),
                "running": sum(1 for c in b.cards.values() if c.status == "running"),
                "done": sum(1 for c in b.cards.values() if c.status == "done"),
            }
            for b in self._boards.values()
        ]

    # ── 卡片操作 ─────────────────────────────────────────────────────
    def add_card(
        self,
        board: str,
        *,
        title: str,
        prompt: str,
        depends_on: list[str] | None = None,
        engine: str = "claude",
        model: str = "",
    ) -> Card:
        b = self.get(board)
        if not b:
            raise KeyError(f"看板不存在: {board}")
        card = Card(
            title=title,
            prompt=prompt,
            depends_on=list(depends_on or []),
            engine=engine,
            model=model,
        )
        b.cards[card.id] = card
        self.save()
        return card

    def link(self, board: str, *, from_id: str, to_id: str) -> None:
        """依赖链: to 依赖 from (from 完成并 trash 后, to 自动启动)。"""
        b = self.get(board)
        if not b:
            raise KeyError(f"看板不存在: {board}")
        if from_id not in b.cards or to_id not in b.cards:
            raise KeyError("卡片不存在")
        b.cards[to_id].depends_on.append(from_id)
        self.save()

    def dependencies_satisfied(self, board: str, card_id: str) -> bool:
        b = self.get(board)
        assert b is not None
        card = b.cards[card_id]
        # done 或 trash (已归档完成) 均视为满足
        return all(b.cards[d].status in ("done", "trash") for d in card.depends_on if d in b.cards)

    def pending_dependencies(self, board: str, card_id: str) -> list[str]:
        b = self.get(board)
        assert b is not None
        card = b.cards[card_id]
        return [
            d
            for d in card.depends_on
            if d in b.cards and b.cards[d].status not in ("done", "trash")
        ]

    def downstream(self, board: str, card_id: str) -> list[str]:
        """依赖本卡的 todo 卡 (触发自动启动用)。"""
        b = self.get(board)
        assert b is not None
        return [cid for cid, c in b.cards.items() if card_id in c.depends_on and c.status == "todo"]


# =========================================================================
# GitWorktree — 每卡独立工作树
# =========================================================================


class GitWorktree:
    """git worktree 封装: 创建 / 执行 / auto-commit / diff / 清理。"""

    def __init__(self, repo: str) -> None:
        self.repo = Path(repo).resolve()
        if not (self.repo / ".git").exists():
            raise ValueError(f"不是 git 仓库: {repo}")

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        r = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr[-500:]}")
        return r.stdout.strip()

    def add(self, branch: str) -> str:
        """创建 worktree (独立分支), 返回 worktree 路径。"""
        wt = str(self.repo / ".veya-wt" / branch)
        if Path(wt).exists():
            shutil.rmtree(wt, ignore_errors=True)
        self._git("worktree", "add", "-b", branch, wt)
        return wt

    def commit(self, wt: str, message: str) -> str:
        """auto-commit: 卡内全部改动提交到独立分支, 返回 commit sha。"""
        self._git("add", "-A", cwd=Path(wt))
        # 无改动时 git commit 会失败 → 空提交保持可追踪
        r = subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=wt,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            raise RuntimeError(f"commit 失败: {r.stderr[-500:]}")
        return self._git("rev-parse", "HEAD", cwd=Path(wt))

    def diff_stat(self, base: str = "main", wt: str | None = None) -> dict[str, Any]:
        """可审查产物: 分支相对 base 的 diff 统计。"""
        branch = Path(wt).name if wt else ""
        r = subprocess.run(
            ["git", "diff", "--stat", f"{base}...{branch}"],
            cwd=wt or self.repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {"stat": r.stdout.strip() or "(无差异)", "base": base}

    def remove(self, wt: str) -> None:
        """清理 worktree (done 且已审查后)。"""
        try:
            self._git("worktree", "remove", "--force", wt)
        except RuntimeError:
            shutil.rmtree(wt, ignore_errors=True)


# =========================================================================
# BoardWorker — 异步执行 + 依赖链触发
# =========================================================================


class BoardWorker:
    """看板执行器: start 卡 (worktree 隔离执行) → auto-commit → 触发依赖链。"""

    def __init__(self, store: BoardStore) -> None:
        self.store = store
        self._running: set[str] = set()  # 正在跑的 card id (board/card 去重用)
        self._tasks: set[asyncio.Task] = set()  # 后台任务引用 (防 GC)

    async def start_card(self, board: str, card_id: str) -> Card:
        """启动卡片: 依赖检查 → worktree → 后台执行 (不阻塞调用方)。"""
        b = self.store.get(board)
        if not b:
            raise KeyError(f"看板不存在: {board}")
        card = b.cards[card_id]
        if card.status == "running":
            return card
        pending = self.store.pending_dependencies(board, card_id)
        if pending:
            raise ValueError(f"依赖未完成: {pending} (先完成并 trash 前置卡)")
        if not b.repo:
            raise ValueError("看板未绑定仓库 (Board.repo)")

        wt = GitWorktree(b.repo)
        card.branch = f"card-{card_id}"
        card.worktree = wt.add(card.branch)
        card.status = "running"
        card.started_at = time.time()
        self.store.save()

        task = asyncio.create_task(self._run_card(board, card_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return card

    async def _run_card(self, board: str, card_id: str) -> None:
        b = self.store.get(board)
        assert b is not None
        card = b.cards[card_id]
        self._running.add(card_id)
        try:
            await self._execute(board, card)
        finally:
            self._running.discard(card_id)
            self.store.save()

    async def _execute(self, board: str, card: Card) -> None:
        from server.engine_runner import run_engine

        result = await run_engine(
            card.engine,
            card.prompt,
            model=card.model or None,
            cwd=card.worktree or None,
            timeout_s=900.0,
        )
        card.result = str(result.get("output", ""))[:4000]
        card.error = str(result.get("error", ""))[:2000]
        card.exit_code = 0 if result.get("ok") else 1

        # 收敛: auto-commit (无论成败, 产物可审查)
        try:
            wt = GitWorktree(self.store.get(board).repo if self.store.get(board) else "")
            card.commit_sha = wt.commit(card.worktree, f"card {card.id}: {card.title}")
        except RuntimeError as e:
            card.error = f"{card.error}\n[commit 失败] {e}"
        card.status = "done"
        card.finished_at = time.time()

    async def trash_card(self, board: str, card_id: str) -> list[str]:
        """完成卡 → trash (触发依赖链: 下游 todo 卡自动启动)。"""
        b = self.store.get(board)
        if not b:
            raise KeyError(f"看板不存在: {board}")
        card = b.cards[card_id]
        if card.status != "done":
            raise ValueError(f"只有 done 卡可 trash (当前 {card.status})")
        card.status = "trash"
        self.store.save()

        triggered: list[str] = []
        for downstream_id in self.store.downstream(board, card_id):
            if self.store.dependencies_satisfied(board, downstream_id):
                await self.start_card(board, downstream_id)
                triggered.append(downstream_id)
        return triggered

    def diff_card(self, board: str, card_id: str) -> dict[str, Any]:
        """可审查产物: 卡分支相对 main 的 diff 统计。"""
        b = self.store.get(board)
        if not b:
            raise KeyError(f"看板不存在: {board}")
        card = b.cards[card_id]
        if not card.worktree or not Path(card.worktree).exists():
            return {"stat": "(worktree 已清理)", "commit_sha": card.commit_sha}
        wt = GitWorktree(b.repo)
        return wt.diff_stat(wt=card.worktree)

    def cleanup_card(self, board: str, card_id: str) -> None:
        """审查后清理 worktree (保留 commit 历史与分支)。"""
        b = self.store.get(board)
        if not b:
            raise KeyError(f"看板不存在: {board}")
        card = b.cards[card_id]
        if card.worktree and Path(card.worktree).exists():
            GitWorktree(b.repo).remove(card.worktree)
            card.worktree = ""
            self.store.save()


# 全局单例 (API 层注入)
_default_store = BoardStore()
_default_worker = BoardWorker(_default_store)


def get_board_store() -> BoardStore:
    return _default_store


def get_board_worker() -> BoardWorker:
    return _default_worker
