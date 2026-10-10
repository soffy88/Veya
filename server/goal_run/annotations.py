"""server.goal_run.annotations — per-goal-run line annotations on diff hunks.

Storage: .veya-project/goal-runs/<goal_id>/annotations.json (existing durable
store directory — no new task store created).

Delivery: POST .../send batches all OPEN annotations into a FOLLOW_UP message
via agent_mailbox. Delivered annotations are marked resolved; unresolved stay
for the next batch.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from server.goal_run.agent_mailbox import AgentMailbox, DeliveryMode

_GOAL_RUNS_DIR = ".veya-project/goal-runs"
_ANNOTATIONS_FILE = "annotations.json"
_LOCK = threading.RLock()

AnnotationStatus = Literal["open", "resolved"]
AnnotationSide = Literal["left", "right"]


@dataclass
class LineAnnotation:
    id: str
    goal_id: str
    worktree: str
    file: str
    line: int
    side: AnnotationSide
    body: str
    status: AnnotationStatus
    created_at: float = field(default_factory=time.time)
    resolved_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LineAnnotation:
        data = dict(data)
        return cls(**data)


def _annotations_path(project_root: str, goal_id: str) -> Path:
    return Path(project_root) / _GOAL_RUNS_DIR / goal_id / _ANNOTATIONS_FILE


def _read_annotations(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_annotations(path: Path, annotations: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(annotations, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def create_annotation(
    project_root: str,
    goal_id: str,
    *,
    worktree: str,
    file: str,
    line: int,
    side: AnnotationSide = "right",
    body: str,
) -> LineAnnotation:
    """Create and persist a new open annotation."""
    annotation = LineAnnotation(
        id=str(uuid.uuid4()),
        goal_id=goal_id,
        worktree=worktree,
        file=file,
        line=line,
        side=side,
        body=body,
        status="open",
    )
    path = _annotations_path(project_root, goal_id)
    with _LOCK:
        existing = _read_annotations(path)
        existing.append(annotation.to_dict())
        _write_annotations(path, existing)
    return annotation


def list_annotations(
    project_root: str,
    goal_id: str,
    *,
    status: AnnotationStatus | None = None,
) -> list[LineAnnotation]:
    """List annotations for a goal_run, optionally filtered by status."""
    path = _annotations_path(project_root, goal_id)
    with _LOCK:
        raw = _read_annotations(path)
    annotations = [LineAnnotation.from_dict(a) for a in raw]
    if status is not None:
        annotations = [a for a in annotations if a.status == status]
    return annotations


def resolve_annotation(
    project_root: str,
    goal_id: str,
    annotation_id: str,
) -> bool:
    """Mark a single annotation resolved. Returns True if found."""
    path = _annotations_path(project_root, goal_id)
    with _LOCK:
        raw = _read_annotations(path)
        found = False
        for a in raw:
            if a["id"] == annotation_id and a["status"] == "open":
                a["status"] = "resolved"
                a["resolved_at"] = time.time()
                found = True
        if found:
            _write_annotations(path, raw)
    return found


def batch_send(
    project_root: str,
    goal_id: str,
    *,
    recipient: str,
    mailbox: AgentMailbox | None = None,
) -> dict[str, Any]:
    """Combine all OPEN annotations into one FOLLOW_UP message and deliver.

    Returns a summary: how many sent, message_id, and which ids were resolved.
    Already-resolved annotations are never included.
    """
    if mailbox is None:
        mailbox = AgentMailbox()

    path = _annotations_path(project_root, goal_id)
    with _LOCK:
        raw = _read_annotations(path)
        open_annotations = [LineAnnotation.from_dict(a) for a in raw if a["status"] == "open"]
        if not open_annotations:
            return {"sent": 0, "message_id": None, "resolved_ids": []}

        # Format: group by file
        by_file: dict[str, list[LineAnnotation]] = {}
        for ann in open_annotations:
            by_file.setdefault(ann.file, []).append(ann)

        lines: list[str] = []
        for file_path, anns in sorted(by_file.items()):
            for ann in sorted(anns, key=lambda a: a.line):
                lines.append(f"{file_path}:{ann.line} — {ann.body}")

        payload_text = "\n".join(lines)
        msg = mailbox.send(
            sender="review_annotations",
            recipient=recipient,
            delivery_mode=DeliveryMode.FOLLOW_UP,
            payload={"text": payload_text, "goal_id": goal_id, "annotation_count": len(open_annotations)},
            goal_id=goal_id,
        )

        # Mark sent annotations as resolved
        resolved_ids = [ann.id for ann in open_annotations]
        now = time.time()
        for a in raw:
            if a["id"] in resolved_ids:
                a["status"] = "resolved"
                a["resolved_at"] = now
        _write_annotations(path, raw)

    return {
        "sent": len(resolved_ids),
        "message_id": msg.message_id,
        "resolved_ids": resolved_ids,
    }
