"""P1-B Verification OS: Verification Engine.

Orchestrates the full verification flow:
1. Generate VerificationSpec before GoalRun starts
2. Execute via ControlHarness (doctor/launch/drive/snapshot/trace/cleanup)
3. Collect EvidenceBundle bound to task/goal/HEAD/spec
4. Independent Verifier evaluates immutable spec + evidence -> PASS/FAIL/BLOCKED
5. HEAD invalidation: verdict becomes stale when HEAD changes
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from runtime.execution.artifacts import ArtifactStore
from runtime.execution.durable import DurableExecutionRepository
from runtime.execution.side_effects import SideEffectLedger
from runtime.verification.models import (
    AcceptanceCriterion,
    CleanupAction,
    ControlHarnessRef,
    EvidenceBundle,
    EvidenceItem,
    FeatureAction,
    FeatureEntryPoint,
    FeatureFailureState,
    FeatureMap,
    FeatureMapEntry,
    FeatureSuccessEvidence,
    HarnessOperation,
    NegativeCase,
    RequiredEvidence,
    UserJourney,
    VerificationSpec,
    VerificationVerdict,
    get_current_head_sha,
)


class VerificationEngine:
    """Main verification engine integrating with GoalRun and execution runtime."""

    def __init__(
        self,
        project_root: str | Path,
        durable_repo: DurableExecutionRepository | None = None,
    ):
        self.project_root = Path(project_root).expanduser().resolve()
        self.durable_repo = durable_repo
        self._harness: ControlHarnessRef | None = None
        self._feature_map: FeatureMap | None = None

    @property
    def harness(self) -> ControlHarnessRef:
        if self._harness is None:
            self._harness = ControlHarnessRef.discover_or_create(str(self.project_root))
        return self._harness

    @property
    def feature_map(self) -> FeatureMap:
        if self._feature_map is None:
            self._feature_map = self._load_or_create_feature_map()
        return self._feature_map

    def _load_or_create_feature_map(self) -> FeatureMap:
        """Load existing feature map or create from project analysis."""
        map_path = self.project_root / ".veya" / "feature_map.json"
        if map_path.exists():
            try:
                data = json.loads(map_path.read_text())
                return FeatureMap(**data)
            except Exception:
                pass
        return self._create_default_feature_map()

    def _create_default_feature_map(self) -> FeatureMap:
        """Create a default feature map for Veya Web/CLI."""
        return FeatureMap(
            project_root=str(self.project_root),
            features=[
                FeatureMapEntry(
                    feature="product_canonical",
                    entry_points=[
                        FeatureEntryPoint(
                            id="product_task",
                            type="api",
                            path="server/routes/product.py",
                            description="Execute one product task through GoalRun",
                        ),
                    ],
                    preconditions=["GoalRun and canonical action protocol are available"],
                    actions=[
                        FeatureAction(
                            id="canonical_action",
                            name="Execute a canonical action",
                            description="MasterAgent selects an action executed by GoalRun",
                        ),
                    ],
                    success_evidence=[
                        FeatureSuccessEvidence(
                            id="canonical_action_observed",
                            kind="assertion",
                            description="A GoalRun-owned canonical action result is observed",
                            validation="action result is bound to the GoalRun",
                        ),
                    ],
                ),
                FeatureMapEntry(
                    feature="veya_cli",
                    entry_points=[
                        FeatureEntryPoint(
                            id="cli_main",
                            type="cli",
                            path="veya/cli/main.py",
                            description="Main CLI entry point",
                        ),
                    ],
                    preconditions=["Python 3.11+", "dependencies installed"],
                    actions=[
                        FeatureAction(
                            id="run_command",
                            name="Run CLI command",
                            description="Execute a Veya CLI command",
                            input_schema={"command": "string", "args": "array"},
                            output_schema={"exit_code": "integer", "stdout": "string", "stderr": "string"},
                        ),
                    ],
                    success_evidence=[
                        FeatureSuccessEvidence(
                            id="cli_exit_zero",
                            kind="assertion",
                            description="CLI exits with code 0",
                            validation="exit_code == 0",
                        ),
                        FeatureSuccessEvidence(
                            id="cli_output_expected",
                            kind="log_pattern",
                            description="Expected output appears in stdout",
                            validation="expected_string in stdout",
                        ),
                    ],
                    failure_states=[
                        FeatureFailureState(
                            id="cli_invalid_command",
                            trigger="Invalid command or args",
                            expected_behavior="Clear error message, non-zero exit",
                            evidence_required=["stderr", "exit_code"],
                        ),
                    ],
                    cleanup=["Clean up temp files"],
                    related_features=["veya_web", "veya_serve"],
                ),
                FeatureMapEntry(
                    feature="veya_serve",
                    entry_points=[
                        FeatureEntryPoint(
                            id="serve_api",
                            type="api",
                            path="server/app.py",
                            description="FastAPI server entry",
                        ),
                    ],
                    preconditions=["Server dependencies", "Port available"],
                    actions=[
                        FeatureAction(
                            id="start_server",
                            name="Start HTTP server",
                            description="Launch the Veya API server",
                            input_schema={"port": "integer", "host": "string"},
                            output_schema={"pid": "integer", "url": "string"},
                        ),
                    ],
                    success_evidence=[
                        FeatureSuccessEvidence(
                            id="server_health",
                            kind="assertion",
                            description="/health endpoint returns 200",
                            validation="GET /health -> 200 OK",
                        ),
                    ],
                    failure_states=[
                        FeatureFailureState(
                            id="port_conflict",
                            trigger="Port already in use",
                            expected_behavior="Clear error, exit non-zero",
                            evidence_required=["stderr", "exit_code"],
                        ),
                    ],
                    cleanup=["Stop server process", "Release port"],
                    related_features=["veya_cli", "veya_web"],
                ),
                FeatureMapEntry(
                    feature="veya_web",
                    entry_points=[
                        FeatureEntryPoint(
                            id="web_frontend",
                            type="ui",
                            path="apps/web",
                            description="SvelteKit frontend",
                        ),
                    ],
                    preconditions=["Node.js", "pnpm", "built assets"],
                    actions=[
                        FeatureAction(
                            id="build_web",
                            name="Build web frontend",
                            description="Build SvelteKit app for production",
                            input_schema={},
                            output_schema={"build_dir": "string"},
                        ),
                    ],
                    success_evidence=[
                        FeatureSuccessEvidence(
                            id="build_succeeds",
                            kind="artifact",
                            description="Build completes without errors",
                            validation="build/index.html exists",
                        ),
                    ],
                    failure_states=[
                        FeatureFailureState(
                            id="build_failure",
                            trigger="TypeScript/type errors",
                            expected_behavior="Clear error output, non-zero exit",
                            evidence_required=["stderr", "exit_code"],
                        ),
                    ],
                    cleanup=["Clean build artifacts"],
                    related_features=["veya_serve", "veya_cli"],
                ),
            ],
        )

    async def generate_verification_spec(
        self,
        task_id: str,
        goal_run_id: str,
        head_sha: str,
        *,
        feature_name: str = "veya_cli",
    ) -> VerificationSpec:
        """Generate VerificationSpec BEFORE GoalRun starts.

        This is called by the GoalRun creation flow to freeze acceptance criteria.
        """
        feature = self.feature_map.get_feature(feature_name)
        if not feature:
            raise ValueError(f"Feature '{feature_name}' not found in feature map")

        # Build acceptance criteria from feature map
        acceptance_criteria = []
        for _i, evidence in enumerate(feature.success_evidence):
            acceptance_criteria.append(AcceptanceCriterion(
                id=f"ac-{evidence.id}",
                description=f"Success evidence: {evidence.description}",
                required=True,
                kind="functional",
            ))

        # Add negative cases as acceptance criteria
        for _i, neg in enumerate(feature.failure_states):
            acceptance_criteria.append(AcceptanceCriterion(
                id=f"ac-negative-{neg.id}",
                description=f"Negative case: {neg.expected_behavior}",
                required=True,
                kind="negative",
            ))

        # User journeys from feature actions
        user_journeys = []
        for action in feature.actions:
            user_journeys.append(UserJourney(
                id=f"journey-{action.id}",
                name=action.name,
                steps=[f"Call {action.name}"],
                preconditions=feature.preconditions,
                expected_outcome=f"Action {action.name} completes successfully",
                negative_cases=[n.expected_behavior for n in feature.failure_states],
            ))

        # Required evidence
        required_evidence = [
            RequiredEvidence(
                id=f"ev-{ev.id}",
                kind="artifact" if ev.kind == "artifact" else "log",
                description=ev.description,
                source=f"harness.{ev.kind}",
                required=True,
            )
            for ev in feature.success_evidence
        ]

        # Negative cases
        negative_cases = [
            NegativeCase(
                id=neg.id,
                description=neg.expected_behavior,
                trigger=neg.trigger,
                expected_behavior=neg.expected_behavior,
                severity="high" if "critical" in neg.expected_behavior.lower() else "medium",
            )
            for neg in feature.failure_states
        ]

        # Cleanup actions
        cleanup_actions = [
            CleanupAction(
                id=f"cleanup-{i}",
                description=action,
                trigger="always",
                action=action,
                required=True,
            )
            for i, action in enumerate(feature.cleanup)
        ]

        spec = VerificationSpec.create_for_task(
            task_id=task_id,
            goal_run_id=goal_run_id,
            head_sha=head_sha,
            acceptance_criteria=acceptance_criteria,
            user_journeys=user_journeys,
            required_evidence=required_evidence,
            negative_cases=negative_cases,
            cleanup_actions=cleanup_actions,
            feature_map_id=self.feature_map.map_id,
        )

        # Persist spec
        spec_path = self.project_root / ".veya" / "runs" / task_id / "verification_spec.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(spec.to_dict(), indent=2))

        return spec

    async def run_harness_operation(
        self,
        operation: HarnessOperation,
        task_id: str,
        goal_run_id: str,
        *,
        args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a single harness operation via ControlHarnessRef."""
        impl = self.harness.get_operation(operation)
        if not impl:
            return {"ok": False, "error": f"Operation {operation} not found in harness"}

        if not impl.command:
            return {"ok": False, "error": f"Operation {operation} not implemented"}

        cmd = impl.command
        if args:
            cmd += " " + " ".join(args)

        # Execute via SideEffectLedger if available
        if self.durable_repo:
            ledger = SideEffectLedger(self.durable_repo)
            try:
                result = await ledger.execute(
                    goal_run_id=goal_run_id,
                    work_item_id=f"harness-{operation}",
                    operation_key=f"harness:{operation}",
                    operation_type="harness_operation",
                    target_ref=operation,
                    request={"command": cmd, "args": args or []},
                    provider=lambda: self._run_command(cmd),
                    capability="idempotency_key",
                )
                return {"ok": True, "result": result}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        # Direct execution fallback
        return await self._run_command(cmd)

    async def _run_command(self, cmd: str) -> dict[str, Any]:
        """Run a shell command and return structured result."""
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=self.project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return {
                "exit_code": proc.returncode,
                "stdout": stdout.decode() if stdout else "",
                "stderr": stderr.decode() if stderr else "",
                "command": cmd,
            }
        except Exception as e:
            return {"exit_code": -1, "stdout": "", "stderr": str(e), "command": cmd}

    async def collect_evidence_bundle(
        self,
        task_id: str,
        goal_run_id: str,
        head_sha: str,
        spec: VerificationSpec,
        *,
        artifact_store: ArtifactStore | None = None,
    ) -> EvidenceBundle:
        """Collect EvidenceBundle bound to task/goal/HEAD/spec."""
        bundle = EvidenceBundle(
            task_id=task_id,
            goal_run_id=goal_run_id,
            head_sha=head_sha,
            verification_spec_version=spec.version,
            verification_spec_hash=spec.spec_hash,
        )

        # Collect from artifact store
        if artifact_store:
            manifest = artifact_store.manifest()
            for artifact in manifest.artifacts:
                bundle = bundle.add_evidence(EvidenceItem(
                    id=f"artifact-{artifact.path}",
                    kind="artifact",
                    source="artifact_store",
                    content=f"Artifact: {artifact.path} (sha256={artifact.sha256})",
                    producer="artifact_store",
                    metadata={"path": artifact.path, "kind": artifact.kind, "sha256": artifact.sha256},
                ))

        # Collect from harness operations (snapshots, traces)
        for op_name in ["snapshot", "trace"]:
            result = await self.run_harness_operation(op_name, task_id, goal_run_id)  # type: ignore[arg-type]
            bundle = bundle.add_evidence(EvidenceItem(
                id=f"harness-{op_name}",
                kind="snapshot" if op_name == "snapshot" else "trace",
                source=f"harness.{op_name}",
                content=json.dumps(result),
                producer="control_harness",
                metadata={"operation": op_name, "result": result},
            ))

        # Collect from GoalRun durable repo if available
        if self.durable_repo:
            # Query events for this goal_run
            pass  # Would query the durable repo for events

        return bundle

    async def run_independent_verifier(
        self,
        spec: VerificationSpec,
        bundle: EvidenceBundle,
        head_sha: str,
    ) -> VerificationVerdict:
        """Independent Verifier: reads immutable spec + evidence -> PASS/FAIL/BLOCKED.

        Worker self-reported success CANNOT override this verdict.
        """
        # Verify spec and bundle integrity
        if not spec.verify_immutable():
            return VerificationVerdict.create_blocked(
                task_id=spec.task_id,
                goal_run_id=spec.goal_run_id,
                head_sha=head_sha,
                spec_hash=spec.spec_hash,
                bundle_hash=bundle.bundle_hash,
                reason="VerificationSpec has been modified (immutability violated)",
            )

        if not bundle.verify_integrity():
            return VerificationVerdict.create_blocked(
                task_id=spec.task_id,
                goal_run_id=spec.goal_run_id,
                head_sha=head_sha,
                spec_hash=spec.spec_hash,
                bundle_hash=bundle.bundle_hash,
                reason="EvidenceBundle integrity check failed",
            )

        if not bundle.is_bound_to(spec.task_id, spec.goal_run_id, head_sha, spec.spec_hash):
            return VerificationVerdict.create_blocked(
                task_id=spec.task_id,
                goal_run_id=spec.goal_run_id,
                head_sha=head_sha,
                spec_hash=spec.spec_hash,
                bundle_hash=bundle.bundle_hash,
                reason="EvidenceBundle not bound to correct task/goal/HEAD/spec",
            )

        # Evaluate acceptance criteria
        criteria_results = {}
        missing_evidence = []

        for criterion in spec.acceptance_criteria:
            # Check if evidence exists for this criterion
            found = False
            for evidence in bundle.evidence:
                if criterion.id in evidence.id or criterion.id in evidence.metadata.get("criterion_id", ""):
                    found = True
                    break
            criteria_results[criterion.id] = found
            if criterion.required and not found:
                missing_evidence.append(criterion.id)

        # Evaluate negative cases
        negative_case_results = {}
        for neg_case in spec.negative_cases:
            # Check if negative case evidence was collected
            found = False
            for evidence in bundle.evidence:
                if neg_case.id in evidence.id:
                    found = True
                    break
            negative_case_results[neg_case.id] = found

        # Check cleanup
        cleanup_verified = True
        for cleanup in spec.cleanup_actions:
            found = False
            for evidence in bundle.evidence:
                if cleanup.id in evidence.id:
                    found = True
                    break
            if cleanup.required and not found:
                cleanup_verified = False

        # Determine outcome
        all_required_passed = all(
            criteria_results.get(c.id, False)
            for c in spec.acceptance_criteria
            if c.required
        )
        all_negative_handled = all(negative_case_results.values())

        if all_required_passed and all_negative_handled and cleanup_verified:
            return VerificationVerdict.create_pass(
                task_id=spec.task_id,
                goal_run_id=spec.goal_run_id,
                head_sha=head_sha,
                spec_hash=spec.spec_hash,
                bundle_hash=bundle.bundle_hash,
                criteria_results=criteria_results,
                negative_case_results=negative_case_results,
                cleanup_verified=cleanup_verified,
                summary="All acceptance criteria satisfied, negative cases handled, cleanup verified",
            )
        elif not all_required_passed:
            return VerificationVerdict.create_fail(
                task_id=spec.task_id,
                goal_run_id=spec.goal_run_id,
                head_sha=head_sha,
                spec_hash=spec.spec_hash,
                bundle_hash=bundle.bundle_hash,
                criteria_results=criteria_results,
                missing_evidence=missing_evidence,
                negative_case_results=negative_case_results,
                cleanup_verified=cleanup_verified,
                summary=f"Missing required evidence: {missing_evidence}",
            )
        else:
            return VerificationVerdict.create_fail(
                task_id=spec.task_id,
                goal_run_id=spec.goal_run_id,
                head_sha=head_sha,
                spec_hash=spec.spec_hash,
                bundle_hash=bundle.bundle_hash,
                criteria_results=criteria_results,
                missing_evidence=missing_evidence,
                negative_case_results=negative_case_results,
                cleanup_verified=cleanup_verified,
                summary="Negative cases not handled or cleanup incomplete",
            )

    async def verify_and_invalidate(
        self,
        task_id: str,
        goal_run_id: str,
        feature_name: str = "veya_cli",
    ) -> tuple[VerificationVerdict, bool]:
        """Run full verification and check HEAD invalidation.

        Returns (verdict, is_stale) where is_stale means HEAD changed during verification.
        """
        head_sha = get_current_head_sha(self.project_root)

        # 1. Generate spec (or load existing)
        spec_path = self.project_root / ".veya" / "runs" / task_id / "verification_spec.json"
        if spec_path.exists():
            spec = VerificationSpec.load(spec_path)
        else:
            spec = await self.generate_verification_spec(task_id, goal_run_id, head_sha, feature_name=feature_name)

        # 2. Run harness operations to collect evidence
        artifact_store = ArtifactStore(self.project_root, task_id)

        # Run doctor first
        await self.run_harness_operation("doctor", task_id, goal_run_id)

        # Launch product
        await self.run_harness_operation("launch", task_id, goal_run_id)

        # Drive user path
        await self.run_harness_operation("drive", task_id, goal_run_id)

        # Snapshot and trace
        await self.run_harness_operation("snapshot", task_id, goal_run_id)
        await self.run_harness_operation("trace", task_id, goal_run_id)

        # Cleanup
        await self.run_harness_operation("cleanup", task_id, goal_run_id)

        # 3. Collect evidence bundle
        bundle = await self.collect_evidence_bundle(task_id, goal_run_id, head_sha, spec, artifact_store=artifact_store)
        # 4. Independent verification
        verdict = await self.run_independent_verifier(spec, bundle, head_sha)

        # 5. Check HEAD invalidation
        current_head = get_current_head_sha(self.project_root)
        is_stale = verdict.is_stale(current_head)

        return verdict, is_stale


async def create_verification_engine(
    project_root: str | Path,
    durable_repo: DurableExecutionRepository | None = None,
) -> VerificationEngine:
    """Factory to create a VerificationEngine."""
    return VerificationEngine(project_root, durable_repo)


__all__ = ["VerificationEngine", "create_verification_engine"]
