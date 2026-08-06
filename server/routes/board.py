"""server/routes/board.py — Kanban 多 Agent 编排 API。

对标 Cline Kanban: worktree 隔离并行 + 依赖链自动触发 + auto-commit 可审查。

  POST /api/v1/board {action: create, name, repo}        创建看板
  POST /api/v1/board {action: list}                      看板列表
  POST /api/v1/board {action: add_card, board, title, prompt, depends_on?, engine?, model?}
  POST /api/v1/board {action: link, board, from, to}     依赖链 (to 依赖 from)
  POST /api/v1/board {action: start, board, card_id}     启动卡 (worktree 隔离执行)
  POST /api/v1/board {action: status, board}             看板全貌
  POST /api/v1/board {action: trash, board, card_id}     完成→trash (触发依赖链)
  POST /api/v1/board {action: diff, board, card_id}      审查产物 (diff 统计)
  POST /api/v1/board {action: cleanup, board, card_id}   清理 worktree
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.board import get_board_store, get_board_worker

router = APIRouter(tags=["board"])


class BoardActionRequest(BaseModel):
    action: Literal["create", "list", "add_card", "link", "start", "status",
                    "trash", "diff", "cleanup"] = "list"
    name: str = ""
    repo: str = ""
    board: str = ""
    title: str = ""
    prompt: str = ""
    card_id: str = ""
    from_id: str = ""
    to_id: str = ""
    depends_on: list[str] = Field(default_factory=list)
    engine: str = "claude"
    model: str = ""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise HTTPException(status_code=400, detail=msg)


@router.post("/api/v1/board")
async def board_ep(req: BoardActionRequest) -> dict[str, Any]:
    store = get_board_store()
    worker = get_board_worker()

    try:
        if req.action == "create":
            _require(bool(req.name), "name 必填")
            board = store.create(req.name, repo=req.repo)
            return {"status": "created", "board": board.name, "repo": board.repo}

        if req.action == "list":
            return {"boards": store.list()}

        if req.action == "add_card":
            _require(bool(req.board) and bool(req.prompt), "board/prompt 必填")
            card = store.add_card(
                req.board, title=req.title or req.prompt[:40],
                prompt=req.prompt, depends_on=req.depends_on,
                engine=req.engine, model=req.model,
            )
            return {"status": "added", "card_id": card.id, "card": card.to_dict()}

        if req.action == "link":
            _require(bool(req.board) and bool(req.from_id) and bool(req.to_id),
                     "board/from/to 必填")
            store.link(req.board, from_id=req.from_id, to_id=req.to_id)
            return {"status": "linked", "from": req.from_id, "to": req.to_id}

        if req.action == "start":
            _require(bool(req.board) and bool(req.card_id), "board/card_id 必填")
            card = await worker.start_card(req.board, req.card_id)
            return {"status": "started", "card_id": card.id,
                    "worktree": card.worktree, "branch": card.branch}

        if req.action == "status":
            _require(bool(req.board), "board 必填")
            b = store.get(req.board)
            if not b:
                raise KeyError(f"看板不存在: {req.board}")
            return {"board": b.name, "repo": b.repo,
                    "cards": [c.to_dict() for c in b.cards.values()]}

        if req.action == "trash":
            _require(bool(req.board) and bool(req.card_id), "board/card_id 必填")
            triggered = await worker.trash_card(req.board, req.card_id)
            return {"status": "trashed", "card_id": req.card_id,
                    "triggered": triggered}

        if req.action == "diff":
            _require(bool(req.board) and bool(req.card_id), "board/card_id 必填")
            return {"card_id": req.card_id, **worker.diff_card(req.board, req.card_id)}

        if req.action == "cleanup":
            _require(bool(req.board) and bool(req.card_id), "board/card_id 必填")
            worker.cleanup_card(req.board, req.card_id)
            return {"status": "cleaned", "card_id": req.card_id}
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=404 if isinstance(e, KeyError) else 400,
                            detail=str(e)) from e

    raise HTTPException(status_code=400, detail=f"未知 action: {req.action}")


__all__ = ["router"]
