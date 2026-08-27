"""Git panel REST — 工作区 Git 面板 (P4, 借鉴 ccgui Git panel)。

deny-by-default: status/diff 只读; commit 需要显式 message (前端确认)。
工作区边界 = 容器工作目录 (VEYA_WORKSPACE / cwd) 内的 git 仓库。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import auth as auth_mod

router = APIRouter(tags=["git-panel"], dependencies=[Depends(auth_mod.require_user)])

_GIT_TIMEOUT = 15


def _workspace() -> Path:
    """Git 面板工作区: 优先 /repo (宿主 veya 仓库挂载点), 否则 VEYA_WORKSPACE/cwd。"""
    for candidate in ("/repo", os.environ.get("VEYA_WORKSPACE"), os.getcwd()):
        if candidate and Path(candidate).is_dir():
            return Path(candidate).resolve()
    return Path.cwd().resolve()


def _git(*args: str, timeout: int = _GIT_TIMEOUT) -> str:
    r = subprocess.run(
        ["git", "-C", str(_workspace()), *args], capture_output=True, text=True, timeout=timeout
    )
    if r.returncode != 0:
        raise HTTPException(status_code=400, detail=(r.stderr or r.stdout or "git 失败")[-400:])
    return r.stdout


@router.get("/api/v1/git/status")
async def git_status() -> dict:
    """工作区 git 状态: 分支 + 变更文件 (index/worktree)。"""
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    except HTTPException:
        return {"error": "非 git 仓库", "branch": "", "files": []}
    try:
        ahead = _git("rev-list", "--left-right", "--count", "HEAD...@{upstream}").strip().split()
    except HTTPException:
        ahead = ["0", "0"]
    files: list[dict] = []
    try:
        out = _git("status", "--porcelain=v1")
        for line in out.splitlines():
            if len(line) < 4:
                continue
            idx, wt, path = line[0], line[1], line[3:]
            files.append({"path": path, "index": idx, "worktree": wt})
    except HTTPException:
        pass
    return {
        "branch": branch,
        "ahead": ahead[0] if ahead else "0",
        "behind": ahead[1] if len(ahead) > 1 else "0",
        "files": files[:100],
        "dirty": bool(files),
    }


@router.get("/api/v1/git/diff")
async def git_diff(path: str = "", stat: bool = False) -> dict:
    """变更 diff (默认已暂存+未暂存; path 过滤; stat=true 只给摘要)。"""
    args = ["diff", "--no-color"]
    if stat:
        args.append("--stat")
    if path:
        args.append("--")
        args.append(path)
    try:
        out = _git(*args)
    except HTTPException as exc:
        raise exc
    return {"diff": out[:40000], "truncated": len(out) > 40000}


class CommitReq(BaseModel):
    message: str
    files: list[str] = []
    all: bool = True


@router.post("/api/v1/git/commit")
async def git_commit(req: CommitReq) -> dict:
    """暂存并提交 (deny-by-default: 显式 message 必填; files 为空且 all=true 提交全部)。"""
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=422, detail="commit message 不能为空")
    if len(msg) > 500:
        raise HTTPException(status_code=422, detail="message 过长 (>500)")
    if req.files:
        _git("add", "--", *req.files)
    elif req.all:
        _git("add", "-A")
    else:
        raise HTTPException(status_code=422, detail="files 为空且 all=false, 无提交内容")
    _git("commit", "-m", msg)
    sha = _git("rev-parse", "--short", "HEAD").strip()
    return {"ok": True, "sha": sha, "message": msg}
