# Veya Harness Engineering

Veya follows the production-agent model **Agent = Model + Harness**.  The
MasterAgent remains the only user-facing semantic authority; the harness adds
project context, deterministic checks, safety gates, evidence and observability
around coding work.

## Six layers

1. **Guides** — `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.veya/GUIDES.md`
   and `.veya/project_rules.md` are loaded as source-attributed project context.
2. **Sensors** — computational checks such as tests, lint, typecheck, build and
   schema probes produce `SensorResult` evidence.  LLM judges are advisory.
3. **Agentic Loop** — the existing MasterAgent/GoalRun/AgentLoop remains in
   charge of planning and execution.  Harness code does not create a second
   loop.
4. **Memory** — existing Veya memory and skill scope remains authoritative;
   coding contracts identify the workspace memory scope.
5. **Permissions** — existing permission profiles and coding sandbox policies
   control writes, process execution and remote effects.
6. **Observability** — command results, sensor reports, verification reports and
   task-scoped artifacts make claims inspectable.

## Guides

Guide rules keep `source_path` and `source_line`.  Conflicting instructions are
reported to the model and Doctor; they are not silently resolved.  A rule with
no source is not a permanent guide rule.

Use the MasterAgent tools `harness_guides_load`, `harness_guides_search` and
`harness_guides_show` to inspect context.  A Ratchet candidate may append an
approved rule to `.veya/GUIDES.md`, marked with its candidate id.

## Sensors and verification

Computational sensors run before a coding patch is considered verified.  A
required sensor failure fails acceptance; a skipped required sensor means
insufficient evidence.  Optional and `llm_judge` sensors cannot make a patch
verified.  Sensor output is task-scoped and is referenced from the verification
report and ArtifactManifest.

## Ratchet principle

Failures can create an evidence-backed `RatchetCandidate`.  Candidate,
approved and applied are separate states.  No model suggestion writes a guide,
permission, tool or sensor automatically.  A user/policy approval is required;
guide applications carry a marker for review and rollback.

## Doctor

```bash
veya harness doctor --path .
veya harness doctor --path . --quick --json
veya harness doctor --path . --full --json
```

The default command only diagnoses. `--quick` runs low-cost required
deterministic sensors; `--full` runs the complete deterministic suite. Both
persist `sensor_report.json` and `verification_report.json` below
`.veya/runs/<doctor_run_id>/outputs/` and make readiness from the reloaded
evidence. Neither mode runs an LLM judge, accesses the network, installs
dependencies or modifies source files.

`HARNESS_READY` means the configured context and checks are available.
`HARNESS_DEGRADED` means ordinary use may continue but unattended coding lacks
some evidence. `HARNESS_BLOCKED` means an unsafe command or invalid harness
state must be fixed first. Missing guides degrade; they do not block ordinary
use.

## What Veya does not automate

The harness does not push branches, create or post GitHub PRs, publish releases,
rewrite the Execution Runtime ABI, alter Personal Gold labels, or replace the
single MasterAgent path.
