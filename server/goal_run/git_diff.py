"""server.goal_run.git_diff — 抓一个任务执行前后的 diff, 供 code_review.py 用。

对标 mattpocock/skills 的 code-review: 它 diff 固定点到 HEAD; goal_run 的任务
粒度更细(单任务, 不是整个分支), 所以这里抓的是"执行前 HEAD → 执行后(含未提交
改动)"——覆盖 hicode 自己有没有提交都能拿到 diff。非 git 仓库/命令失败一律
返回空串, 不拖垮任务调度(review 是锦上添花, 不是任务能不能跑完的前提)。
"""

from __future__ import annotations

import subprocess


def current_head(project_root: str) -> str:
    """当前 HEAD commit hash; 非 git 仓库/无提交返回空串。"""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def capture_task_diff(project_root: str, before_ref: str, *, max_chars: int = 20_000) -> str:
    """``before_ref`` 到当前状态的 diff(含已提交 + 未提交改动)。失败/无 diff 返回空串。"""
    if not before_ref:
        return _uncommitted_diff(project_root, max_chars)
    try:
        committed = subprocess.run(
            ["git", "diff", f"{before_ref}..HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        uncommitted = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    parts = [p.stdout for p in (committed, uncommitted) if p.returncode == 0 and p.stdout.strip()]
    text = "\n".join(parts)
    return text[:max_chars]


def _uncommitted_diff(project_root: str, max_chars: int) -> str:
    try:
        r = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout[:max_chars] if r.returncode == 0 else ""
