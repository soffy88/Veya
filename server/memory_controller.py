"""server.memory_controller — VAOM MemoryRecord + MemoryController。

对标 docs/dev/rfc-01-vaom.md，P3 落地（PR-18/19，见 docs/VEYA_3.0_GAP_AUDIT.md
§5 表）。存储底座决策见 docs/dev/rfc-04-data-plane-decision.md——不引入
Postgres，不改造 oskill.hybrid_search 的语料摄入管线，元数据走 JSON 单文件
（惯例同 server/memory_bank.py），search() 是关键词/结构化过滤，不是向量语义
检索（诚实起点，见 RFC-04 §3 第4条）。

范围边界：
- 不碰既有 `server/memory_bank.py`（用户偏好账本）与
  `platform/3O/omodul/omodul/store_memory.py`（KU 图）——两者继续独立运行，
  本模块是并行的第三条 Memory 线，不合并、不替代。
- `resolve_conflict` 只做检测+标记（写 `contradicts`/`supersedes` 字段），不
  自动判定谁对谁错——2.0 文档"不让向量相似度决定事实真伪"的字面落实。深度
  复用 `platform/3O/omodul/omodul/knowledge_reflux.py` 的冲突处理（图结构，
  `epistemic_status.grade` 阶梯）需要先把 MemoryRecord 适配成 KU 节点/边形状，
  本轮未做，是后续需要时才做的深化，不是被遗漏。
- `extract_candidates` 唯一真实数据来源是 `server/goal_run/trust_plane.py` 的
  TaskEpisode/VerifiedState（P1 产物）——从"验证通过的任务声明"里提炼候选记忆，
  不凭空生成看起来合理的记忆条目。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.events import append_canonical_event


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    import uuid

    return f"memory_{uuid.uuid4().hex[:12]}"


_CANONICAL_STATUS: dict[str, str] = {
    "candidate": "active",
    "verified": "active",
    "deprecated": "superseded",
    "rejected": "invalidated",
    "forgotten": "forgotten",
}


@dataclass
class MemoryRecord:
    """从 Episode/Knowledge 中提炼、用于未来召回的长期信息（VAOM MemoryRecord）。"""

    content: str
    memory_id: str = field(default_factory=_new_id)
    type: str = "semantic"  # working | episodic | semantic | procedural
    source_episode_ids: list[str] = field(default_factory=list)
    source_artifact_ids: list[str] = field(default_factory=list)
    source_knowledge_ids: list[str] = field(default_factory=list)
    scope: str = "project"  # user | project | repo | global
    provenance: str = ""
    trust_level: str = "unknown"
    confidence: float | None = None
    valid_from: str = field(default_factory=_now_iso)
    valid_until: str | None = None
    supersedes: list[str] = field(default_factory=list)
    contradicts: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    status: str = "candidate"  # candidate | verified | deprecated | rejected
    version: int = 1
    created_at: str = field(default_factory=_now_iso)
    # P2 MemoryRecord v2 aliases. The legacy fields above remain for persisted
    # records and existing callers; these fields are the canonical scope/type
    # dimensions for new records.
    scope_type: str = "workspace"
    scope_id: str = ""
    memory_type: str = "semantic"
    source_event_ids: list[str] = field(default_factory=list)
    last_verified_at: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def canonical_status(self) -> str:
        """P2 canonical lifecycle status, preserving legacy persisted aliases."""
        return _CANONICAL_STATUS.get(self.status, self.status)

    def canonical_dict(self) -> dict[str, Any]:
        """Serialize the spec-shaped record without breaking old callers."""
        data = asdict(self)
        data["status"] = self.canonical_status
        data["legacy_status"] = self.status
        return data


_DEFAULT_STORAGE_PATH = str(Path.home() / ".veya" / "vaom_memory_records.json")


class _MemoryStore:
    """单文件 JSON 存储，惯例同 server/memory_bank.py::VeyaMemoryBank。"""

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = Path(
            storage_path or os.environ.get("VEYA_MEMORY_RECORDS_PATH", _DEFAULT_STORAGE_PATH)
        ).expanduser()
        self._lock = threading.RLock()
        self._records: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if self.storage_path.exists():
            try:
                with open(self.storage_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        with self._lock:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.storage_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._records, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.storage_path)

    def put(self, record: MemoryRecord) -> None:
        with self._lock:
            self._records[record.memory_id] = asdict(record)
            self._save()

    def get(self, memory_id: str) -> MemoryRecord | None:
        r = self._records.get(memory_id)
        return MemoryRecord(**r) if r else None

    def all(self) -> list[MemoryRecord]:
        return [MemoryRecord(**r) for r in self._records.values()]


class MemoryController:
    """observe, extract_candidates, search, consolidate, resolve_conflict,
    promote, deprecate, explain_provenance（VAOM MemoryController 最小接口）。"""

    def __init__(self, store: _MemoryStore | None = None):
        self._store = store or _MemoryStore()

    @staticmethod
    def _record_event(topic: str, record: MemoryRecord, **extra: Any) -> None:
        with contextlib.suppress(Exception):
            append_canonical_event(
                topic,
                {"memory": record.canonical_dict(), **extra},
                actor="system",
                session_id=record.scope_id if record.scope_type == "session" else None,
            )

    # ── 写入 ─────────────────────────────────────────────────────────────

    def observe(
        self,
        content: str,
        *,
        type: str = "semantic",
        scope: str = "project",
        source_episode_ids: list[str] | None = None,
        source_artifact_ids: list[str] | None = None,
        source_knowledge_ids: list[str] | None = None,
        entities: list[str] | None = None,
        keywords: list[str] | None = None,
        provenance: str = "",
        trust_level: str = "unknown",
        scope_type: str | None = None,
        scope_id: str | None = None,
        memory_type: str | None = None,
        source_event_ids: list[str] | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
    ) -> MemoryRecord:
        canonical_scope_type = scope_type or {
            "project": "workspace",
            "repo": "workspace",
            "global": "user",
        }.get(scope, "workspace")
        canonical_scope_id = scope_id or scope
        canonical_memory_type = memory_type or type
        record = MemoryRecord(
            content=content,
            type=type,
            scope=scope,
            source_episode_ids=list(source_episode_ids or []),
            source_artifact_ids=list(source_artifact_ids or []),
            source_knowledge_ids=list(source_knowledge_ids or []),
            entities=list(entities or []),
            keywords=list(keywords or []),
            provenance=provenance,
            trust_level=trust_level,
            confidence=confidence,
            scope_type=canonical_scope_type,
            scope_id=canonical_scope_id,
            memory_type=canonical_memory_type,
            source_event_ids=list(source_event_ids or []),
            tags=list(tags or []),
        )
        self._store.put(record)
        self._record_event("memory.candidate_created", record)
        return record

    def extract_candidates(self, project_root: str, goal_id: str) -> list[MemoryRecord]:
        """从一次 goal_run 的 TaskEpisode(P1 产物)里提炼候选记忆: 每条已验证的
        VerifiedState 对应的 Claim 陈述, 变成一条 episodic 候选记忆。唯一真实
        数据来源, 不编造。goal_id 找不到 episode 时返回空列表。"""
        from server.goal_run.trust_plane import read_task_episode, read_trust_plane_records

        episode = read_task_episode(project_root, goal_id)
        if episode is None:
            return []

        records = read_trust_plane_records(project_root, goal_id)
        claims_by_id = {r["claim_id"]: r for r in records if r["_type"] == "Claim"}
        verified_states = [r for r in records if r["_type"] == "VerifiedState"]

        candidates: list[MemoryRecord] = []
        for vs in verified_states:
            claim = claims_by_id.get(vs["claim_id"])
            if claim is None:
                continue
            candidates.append(
                self.observe(
                    claim["statement"],
                    type="episodic",
                    scope="project",
                    source_episode_ids=[episode["episode_id"]],
                    provenance=f"goal_run:{goal_id}",
                    trust_level="L2_verified",
                )
            )
        return candidates

    # ── 查询 ─────────────────────────────────────────────────────────────

    def get(self, memory_id: str) -> MemoryRecord | None:
        return self._store.get(memory_id)

    def search(self, query: str = "", *, scope: str | None = None) -> list[MemoryRecord]:
        """关键词过滤(content/entities/keywords), 见模块 docstring——不是向量语义检索。"""
        # forgotten/deprecated 记录保留用于审计与纠错链，但不能继续作为可用事实召回。
        items = [r for r in self._store.all() if r.status not in {"deprecated", "forgotten"}]
        if scope is not None:
            items = [r for r in items if r.scope == scope]
        if not query:
            return items
        q = query.lower()
        return [
            r
            for r in items
            if q in r.content.lower()
            or any(q in e.lower() for e in r.entities)
            or any(q in k.lower() for k in r.keywords)
        ]

    def explain_provenance(self, memory_id: str) -> dict[str, Any] | None:
        record = self._store.get(memory_id)
        if record is None:
            return None
        return {
            "provenance": record.provenance,
            "source_episode_ids": record.source_episode_ids,
            "source_artifact_ids": record.source_artifact_ids,
            "source_knowledge_ids": record.source_knowledge_ids,
            "trust_level": record.trust_level,
        }

    # ── 冲突/晋升/退役 ────────────────────────────────────────────────────

    def consolidate(self) -> int:
        """完全同 scope+content 的重复记录: 保留最新一条 verified/candidate, 旧的
        标 superseded 并写 supersedes 指向新记录。返回被标记 superseded 的条数。"""
        by_key: dict[tuple[str, str], list[MemoryRecord]] = {}
        for r in self._store.all():
            if r.status == "deprecated":
                continue
            by_key.setdefault((r.scope, r.content), []).append(r)

        superseded_count = 0
        for group in by_key.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda r: r.created_at)
            newest = group[-1]
            for old in group[:-1]:
                old.status = "deprecated"
                old.supersedes = list(set(old.supersedes) | {newest.memory_id})
                self._store.put(old)
                superseded_count += 1
        return superseded_count

    def resolve_conflict(self) -> int:
        """同 scope 且共享至少一个 entity、但 content 不同的记录对: 互相标
        contradicts(不自动判定谁对), 见模块 docstring。返回新标记的冲突条数。"""
        items = [r for r in self._store.all() if r.status != "deprecated"]
        marked = 0
        for i, a in enumerate(items):
            if not a.entities:
                continue
            for b in items[i + 1 :]:
                if a.scope != b.scope or not b.entities:
                    continue
                if a.content == b.content:
                    continue
                shared = set(a.entities) & set(b.entities)
                if not shared:
                    continue
                if b.memory_id in a.contradicts:
                    continue
                a.contradicts = list(set(a.contradicts) | {b.memory_id})
                b.contradicts = list(set(b.contradicts) | {a.memory_id})
                self._store.put(a)
                self._store.put(b)
                marked += 1
        return marked

    def promote(self, memory_id: str) -> bool:
        """Memory promotion 要有 source Episode/Evidence(2.0 文档 §18)——没有
        source_episode_ids/source_artifact_ids/source_knowledge_ids 就拒绝转 verified。"""
        record = self._store.get(memory_id)
        if record is None:
            return False
        if not (
            record.source_episode_ids or record.source_artifact_ids or record.source_knowledge_ids
        ):
            return False
        record.status = "verified"
        record.last_verified_at = _now_iso()
        self._store.put(record)
        self._record_event("memory.committed", record)
        return True

    def deprecate(self, memory_id: str) -> None:
        record = self._store.get(memory_id)
        if record is None:
            return
        record.status = "deprecated"
        self._store.put(record)
        self._record_event("memory.superseded", record, action="deprecated")

    # ── P2-03 Memory Toolset: correct / supersede / forget ─────────────────

    def correct_record(self, memory_id: str, *, content: str, provenance: str = "") -> str | None:
        record = self._store.get(memory_id)
        if record is None:
            return None
        if record.status not in ("candidate", "verified"):
            return None
        replacement = MemoryRecord(
            content=content,
            type=record.type,
            scope=record.scope,
            provenance=provenance or f"corrected_from:{memory_id}",
            trust_level=record.trust_level,
            confidence=record.confidence,
            source_episode_ids=list(record.source_episode_ids),
            source_artifact_ids=list(record.source_artifact_ids),
            source_knowledge_ids=list(record.source_knowledge_ids),
            entities=list(record.entities),
            keywords=list(record.keywords),
            scope_type=record.scope_type,
            scope_id=record.scope_id,
            memory_type=record.memory_type,
            source_event_ids=list(record.source_event_ids),
            tags=list(record.tags),
            supersedes=[memory_id],
            version=record.version + 1,
        )
        record.status = "deprecated"
        record.supersedes = list(set(record.supersedes) | {replacement.memory_id})
        self._store.put(record)
        self._store.put(replacement)
        self._record_event("memory.corrected", replacement, supersedes=memory_id)
        return replacement.memory_id

    def correct(self, memory_id: str, *, content: str, provenance: str = "") -> bool:
        return self.correct_record(memory_id, content=content, provenance=provenance) is not None

    def supersede(self, memory_id: str, *, content: str, provenance: str = "") -> str | None:
        record = self._store.get(memory_id)
        if record is None:
            return None
        if record.status not in ("candidate", "verified"):
            return None
        record.status = "deprecated"
        self._store.put(record)
        new_record = MemoryRecord(
            content=content,
            type=record.type,
            scope=record.scope,
            entities=list(record.entities),
            keywords=list(record.keywords),
            provenance=provenance or f"superseded_from:{memory_id}",
            trust_level=record.trust_level,
            status="candidate",
            supersedes=[memory_id],
        )
        self._store.put(new_record)
        record.supersedes = list(set(record.supersedes) | {new_record.memory_id})
        self._store.put(record)
        self._record_event("memory.superseded", new_record, supersedes=memory_id)
        return new_record.memory_id

    def forget(self, memory_id: str) -> bool:
        record = self._store.get(memory_id)
        if record is None:
            return False
        if record.status not in ("deprecated", "rejected"):
            return False
        with self._store._lock:
            if memory_id in self._store._records:
                record.status = "forgotten"
                self._store._records[memory_id] = asdict(record)
                self._store._save()
                self._record_event("memory.forgotten", record)
                return True
        return False


# ── 模块级单例（惯例同 server/memory_bank.py::VeyaMemoryBank 的模块用法）────

memory_controller = MemoryController()


# ── P2-03 Memory Toolset: correct / supersede / forget ─────────────────


class MemoryToolset:
    """P2-03 Memory Toolset — 提供 search/write/correct/supersede/forget。

    对接 MemoryController, 给 tool_registry 提供高层接口。
    不碰 ChatConsole 热路径 (trajectory 同纪律); 工具由大模型自主调用。
    """

    def __init__(self, controller: MemoryController | None = None):
        self.controller = controller or memory_controller

    def search(self, query: str = "", *, scope: str | None = None) -> list[dict[str, Any]]:
        """搜索记忆 (content/entities/keywords 关键词过滤)。"""
        records = self.controller.search(query, scope=scope)
        return [r.canonical_dict() for r in records]

    def write(self, content: str, *, type: str = "semantic", scope: str = "project",
              entities: list[str] | None = None, keywords: list[str] | None = None,
              provenance: str = "", trust_level: str = "unknown",
              scope_type: str | None = None, scope_id: str | None = None,
              memory_type: str | None = None, source_event_ids: list[str] | None = None,
              tags: list[str] | None = None, confidence: float | None = None) -> dict[str, Any]:
        """写入新记忆 (observe)。"""
        record = self.controller.observe(
            content, type=type, scope=scope, entities=entities,
            keywords=keywords, provenance=provenance, trust_level=trust_level,
            scope_type=scope_type, scope_id=scope_id, memory_type=memory_type,
            source_event_ids=source_event_ids, tags=tags, confidence=confidence,
        )
        return record.canonical_dict()

    def correct(self, memory_id: str, content: str, *, provenance: str = "") -> dict[str, Any]:
        """修正一条现有记忆。"""
        new_id = self.controller.correct_record(memory_id, content=content, provenance=provenance)
        if new_id is None:
            return {"status": "error", "error": f"memory not found or status not correctable: {memory_id}"}
        return {"status": "corrected", "memory_id": memory_id, "new_id": new_id}

    def supersede(self, memory_id: str, content: str, *, provenance: str = "") -> dict[str, Any]:
        """supersede 一条记忆: 标原记录 deprecated, 创建新 candidate 记录。"""
        new_id = self.controller.supersede(memory_id, content=content, provenance=provenance)
        if new_id is None:
            return {"status": "error", "error": f"memory not found or status not supersedeable: {memory_id}"}
        return {"status": "superseded", "old_id": memory_id, "new_id": new_id}

    def forget(self, memory_id: str) -> dict[str, Any]:
        """软删除一条记忆为 forgotten，保留最小审计记录。"""
        ok = self.controller.forget(memory_id)
        if not ok:
            return {"status": "error", "error": f"memory not found or status not forgettable: {memory_id}"}
        return {"status": "forgotten", "memory_id": memory_id}

    def get(self, memory_id: str) -> dict[str, Any]:
        """获取单条记忆详情。"""
        record = self.controller.get(memory_id)
        if record is None:
            return {"status": "not_found", "memory_id": memory_id}
        return {"status": "ok", "record": record.canonical_dict()}

    def explain(self, memory_id: str) -> dict[str, Any]:
        """解释一条记忆的来源。"""
        result = self.controller.explain_provenance(memory_id)
        if result is None:
            return {"status": "not_found", "memory_id": memory_id}
        return {"status": "ok", "memory_id": memory_id, **result}


# 模块级单例
memory_toolset = MemoryToolset()
