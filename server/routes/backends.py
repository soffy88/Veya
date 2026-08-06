"""server/routes/backends.py — 多 backend 挂载 + Issue 自动拆解 (对标 OpenHands)。

  GET  /api/v1/backends            注册表全貌 (发现 + 手动注册)
  GET  /api/v1/backends/status     Canvas 状态聚合 (可用/忙碌/任务数)
  POST /api/v1/backends/run        {name, prompt, cwd?, model?} 统一执行
  POST /api/v1/backends/register   {name, kind, command, agent} 注册 ACP/CLI 后端
  POST /api/v1/automation/issue-decompose
        {repo_path, github?, issue_number?, body?, engine?}
        GitHub issue (或直接 body) → 拆子任务 → 写 Kanban board → 自动执行
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.backends import BACKEND_KINDS, get_backend_registry
from server.board import get_board_store, get_board_worker

router = APIRouter(tags=["backends"])


class BackendRunRequest(BaseModel):
    name: str
    prompt: str
    cwd: str = ""
    model: str = ""
    timeout_s: float = 600.0


class BackendRegisterRequest(BaseModel):
    name: str
    kind: str = "acp"
    command: list[str] = []
    agent: str = "general"


class IssueDecomposeRequest(BaseModel):
    repo_path: str = ""           # 本地 git 仓库 (Kanban board 绑定)
    github: str = ""              # "owner/repo" (可选, 拉取 issue)
    issue_number: int = 0
    body: str = ""                # 直接给 issue 内容 (跳过 GitHub 拉取)
    title: str = ""
    board: str = ""               # 缺省自动命名 issue-<repo>-<n>
    engine: str = "claude"
    auto_start: bool = True


@router.get("/api/v1/backends")
async def list_backends() -> dict[str, Any]:
    return {"backends": get_backend_registry().list()}


@router.get("/api/v1/backends/status")
async def backends_status() -> dict[str, Any]:
    return {"backends": get_backend_registry().status()}


@router.post("/api/v1/backends/register")
async def register_backend(req: BackendRegisterRequest) -> dict[str, Any]:
    if req.kind not in BACKEND_KINDS:
        raise HTTPException(status_code=400, detail=f"kind 可选: {BACKEND_KINDS}")
    if req.kind != "builtin" and not req.command:
        raise HTTPException(status_code=400, detail="cli/acp backend 需要 command")
    spec = get_backend_registry().register(
        req.name, req.kind, command=req.command, agent=req.agent)
    return {"status": "registered", "backend": spec.to_dict()}


@router.post("/api/v1/backends/run")
async def run_backend(req: BackendRunRequest) -> dict[str, Any]:
    try:
        result = await get_backend_registry().run(
            req.name, req.prompt, cwd=req.cwd or None,
            model=req.model, timeout_s=req.timeout_s)
        result["http_status"] = "ok" if result.get("ok") else "failed"
        return result
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# =========================================================================
# Issue 自动拆解 → Kanban 流水线 (OpenHands 招牌自动化)
# =========================================================================

_MARKDOWN_ITEM = re.compile(r"^\s*[-*]\s+\[( |x)\]\s+(.*)$", re.MULTILINE)
_HEADING = re.compile(r"^#{2,4}\s+(.+)$", re.MULTILINE)


def decompose_issue(body: str, title: str) -> list[dict[str, str]]:
    """把 issue body 拆成子任务卡。

    规则 (无 LLM 依赖, 确定性):
      1. 每个 markdown checklist 项 ([ ] / [x]) → 一张卡
      2. 无 checklist 时, 按 ## 小标题分段 → 一张卡
      3. 兜底: 整条 issue 一张卡
    """
    tasks: list[dict[str, str]] = []
    for m in _MARKDOWN_ITEM.finditer(body):
        tasks.append({"title": m.group(2).strip()[:80] or f"子任务 {len(tasks)+1}",
                      "prompt": m.group(2).strip()[:500]})
    if not tasks:
        for m in _HEADING.finditer(body):
            tasks.append({"title": m.group(1).strip()[:80],
                          "prompt": m.group(1).strip()[:500]})
    if not tasks:
        tasks.append({"title": title or "issue 任务", "prompt": body[:500]})
    return tasks


@router.post("/api/v1/automation/issue-decompose")
async def issue_decompose(req: IssueDecomposeRequest) -> dict[str, Any]:
    """GitHub issue (或直接 body) → 子任务卡 → Kanban board → 自动执行。"""
    body = req.body
    title = req.title
    if not body and req.github and req.issue_number:
        # 拉取 GitHub issue
        try:
            from veya.integrations import GitHubIntegration

            gh = GitHubIntegration()
            owner, _, repo_name = req.github.partition("/")
            res = await gh._api_request(
                "GET", f"/repos/{owner}/{repo_name}/issues/{req.issue_number}")
            body = res.get("body") or ""
            title = res.get("title") or ""
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"GitHub issue 拉取失败: {e}") from e
    if not body:
        raise HTTPException(status_code=400, detail="需要 body 或 github+issue_number")

    tasks = decompose_issue(body, title)
    if not tasks:
        raise HTTPException(status_code=400, detail="issue 无内容可拆")

    # 看板: 缺省自动命名
    board_name = req.board or f"issue-{req.github.replace('/', '-') or 'local'}-{req.issue_number or 'x'}"
    store = get_board_store()
    worker = get_board_worker()
    if store.get(board_name) is None:
        store.create(board_name, repo=req.repo_path)

    # 线性依赖链: 卡2 依赖卡1, 卡3 依赖卡2 ... (串行安全)
    cards = []
    prev_id: str | None = None
    for t in tasks:
        card = store.add_card(board_name, title=t["title"], prompt=t["prompt"],
                              depends_on=[prev_id] if prev_id else None,
                              engine=req.engine)
        cards.append(card.id)
        prev_id = card.id

    started: list[str] = []
    if req.auto_start and req.repo_path:
        try:
            await worker.start_card(board_name, cards[0])
            started.append(cards[0])
        except ValueError as e:
            raise HTTPException(status_code=400,
                                detail=f"首卡启动失败 (仓库需为 git 仓库且已提交): {e}") from e

    return {
        "status": "decomposed",
        "board": board_name,
        "tasks": len(cards),
        "cards": cards,
        "started": started,
        "chain": "linear" if len(cards) > 1 else "single",
    }


__all__ = ["router"]
