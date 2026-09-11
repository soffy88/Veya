"""P1-B Verification OS: Core data contracts for independent verification.

This module defines the 5 core objects:
- VerificationSpec: Immutable acceptance contract generated before GoalRun starts
- FeatureMap: User-behavior-organized feature specification
- ControlHarnessRef: Project-local control harness reference
- EvidenceBundle: Immutable evidence collection bound to task/goal/HEAD
- VerificationVerdict: Independent PASS/FAIL/BLOCKED verdict with HEAD invalidation
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

VerificationStatus = Literal["pending", "running", "passed", "failed", "blocked"]
VerdictOutcome = Literal["PASS", "FAIL", "BLOCKED"]
HarnessOperation = Literal["doctor", "launch", "drive", "snapshot", "trace", "cleanup"]


@dataclass(frozen=True)
class AcceptanceCriterion:
    """Single acceptance criterion in a VerificationSpec."""
    id: str
    description: str
    required: bool = True
    kind: Literal["functional", "negative", "cleanup", "performance", "security"] = "functional"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UserJourney:
    """A user journey in a VerificationSpec."""
    id: str
    name: str
    steps: list[str]
    preconditions: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    negative_cases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RequiredEvidence:
    """Required evidence specification."""
    id: str
    kind: Literal["artifact", "log", "trace", "snapshot", "metric", "test_result"]
    description: str
    source: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NegativeCase:
    """Negative test case specification."""
    id: str
    description: str
    trigger: str
    expected_behavior: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CleanupAction:
    """Cleanup action specification."""
    id: str
    description: str
    trigger: Literal["always", "on_failure", "on_success"]
    action: str
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VerificationSpec:
    """Immutable verification specification generated BEFORE GoalRun starts.

    Once execution begins, the worker MUST NOT silently lower acceptance criteria.
    This spec is frozen at creation and bound to the GoalRun.
    """
    version: str = "1.0"
    spec_id: str = ""
    task_id: str = ""
    goal_run_id: str = ""
    head_sha: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Core acceptance
    acceptance_criteria: list[AcceptanceCriterion] = field(default_factory=list)
    user_journeys: list[UserJourney] = field(default_factory=list)

    # Evidence requirements
    required_evidence: list[RequiredEvidence] = field(default_factory=list)

    # Negative testing
    negative_cases: list[NegativeCase] = field(default_factory=list)

    # Cleanup
    cleanup_actions: list[CleanupAction] = field(default_factory=list)

    # Feature map reference
    feature_map_id: str = ""

    # Metadata
    spec_hash: str = ""
    frozen: bool = True

    def __post_init__(self):
        if not self.spec_id:
            object.__setattr__(self, 'spec_id', f"vspec-{hashlib.sha256(self.task_id.encode()).hexdigest()[:12]}")
        if not self.spec_hash:
            # Hash everything except the hash itself
            data = {k: v for k, v in asdict(self).items() if k != 'spec_hash'}
            object.__setattr__(self, 'spec_hash', hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def create_for_task(
        cls,
        task_id: str,
        goal_run_id: str,
        head_sha: str,
        *,
        acceptance_criteria: list[AcceptanceCriterion] | None = None,
        user_journeys: list[UserJourney] | None = None,
        required_evidence: list[RequiredEvidence] | None = None,
        negative_cases: list[NegativeCase] | None = None,
        cleanup_actions: list[CleanupAction] | None = None,
        feature_map_id: str = "",
    ) -> VerificationSpec:
        """Factory to create a VerificationSpec for a task."""
        return cls(
            task_id=task_id,
            goal_run_id=goal_run_id,
            head_sha=head_sha,
            acceptance_criteria=acceptance_criteria or [],
            user_journeys=user_journeys or [],
            required_evidence=required_evidence or [],
            negative_cases=negative_cases or [],
            cleanup_actions=cleanup_actions or [],
            feature_map_id=feature_map_id,
        )

    def verify_immutable(self) -> bool:
        """Verify this spec hasn't been modified since creation."""
        data = {k: v for k, v in asdict(self).items() if k != 'spec_hash'}
        current_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]
        return current_hash == self.spec_hash

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationSpec:
        """Load VerificationSpec from dict with proper nested object deserialization."""
        # Convert nested objects
        acceptance_criteria = [
            AcceptanceCriterion(**c) if isinstance(c, dict) else c
            for c in data.get("acceptance_criteria", [])
        ]
        user_journeys = [
            UserJourney(**j) if isinstance(j, dict) else j
            for j in data.get("user_journeys", [])
        ]
        required_evidence = [
            RequiredEvidence(**e) if isinstance(e, dict) else e
            for e in data.get("required_evidence", [])
        ]
        negative_cases = [
            NegativeCase(**n) if isinstance(n, dict) else n
            for n in data.get("negative_cases", [])
        ]
        cleanup_actions = [
            CleanupAction(**c) if isinstance(c, dict) else c
            for c in data.get("cleanup_actions", [])
        ]

        # Create new dict with converted objects
        spec_data = {**data}
        spec_data["acceptance_criteria"] = acceptance_criteria
        spec_data["user_journeys"] = user_journeys
        spec_data["required_evidence"] = required_evidence
        spec_data["negative_cases"] = negative_cases
        spec_data["cleanup_actions"] = cleanup_actions

        return cls(**spec_data)

    @classmethod
    def load(cls, path: str | Path) -> VerificationSpec:
        """Load VerificationSpec from JSON file."""
        import json
        path = Path(path)
        data = json.loads(path.read_text())
        return cls.from_dict(data)


