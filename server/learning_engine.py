"""server.learning_engine — VAOM CandidateLearning + PromotionGate（P4 落地）。

对标 docs/dev/rfc-01-vaom.md，PR-20/22/23（CandidateLearning/PromotionGate/
CrossFamilyReviewer），见 docs/VEYA_3.0_GAP_AUDIT.md §5 表。范围边界：

- **PR-21(Replay+BenchmarkRunner) 没有做**。goal_run 的执行调真实 LLM/hicode，
  没有确定性重放机制——"把历史 Episode 重跑一遍确认不回归"这件事在当前架构
  下做不到，伪造一个"看起来在重放"的东西比不做更危险。`promote()` 用"证据
  来自 ≥2 个独立 Episode"作为替代信号（下面 `_MIN_SOURCE_EPISODES`），这是
  简化不是真 Replay 的等价物，本文档档明确标注，不要假装它是。
- **PR-24(Skill/Policy Versioning) 没有单独做新对象**。`SkillSpec` 在 P2
  （`server/capability_model.py`）已经有 version 字段 + `promote`/`rollback`；
  Policy 作为独立 VAOM 对象目前完全不存在（rfc-01-vaom.md 仍标 🔴），版本化
  一个不存在的对象没有意义，等 Policy 对象本身被建出来再谈版本化。
- `reflect()` 的模式发现是**严格规则，不是相似度判断**：只认"完全相同的
  content 出现在 ≥2 个不同 episode 来源"这一种信号，不做任何模糊匹配/embedding
  相似度——2.0 文档"不让向量相似度决定事实真伪"的字面延伸：宁可漏发现，不要
  用不可靠的相似度判断制造假阳性候选。
- `promote()` 通过后，真正被改变状态的是 P3 `MemoryController` 里对应的
  `MemoryRecord`（调用其 `promote()`）——CandidateLearning 本身不是新的持久
  事实来源，它是"决定要不要把已有候选记忆转正"的审批记录。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.memory_controller import MemoryController, memory_controller
from server.promotion_review import dual_axis_promotion_review

# Replay 缺失时的替代信号: 证据必须来自这么多个不同 episode 才允许 promote。
# 不是"统计学意义上的显著性"，只是"不是单次巧合"的最低门槛。
_MIN_SOURCE_EPISODES = 2


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    import uuid

    return f"candidate_{uuid.uuid4().hex[:12]}"


@dataclass
class CandidateLearning:
    """尚未被证明可靠的新知识/流程/Skill/Policy 修改候选（VAOM CandidateLearning）。"""

    claim: str
    source_episode_ids: list[str]
    candidate_id: str = field(default_factory=_new_id)
    type: str = "procedural"  # semantic | procedural | skill_patch | policy_patch | routing_hint
    evidence_for: list[str] = field(default_factory=list)  # memory_id 列表
    evidence_against: list[str] = field(default_factory=list)
    scope: str = "project"
    confidence: float | None = None
    evaluation_plan: str = ""
    benchmark_refs: list[str] = field(default_factory=list)
    review: dict[str, Any] | None = None  # dual_axis_promotion_review 的结果
    status: str = "proposed"  # proposed | testing | verified | rejected | promoted
    created_at: str = field(default_factory=_now_iso)


_DEFAULT_STORAGE_PATH = str(Path.home() / ".veya" / "vaom_candidate_learning.json")


class _CandidateStore:
    """单文件 JSON 存储，惯例同 server/memory_bank.py / server/memory_controller.py。"""

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = Path(
            storage_path or os.environ.get("VEYA_CANDIDATE_LEARNING_PATH", _DEFAULT_STORAGE_PATH)
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

    def put(self, record: CandidateLearning) -> None:
        with self._lock:
            self._records[record.candidate_id] = asdict(record)
            self._save()

    def get(self, candidate_id: str) -> CandidateLearning | None:
        r = self._records.get(candidate_id)
        return CandidateLearning(**r) if r else None

    def all(self) -> list[CandidateLearning]:
        return [CandidateLearning(**r) for r in self._records.values()]


class LearningEngine:
    """reflect, propose, test, benchmark(跳过, 见模块docstring), review, promote, reject。"""

    def __init__(
        self,
        store: _CandidateStore | None = None,
        memory: MemoryController | None = None,
    ):
        self._store = store or _CandidateStore()
        self._memory = memory or memory_controller

    # ── reflect: 从 MemoryController 的候选记忆里发现重复模式 ─────────────

    def reflect(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        """严格规则匹配(见模块 docstring): 完全相同 content、来自 ≥2 个不同
        episode 的候选记忆, 才算一个"模式"。返回未落库的发现列表, 调用方决定
        要不要 propose()。"""
        records = self._memory.search(scope=scope)
        by_content: dict[str, list] = {}
        for r in records:
            if r.status == "deprecated":
                continue
            by_content.setdefault(r.content, []).append(r)

        findings = []
        for content, group in by_content.items():
            episode_ids = {eid for r in group for eid in r.source_episode_ids}
            if len(episode_ids) < _MIN_SOURCE_EPISODES:
                continue
            findings.append(
                {
                    "content": content,
                    "memory_ids": [r.memory_id for r in group],
                    "source_episode_ids": sorted(episode_ids),
                    "scope": group[0].scope,
                }
            )
        return findings

    # ── propose: 把一个 finding 落成 CandidateLearning ────────────────────

    def propose(self, finding: dict[str, Any], *, type: str = "procedural") -> CandidateLearning:
        evidence_for = list(finding.get("memory_ids") or finding.get("evidence_for") or [])
        if not evidence_for and finding.get("source_episode_ids"):
            # Trajectory/Eval patterns become Memory candidates first. They
            # remain unverified until the existing review + promotion gate.
            record = self._memory.observe(
                str(finding["content"]),
                type="procedural" if type == "procedural" else "semantic",
                scope=str(finding.get("scope") or "project"),
                source_episode_ids=list(finding["source_episode_ids"]),
                source_event_ids=list(finding.get("source_event_ids") or []),
                provenance="trajectory+eval pattern",
                trust_level="candidate",
            )
            evidence_for = [record.memory_id]
        candidate = CandidateLearning(
            claim=finding["content"],
            source_episode_ids=list(finding["source_episode_ids"]),
            type=type,
            evidence_for=evidence_for,
            scope=finding.get("scope", "project"),
        )
        self._store.put(candidate)
        return candidate

    def reflect_trajectories(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        """Find repeated, evaluated trajectory patterns without auto-promoting.

        A single success/failure is never enough. The pattern must have at
        least two distinct task ids and a passed acceptance evaluation for
        every occurrence; callers still need ``propose`` → ``review`` →
        ``promote`` before any MemoryRecord becomes verified.
        """
        from server.events import event_store

        trajectories = [
            event
            for event in event_store.read_all(topics={"trajectory.recorded"})
            if isinstance(event.get("payload"), dict)
        ]
        evaluations = {
            str(event.get("task_id")): bool((event.get("payload") or {}).get("passed"))
            for event in event_store.read_all(topics={"eval.recorded"})
            if event.get("task_id")
        }
        groups: dict[str, list[dict[str, Any]]] = {}
        for event in trajectories:
            payload = event["payload"]
            task_id = str(event.get("task_id") or payload.get("task_id") or "")
            objective = str(payload.get("objective") or "").strip()
            if not task_id or not objective or payload.get("outcome") not in {"completed", "success"}:
                continue
            if not evaluations.get(task_id, False):
                continue
            groups.setdefault(objective, []).append(event)

        findings: list[dict[str, Any]] = []
        for objective, events in groups.items():
            unique_tasks = sorted({str(event.get("task_id")) for event in events})
            if len(unique_tasks) < _MIN_SOURCE_EPISODES:
                continue
            if scope is not None and scope != "project":
                continue
            findings.append(
                {
                    "content": f"Repeatedly verified execution pattern: {objective}",
                    "memory_ids": [],
                    "source_episode_ids": unique_tasks,
                    "source_event_ids": [
                        str(event.get("event_id"))
                        for event in events
                        if event.get("event_id")
                    ],
                    "scope": scope or "project",
                }
            )
        return findings

    # ── test/review: 双轴 Promotion 审查(CrossFamilyReviewer 落地, 见
    # server/promotion_review.py) ─────────────────────────────────────────

    async def review(self, candidate_id: str) -> CandidateLearning | None:
        candidate = self._store.get(candidate_id)
        if candidate is None:
            return None
        evidence_texts = [
            m.content for m in (self._memory.get(mid) for mid in candidate.evidence_for) if m
        ]
        report = await dual_axis_promotion_review(claim=candidate.claim, evidence=evidence_texts)
        candidate.review = report
        candidate.status = "rejected" if report["blocked"] else "testing"
        self._store.put(candidate)
        return candidate

    # ── promote/reject ─────────────────────────────────────────────────

    def promote(self, candidate_id: str) -> bool:
        """要求: 通过双轴审查(status=="testing", 不是被拒或还没审)、且证据来自
        ≥_MIN_SOURCE_EPISODES 个不同 episode(Replay 缺失的替代信号, 见模块
        docstring)。通过后同时把 evidence_for 指向的每条 MemoryRecord 转 verified
        ——promotion 真正生效的地方在 P3 的 MemoryController, 不是本对象自己。
        """
        candidate = self._store.get(candidate_id)
        if candidate is None:
            return False
        if candidate.status != "testing":
            return False
        if len(set(candidate.source_episode_ids)) < _MIN_SOURCE_EPISODES:
            return False
        for memory_id in candidate.evidence_for:
            self._memory.promote(memory_id)
        candidate.status = "promoted"
        self._store.put(candidate)
        return True

    def reject(self, candidate_id: str, *, reason: str = "") -> None:
        candidate = self._store.get(candidate_id)
        if candidate is None:
            return
        candidate.status = "rejected"
        if reason:
            candidate.evidence_against.append(reason)
        self._store.put(candidate)


# ── 模块级单例（惯例同 memory_controller / performance_store）─────────────

learning_engine = LearningEngine()
