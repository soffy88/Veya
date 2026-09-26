# P15 placement matrix

This matrix is the Wave 1 compatibility baseline. It records extensions to
existing authorities; it does not introduce a second runtime, registry, or
orchestrator.

| Contract | Canonical owner | Integration rule |
|---|---|---|
| `VerificationSpec`, `FeatureMap` | `runtime/verification/models.py` | Extend existing immutable contracts; `VerificationProfile` and `VerificationGate` layer on top. |
| `Mission`, `ExecutionReport` | `veya/supervision/models.py`, `veya/supervision/store.py` | Mission stores P15 version pins; GoalRun remains execution authority. |
| `AutonomyPolicy` | `runtime/doctrine.py` | Deterministic policy evaluation only; tool/workspace authority wins. |
| `AgentRoleContract` | `runtime/doctrine.py` + existing `obase.agent_registry` | Attach metadata only to an existing registered agent; never grant tools. |
| `TeamPlaybook` | `runtime/doctrine.py` + `DoctrineStore` | Versioned, read-only runtime composition; activation is outside worker scope. |
| `CorrectionRecord`, `RuleCandidate` | `runtime/doctrine.py` + `DoctrineStore` | Durable candidates; no automatic policy activation. |
| `SkillContract` | Existing `server.capability_model.SkillRegistry` | Promotion produces a contract for the existing skill authority; no second registry. |
| `RoutinePolicy` | `runtime/doctrine.py` + `DoctrineStore` | Event-first policy and telemetry; routines trigger only and do not execute tools. |
| Mission persistence | Existing `MissionStore` | Existing JSON/JSONL durable root; no alternate Mission store. |

Compatibility rules:

- Old Mission documents load with conservative `autonomy_level=draft` and
  `autonomy_policy_version=1.0`.
- Empty P15 pins remain empty for legacy missions except the conservative
  autonomy default.
- A pinned Mission cannot reach `ACCEPTED`/`DONE` without a passing
  `VerificationGateResult`.
- Policy text, memory, role descriptions, and playbook guidance do not grant
  executable authority.