@dataclass(frozen=True)
class FeatureEntryPoint:
    """Entry point for a feature."""
    id: str
    type: Literal["cli", "api", "ui", "webhook", "schedule"]
    path: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeatureAction:
    """Action within a feature."""
    id: str
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeatureSuccessEvidence:
    """Success evidence for a feature."""
    id: str
    kind: Literal["assertion", "artifact", "log_pattern", "state_change", "metric"]
    description: str
    validation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeatureFailureState:
    """Failure/empty state for a feature."""
    id: str
    trigger: str
    expected_behavior: str
    evidence_required: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeatureMapEntry:
    """Single feature entry in a FeatureMap."""
    feature: str
    entry_points: list[FeatureEntryPoint] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    actions: list[FeatureAction] = field(default_factory=list)
    success_evidence: list[FeatureSuccessEvidence] = field(default_factory=list)
    failure_states: list[FeatureFailureState] = field(default_factory=list)
    cleanup: list[str] = field(default_factory=list)
    related_features: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FeatureMap:
    """Feature map organized by USER BEHAVIOR (not code structure).

    Each entry describes a feature from the user's perspective:
    - feature: What the user calls it
    - entry_points: How the user accesses it
    - preconditions: What must be true before
    - actions: What the user does
    - success_evidence: How we know it worked
    - failure_states: What happens when it breaks
    - cleanup: What to clean up
    - related_features: Cross-references
    """
    version: str = "1.0"
    map_id: str = ""
    project_root: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    features: list[FeatureMapEntry] = field(default_factory=list)
    map_hash: str = ""

    def __post_init__(self):
        if not self.map_id:
            object.__setattr__(self, 'map_id', f"fmap-{hashlib.sha256(self.project_root.encode()).hexdigest()[:12]}")
        if not self.map_hash:
            data = {k: v for k, v in asdict(self).items() if k != 'map_hash'}
            object.__setattr__(self, 'map_hash', hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def get_feature(self, feature_name: str) -> FeatureMapEntry | None:
        for f in self.features:
            if f.feature == feature_name:
                return f
        return None

    def verify_immutable(self) -> bool:
        data = {k: v for k, v in asdict(self).items() if k != 'map_hash'}
        current_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]
        return current_hash == self.map_hash


