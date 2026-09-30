"""Layered policy resolver OVER PermissionEngine (P0_02).

Projection, not authority: seven policy layers (Managed -> Security ->
Workspace -> Goal -> Agent -> Skill -> Tool) are evaluated in fixed priority
order and then converged with the PermissionEngine verdict, which is always
evaluated last and can never be bypassed.  Intersection/convergence semantics:

* severity order DENY > APPROVAL_REQUIRED > ALLOW; the most severe outcome
  wins, so a higher-priority DENY can never be overridden from below;
* effective capabilities are the INTERSECTION of every layer grant; if the
  engine-derived capability is intersected out, the result is DENY;
* approvals only ever satisfy an APPROVAL_REQUIRED outcome via the existing
  server-issued single-use ApprovalStore flow; a client ``approved=true``
  boolean without an ``approval_id`` is forgery and resolves to DENY;
* an approval can lift APPROVAL_REQUIRED but never DENY (engine or layer).

Evaluation is a single bounded pass over the fixed layer order
(PRODUCT_MAX_ROUNDS_TERMINATION=0: no loops, no retries, no recursion).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .approval import ApprovalStore, get_approval_store
from .models import RemoteErrorCode, RiskClass
from .permission_engine import (
    Decision,
    OperationContext,
    PermissionDecision,
    PermissionEngine,
    ReasonCode,
    parse_command_context,
)

LAYER_ORDER = (
    "managed",
    "security",
    "workspace",
    "goal",
    "agent",
    "skill",
    "tool",
    "permission-engine",
)


class LayerOutcome(StrEnum):
    ALLOW = "ALLOW"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENY = "DENY"
    ABSTAIN = "ABSTAIN"


_SEVERITY = {
    LayerOutcome.ABSTAIN: 0,
    LayerOutcome.ALLOW: 1,
    LayerOutcome.APPROVAL_REQUIRED: 2,
    LayerOutcome.DENY: 3,
}

_SECRET_DISCOVERY = re.compile(
    r"(?:\.ssh|\.gnupg|browser.{0,20}(?:profile|cookie)|keyring|credential.store)",
    re.I,
)

_ENGINE_CAPABILITY = {
    ReasonCode.ALLOW_READ_ONLY: "workspace.read",
    ReasonCode.ALLOW_PROJECT_MUTATION: "workspace.file_write",
    ReasonCode.ALLOW_PROJECT_GIT: "git.normal",
    ReasonCode.ALLOW_USER_RUNTIME: "service.user",
    ReasonCode.ALLOW_NETWORK: "network.normal",
    ReasonCode.ALLOW_WORKER: "worker.dispatch",
    ReasonCode.ALLOW_TRUSTED_ADMIN: "host.admin",
    ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE: "privileged.git_destructive",
    ReasonCode.APPROVAL_HOST_PRIVILEGE: "privileged.host",
    ReasonCode.APPROVAL_HOST_DESTRUCTIVE: "privileged.destructive",
    ReasonCode.APPROVAL_SECURITY_BOUNDARY: "security.boundary",
    ReasonCode.APPROVAL_UNKNOWN_HIGH_IMPACT: "privileged.unknown",
    ReasonCode.DENY_SCOPE_ESCAPE: "denied.scope_escape",
    ReasonCode.DENY_PATH_TRAVERSAL: "denied.path_traversal",
    ReasonCode.DENY_AUTHORITY_VIOLATION: "denied.authority",
    ReasonCode.DENY_INVALID_APPROVAL: "denied.invalid_approval",
}


@dataclass(frozen=True)
class PolicyRequest:
    """One resolution input. ``client_approved`` is untrusted client input."""

    actor: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    workspace: str = ""
    cwd: str = ""
    goal_id: str = ""
    agent_id: str = ""
    skill_id: str = ""
    client_approved: bool = False
    approval_id: str = ""

    @property
    def principal(self) -> str:
        return self.actor


@dataclass(frozen=True)
class LayerVerdict:
    layer: str
    outcome: LayerOutcome
    rule: str
    reason: str
    granted: frozenset[str] | None = None
    detail: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reason: str
    matched_layers: tuple[str, ...] = ()
    effective_capabilities: frozenset[str] = frozenset()
    approval_required: bool = False
    approval: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, dict[str, str]] = field(default_factory=dict)
    trace: tuple[str, ...] = ()
    engine_decision: str = ""
    engine_reason: str = ""
    constraining_layer: str | None = None
    approval_id: str = ""


def _norm_operation(request: PolicyRequest, context: OperationContext) -> str:
    if context.command:
        return " ".join(context.command)
    raw_path = request.args.get("path", "")
    return f"{request.tool} {raw_path}".strip() if raw_path else request.tool


def _engine_capability(decision: PermissionDecision, context: OperationContext) -> str:
    cap = _ENGINE_CAPABILITY.get(decision.reason, "permission.operation")
    words = tuple(context.command or ())
    if words:
        executable = Path(words[0]).name.lower()
        if executable == "sudo":
            return "privileged.sudo"
        if executable == "su":
            return "privileged.root_shell"
    return cap


class PolicyLayer:
    """Base projection layer. Never enforces; only votes."""

    name = "base"

    def evaluate(self, request: PolicyRequest, context: OperationContext) -> LayerVerdict:
        raise NotImplementedError


class ManagedPolicy(PolicyLayer):
    """Operator-managed fleet policy: highest-priority static gates."""

    name = "managed"

    def __init__(
        self,
        *,
        denied_tools: frozenset[str] | set[str] = frozenset(),
        approval_tools: frozenset[str] | set[str] = frozenset(),
        allowed_capabilities: frozenset[str] | set[str] | None = None,
    ) -> None:
        self.denied_tools = frozenset(denied_tools)
        self.approval_tools = frozenset(approval_tools)
        self.allowed_capabilities = (
            None if allowed_capabilities is None else frozenset(allowed_capabilities)
        )

    def evaluate(self, request: PolicyRequest, context: OperationContext) -> LayerVerdict:
        if request.tool in self.denied_tools:
            return LayerVerdict(
                self.name,
                LayerOutcome.DENY,
                f"managed.deny:{request.tool}",
                "tool denied by managed policy",
                self.allowed_capabilities,
            )
        if request.tool in self.approval_tools:
            return LayerVerdict(
                self.name,
                LayerOutcome.APPROVAL_REQUIRED,
                f"managed.approval:{request.tool}",
                "tool gated by managed policy",
                self.allowed_capabilities,
            )
        if self.allowed_capabilities is not None:
            return LayerVerdict(
                self.name,
                LayerOutcome.ALLOW,
                "managed.cap-grant",
                "managed capability grant",
                self.allowed_capabilities,
            )
        return LayerVerdict(
            self.name, LayerOutcome.ABSTAIN, "managed.abstain", "no managed rule matched"
        )


class SecurityPolicy(PolicyLayer):
    """Credential / secret-boundary policy derived from the request itself."""

    name = "security"

    def evaluate(self, request: PolicyRequest, context: OperationContext) -> LayerVerdict:
        raw_command = str(request.args.get("command", ""))
        if raw_command and _SECRET_DISCOVERY.search(raw_command):
            return LayerVerdict(
                self.name,
                LayerOutcome.DENY,
                "security.secret-discovery",
                "credential discovery is permanently denied",
            )
        home = Path.home().resolve()
        for target in context.target_paths:
            try:
                canon = target.expanduser().resolve(strict=False)
            except OSError:
                continue
            if any(
                canon == protected or protected in canon.parents
                for protected in (home / ".ssh", home / ".gnupg", home / ".aws")
            ):
                return LayerVerdict(
                    self.name,
                    LayerOutcome.DENY,
                    "security.credential-boundary",
                    "credential store access denied",
                )
        if any(Path(str(p)).name == ".env" for p in context.target_paths) and (
            context.filesystem_effect != "read" or "secret" in raw_command.lower()
        ):
            return LayerVerdict(
                self.name,
                LayerOutcome.APPROVAL_REQUIRED,
                "security.secret-boundary",
                "secret boundary requires approval",
            )
        return LayerVerdict(
            self.name, LayerOutcome.ABSTAIN, "security.abstain", "no secret boundary hit"
        )


class WorkspacePolicy(PolicyLayer):
    """Workspace containment projection: escape is DENY, inside is ABSTAIN."""

    name = "workspace"

    def evaluate(self, request: PolicyRequest, context: OperationContext) -> LayerVerdict:
        if not context.target_paths or context.workspace_root is None:
            return LayerVerdict(
                self.name,
                LayerOutcome.ABSTAIN,
                "workspace.abstain",
                "no resolvable target inside request",
            )
        try:
            root = context.workspace_root.expanduser().resolve(strict=False)
        except OSError:
            return LayerVerdict(
                self.name,
                LayerOutcome.ABSTAIN,
                "workspace.abstain",
                "workspace root unresolvable",
            )
        for target in context.target_paths:
            try:
                canon = target.expanduser().resolve(strict=False)
            except OSError:
                return LayerVerdict(
                    self.name,
                    LayerOutcome.DENY,
                    "workspace.unresolvable-target",
                    "unresolvable target denied",
                )
            if canon != root and root not in canon.parents:
                return LayerVerdict(
                    self.name,
                    LayerOutcome.DENY,
                    "workspace.escape",
                    f"target escapes workspace root: {target}",
                )
        return LayerVerdict(
            self.name,
            LayerOutcome.ABSTAIN,
            "workspace.contained",
            "targets contained in workspace",
        )


class _ScopedPolicy(PolicyLayer):
    """Goal / agent / skill policy keyed by the request's scope id."""

    scope_key = ""
    layer_name = "scoped"

    def __init__(self, bindings: dict[str, dict[str, Any]] | None = None) -> None:
        self.bindings = dict(bindings or {})

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.layer_name

    def evaluate(self, request: PolicyRequest, context: OperationContext) -> LayerVerdict:
        scope_id = getattr(request, self.scope_key, "")
        if not scope_id or scope_id not in self.bindings:
            return LayerVerdict(
                self.name,
                LayerOutcome.ABSTAIN,
                f"{self.name}.abstain",
                f"no {self.name} binding for {scope_id or '<unset>'}",
            )
        binding = self.bindings[scope_id]
        denied = set(binding.get("deny_tools", ()))
        approval = set(binding.get("approve_tools", ()))
        allow_caps = binding.get("allow_caps")
        granted = None if allow_caps is None else frozenset(allow_caps)
        if request.tool in denied:
            return LayerVerdict(
                self.name,
                LayerOutcome.DENY,
                f"{self.name}.deny:{request.tool}",
                f"tool denied by {self.name} {scope_id}",
                granted,
            )
        if request.tool in approval:
            return LayerVerdict(
                self.name,
                LayerOutcome.APPROVAL_REQUIRED,
                f"{self.name}.approval:{request.tool}",
                f"tool gated by {self.name} {scope_id}",
                granted,
            )
        if granted is not None:
            return LayerVerdict(
                self.name,
                LayerOutcome.ALLOW,
                f"{self.name}.cap-grant",
                f"{self.name} capability grant",
                granted,
            )
        return LayerVerdict(
            self.name,
            LayerOutcome.ALLOW,
            f"{self.name}.allow",
            f"tool allowed by {self.name} {scope_id}",
            granted,
        )


