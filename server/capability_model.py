"""server.capability_model — VAOM Capability/Skill/Knowledge/Harness/Performance 对象模型。

对标 docs/dev/rfc-01-vaom.md，P2 落地（PR-10/11/12/14/17，见
docs/VEYA_3.0_GAP_AUDIT.md §5 表）。范围边界：

- 这里只建"容器"和"从已有真实数据桥接的适配器"，不发明数据。`CapabilitySpec`/
  `KnowledgePack` 目前没有真实来源，注册表建好但保持空——由后续 PR 真正把
  某个能力/知识包接进来时再注册，不在这里编造"看起来合理"的条目。
- `SkillSpec` 从既有 `server.skill_hub.VeyaSkillHub` 的公开接口
  （`get_stats`/`describe`/`skill_risk`）桥接，不重新扫描技能目录——3O 单一
  来源纪律在 veya 层的版本：已有的枚举逻辑不能有第二份实现。
- `HarnessSpec` 目前只是**静态元数据描述**（hicode/dsh/builtin 三个已知执行者
  各自的 workspace/session/sandbox 语义），不路由任何真实调用——`execute`/
  `resume`/`cancel` 故意不实现：真要把 CC/Pi/Hicode/DSH 的调用改走这层需要
  单独设计 adapter（PR-15），且直接触碰执行路径，风险跟这里的元数据登记完全
  不是一回事，不在本文件里顺带做掉。
- `capability_search`/`harness_execute` 这类 MasterAgent 工具面接口**不在本文件
  范围**：往主链加新工具属于 `docs/ARCHITECTURE_STABLE.md` §4「改工具面」，
  需要单独获得用户同意；本文件只是后端数据结构，没有任何一行注册进
  `server/tool_registry.py`。
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


# ── 对象定义（字段取自 2.0 文档第 6/7/9/14 章，见 rfc-01-vaom.md §2）──────────


@dataclass
class CapabilitySpec:
    """ "能完成什么"的高层能力定义。目前无真实来源，注册表允许为空。"""

    capability_id: str
    domain: str
    description: str
    can_do: list[str] = field(default_factory=list)
    cannot_do: list[str] = field(default_factory=list)
    input_types: list[str] = field(default_factory=list)
    output_types: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    knowledge_packs: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    preferred_harnesses: list[str] = field(default_factory=list)
    supported_harnesses: list[str] = field(default_factory=list)
    risk_level: str = "unknown"
    permission_scope: str = ""
    evaluators: list[str] = field(default_factory=list)
    benchmark_suite: str | None = None
    acceptance_criteria: list[str] = field(default_factory=list)
    provenance: str = ""
    historical_performance: dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"  # candidate | verified | deprecated
    version: int = 1


@dataclass
class SkillSpec:
    """ "如何完成"的可复用程序性方法。桥接自既有 skill_hub，不重新定义技能。"""

    skill_id: str
    instructions: str  # manifest description
    version: int = 1
    applicable_when: list[str] = field(default_factory=list)
    not_applicable_when: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    knowledge_refs: list[str] = field(default_factory=list)
    evaluators: list[str] = field(default_factory=list)
    benchmark_suite: str | None = None
    # success/quality/tokens/cost/latency/regressions — 全部留空: 没有 benchmark
    # 数据就不能假装有, verified 状态因此恒为 False（见 SkillRegistry.promote）。
    performance: dict[str, Any] = field(default_factory=dict)
    provenance: str = ""
    status: str = "candidate"  # candidate | verified | deprecated
    trigger_examples: list[str] = field(default_factory=list)
    execution_type: str = "prompt"
    execution_ref: str = ""
    created_by: str = "system"
    source_event_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    trust_status: str = "review_required"  # trusted | review_required | blocked
    success_count: int = 0
    failure_count: int = 0


@dataclass
class KnowledgePack:
    """领域事实/规范/recipe。不承担执行逻辑，目前无真实来源。"""

    knowledge_id: str
    title: str
    domain: str
    version: int = 1
    content_refs: list[str] = field(default_factory=list)
    source: str = ""
    provenance: str = ""
    license: str = ""
    trust_level: str = "unknown"
    valid_from: str | None = None
    valid_until: str | None = None
    scopes: list[str] = field(default_factory=list)
    status: str = "candidate"


@dataclass
class HarnessSpec:
    """承载 Agent loop/workspace/session/tool semantics 的执行后端。

    静态元数据，不是可调用对象——本文件不路由任何真实执行（见模块 docstring）。
    """

    harness_id: str
    version: str
    capabilities: list[str] = field(default_factory=list)
    supported_models: list[str] = field(default_factory=list)
    workspace_semantics: str = ""
    session_semantics: str = ""
    tool_semantics: str = ""
    context_policies: list[str] = field(default_factory=list)
    sandbox_level: str = "none"
    permission_model: str = ""
    status: str = "candidate"


@dataclass
class CapabilityPackage:
    """标准打包单元（Capability+Skill+Knowledge+Evaluator+Benchmark+Adapter 的引用集合）。"""

    package_id: str
    capability_ids: list[str] = field(default_factory=list)
    skill_ids: list[str] = field(default_factory=list)
    knowledge_ids: list[str] = field(default_factory=list)
    evaluator_refs: list[str] = field(default_factory=list)
    benchmark_refs: list[str] = field(default_factory=list)
    adapter_refs: list[str] = field(default_factory=list)
    status: str = "candidate"


@dataclass
class PerformanceProfile:
    """Capability/Harness/Model/Skill 在真实历史中的表现统计（聚合视图，见 PerformanceStore）。"""

    harness_id: str
    task_archetype: str
    capability_id: str | None = None
    model_id: str | None = None
    skill_version: str | None = None
    sample_size: int = 0
    success_rate: float | None = None
    failure_patterns: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=_now_iso)


# ── 单文件 JSON 存储（惯例同 server/memory_bank.py::VeyaMemoryBank）──────────

_DEFAULT_STORAGE_PATH = str(Path.home() / ".veya" / "vaom_capability_registry.json")

_KIND_TO_CLS: dict[str, type] = {
    "capability": CapabilitySpec,
    "skill": SkillSpec,
    "knowledge": KnowledgePack,
    "harness": HarnessSpec,
    "package": CapabilityPackage,
}


class _JsonRegistryStore:
    """四个 Registry 共用的底层存储：一个 JSON 文件，按 kind 分区。"""

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = Path(
            storage_path or os.environ.get("VEYA_CAPABILITY_REGISTRY_PATH", _DEFAULT_STORAGE_PATH)
        ).expanduser()
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, dict]] = self._load()

    def _load(self) -> dict[str, dict[str, dict]]:
        if self.storage_path.exists():
            try:
                with open(self.storage_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {k: dict(v) for k, v in data.items() if k in _KIND_TO_CLS}
            except (json.JSONDecodeError, OSError):
                pass
        return {k: {} for k in _KIND_TO_CLS}

    def _save(self) -> None:
        with self._lock:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.storage_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.storage_path)

    def put(self, kind: str, item_id: str, record: dict) -> None:
        with self._lock:
            self._data.setdefault(kind, {})[item_id] = record
            self._save()

    def get(self, kind: str, item_id: str) -> dict | None:
        return self._data.get(kind, {}).get(item_id)

    def all(self, kind: str) -> list[dict]:
        return list(self._data.get(kind, {}).values())

    def delete(self, kind: str, item_id: str) -> None:
        with self._lock:
            self._data.get(kind, {}).pop(item_id, None)
            self._save()


def _to_record(obj: Any) -> dict:
    return asdict(obj)


# ── Registry 最小接口（字段取自 rfc-01-vaom.md §"Registry 与内部 API"）──────


class CapabilityRegistry:
    """search, get, register_candidate, verify, deprecate。"""

    def __init__(self, store: _JsonRegistryStore | None = None):
        self._store = store or _JsonRegistryStore()

    def search(self, query: str = "") -> list[CapabilitySpec]:
        items = [CapabilitySpec(**r) for r in self._store.all("capability")]
        if not query:
            return items
        q = query.lower()
        return [i for i in items if q in i.description.lower() or q in i.domain.lower()]

    def get(self, capability_id: str) -> CapabilitySpec | None:
        r = self._store.get("capability", capability_id)
        return CapabilitySpec(**r) if r else None

    def register_candidate(self, spec: CapabilitySpec) -> None:
        spec.status = "candidate"
        self._store.put("capability", spec.capability_id, _to_record(spec))

    def verify(self, capability_id: str) -> bool:
        """没有 evaluators/benchmark_suite 就不能转 verified——P4 Promotion Gate
        原则(见 rfc-01/2.0 文档 §9.1)在这里先立住，不等 P4 才生效。"""
        spec = self.get(capability_id)
        if spec is None:
            return False
        if not spec.evaluators or not spec.benchmark_suite:
            return False
        spec.status = "verified"
        self._store.put("capability", capability_id, _to_record(spec))
        return True

    def deprecate(self, capability_id: str) -> None:
        spec = self.get(capability_id)
        if spec is None:
            return
        spec.status = "deprecated"
        self._store.put("capability", capability_id, _to_record(spec))


class SkillRegistry:
    """search, get_version, benchmark, promote, rollback。"""

    def __init__(self, store: _JsonRegistryStore | None = None):
        self._store = store or _JsonRegistryStore()

    def search(self, query: str = "") -> list[SkillSpec]:
        items = [SkillSpec(**r) for r in self._store.all("skill")]
        if not query:
            return items
        q = query.lower()
        return [i for i in items if q in i.instructions.lower() or q in i.skill_id.lower()]

    def get_version(self, skill_id: str) -> SkillSpec | None:
        r = self._store.get("skill", skill_id)
        return SkillSpec(**r) if r else None

    def register_candidate(self, spec: SkillSpec) -> None:
        spec.status = "candidate"
        spec.updated_at = _now_iso()
        self._store.put("skill", spec.skill_id, _to_record(spec))

    @staticmethod
    def _record_event(topic: str, spec: SkillSpec, **extra: Any) -> dict[str, Any] | None:
        with contextlib.suppress(Exception):
            return append_canonical_event(
                topic,
                {"skill": _to_record(spec), **extra},
                actor=spec.created_by or "system",
                trace_id=spec.skill_id,
            )
        return None

    def propose_skill(self, description: str, config: dict[str, Any] | None = None) -> SkillSpec:
        """Propose a new skill candidate in two-phase teaching flow.

        Creates a skill spec with status "candidate" (pending user confirmation).
        The caller (frontend/UI) must later confirm or reject via confirm_skill()
        or reject_skill().  This enforces the candidate→confirm separation so
        no skill is registered without explicit user authorization.

        Returns the candidate skill spec with skill_id for tracking.
        """
        import uuid

        name = description[:50].strip().replace(" ", "-") or f"skill-{uuid.uuid4().hex[:8]}"
        safe_name = name
        # Ensure unique name in skill_hub
        try:
            from server.skill_hub import skill_hub

            if skill_hub.has(name):
                safe_name = f"{name}-{uuid.uuid4().hex[:4]}"
        except Exception:
            pass
        config = config or {}
        spec = SkillSpec(
            skill_id=safe_name,
            instructions=description,
            version=1,
            provenance=f"skill_teach_proposal@{datetime.now().isoformat()}",
            status="candidate",
            trigger_examples=list(config.get("trigger_examples") or []),
            execution_type=str(config.get("execution_type") or "prompt"),
            execution_ref=str(config.get("execution_ref") or ""),
            created_by=str(config.get("created_by") or "user"),
            source_event_ids=list(config.get("source_event_ids") or []),
            trust_status="review_required",
        )
        self.register_candidate(spec)
        event = self._record_event("skill.candidate_created", spec)
        if event and not spec.source_event_ids and event.get("event_id"):
            spec.source_event_ids = [str(event["event_id"])]
            self._store.put("skill", spec.skill_id, _to_record(spec))
        return spec

    @staticmethod
    def _scan_candidate(spec: SkillSpec) -> dict[str, Any]:
        """Small deterministic gate for prompt/executable skill candidates.

        Teaching must not turn unreviewed instructions into trusted runtime
        behavior. Existing file-backed skills continue to use skill_hub's
        full AST/static and semantic scanners; this gate covers the registry's
        text-based teaching path.
        """
        source = "\n".join([spec.instructions, spec.execution_ref, *spec.required_tools]).lower()
        forbidden = (
            "rm -rf",
            "subprocess",
            "os.system(",
            "eval(",
            "exec(",
            "__import__",
        )
        findings = [token for token in forbidden if token in source]
        return {
            "verdict": "blocked" if findings else "pass",
            "findings": findings,
            "scan": "deterministic_registry_gate",
        }

    def confirm_skill(self, skill_id: str) -> SkillSpec | None:
        """Confirm a previously proposed skill candidate.

        Changes status from "candidate" to "verified" — the skill is now
        permanently in the registry and discoverable by the model.
        """
        spec = self.get_version(skill_id)
        if spec is None:
            return None
        if spec.status != "candidate":
            raise ValueError(f"Skill {skill_id} is not a candidate (status={spec.status})")
        scan = self._scan_candidate(spec)
        self._record_event("skill.scan_completed", spec, result=scan)
        if scan["verdict"] != "pass":
            spec.trust_status = "blocked"
            self._store.put("skill", skill_id, _to_record(spec))
            raise ValueError(
                f"Skill {skill_id} failed static safety scan: {', '.join(scan['findings'])}"
            )
        spec.status = "verified"
        spec.trust_status = "trusted"
        spec.version += 1
        spec.updated_at = _now_iso()
        self._store.put("skill", skill_id, _to_record(spec))
        self._record_event("skill.created", spec)
        return spec

    def reject_skill(self, skill_id: str) -> bool:
        """Reject a previously proposed skill candidate.

        Changes status to "deprecated" — the skill is removed from
        active consideration.
        Returns True if the skill was found and rejected.
        """
        spec = self.get_version(skill_id)
        if spec is None:
            return False
        spec.status = "deprecated"
        spec.updated_at = _now_iso()
        self._store.put("skill", skill_id, _to_record(spec))
        self._record_event("skill.updated", spec, action="rejected")
        return True

    def benchmark(self, skill_id: str, metrics: dict[str, Any]) -> SkillSpec | None:
        spec = self.get_version(skill_id)
        if spec is None:
            return None
        spec.performance.update(metrics)
        spec.updated_at = _now_iso()
        self._store.put("skill", skill_id, _to_record(spec))
        self._record_event("skill.updated", spec, action="benchmark")
        return spec

    def record_usage(
        self,
        skill_id: str,
        *,
        success: bool,
        evidence: list[str] | None = None,
    ) -> SkillSpec | None:
        """Record usage evidence without auto-promoting or rewriting a skill."""
        spec = self.get_version(skill_id)
        if spec is None:
            return None
        if success:
            spec.success_count += 1
        else:
            spec.failure_count += 1
        if evidence:
            spec.performance.setdefault("usage_evidence", []).extend(evidence)
        spec.updated_at = _now_iso()
        self._store.put("skill", skill_id, _to_record(spec))
        self._record_event(
            "skill.failed" if not success else "skill.executed", spec, evidence=evidence or []
        )
        return spec

    def promote(self, skill_id: str) -> bool:
        """没有 benchmark 数据不能 promote(见 2.0 文档 §9.1 "Skill 必须可验证")。"""
        spec = self.get_version(skill_id)
        if spec is None or not spec.performance:
            return False
        spec.status = "verified"
        spec.trust_status = "trusted"
        spec.updated_at = _now_iso()
        self._store.put("skill", skill_id, _to_record(spec))
        self._record_event("skill.created", spec, action="promoted")
        return True

    def rollback(self, skill_id: str) -> None:
        spec = self.get_version(skill_id)
        if spec is None:
            return
        spec.status = "deprecated"
        spec.updated_at = _now_iso()
        self._store.put("skill", skill_id, _to_record(spec))
        self._record_event("skill.updated", spec, action="rollback")


class KnowledgeRegistry:
    """search, import_pack, provenance, invalidate。"""

    def __init__(self, store: _JsonRegistryStore | None = None):
        self._store = store or _JsonRegistryStore()

    def search(self, query: str = "") -> list[KnowledgePack]:
        items = [KnowledgePack(**r) for r in self._store.all("knowledge")]
        if not query:
            return items
        q = query.lower()
        return [i for i in items if q in i.title.lower() or q in i.domain.lower()]

    def import_pack(self, pack: KnowledgePack) -> None:
        self._store.put("knowledge", pack.knowledge_id, _to_record(pack))

    def provenance(self, knowledge_id: str) -> dict[str, Any] | None:
        r = self._store.get("knowledge", knowledge_id)
        if r is None:
            return None
        return {"source": r.get("source", ""), "provenance": r.get("provenance", "")}

    def invalidate(self, knowledge_id: str) -> None:
        r = self._store.get("knowledge", knowledge_id)
        if r is None:
            return
        pack = KnowledgePack(**r)
        pack.status = "deprecated"
        self._store.put("knowledge", knowledge_id, _to_record(pack))


class HarnessRegistry:
    """list, capability_matrix, profile, execute（PR-15, 见 docs/dev/rfc-01-vaom.md）。

    execute() 是 hicode/dsh/builtin 真实调用路径的唯一路由点(server/project_ask.py
    的 _run_builtin/_run_hicode/_run_dsh、server/goal_run/leaf.py 都改经这里调,
    见两处的接线改动)——本方法不重写这三个函数的任何一行, 只是把"选哪个函数"
    这个决策收口成一处, 局部 import 避免 capability_model.py(轻量叶子模块) 对
    project_ask.py(带 ProjectStore/Understand 等重依赖) 产生模块级耦合。resume/
    cancel 仍未实现——hicode/dsh 目前都是"提交后等到底"的同步语义, 没有真实的
    "恢复一个进行中调用"场景, 加这两个方法只会是没人用的空壳。"""

    def __init__(self, store: _JsonRegistryStore | None = None):
        self._store = store or _JsonRegistryStore()

    def list(self) -> list[HarnessSpec]:
        return [HarnessSpec(**r) for r in self._store.all("harness")]

    def get(self, harness_id: str) -> HarnessSpec | None:
        r = self._store.get("harness", harness_id)
        return HarnessSpec(**r) if r else None

    def register(self, spec: HarnessSpec) -> None:
        self._store.put("harness", spec.harness_id, _to_record(spec))

    def capability_matrix(self) -> dict[str, list[str]]:
        return {h.harness_id: h.capabilities for h in self.list()}

    def profile(self, harness_id: str) -> PerformanceProfile | None:
        """读 PerformanceStore 里这个 harness 的聚合表现(见下方 performance_store 单例)。"""
        return performance_store.aggregate(harness_id)

    async def execute(
        self,
        harness_id: str,
        *,
        store: Any,
        task_id: str,
        request: str,
        project_root: str | None = None,
        understand_prefix: str = "",
    ) -> Any:
        """按 harness_id 路由到既有的 _run_builtin/_run_hicode/_run_dsh, 参数/返回值
        跟直接调用这三个函数完全一致——纯路由, 不改变任何一个的行为。"""
        from server.project_ask import _run_builtin, _run_dsh, _run_hicode

        if harness_id == "builtin":
            return _run_builtin(store, task_id, request)
        if harness_id == "hicode":
            return await _run_hicode(store, task_id, project_root, request, understand_prefix)
        if harness_id == "dsh":
            return await _run_dsh(store, task_id, project_root, request, understand_prefix)
        raise ValueError(f"unknown harness_id: {harness_id!r}")


# ── PerformanceStore：record_outcome, aggregate, compare, confidence ─────────

_DEFAULT_PERF_PATH = str(Path.home() / ".veya" / "vaom_performance.jsonl")


class PerformanceStore:
    """record_outcome, aggregate, compare, confidence。append-only JSONL,
    惯例同 server/goal_run/store.py::append_event。"""

    def __init__(self, storage_path: str | Path | None = None):
        self.storage_path = Path(
            storage_path or os.environ.get("VEYA_PERFORMANCE_STORE_PATH", _DEFAULT_PERF_PATH)
        ).expanduser()
        self._lock = threading.RLock()

    def record_outcome(
        self,
        *,
        harness_id: str,
        task_archetype: str,
        success: bool,
        capability_id: str | None = None,
        model_id: str | None = None,
    ) -> None:
        with self._lock:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "harness_id": harness_id,
                "task_archetype": task_archetype,
                "success": success,
                "capability_id": capability_id,
                "model_id": model_id,
                "ts": _now_iso(),
            }
            with self.storage_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _read_all(self) -> list[dict]:
        if not self.storage_path.exists():
            return []
        lines = self.storage_path.read_text(encoding="utf-8").strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def aggregate(
        self, harness_id: str, task_archetype: str | None = None
    ) -> PerformanceProfile | None:
        samples = [
            r
            for r in self._read_all()
            if r["harness_id"] == harness_id
            and (task_archetype is None or r["task_archetype"] == task_archetype)
        ]
        if not samples:
            return None
        success_count = sum(1 for r in samples if r["success"])
        return PerformanceProfile(
            harness_id=harness_id,
            task_archetype=task_archetype or "*",
            sample_size=len(samples),
            success_rate=success_count / len(samples),
        )

    def compare(self, harness_ids: list[str], task_archetype: str | None = None) -> dict[str, Any]:
        return {
            hid: self.aggregate(hid, task_archetype)
            for hid in harness_ids
            if self.aggregate(hid, task_archetype) is not None
        }

    def confidence(self, harness_id: str, task_archetype: str | None = None) -> float:
        """极简置信度: sample_size 越大越可信, 封顶 1.0。不是统计学意义上的置信区间,
        只是"要不要信这个 success_rate"的粗粒度信号, 真正的 confidence_interval
        留给后续需要时再做。"""
        profile = self.aggregate(harness_id, task_archetype)
        if profile is None:
            return 0.0
        return min(1.0, profile.sample_size / 20)


# ── 模块级单例（惯例同 skill_hub.py::skill_hub / darwin_evolution.py::darwin_evolution）──

_shared_store = _JsonRegistryStore()
capability_registry = CapabilityRegistry(_shared_store)
skill_registry = SkillRegistry(_shared_store)
knowledge_registry = KnowledgeRegistry(_shared_store)
harness_registry = HarnessRegistry(_shared_store)
performance_store = PerformanceStore()


# ── 桥接：从既有 skill_hub 同步 SkillSpec，不重新扫描技能目录 ─────────────


def sync_skills_from_hub(hub: Any) -> int:
    """把 hub(server.skill_hub.VeyaSkillHub 实例)里已加载的技能同步成 SkillSpec。

    只读 hub 的公开接口(get_stats/describe/skill_risk), 不碰 hub 内部状态,
    不触发重新扫描。返回同步条数。"""
    stats = hub.get_stats()
    names: list[str] = stats.get("skills", [])
    count = 0
    for name in names:
        risk = hub.skill_risk(name)
        spec = SkillSpec(
            skill_id=name,
            instructions=hub.describe(name),
            required_tools=[],
            provenance=f"server.skill_hub.VeyaSkillHub({hub.skills_dir})",
            status="candidate",
        )
        if risk.get("max_severity") not in (None, "none"):
            spec.not_applicable_when.append(
                f"static scan flagged {risk.get('max_severity')} risk: {risk.get('categories')}"
            )
        skill_registry.register_candidate(spec)
        count += 1
    return count


# ── 已知 Harness 的静态元数据登记(hicode/dsh/builtin, 见模块 docstring) ─────


def bootstrap_default_harnesses() -> None:
    """登记 hicode/dsh/builtin 三个已知执行者的静态元数据。幂等(按 harness_id
    覆盖写), 可重复调用。"""
    harness_registry.register(
        HarnessSpec(
            harness_id="hicode",
            version="unversioned",
            capabilities=["long_task_coding", "test_execution", "git_workflow"],
            workspace_semantics=(
                "git snapshot/commit/rollback，跨会话共享同一 project_root 时经 "
                "platform/3O/omodul/omodul/sandbox_broker.py::SandboxBroker."
                "async_workspace() 互斥（server/hicode_agent.py）"
            ),
            session_semantics="hicode serve 常驻会话优先，不可达时 CLI 一次性调用兜底",
            sandbox_level="workspace_lock",
            status="candidate",
        )
    )
    harness_registry.register(
        HarnessSpec(
            harness_id="dsh",
            version="unversioned",
            capabilities=["domain_specific_execution"],
            session_semantics="一次性 subprocess 调用（server/project_ask.py::_dsh_exec）",
            sandbox_level="subprocess",
            status="candidate",
        )
    )
    harness_registry.register(
        HarnessSpec(
            harness_id="builtin",
            version="unversioned",
            capabilities=["direct_tool_execution"],
            session_semantics="MasterAgent 自身工具调用循环，无独立子会话",
            sandbox_level="none",
            status="candidate",
        )
    )