@dataclass(frozen=True)
class ControlHarnessImplementation:
    """Implementation of a single harness operation."""
    operation: HarnessOperation
    command: str
    description: str
    timeout_s: int = 60
    required: bool = True
    generates_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ControlHarnessRef:
    """Reference to a project-local control harness.

    The harness provides these operations:
    - doctor: Health check
    - launch: Start the product
    - drive: Execute user path
    - snapshot: Capture state
    - trace: Collect traces
    - cleanup: Teardown

    If the harness doesn't exist, it can be generated incrementally
    but MUST be self-testable.
    """
    version: str = "1.0"
    harness_id: str = ""
    project_root: str = ""
    harness_path: str = ""
    exists: bool = False
    self_testable: bool = False
    operations: list[ControlHarnessImplementation] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    harness_hash: str = ""

    def __post_init__(self):
        if not self.harness_id:
            object.__setattr__(self, 'harness_id', f"harness-{hashlib.sha256(self.project_root.encode()).hexdigest()[:12]}")
        if not self.harness_hash:
            data = {k: v for k, v in asdict(self).items() if k != 'harness_hash'}
            object.__setattr__(self, 'harness_hash', hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def get_operation(self, op: HarnessOperation) -> ControlHarnessImplementation | None:
        for o in self.operations:
            if o.operation == op:
                return o
        return None

    def verify_immutable(self) -> bool:
        data = {k: v for k, v in asdict(self).items() if k != 'harness_hash'}
        current_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]
        return current_hash == self.harness_hash

    @classmethod
    def discover_or_create(cls, project_root: str) -> ControlHarnessRef:
        """Discover existing harness or create a minimal stub.

        Priority order:
        1. Project-local: tools/harness/control_harness.py (versionable)
        2. Legacy: .veya/control_harness.py (runtime, ignored by git)
        """
        project_root_path = Path(project_root)
        harness_paths = [
            project_root_path / "tools" / "harness" / "control_harness.py",
            project_root_path / ".veya" / "control_harness.py",  # legacy fallback
        ]
        harness_path = None
        for path in harness_paths:
            if path.exists():
                harness_path = path
                break

        exists = harness_path is not None
        if not exists:
            # Default to versionable location for new projects
            harness_path = project_root_path / "tools" / "harness" / "control_harness.py"

        # At this point harness_path is guaranteed to be a Path
        assert harness_path is not None
        operations = []
        if exists:
            # Try to load and introspect
            operations = cls._introspect_harness(harness_path)
        else:
            # Generate minimal stub
            operations = cls._generate_stub_operations()

        self_testable = exists and cls._check_self_testable(harness_path)

        return cls(
            project_root=project_root,
            harness_path=str(harness_path),
            exists=exists,
            self_testable=self_testable,
            operations=operations,
        )

    @staticmethod
    def _introspect_harness(harness_path: Path) -> list[ControlHarnessImplementation]:
        """Introspect a harness file for operations."""
        operations = []
        content = harness_path.read_text()

        for op_name in ["doctor", "launch", "drive", "snapshot", "trace", "cleanup"]:
            # Simple check: does the harness have this function?
            if f"def {op_name}" in content or f"async def {op_name}" in content:
                operations.append(ControlHarnessImplementation(
                    operation=op_name,  # type: ignore
                    command=f"python {harness_path} {op_name}",
                    description=f"Run {op_name} operation",
                    required=True,
                ))
            else:
                operations.append(ControlHarnessImplementation(
                    operation=op_name,  # type: ignore
                    command="",
                    description=f"MISSING: {op_name} operation not implemented",
                    required=True,
                ))

        return operations

    @staticmethod
    def _generate_stub_operations() -> list[ControlHarnessImplementation]:
        """Generate stub operations for a new harness."""
        return [
            ControlHarnessImplementation(
                operation="doctor",
                command="echo 'doctor: not implemented' && exit 1",
                description="Health check (STUB)",
                required=True,
            ),
            ControlHarnessImplementation(
                operation="launch",
                command="echo 'launch: not implemented' && exit 1",
                description="Launch product (STUB)",
                required=True,
            ),
            ControlHarnessImplementation(
                operation="drive",
                command="echo 'drive: not implemented' && exit 1",
                description="Execute user path (STUB)",
                required=True,
            ),
            ControlHarnessImplementation(
                operation="snapshot",
                command="echo 'snapshot: not implemented' && exit 1",
                description="Capture state (STUB)",
                required=True,
            ),
            ControlHarnessImplementation(
                operation="trace",
                command="echo 'trace: not implemented' && exit 1",
                description="Collect traces (STUB)",
                required=True,
            ),
            ControlHarnessImplementation(
                operation="cleanup",
                command="echo 'cleanup: not implemented' && exit 1",
                description="Teardown (STUB)",
                required=True,
            ),
        ]

    @staticmethod
    def _check_self_testable(harness_path: Path) -> bool:
        """Check if harness has self-tests."""
        test_path = harness_path.parent / f"test_{harness_path.name}"
        if test_path.exists():
            return True
        # Check if harness has a --self-test flag or similar
        content = harness_path.read_text()
        return "self_test" in content or "self-test" in content


