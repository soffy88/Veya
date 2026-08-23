"""goal_run trust_plane — Claim/Evidence/EvaluationResult/VerifiedState/TaskEpisode。

对标 docs/dev/rfc-01-vaom.md 定义的 VAOM Trust Plane 对象，PR-04~08 的实现型落地。

范围边界（见 docs/VEYA_3.0_GAP_AUDIT.md「迁移兼容原则」）：
- 纯旁路记录。runner.py 现有的 task.status 转移逻辑一行未改——verify_task() 的
  passed/failed 依然是唯一驱动状态机的信号，本模块只是在那之后把同一次判断
  结果按 VAOM schema 多记一份，供将来查询/回放，不影响任何现有行为。
- 命名避开 RFC-01 §3 记录的冲突：新对象叫 TaskEpisode，不叫 Episode（
  platform/3O/omodul/omodul/append_episode.py 的 Episode 是语义完全不同的
  学习回流入口，不可混用）。
- ID 格式沿用 loop-plane 已有的 `{prefix}{uuid4().hex[:12]}` 惯例（见
  docs/dev/rfc-02-canonical-ids.md），不引入新依赖去追 ULID——目标格式留给
  Phase 2 的正式 Canonical ID 迁移，这里先跟现有代码手法保持一致。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── Trust Plane 对象 ────────────────────────────────────────────────────


@dataclass
class Claim:
    """执行者声称已完成的事实（VAOM Claim）。永远只是自报，不是事实本身。"""

    task_id: str
    goal_id: str
    actor: str
    statement: str
    target_refs: list[str] = field(default_factory=list)
    claim_id: str = field(default_factory=lambda: _new_id("claim"))
    claim_type: str = "task_completion"
    confidence_self_reported: float | None = None
    status: str = "claimed"  # claimed | observed | verified | rejected
    created_at: str = field(default_factory=_now_iso)


@dataclass
class Evidence:
    """可检查的环境事实（VAOM Evidence）。hash 防"验证后内容被换掉"。"""

    claim_id: str
    kind: str  # git_diff | test | log | file
    source: str
    artifact_ref: str
    evidence_id: str = field(default_factory=lambda: _new_id("evidence"))
    collected_by: str = "goal_run.trust_plane"
    hash: str = ""
    trust_level: str = "L1_observed"
    timestamp: str = field(default_factory=_now_iso)
    immutable_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.hash and self.artifact_ref:
            self.hash = hashlib.sha256(self.artifact_ref.encode("utf-8")).hexdigest()[:16]


@dataclass
class EvaluationResult:
    """确定性/领域/模型/结果层的验证记录（VAOM EvaluationResult）。

    evaluator_type 对齐 rfc-01 §2 提到的 E0-E3 分层：
    E0 deterministic（verify.py 的 rule/mechanical 检查）、
    E2 independent_model（verify.py 的 LLM 判定，以及 code_review.py 双轴审查）。
    E1 domain / E3 outcome 目前没有数据来源，不在本轮填充。
    """

    task_id: str
    goal_id: str
    claim_id: str
    evaluator_type: str  # E0_deterministic | E2_independent_model
    verdict: str  # pass | fail
    eval_id: str = field(default_factory=lambda: _new_id("eval"))
    evaluator_version: str = "goal_run.verify_task@v1"
    objective_metrics: dict[str, Any] = field(default_factory=dict)
    rubric_metrics: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    score: float | None = None
    confidence: float | None = None
    reviewer_model: str | None = None
    failures: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)


@dataclass
class VerifiedState:
    """只有这个对象存在，任务进度才算"经证据验证"（VAOM VerifiedState）。

    2.0 文档 P4 "Claim 不是事实"的落地: task.status==completed 不能只靠
    verify_result.passed 这个内存里的 bool——本对象是它的持久化对应物。
    """

    task_id: str
    goal_id: str
    claim_id: str
    evidence_ids: list[str]
    evaluation_ids: list[str]
    assertion: str
    state_id: str = field(default_factory=lambda: _new_id("state"))
    status: str = "verified"  # verified | rejected | superseded
    confidence: float | None = None
    valid_from: str = field(default_factory=_now_iso)
    valid_until: str | None = None
    verifier: str = "goal_run.verify_task"
    verified_at: str = field(default_factory=_now_iso)
    version: int = 1


@dataclass
class TaskEpisode:
    """一次 goal_run 的完整因果账本（VAOM Episode，改名避开 omodul 命名冲突，见模块 docstring）。"""

    episode_id: str
    goal_id: str
    goal_text: str
    task_ids: list[str]
    claim_ids: list[str]
    evidence_ids: list[str]
    evaluation_ids: list[str]
    verified_state_ids: list[str]
    outcome: str  # completed | blocked | cancelled
    started_at: str | None
    completed_at: str | None
    schema_version: int = 1


# ── Trust Plane 记录构建：从现有 verify/review 结果拼出 Claim→Evidence→Evaluation(→VerifiedState) ──


def record_task_verification(
    *,
    task_id: str,
    goal_id: str,
    actor: str,
    statement: str,
    target_refs: list[str],
    verify_passed: bool,
    verify_summary: str,
    diff_text: str,
    review_findings: dict[str, Any] | None,
) -> tuple[Claim, list[Evidence], list[EvaluationResult], VerifiedState | None]:
    """把一次任务验收的现有产物（verify_task 结果 + 双轴 review）拼成 Trust Plane 记录。

    不调用任何 LLM/IO——纯函数，接收调用方已经算出来的结果，只负责按 VAOM
    schema 组装。git diff 的 hash 化落在 Evidence.hash 里，供未来"验证后内容
    被换掉"取证用。
    """
    claim = Claim(
        task_id=task_id,
        goal_id=goal_id,
        actor=actor,
        statement=statement,
        target_refs=list(target_refs),
        status="observed",
    )

    evidences: list[Evidence] = []
    if diff_text.strip():
        evidences.append(
            Evidence(
                claim_id=claim.claim_id,
                kind="git_diff",
                source="server.goal_run.git_diff.capture_task_diff",
                artifact_ref=diff_text[:4000],
                trust_level="L1_observed",
            )
        )
    if verify_summary.strip():
        evidences.append(
            Evidence(
                claim_id=claim.claim_id,
                kind="log",
                source="server.goal_run.verify.verify_task",
                artifact_ref=verify_summary,
                trust_level="L1_observed",
            )
        )

    evaluations: list[EvaluationResult] = []
    evaluations.append(
        EvaluationResult(
            task_id=task_id,
            goal_id=goal_id,
            claim_id=claim.claim_id,
            evaluator_type="E0_deterministic",
            verdict="pass" if verify_passed else "fail",
            objective_metrics={"verify_summary": verify_summary},
            evidence_ids=[e.evidence_id for e in evidences],
            failures=[] if verify_passed else [verify_summary],
        )
    )
    if review_findings:
        for axis in ("standards", "spec"):
            axis_report = review_findings.get(axis) or {}
            worst = axis_report.get("worst")
            evaluations.append(
                EvaluationResult(
                    task_id=task_id,
                    goal_id=goal_id,
                    claim_id=claim.claim_id,
                    evaluator_type="E2_independent_model",
                    verdict="advisory",  # advisory only，见 code_review.py 模块 docstring
                    rubric_metrics={"axis": axis, "findings": axis_report.get("findings", [])},
                    reviewer_model="dual_axis_review",
                    failures=[worst] if worst else [],
                )
            )

    verified_state: VerifiedState | None = None
    if verify_passed:
        claim.status = "verified"
        verified_state = VerifiedState(
            task_id=task_id,
            goal_id=goal_id,
            claim_id=claim.claim_id,
            evidence_ids=[e.evidence_id for e in evidences],
            evaluation_ids=[ev.eval_id for ev in evaluations],
            assertion=statement,
        )
    else:
        claim.status = "rejected"

    return claim, evidences, evaluations, verified_state


# ── 持久化：trust_plane.jsonl（同目录，append-only，跟 events.jsonl 同一模式）──

_TRUST_PLANE_JSONL = "trust_plane.jsonl"
_EPISODE_JSON = "episode.json"


def _goal_run_dir(project_root: str, goal_id: str) -> Path:
    return Path(project_root) / ".veya-project" / "goal-runs" / goal_id


def append_trust_plane_records(
    project_root: str,
    goal_id: str,
    *,
    claim: Claim,
    evidences: list[Evidence],
    evaluations: list[EvaluationResult],
    verified_state: VerifiedState | None,
) -> None:
    """追加写入本次验收产生的全部 Trust Plane 记录，每行一条，`_type` 字段区分类型。"""
    run_dir = _goal_run_dir(project_root, goal_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / _TRUST_PLANE_JSONL
    records: list[dict[str, Any]] = [{"_type": "Claim", **asdict(claim)}]
    records.extend({"_type": "Evidence", **asdict(e)} for e in evidences)
    records.extend({"_type": "EvaluationResult", **asdict(ev)} for ev in evaluations)
    if verified_state is not None:
        records.append({"_type": "VerifiedState", **asdict(verified_state)})
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_trust_plane_records(project_root: str, goal_id: str) -> list[dict[str, Any]]:
    path = _goal_run_dir(project_root, goal_id) / _TRUST_PLANE_JSONL
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def build_and_write_task_episode(
    project_root: str,
    goal_id: str,
    goal_text: str,
    *,
    task_ids: list[str],
    outcome: str,
    started_at: str | None,
    completed_at: str | None,
) -> TaskEpisode:
    """G3 Finalize 时调用：聚合本次 goal_run 全部 Trust Plane 记录成一个 TaskEpisode，覆盖写 episode.json。"""
    records = read_trust_plane_records(project_root, goal_id)
    episode = TaskEpisode(
        episode_id=_new_id("episode"),
        goal_id=goal_id,
        goal_text=goal_text,
        task_ids=list(task_ids),
        claim_ids=[r["claim_id"] for r in records if r["_type"] == "Claim"],
        evidence_ids=[r["evidence_id"] for r in records if r["_type"] == "Evidence"],
        evaluation_ids=[r["eval_id"] for r in records if r["_type"] == "EvaluationResult"],
        verified_state_ids=[r["state_id"] for r in records if r["_type"] == "VerifiedState"],
        outcome=outcome,
        started_at=started_at,
        completed_at=completed_at,
    )
    run_dir = _goal_run_dir(project_root, goal_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / _EPISODE_JSON).write_text(
        json.dumps(asdict(episode), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return episode


def read_task_episode(project_root: str, goal_id: str) -> dict[str, Any] | None:
    path = _goal_run_dir(project_root, goal_id) / _EPISODE_JSON
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
