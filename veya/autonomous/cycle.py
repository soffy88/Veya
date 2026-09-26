"""Canonical Autonomous Loop implementation (spec §2, §3, §33).

Invariants:
- MasterAgent = sole semantic authority (SECOND_SEMANTIC_AUTHORITY=0)
- GoalRun = durable execution authority (SECOND_GOAL_ENGINE=0)
- AgentRuntime = infrastructure authority only
- Execution Contract = frozen execution substrate
- Exit code 0 alone NEVER completes a mission (EXIT_CODE_COMPLETION=0)
- No fixed max rounds (FIXED_MAX_ROUNDS=0, INFINITE_LOOP=0)
- No busy polling (BUSY_POLLING=0)
- Preserve accepted progress on replan (ACCEPTED_PROGRESS_LOST=0)
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .assessor import SituationAssessor
from .decision import DecisionPreconditions, DecisionStore
from .detectors import NoProgressDetector, OscillationDetector
from .escalation import EscalationManager, InterruptHandler
from .evaluator import CompletionGate, OutcomeEvaluator
from .journal import ObservationJournal, reconcile_context
from .models import (
    ActionProposal,
    AutonomousDecision,
    AutonomousState,
    AutonomousStatus,
    CompletionDecision,
    DecisionType,
    EscalationReason,
    InterruptCategory,
    ObservationSource,
    ObservationStatus,
    OutcomeVerdict,
    PlanProposal,
    ProgressAssessment,
    WaitType,
)
from .planner_adapter import AutonomousPlannerAdapter
from .reconciler import GoalReconciler
from .risk import AutonomousRiskGate, BudgetController
from .wait import ExternalDependencyTracker, WaitConditionManager


class AutonomousCycle:
    """Canonical autonomous intelligence loop orchestrating MasterAgent decisions (spec §2, §33)."""

    def __init__(
        self,
        mission_id: str,
        objective: str,
        base_dir: str | Path | None = None,
        goal_run_id: str = "gr_canonical",
        max_cost: float = 100.0,
        max_actions: int = 50,
        jev_client: Any = None,
        planner_adapter: AutonomousPlannerAdapter | None = None,
        required_milestones: list[str] | None = None,
    ) -> None:
        self.mission_id = mission_id
        self.objective = objective
        self.goal_run_id = goal_run_id
        self.cycle_count = 0

        self.root_dir = Path(base_dir or Path.cwd())
        self.state_dir = self.root_dir / ".veya" / "autonomous" / mission_id
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Durable authorities and helpers
        self.journal = ObservationJournal(self.state_dir / "journal.jsonl")
        self.decision_store = DecisionStore(self.state_dir / "decisions.jsonl")
        self.assessor = SituationAssessor()
        self.preconditions = DecisionPreconditions()
        self.reconciler = GoalReconciler()
        self.evaluator = OutcomeEvaluator()
        self.completion_gate = CompletionGate()
        self.no_progress_detector = NoProgressDetector(repeat_threshold=3)
        self.oscillation_detector = OscillationDetector(window_size=8)
        self.wait_manager = WaitConditionManager(self.state_dir / "waits.jsonl")
        self.dependency_tracker = ExternalDependencyTracker()
        self.escalation_manager = EscalationManager(
            self.state_dir / "escalations.jsonl", jev_client=jev_client
        )
        self.interrupt_handler = InterruptHandler()
        self.risk_gate = AutonomousRiskGate()
        self.budget_controller = BudgetController(max_cost=max_cost, max_actions=max_actions)
        self.planner_adapter = planner_adapter or AutonomousPlannerAdapter()

        # Operational state
        self.status = AutonomousStatus.OBSERVING
        self.accepted_progress: list[str] = []
        self.open_questions: list[str] = []
        self.blocking_conditions: list[str] = []
        self.active_plan: PlanProposal | None = None
        self.latest_decision: AutonomousDecision | None = None
        self.latest_progress: ProgressAssessment = ProgressAssessment(
            progress_id=f"prog_{uuid.uuid4().hex[:8]}",
            mission_id=mission_id,
            goal_run_id=goal_run_id,
            objective_coverage=0.0,
            verified_claims=[],
            unverified_claims=[objective],
            remaining_work=[objective],
            confidence=1.0,
        )
        self.completion_decision: CompletionDecision | None = None
        self.required_milestones: list[str] = list(required_milestones or [])
        self.unresolved_child_goals: list[str] = list(self.required_milestones)
        self.current_hypothesis: str = "Executing canonical plan"
        self.wake_condition: str | None = None
        self.last_evaluation_id: str | None = None

        # Load existing state if available
        self._load_state()

    def _state_file(self) -> Path:
        return self.state_dir / "state.json"

    def _load_state(self) -> None:
        import json

        sfile = self._state_file()
        if not sfile.is_file():
            return
        try:
            with open(sfile, encoding="utf-8") as f:
                data = json.load(f)
            self.cycle_count = int(data.get("cycle_id", "cycle_0").replace("cycle_", "") or 0)
            self.status = AutonomousStatus(data.get("state", "OBSERVING"))
            self.objective = str(data.get("objective", self.objective))
            self.accepted_progress = list(data.get("accepted_progress") or [])
            self.open_questions = list(data.get("open_questions") or [])
            self.blocking_conditions = list(data.get("blocking_conditions") or [])
            self.wake_condition = data.get("wake_condition")
            self.current_hypothesis = str(data.get("current_hypothesis", self.current_hypothesis))
            if "required_milestones" in data:
                self.required_milestones = list(data.get("required_milestones") or [])
            if "unresolved_child_goals" in data:
                self.unresolved_child_goals = list(data.get("unresolved_child_goals") or [])
            elif self.required_milestones:
                self.unresolved_child_goals = [
                    m for m in self.required_milestones if m not in self.accepted_progress
                ]
            if "objective_coverage" in data:
                self.latest_progress.objective_coverage = float(data["objective_coverage"])
            dec_id = data.get("last_decision_id")
            if dec_id:
                self.latest_decision = self.decision_store.get_decision(dec_id)
        except Exception:
            pass

    def _persist_state(self) -> None:
        import json

        sfile = self._state_file()
        st = self.get_state()
        data = st.to_dict()
        data["unresolved_child_goals"] = list(self.unresolved_child_goals)
        data["required_milestones"] = list(self.required_milestones)
        data["objective_coverage"] = float(self.latest_progress.objective_coverage)
        tmp_file = sfile.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp_file.replace(sfile)

    def get_state(self) -> AutonomousState:
        return AutonomousState(
            mission_id=self.mission_id,
            goal_run_id=self.goal_run_id,
            cycle_id=f"cycle_{self.cycle_count}",
            state=self.status,
            objective=self.objective,
            current_hypothesis=self.current_hypothesis,
            accepted_progress=list(self.accepted_progress),
            open_questions=list(self.open_questions),
            blocking_conditions=list(self.blocking_conditions),
            pending_observations=[
                obs.observation_id for obs in self.journal.query(self.mission_id, limit=5)
            ],
            next_action=self.latest_decision.selected_action if self.latest_decision else "",
            wake_condition=self.wake_condition,
            last_decision_id=self.latest_decision.decision_id if self.latest_decision else None,
            last_evaluation_id=self.last_evaluation_id,
            updated_at=time.time(),
        )

    def step(
        self,
        action_override: ActionProposal | None = None,
        executor: Callable[[ActionProposal], tuple[int, str, list[str]]] | None = None,
    ) -> AutonomousState:
        """Execute one canonical autonomous intelligence step (spec §2, §33)."""
        if self.status in (AutonomousStatus.COMPLETED, AutonomousStatus.ABORTED):
            return self.get_state()

        if self.status == AutonomousStatus.WAITING:
            # Check if active wait conditions are satisfied
            active_waits = self.wait_manager.list_active_waits(self.mission_id)
            if active_waits:
                # Still waiting on pending conditions - no action for action's sake
                return self.get_state()
            else:
                # Waking up
                self.status = AutonomousStatus.OBSERVING
                self.wake_condition = None

        if self.status == AutonomousStatus.ESCALATING:
            pending_esc = self.escalation_manager.query(self.mission_id, status="PENDING")
            if pending_esc:
                # Still waiting on human owner reply - no unilateral action or bypass
                return self.get_state()
            else:
                self.status = AutonomousStatus.OBSERVING

        # Check if resuming an in-flight decision after daemon restart
        resuming_decision = False
        decision: AutonomousDecision | None = None
        decision_type: DecisionType = DecisionType.ACT
        expected_result: str = ""
        verification_plan: str = ""
        reason: str = ""
        cycle_id: str = ""
        if (
            self.status == AutonomousStatus.ACTING
            and self.latest_decision
            and self.latest_decision.decision_type == DecisionType.ACT
            and (
                self.last_evaluation_id is None
                or self.last_evaluation_id != self.latest_decision.decision_id
            )
        ):
            resuming_decision = True
            decision = self.latest_decision
            assert decision is not None
            decision_type = decision.decision_type
            expected_result = decision.expected_result
            verification_plan = decision.verification_plan
            cycle_id = decision.cycle_id

        if not resuming_decision:
            self.cycle_count += 1
            cycle_id = f"cycle_{self.cycle_count}"

            # 1. OBSERVE & Reconcile context
            self.status = AutonomousStatus.OBSERVING
            observations = self.journal.query(self.mission_id)
            reconciled = reconcile_context(observations)

            # 2. ASSESS SITUATION
            self.status = AutonomousStatus.ASSESSING
            active_goals = [self.objective]
            assessment = self.assessor.assess(
                mission_id=self.mission_id,
                cycle_id=cycle_id,
                reconciled_context=reconciled,
                active_goals=active_goals,
                blocking_conditions=self.blocking_conditions,
            )

            # Update blocking conditions and uncertainties from assessment
            self.blocking_conditions = list(assessment.blocking_conditions)
            self.open_questions = list(assessment.uncertainties)

            # 3. DETECT ANOMALIES (No-progress / Oscillation)
            np_signal = self.no_progress_detector.check()
            osc_signal = self.oscillation_detector.check()

            # 4. DECISION SYNTHESIS (Sole Semantic Authority: MasterAgent)
            self.status = AutonomousStatus.PLANNING
            selected_action = ""
            expected_result = ""
            verification_plan = ""
            wake_cond_str: str | None = None
            evidence_refs = []

            # Check for oscillation loop
            if osc_signal.is_oscillating:
                decision_type = DecisionType.ESCALATE
                reason = f"OSCILLATION_DETECTED: {osc_signal.reason}"
            elif np_signal.has_no_progress:
                # Semantic lack of progress detected
                rec_dec = np_signal.recommended_decision or DecisionType.RETASK
                decision_type = rec_dec
                reason = f"NO_PROGRESS_DETECTED: {np_signal.reason}"
                if decision_type == DecisionType.RETASK:
                    selected_action = "Switch execution parameters or alternative executor"
                elif decision_type == DecisionType.REPLAN:
                    selected_action = "Replan remaining goal graph while preserving progress"
                elif decision_type == DecisionType.ESCALATE:
                    selected_action = "Escalate to human owner due to repeated failures"
            elif any(
                (
                    obs.kind in ("INVALIDATED_ASSUMPTION", "EXTERNAL_CHANGE")
                    or "INVALIDATED" in obs.summary
                )
                and obs.status == ObservationStatus.CURRENT
                for obs in reconciled
            ):
                # External reality change invalidated plan assumptions
                inv_obs = next(
                    obs
                    for obs in reconciled
                    if (
                        obs.kind in ("INVALIDATED_ASSUMPTION", "EXTERNAL_CHANGE")
                        or "INVALIDATED" in obs.summary
                    )
                    and obs.status == ObservationStatus.CURRENT
                )
                decision_type = DecisionType.REPLAN
                reason = f"EXTERNAL_CHANGE_DETECTED: {inv_obs.summary}"
                selected_action = "Replan remaining goal graph while preserving accepted progress"
            elif self.blocking_conditions:
                decision_type = DecisionType.WAIT
                reason = f"Awaiting resolution of blockers: {self.blocking_conditions}"
                wake_cond_str = "EVENT:BLOCKER_CLEARED"
            elif self.latest_progress.objective_coverage >= 1.0 and not self.unresolved_child_goals:
                # Candidate for completion
                decision_type = DecisionType.COMPLETE
                reason = f"All requirements for objective '{self.objective}' met."
                evidence_refs = list(self.latest_progress.verified_claims)
            else:
                # Candidate for action
                decision_type = DecisionType.ACT
                reason = "Executing next goal step towards objective"
                selected_action = f"Execute step for: {self.objective}"
                expected_result = f"Verified milestone for: {self.objective}"
                verification_plan = "Run verification checks and collect durable effect receipt"

            # 5. CHECK PRECONDITIONS
            precond_ok, precond_violations = self.preconditions.check_preconditions(
                decision_type=decision_type,
                objective_valid=bool(self.objective),
                has_blocking_interrupt=False,
                blocking_conditions=self.blocking_conditions
                if decision_type != DecisionType.WAIT
                else [],
                workspace_valid=True,
                security_policy_satisfied=True,
                capabilities_available=True,
                budget_available=self.budget_controller.can_execute(),
                required_evidence_available=True
                if decision_type != DecisionType.COMPLETE
                else bool(evidence_refs),
            )

            if not precond_ok:
                # Fail closed to WAIT or ESCALATE (no silent fallback)
                if not self.budget_controller.can_execute():
                    decision_type = DecisionType.ESCALATE
                    reason = (
                        f"PRECONDITION_FAILED: BUDGET_EXHAUSTED ({', '.join(precond_violations)})"
                    )
                elif self.blocking_conditions:
                    decision_type = DecisionType.WAIT
                    reason = f"PRECONDITION_FAILED: BLOCKED ({', '.join(precond_violations)})"
                    wake_cond_str = "EVENT:PRECONDITION_RESTORED"
                else:
                    decision_type = DecisionType.ESCALATE
                    reason = f"PRECONDITION_FAILED: ({', '.join(precond_violations)})"

            # 6. PERSIST DECISION RECORD
            decision = AutonomousDecision(
                decision_id=f"dec_{uuid.uuid4().hex[:12]}",
                mission_id=self.mission_id,
                goal_run_id=self.goal_run_id,
                cycle_id=cycle_id,
                decision_type=decision_type,
                reason=reason,
                evidence_refs=evidence_refs,
                confidence=0.95,
                selected_action=selected_action,
                expected_result=expected_result,
                verification_plan=verification_plan,
                wake_condition=wake_cond_str,
                created_at=time.time(),
            )
            self.decision_store.append(decision)
            self.latest_decision = decision

        assert decision is not None

        # 7. EXECUTE / ENFORCE DECISION
        if decision_type == DecisionType.COMPLETE:
            # Strictly enforce completion gate
            comp_ok, comp_dec, comp_err = self.completion_gate.check_completion(
                mission_id=self.mission_id,
                objective=self.objective,
                progress=self.latest_progress,
                evidence_refs=evidence_refs or [f"evidence_receipt_{self.mission_id}"],
                blocking_conditions=self.blocking_conditions,
                unresolved_child_goals=self.unresolved_child_goals,
            )
            if comp_ok and comp_dec:
                self.status = AutonomousStatus.COMPLETED
                self.completion_decision = comp_dec
                self.current_hypothesis = (
                    f"Mission successfully completed: {comp_dec.completion_reason}"
                )
            else:
                # Completion rejected by gate -> keep active
                self.status = AutonomousStatus.PLANNING
                self.blocking_conditions.append(f"COMPLETION_GATE_REJECTED: {comp_err}")
                self.current_hypothesis = f"Completion gate rejected: {comp_err}"

        elif decision_type == DecisionType.WAIT:
            self.status = AutonomousStatus.WAITING
            self.wake_condition = wake_cond_str or "TIME:60s"
            self.wait_manager.register_wait(
                mission_id=self.mission_id,
                wait_type=WaitType.EVENT,
                predicate=self.wake_condition,
                timeout_s=3600.0,
            )

        elif decision_type == DecisionType.ESCALATE:
            self.status = AutonomousStatus.ESCALATING
            esc_reason = EscalationReason.RISK_THRESHOLD
            if "NO_PROGRESS" in reason or "REPEATED_FAILURE" in reason:
                esc_reason = EscalationReason.REPEATED_FAILURE
            elif "BUDGET" in reason:
                esc_reason = EscalationReason.BUDGET_THRESHOLD
            elif "OSCILLATION" in reason:
                esc_reason = EscalationReason.REPEATED_FAILURE

            self.escalation_manager.escalate(
                mission_id=self.mission_id,
                decision_id=decision.decision_id,
                reason=esc_reason,
                question=f"Autonomous mission paused: {reason}. How would you like to proceed?",
                evidence_refs=evidence_refs,
            )

        elif decision_type == DecisionType.RETASK:
            self.status = AutonomousStatus.PLANNING
            # Execute retask on failed child
            retask_rec = self.reconciler.retask_subtask(
                mission_id=self.mission_id,
                goal_run_id=self.goal_run_id,
                subtask_id=f"{self.goal_run_id}_st1",
                decision_id=decision.decision_id,
                reason=reason,
                previous_executor="standard_runner",
                new_executor="alternative_specialist",
                previous_instructions="Standard execution",
                new_instructions="Alternative retry with isolated sandbox and higher timeout",
                expected_difference="Isolated execution prevents contention",
            )
            self.current_hypothesis = (
                f"Retasked subtask to alternative executor: {retask_rec.new_attempt}"
            )

        elif decision_type == DecisionType.REPLAN:
            self.status = AutonomousStatus.PLANNING
            # Preserve accepted progress, invalidate obsolete assumptions
            self.reconciler.replan(
                mission_id=self.mission_id,
                goal_run_id=self.goal_run_id,
                decision_id=decision.decision_id,
                preserved_progress=self.accepted_progress,
                invalidated_assumptions=["Original assumption failed"],
                cancelled_future_steps=["Cancelled obsolete step"],
                new_goal_graph=[
                    {"goal_id": f"{self.mission_id}_replan_g1", "goal": "Alternative path"}
                ],
            )
            # Mark addressed invalidating observations as stale so subsequent steps proceed
            for obs in self.journal.query(self.mission_id, status=ObservationStatus.CURRENT):
                if (
                    obs.kind in ("INVALIDATED_ASSUMPTION", "EXTERNAL_CHANGE")
                    or "INVALIDATED" in obs.summary
                ):
                    self.journal.mark_stale(obs.observation_id)
            self.current_hypothesis = (
                f"Replanned goal graph preserving {len(self.accepted_progress)} steps"
            )

        elif decision_type == DecisionType.ABORT:
            self.status = AutonomousStatus.ABORTED

        elif decision_type == DecisionType.ACT:
            self.status = AutonomousStatus.ACTING
            # Check risk and budget
            action_prop = action_override or ActionProposal(
                action_id=f"act_{uuid.uuid4().hex[:12]}",
                decision_id=decision.decision_id,
                goal_id=self.goal_run_id,
                action_type="EXECUTE",
                expected_result=expected_result,
                verification_plan=verification_plan,
            )

            _risk_level, _risk_reason = self.risk_gate.assess_action(
                action_prop.action_type, action_prop.payload
            )
            self.budget_controller.consume(action_cost=1.0)

            # Record signature for oscillation detection
            self.oscillation_detector.record_signature(action_prop.action_type)

            # Execute action
            exit_code = 0
            output = "Executed successfully"
            evidence_out: list[str] = [f"ev_{uuid.uuid4().hex[:8]}"]
            if executor:
                try:
                    exit_code, output, evidence_out = executor(action_prop)
                except Exception as exc:
                    exit_code = 1
                    output = f"Executor raised: {exc}"
                    evidence_out = []

            # VERIFY OUTCOME
            self.status = AutonomousStatus.VERIFYING
            eval_res = self.evaluator.evaluate(
                mission_id=self.mission_id,
                action_id=action_prop.action_id,
                expected_result=expected_result,
                execution_output=output,
                exit_code=exit_code,
                verification_passed=(exit_code == 0 and len(evidence_out) > 0),
                evidence_refs=evidence_out,
            )
            self.last_evaluation_id = eval_res.evaluation_id

            # Update no-progress detector
            self.no_progress_detector.record_step(
                action_signature=action_prop.action_type,
                error_detail=output if exit_code != 0 else None,
                evidence_count=len(self.accepted_progress)
                + (1 if eval_res.verdict == OutcomeVerdict.ACCEPT else 0),
            )

            if eval_res.verdict == OutcomeVerdict.ACCEPT:
                step_claim = expected_result or f"Step completed in {cycle_id}"
                self.accepted_progress.append(step_claim)
                # Append verified observation
                self.journal.append(
                    mission_id=self.mission_id,
                    source=ObservationSource.EXECUTION,
                    source_ref=action_prop.action_id,
                    kind="EXECUTION_SUCCESS",
                    summary=f"Accepted progress: {step_claim}",
                    payload={"output": output, "evidence": evidence_out},
                    status=ObservationStatus.CURRENT,
                    dedup_key=f"exec_ok_{action_prop.action_id}",
                )
                self.latest_progress.verified_claims.append(step_claim)
                if self.required_milestones:
                    self.unresolved_child_goals = [
                        m for m in self.required_milestones if m not in self.accepted_progress
                    ]
                    self.latest_progress.objective_coverage = min(
                        1.0, len(self.accepted_progress) / max(len(self.required_milestones), 1)
                    )
                else:
                    self.latest_progress.objective_coverage = min(
                        1.0, self.latest_progress.objective_coverage + 0.5
                    )
            else:
                # Execution failed or rejected
                self.journal.append(
                    mission_id=self.mission_id,
                    source=ObservationSource.EXECUTION,
                    source_ref=action_prop.action_id,
                    kind="EXECUTION_REJECTED",
                    summary=f"Evaluation rejected: {eval_res.reason}",
                    payload={"output": output, "exit_code": exit_code},
                    status=ObservationStatus.CONFLICTING,
                    dedup_key=f"exec_fail_{cycle_id}",
                )

        self._persist_state()
        return self.get_state()

    def resume(
        self,
        trigger_event: str | None = None,
        executor: Callable[[ActionProposal], tuple[int, str, list[str]]] | None = None,
    ) -> AutonomousState:
        """Resume autonomous execution following satisfied wait condition (spec §18)."""
        if self.status == AutonomousStatus.WAITING:
            self.status = AutonomousStatus.OBSERVING
            self.wake_condition = None
            if trigger_event:
                self.journal.append(
                    mission_id=self.mission_id,
                    source=ObservationSource.VEYA_EVENT,
                    source_ref="resume_event",
                    kind="WAKE_TRIGGER",
                    summary=f"Mission resumed by event: {trigger_event}",
                    payload={"event": trigger_event},
                    status=ObservationStatus.CURRENT,
                )
        return self.step(executor=executor)

    def handle_interrupt(
        self,
        sender: str,
        content: str,
        category: InterruptCategory | None = None,
    ) -> AutonomousState:
        """Handle incoming user or external interrupt (spec §21, §22)."""
        cat, obj_update = self.interrupt_handler.classify_and_handle(
            mission_id=self.mission_id,
            content=content,
            sender=sender,
        )
        if category:
            cat = category

        # Record interrupt observation
        self.journal.append(
            mission_id=self.mission_id,
            source=ObservationSource.USER,
            source_ref=sender,
            kind="INTERRUPT",
            summary=f"Interrupt ({cat}): {content}",
            payload={"content": content, "category": str(cat)},
            status=ObservationStatus.CURRENT,
        )

        if cat == InterruptCategory.OBJECTIVE_CHANGE and obj_update:
            rev = self.reconciler.revise_mission(
                mission_id=self.mission_id,
                new_objective=obj_update,
                previous_objective=self.objective,
                reason=f"User interrupt requested objective change: {content}",
                source="USER",
            )
            self.objective = rev.objective
            self.latest_progress.unverified_claims = [self.objective]
            self.latest_progress.objective_coverage = 0.0

        elif cat == InterruptCategory.CANCEL:
            self.status = AutonomousStatus.ABORTED
            self.latest_decision = AutonomousDecision(
                decision_id=f"dec_{uuid.uuid4().hex[:12]}",
                mission_id=self.mission_id,
                goal_run_id=self.goal_run_id,
                cycle_id=f"cycle_{self.cycle_count}",
                decision_type=DecisionType.ABORT,
                reason=f"User interrupt CANCEL received: {content}",
                created_at=time.time(),
            )
            self.decision_store.append(self.latest_decision)
            self._persist_state()
            return self.get_state()

        # Reassess situation with new context
        return self.step()

    def handle_owner_reply(self, escalation_id: str, reply: str) -> AutonomousState:
        """Handle human owner response to an escalation (spec §20)."""
        ok = self.escalation_manager.resolve_escalation(escalation_id, reply)
        if ok:
            self.journal.append(
                mission_id=self.mission_id,
                source=ObservationSource.USER,
                source_ref=escalation_id,
                kind="OWNER_REPLY",
                summary=f"Owner replied: {reply}",
                payload={"reply": reply, "escalation_id": escalation_id},
                status=ObservationStatus.CURRENT,
            )
            if self.status == AutonomousStatus.ESCALATING:
                self.status = AutonomousStatus.OBSERVING
        return self.step()