class GoalPolicy(_ScopedPolicy):
    scope_key = "goal_id"
    layer_name = "goal"


class AgentPolicy(_ScopedPolicy):
    scope_key = "agent_id"
    layer_name = "agent"


class SkillPolicy(_ScopedPolicy):
    scope_key = "skill_id"
    layer_name = "skill"


class ToolPolicy(PolicyLayer):
    """Per-tool default grants. Unknown tools ABSTAIN (engine decides)."""

    name = "tool"

    def __init__(self, grants: dict[str, frozenset[str] | set[str] | None] | None = None) -> None:
        defaults: dict[str, frozenset[str] | None] = {
            "file.read": frozenset({"workspace.read"}),
            "file.search": frozenset({"workspace.read"}),
            "artifact.read": frozenset({"workspace.read"}),
            "file.write": frozenset({"workspace.file_write"}),
            "file.patch": frozenset({"workspace.file_write"}),
            "artifact.write": frozenset({"workspace.file_write"}),
            # shell.exec spans read-only argv through privileged host
            # mutation; only the engine can name its capability, so the tool
            # layer allows without constraining (granted=None).
            "shell.exec": None,
        }
        if grants:
            defaults.update(
                {
                    key: (None if value is None else frozenset(value))
                    for key, value in grants.items()
                }
            )
        self.grants = defaults

    def evaluate(self, request: PolicyRequest, context: OperationContext) -> LayerVerdict:
        if request.tool not in self.grants:
            return LayerVerdict(
                self.name,
                LayerOutcome.ABSTAIN,
                "tool.abstain",
                f"no tool grant for {request.tool}",
            )
        granted = self.grants[request.tool]
        if granted is None:
            return LayerVerdict(
                self.name,
                LayerOutcome.ALLOW,
                f"tool.allow:{request.tool}",
                "tool allowed without capability constraint",
                None,
            )
        return LayerVerdict(
            self.name,
            LayerOutcome.ALLOW,
            f"tool.grant:{request.tool}",
            "tool capability grant",
            granted,
        )