@dataclass(frozen=True)
class EvidenceItem:
    """Single piece of evidence in an EvidenceBundle."""
    id: str
    kind: Literal["artifact", "log", "trace", "snapshot", "metric", "test_result", "observation"]
    source: str
    content: str
    producer: str
    confidence: float | None = None
    sha256: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def compute_hash(self) -> str:
        data = f"{self.kind}:{self.source}:{self.content}:{self.producer}:{self.timestamp}"
        return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


@dataclass(frozen=True)
class EvidenceBundle:
    """Immutable evidence collection bound to task/goal/HEAD/spec version.

    Must bind:
    - task_id
    - goal_run_id
    - HEAD SHA
    - verification_spec version
    """
    version: str = "1.0"
    bundle_id: str = ""
    task_id: str = ""
    goal_run_id: str = ""
    head_sha: str = ""
    verification_spec_version: str = ""
    verification_spec_hash: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    evidence: list[EvidenceItem] = field(default_factory=list)
    bundle_hash: str = ""

    def __post_init__(self):
        if not self.bundle_id:
            object.__setattr__(self, 'bundle_id', f"evbundle-{hashlib.sha256(f'{self.task_id}:{self.goal_run_id}'.encode()).hexdigest()[:12]}")
        if not self.bundle_hash:
            data = {k: v for k, v in asdict(self).items() if k != 'bundle_hash'}
            object.__setattr__(self, 'bundle_hash', hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def add_evidence(self, item: EvidenceItem) -> EvidenceBundle:
        """Return a new bundle with added evidence (immutable)."""
        new_evidence = [*list(self.evidence), item]
        return EvidenceBundle(
            version=self.version,
            bundle_id=self.bundle_id,
            task_id=self.task_id,
            goal_run_id=self.goal_run_id,
            head_sha=self.head_sha,
            verification_spec_version=self.verification_spec_version,
            verification_spec_hash=self.verification_spec_hash,
            created_at=self.created_at,
            evidence=new_evidence,
        )

    def get_evidence_by_kind(self, kind: str) -> list[EvidenceItem]:
        return [e for e in self.evidence if e.kind == kind]

    def verify_integrity(self) -> bool:
        """Verify bundle integrity and binding."""
        # Check hash
        data = {k: v for k, v in asdict(self).items() if k != 'bundle_hash'}
        current_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]
        if current_hash != self.bundle_hash:
            return False
        # Check all evidence items have valid hashes
        for item in self.evidence:
            if item.sha256 and item.sha256 != item.compute_hash():
                return False
        return True

    def is_bound_to(self, task_id: str, goal_run_id: str, head_sha: str, spec_hash: str) -> bool:
        """Check if bundle is bound to the given context."""
        return (
            self.task_id == task_id and
            self.goal_run_id == goal_run_id and
            self.head_sha == head_sha and
            self.verification_spec_hash == spec_hash
        )


