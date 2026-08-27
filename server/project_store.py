"""project_store — .veya-project/ 项目级记忆 (M1) + project_ask 契约草案。

2026-08-15 审查结论落地 (docs 见对话记录, 未来可补 docs/PROJECT_STORE_SPEC.md):
  - 不新建第二套任务队列; 权威调度仍是 server.hicode_queue.HicodeTaskQueue,
    本模块只做 STATE/DECISIONS/LESSONS 记忆 + 队列快照镜像 + 单次运行产物目录。
  - 目录名用 .veya-project/ (项目内, 每个 workspace 一份), 不用 .veya/
    (那是本机运行时根 ~/.veya/{loop,audit}, 作用域不同, 同名会混淆)。
  - project_ask 是唯一对外入口 (Coordinator 只调这一个 tool, 不做多 tool
    分支路由); 本文件只定它的入参/出参契约, 尚未接线派发逻辑 (M2)。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# ── 状态映射表: HicodeTaskQueue 内部枚举 → 项目侧终态语义 ─────────────
# 内部 (server.hicode_queue.TaskRecord.status): queued → running → done | failed | cancelled
# 项目侧只对外暴露两个终态: completed | blocked (+ block_reason)；不允许静默丢失。
_BLOCKED_REASONS = {
    "failed": "worker failed",
    "cancelled": "cancelled before completion",
}


def to_project_status(status: str, error: str = "") -> tuple[str, str | None]:
    """把 TaskRecord.status 映射为项目侧 completed|blocked。

    queued/running 是非终态, 原样透传 (调用方应继续等待/轮询);
    done → completed；failed/cancelled → blocked + block_reason
    (优先用 TaskRecord.error, 没有则用默认原因, 保证 reason 永不为空)。
    """
    if status == "done":
        return "completed", None
    if status in _BLOCKED_REASONS:
        return "blocked", error or _BLOCKED_REASONS[status]
    return status, None


# ── project_ask 契约草案 (M2 待接线, 本次只定 schema) ──────────────────


@dataclass
class ProjectAskRequest:
    """project_ask 单一入口的入参。

    Coordinator 只调这一个 tool；self-do vs. dispatch 的决策在 tool 实现
    内部完成, 不得拆成多个 tool 让 Coordinator 分支选择。
    """

    project_root: str
    request: str
    assignee_hint: str | None = None  # "builtin" | "hicode"（未来 "dsh" 等）, 仅提示不强制


@dataclass
class ProjectAskResponse:
    """project_ask 出参。终态只有 completed | blocked, 不允许静默丢失。

    phase/interpretation/assumptions/questions/confidence/parent_task_id 是
    Understand 门禁 (docs/PROJECT_AGENT.md §7) 新增字段: understood_ask 表示
    只追问、无业务副作用; executed 表示已走 builtin/hicode/dsh 执行腿;
    rejected 对应既有的非法 hint/mode 早退。
    """

    task_id: str
    status: Literal["completed", "blocked", "queued", "running"]
    summary: str = ""
    block_reason: str | None = None
    artifacts: list[str] = field(default_factory=list)
    phase: Literal["understood_ask", "executed", "rejected"] = "executed"
    interpretation: str | None = None
    assumptions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    confidence: float | None = None
    parent_task_id: str | None = None


# ── M1: .veya-project/ Store ──────────────────────────────────────────

_DIR_NAME = ".veya-project"
_STATE_FILE = "PROJECT_STATE.md"
_DECISIONS_FILE = "DECISIONS.md"
_LESSONS_FILE = "LESSONS.md"
_QUEUE_MIRROR_FILE = "queue-mirror.json"

_STATE_TEMPLATE = (
    "# Project State\n"
    "Updated: {ts}\n\n"
    "## Goal\n\n"
    "## Current status\n\n"
    "## Key artifacts\n\n"
    "## Open risks\n"
)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ProjectStore:
    """项目级记忆持久化 (M1)。

    权威任务调度仍是 HicodeTaskQueue；本 Store 只负责:
      - PROJECT_STATE.md / DECISIONS.md / LESSONS.md 三件套记忆
      - 队列快照镜像 (queue-mirror.json，非权威，供离线查看/审计)
      - 单次运行产物目录 (runs/<task_id>/)
    """

    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root)
        self.dir = self.root / _DIR_NAME

    def ensure_layout(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "workers").mkdir(exist_ok=True)
        (self.dir / "output").mkdir(exist_ok=True)
        (self.dir / "runs").mkdir(exist_ok=True)
        state_path = self.dir / _STATE_FILE
        if not state_path.exists():
            state_path.write_text(_STATE_TEMPLATE.format(ts=_now_iso()), encoding="utf-8")
        for fname in (_DECISIONS_FILE, _LESSONS_FILE):
            path = self.dir / fname
            if not path.exists():
                path.write_text("", encoding="utf-8")

    def read_state(self) -> str:
        self.ensure_layout()
        return (self.dir / _STATE_FILE).read_text(encoding="utf-8")

    def write_state(self, md: str) -> None:
        self.ensure_layout()
        (self.dir / _STATE_FILE).write_text(md, encoding="utf-8")

    def append_decision(self, entry: str) -> None:
        self._append(_DECISIONS_FILE, entry)

    def append_lesson(self, entry: str) -> None:
        self._append(_LESSONS_FILE, entry)

    def read_decisions(self) -> str:
        self.ensure_layout()
        return (self.dir / _DECISIONS_FILE).read_text(encoding="utf-8")

    def read_lessons(self) -> str:
        self.ensure_layout()
        return (self.dir / _LESSONS_FILE).read_text(encoding="utf-8")

    def _append(self, fname: str, entry: str) -> None:
        self.ensure_layout()
        path = self.dir / fname
        with path.open("a", encoding="utf-8") as f:
            f.write(entry.rstrip("\n") + "\n\n")

    def load_queue_mirror(self) -> dict[str, Any]:
        path = self.dir / _QUEUE_MIRROR_FILE
        if not path.exists():
            return {"tasks": []}
        return json.loads(path.read_text(encoding="utf-8") or '{"tasks": []}')

    def save_queue_mirror(self, snapshot: dict[str, Any]) -> None:
        self.ensure_layout()
        path = self.dir / _QUEUE_MIRROR_FILE
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_dir(self, task_id: str) -> Path:
        self.ensure_layout()
        d = self.dir / "runs" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_understand(self, task_id: str, data: dict[str, Any]) -> None:
        """落盘 Understand 门禁判定结果 (runs/<task_id>/understand.json), 供审计与

        parent_task_id 续答链读取 (见 server.project_ask._load_chain)。
        """
        path = self.run_dir(task_id) / "understand.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_understand(self, task_id: str) -> dict[str, Any] | None:
        path = self.dir / "runs" / task_id / "understand.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