def _build_context(request: PolicyRequest) -> OperationContext:
    workspace = request.workspace or request.cwd
    root = Path(workspace).expanduser() if workspace else Path(request.cwd or ".")
    current = Path(request.cwd).expanduser() if request.cwd else root
    raw_path = request.args.get("path")
    if request.tool == "shell.exec":
        context = parse_command_context(
            str(request.args.get("command", "")), cwd=current, workspace_root=root
        )
        return OperationContext(
            **{
                **context.__dict__,
                "actor": request.actor,
                "tool": request.tool,
                "session_id": None,
                "goal_run_id": request.goal_id or None,
            }
        )
    target_paths: tuple[Path, ...] = ()
    if raw_path:
        candidate = Path(str(raw_path))
        target_paths = ((candidate if candidate.is_absolute() else current / candidate),)
    effect = "write" if request.tool in {"file.write", "file.patch", "artifact.write"} else "read"
    return OperationContext(
        actor=request.actor,
        tool=request.tool,
        operation=request.tool,
        workspace_root=root,
        cwd=current,
        target_paths=target_paths,
        filesystem_effect=effect,
        goal_run_id=request.goal_id or None,
    )


class PolicyResolver:
    """Fixed-order layered resolution with PermissionEngine as final authority."""

    def __init__(
        self,
        *,
        managed: ManagedPolicy | None = None,
        security: SecurityPolicy | None = None,
        workspace: WorkspacePolicy | None = None,
        goal: GoalPolicy | None = None,
        agent: AgentPolicy | None = None,
        skill: SkillPolicy | None = None,
        tool: ToolPolicy | None = None,
        engine: PermissionEngine | None = None,
        approval_store: ApprovalStore | None = None,
    ) -> None:
        self.managed = managed or ManagedPolicy()
        self.security = security or SecurityPolicy()
        self.workspace = workspace or WorkspacePolicy()
        self.goal = goal or GoalPolicy()
        self.agent = agent or AgentPolicy()
        self.skill = skill or SkillPolicy()
        self.tool = tool or ToolPolicy()
        self.engine = engine or PermissionEngine()
        self.approval_store = approval_store or get_approval_store()

    @property
    def layers(self) -> tuple[PolicyLayer, ...]:
        return (
            self.managed,
            self.security,
            self.workspace,
            self.goal,
            self.agent,
            self.skill,
            self.tool,
        )

    def resolve(self, request: PolicyRequest, *, consume_approval: bool = True) -> PolicyDecision:
        context = _build_context(request)
        verdicts = [layer.evaluate(request, context) for layer in self.layers]
        engine_decision = self.engine.evaluate(context)
        engine_capability = _engine_capability(engine_decision, context)
        engine_outcome = {
            Decision.ALLOW: LayerOutcome.ALLOW,
            Decision.APPROVAL_REQUIRED: LayerOutcome.APPROVAL_REQUIRED,
            Decision.DENY: LayerOutcome.DENY,
        }[engine_decision.decision]
        verdicts.append(
            LayerVerdict(
                "permission-engine",
                engine_outcome,
                f"engine.{engine_decision.reason.value.lower()}",
                f"PermissionEngine {engine_decision.decision.value}: "
                f"{engine_decision.reason.value}",
                frozenset({engine_capability}),
            )
        )

        trace = tuple(f"layer={v.layer} outcome={v.outcome.value} rule={v.rule}" for v in verdicts)
        provenance = {
            v.layer: {"rule": v.rule, "outcome": v.outcome.value, "reason": v.reason}
            for v in verdicts
            if v.outcome != LayerOutcome.ABSTAIN
        }
        matched = [v.layer for v in verdicts if v.outcome != LayerOutcome.ABSTAIN]

        # Anti-forgery invariant: a client boolean is never a permission fact.
        if request.client_approved and not request.approval_id:
            return PolicyDecision(
                decision=Decision.DENY,
                reason=str(ReasonCode.DENY_INVALID_APPROVAL),
                matched_layers=tuple(matched),
                effective_capabilities=frozenset(),
                provenance=provenance,
                trace=(
                    *trace,
                    "guard=client-approval rejected: approved=true without "
                    "server-issued approval_id is forgery",
                ),
                engine_decision=engine_decision.decision.value,
                engine_reason=engine_decision.reason.value,
                constraining_layer="client-approval-guard",
            )

        grants = [v.granted for v in verdicts if v.granted is not None]
        effective: frozenset[str] = (
            frozenset.intersection(*grants) if grants else frozenset({engine_capability})
        )
        if not effective or engine_capability not in effective:
            constraining = next(
                (
                    v.layer
                    for v in verdicts
                    if v.granted is not None and engine_capability not in v.granted
                ),
                "permission-engine",
            )
            return PolicyDecision(
                decision=Decision.DENY,
                reason="CAPABILITY_INTERSECTED_OUT",
                matched_layers=tuple(matched),
                effective_capabilities=effective,
                provenance=provenance,
                trace=(
                    *trace,
                    f"guard=capability-intersection: engine capability "
                    f"{engine_capability!r} not in effective {sorted(effective)}",
                ),
                engine_decision=engine_decision.decision.value,
                engine_reason=engine_decision.reason.value,
                constraining_layer=constraining,
            )

        top = max(_SEVERITY[v.outcome] for v in verdicts)
        constraining = next(v.layer for v in verdicts if _SEVERITY[v.outcome] == top)
        final = {
            0: Decision.ALLOW,
            1: Decision.ALLOW,
            2: Decision.APPROVAL_REQUIRED,
            3: Decision.DENY,
        }[top]
        # DENY is terminal: no approval can lift a layer or engine denial.
        if final is Decision.DENY:
            blocker = next(v for v in verdicts if v.outcome == LayerOutcome.DENY)
            return PolicyDecision(
                decision=Decision.DENY,
                reason=blocker.reason,
                matched_layers=tuple(matched),
                effective_capabilities=effective,
                provenance=provenance,
                trace=trace,
                engine_decision=engine_decision.decision.value,
                engine_reason=engine_decision.reason.value,
                constraining_layer=blocker.layer,
            )

        normalized = _norm_operation(request, context)
        risk = (
            RiskClass.P3_CRITICAL_HOST
            if engine_decision.reason
            in {ReasonCode.APPROVAL_IRREVERSIBLE_REMOTE, ReasonCode.APPROVAL_SECURITY_BOUNDARY}
            else RiskClass.P2_ROOT_MUTATION
        )
        approval_spec: dict[str, Any] = {
            "capability_id": engine_capability,
            "normalized_operation": normalized,
            "cwd": str(request.cwd),
            "workspace": str(request.workspace or request.cwd),
            "risk_class": str(risk),
            "approval_id": "",
        }
        if final is Decision.APPROVAL_REQUIRED:
            approval_spec["approval_id"] = ""
            if not request.approval_id:
                return PolicyDecision(
                    decision=Decision.APPROVAL_REQUIRED,
                    reason="APPROVAL_REQUIRED",
                    matched_layers=tuple(matched),
                    effective_capabilities=effective,
                    approval_required=True,
                    approval=approval_spec,
                    provenance=provenance,
                    trace=trace,
                    engine_decision=engine_decision.decision.value,
                    engine_reason=engine_decision.reason.value,
                    constraining_layer=constraining,
                )
            ok, err_code, err_msg = (
                self.approval_store.verify_and_consume(
                    request.approval_id,
                    principal=request.principal,
                    capability_id=engine_capability,
                    normalized_operation=normalized,
                    cwd=str(request.cwd),
                    workspace=str(request.workspace or request.cwd),
                )
                if consume_approval
                else (True, None, None)
            )
            if not ok:
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=str(err_code),
                    matched_layers=tuple(matched),
                    effective_capabilities=effective,
                    approval_required=True,
                    approval={**approval_spec, "approval_id": request.approval_id},
                    provenance=provenance,
                    trace=(
                        *trace,
                        f"guard=approval-verify failed: {err_code} {err_msg}",
                    ),
                    engine_decision=engine_decision.decision.value,
                    engine_reason=engine_decision.reason.value,
                    constraining_layer="approval-store",
                )
            return PolicyDecision(
                decision=Decision.ALLOW,
                reason="APPROVAL_CONSUMED",
                matched_layers=tuple(matched),
                effective_capabilities=effective,
                provenance=provenance,
                trace=(
                    *trace,
                    f"guard=approval {request.approval_id!r} verified and consumed",
                ),
                engine_decision=engine_decision.decision.value,
                engine_reason=engine_decision.reason.value,
                constraining_layer=constraining,
                approval_id=request.approval_id,
                approval={**approval_spec, "approval_id": request.approval_id},
            )
        # ALLOW: any approval_id present is ignored, never consumed (Rule 37).
        return PolicyDecision(
            decision=Decision.ALLOW,
            reason="ALLOW",
            matched_layers=tuple(matched),
            effective_capabilities=effective,
            provenance=provenance,
            trace=trace,
            engine_decision=engine_decision.decision.value,
            engine_reason=engine_decision.reason.value,
            constraining_layer=constraining,
        )

    def request_approval(self, request: PolicyRequest) -> Any:
        """Mint a server-issued approval for an APPROVAL_REQUIRED decision."""
        decision = self.resolve(request)
        if decision.decision is not Decision.APPROVAL_REQUIRED:
            raise ValueError(
                f"approval requires an APPROVAL_REQUIRED decision, got {decision.decision}"
            )
        spec = decision.approval
        return self.approval_store.create_approval(
            principal=request.principal,
            capability_id=spec["capability_id"],
            normalized_operation=spec["normalized_operation"],
            cwd=spec["cwd"],
            workspace=spec["workspace"],
            risk_class=RiskClass(
                spec["risk_class"].split(".")[-1]
                if "." in spec["risk_class"]
                else spec["risk_class"]
            ),
        )

    def explain(self, request: PolicyRequest) -> dict[str, Any]:
        """Answer 'why ALLOW / APPROVAL_REQUIRED / DENY?' for one request."""
        decision = self.resolve(request, consume_approval=False)
        matched_rule = ""
        if decision.constraining_layer:
            matched_rule = decision.provenance.get(decision.constraining_layer, {}).get("rule", "")
        why = (
            f"{decision.decision.value} because layer "
            f"{decision.constraining_layer or '<none>'} matched rule {matched_rule!r}; "
            f"engine verdict was {decision.engine_decision} "
            f"({decision.engine_reason})."
        )
        if decision.decision is Decision.APPROVAL_REQUIRED:
            why += (
                f" Server-issued approval required for capability "
                f"{decision.approval.get('capability_id')!r} "
                f"(operation {decision.approval.get('normalized_operation')!r})."
            )
        if decision.approval_id:
            why += f" Approval {decision.approval_id!r} was verified and consumed."
        return {
            "decision": decision.decision.value,
            "reason": decision.reason,
            "matched_layer": decision.constraining_layer,
            "matched_rule": matched_rule,
            "constraining_higher_layer": decision.constraining_layer,
            "matched_layers": list(decision.matched_layers),
            "effective_capabilities": sorted(decision.effective_capabilities),
            "approval_required": decision.approval_required,
            "approval": decision.approval,
            "approval_id": decision.approval_id,
            "engine_decision": decision.engine_decision,
            "engine_reason": decision.engine_reason,
            "provenance": decision.provenance,
            "trace": list(decision.trace),
            "why": why,
        }

    def check_via_gateway(
        self, request: PolicyRequest, session: Any, *, gateway: Any = None
    ) -> tuple[bool, Any, str | None, Any, PolicyDecision]:
        """Chain layered resolve -> PermissionEngine -> ActionGateway.

        Layers DENY short-circuits before the gateway; otherwise the gateway
        re-enforces via the engine (single authority, no bypass). Approval
        consumption stays in the gateway's existing approval_id flow.
        """
        from .action_gateway import ActionGateway

        gw = gateway or ActionGateway(approval_store=self.approval_store)
        layered = self.resolve(
            PolicyRequest(
                actor=request.actor,
                tool=request.tool,
                args=request.args,
                workspace=request.workspace,
                cwd=request.cwd,
                goal_id=request.goal_id,
                agent_id=request.agent_id,
                skill_id=request.skill_id,
                client_approved=request.client_approved,
                approval_id="",
            ),
            consume_approval=False,
        )
        if layered.decision is Decision.DENY:
            return False, RemoteErrorCode.POLICY_BLOCKED, layered.reason, None, layered
        gateway_args = dict(request.args)
        if request.approval_id:
            gateway_args["approval_id"] = request.approval_id
        if request.client_approved:
            gateway_args["approved"] = True
        ok, err_code, err_msg, classification = gw.check_action(
            request.tool, gateway_args, session, str(request.cwd)
        )
        return ok, err_code, err_msg, classification, layered

    def snapshot(self) -> dict[str, Any]:
        """Serializable layer config for restart/recovery round-trips."""

        def _frozen(value: frozenset[str] | None) -> list[str] | None:
            return None if value is None else sorted(value)

        return {
            "managed": {
                "denied_tools": sorted(self.managed.denied_tools),
                "approval_tools": sorted(self.managed.approval_tools),
                "allowed_capabilities": _frozen(self.managed.allowed_capabilities),
            },
            "goal": {"bindings": self.goal.bindings},
            "agent": {"bindings": self.agent.bindings},
            "skill": {"bindings": self.skill.bindings},
            "tool": {
                "grants": {
                    k: (None if v is None else sorted(v)) for k, v in self.tool.grants.items()
                },
            },
        }

    @classmethod
    def restore(
        cls, data: dict[str, Any], *, approval_store: ApprovalStore | None = None
    ) -> PolicyResolver:
        managed = data.get("managed", {})
        caps = managed.get("allowed_capabilities")
        return cls(
            managed=ManagedPolicy(
                denied_tools=frozenset(managed.get("denied_tools", ())),
                approval_tools=frozenset(managed.get("approval_tools", ())),
                allowed_capabilities=None if caps is None else frozenset(caps),
            ),
            goal=GoalPolicy(bindings=data.get("goal", {}).get("bindings", {})),
            agent=AgentPolicy(bindings=data.get("agent", {}).get("bindings", {})),
            skill=SkillPolicy(bindings=data.get("skill", {}).get("bindings", {})),
            tool=ToolPolicy(
                grants={
                    k: (None if v is None else frozenset(v))
                    for k, v in data.get("tool", {}).get("grants", {}).items()
                }
            )
            if data.get("tool", {}).get("grants")
            else None,
            approval_store=approval_store,
        )