@dataclass(frozen=True)
class VerificationVerdict:
    """Independent verification verdict.

    Worker result -> Verifier reads immutable VerificationSpec + EvidenceBundle -> PASS/FAIL/BLOCKED
    Worker self-reported success CANNOT override verifier verdict.
    Verdict becomes stale when HEAD changes.
    """
    version: str = "1.0"
    verdict_id: str = ""
    task_id: str = ""
    goal_run_id: str = ""
    head_sha_at_verdict: str = ""
    verification_spec_hash: str = ""
    evidence_bundle_hash: str = ""
    outcome: VerdictOutcome = "BLOCKED"
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    criteria_results: dict[str, bool] = field(default_factory=dict)
    missing_evidence: list[str] = field(default_factory=list)
    negative_case_results: dict[str, bool] = field(default_factory=dict)
    cleanup_verified: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    verified_at: str = ""
    verdict_hash: str = ""

    def __post_init__(self):
        if not self.verdict_id:
            object.__setattr__(self, 'verdict_id', f"verdict-{hashlib.sha256(f'{self.task_id}:{self.goal_run_id}:{self.head_sha_at_verdict}'.encode()).hexdigest()[:12]}")
        if not self.verified_at:
            object.__setattr__(self, 'verified_at', datetime.now(UTC).isoformat())
        if not self.verdict_hash:
            data = {k: v for k, v in asdict(self).items() if k != 'verdict_hash'}
            object.__setattr__(self, 'verdict_hash', hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_stale(self, current_head_sha: str) -> bool:
        """Check if verdict is stale due to HEAD change."""
        return self.head_sha_at_verdict != current_head_sha

    def verify_integrity(self) -> bool:
        """Verify verdict integrity."""
        data = {k: v for k, v in asdict(self).items() if k != 'verdict_hash'}
        current_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]
        return current_hash == self.verdict_hash

    @classmethod
    def create_pass(
        cls,
        task_id: str,
        goal_run_id: str,
        head_sha: str,
        spec_hash: str,
        bundle_hash: str,
        criteria_results: dict[str, bool],
        negative_case_results: dict[str, bool],
        cleanup_verified: bool,
        summary: str = "",
    ) -> VerificationVerdict:
        return cls(
            task_id=task_id,
            goal_run_id=goal_run_id,
            head_sha_at_verdict=head_sha,
            verification_spec_hash=spec_hash,
            evidence_bundle_hash=bundle_hash,
            outcome="PASS",
            summary=summary or "All criteria passed",
            criteria_results=criteria_results,
            negative_case_results=negative_case_results,
            cleanup_verified=cleanup_verified,
        )

    @classmethod
    def create_fail(
        cls,
        task_id: str,
        goal_run_id: str,
        head_sha: str,
        spec_hash: str,
        bundle_hash: str,
        criteria_results: dict[str, bool],
        missing_evidence: list[str],
        negative_case_results: dict[str, bool],
        cleanup_verified: bool,
        summary: str = "",
    ) -> VerificationVerdict:
        return cls(
            task_id=task_id,
            goal_run_id=goal_run_id,
            head_sha_at_verdict=head_sha,
            verification_spec_hash=spec_hash,
            evidence_bundle_hash=bundle_hash,
            outcome="FAIL",
            summary=summary or "Verification failed",
            criteria_results=criteria_results,
            missing_evidence=missing_evidence,
            negative_case_results=negative_case_results,
            cleanup_verified=cleanup_verified,
        )

    @classmethod
    def create_blocked(
        cls,
        task_id: str,
        goal_run_id: str,
        head_sha: str,
        spec_hash: str,
        bundle_hash: str,
        reason: str,
    ) -> VerificationVerdict:
        return cls(
            task_id=task_id,
            goal_run_id=goal_run_id,
            head_sha_at_verdict=head_sha,
            verification_spec_hash=spec_hash,
            evidence_bundle_hash=bundle_hash,
            outcome="BLOCKED",
            summary=reason,
        )


def get_current_head_sha(project_root: str | Path) -> str:
    """Get current HEAD SHA from git."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


__all__ = [
    "AcceptanceCriterion",
    "CleanupAction",
    "ControlHarnessImplementation",
    "ControlHarnessRef",
    "EvidenceBundle",
    "EvidenceItem",
    "FeatureAction",
    "FeatureEntryPoint",
    "FeatureFailureState",
    "FeatureMap",
    "FeatureMapEntry",
    "FeatureSuccessEvidence",
    "HarnessOperation",
    "NegativeCase",
    "RequiredEvidence",
    "UserJourney",
    "VerdictOutcome",
    "VerificationSpec",
    "VerificationStatus",
    "VerificationVerdict",
    "get_current_head_sha",
]
