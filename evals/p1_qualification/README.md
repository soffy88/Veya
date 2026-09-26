# P1 Qualification Harness (rebased onto I4 `b7080b7b`)

Two modes (`--mode`):

- **smoke**: 7 controlled mechanics scenarios (HARNESS_MECHANICS) + short
  soak. Fast self-check of the machinery.
- **formal**: canonical production-seam entry and nothing else as the timed
  workload:

  ```
  g1_plan -> worker.before_execution -> harness.attach ->
  coordinator.bind -> seam_action (MasterAgent decision point) ->
  execute_canonical_action -> ActionGateway -> SideEffectLedger ->
  supervisor restart (load_goal_run resume) ->
  verifier FAIL -> new evidence -> PASS -> finalize
  ```

  Controlled faults (approval hold, primary failure, tool failure, restart,
  verifier FAIL, ledger replay) run on a fixed schedule as fault
  injection/control. Acceptance consumes production-owned records only
  (budget/checkpoint/ledger/harness/verifier/events):
  DIRECT_COMPONENT_SHORTCUTS_AS_ACCEPTANCE=0.

Notes:

- main tree has no CandidateReady component; readiness is expressed via
  verified-acceptance gating + harness observe (bb349d39, committed). The
  harness invents no component for that chain link.
- External-LLM provider outage is NOT_RUN in this environment; the
  provider-layer checks traverse ReliableProviderAdapter with real local
  compute (controlled fault injection, labeled as such).
- Full G2 leaf execution (hicode/dsh CLIs) is environment-dependent and is
  not the soak driver; the soak traverses the identical I4 action seam.

Target state: I4 PASS has NOT happened in this window. This directory holds
the 30–60 min qualification harness so it is ready to execute immediately
after I4 PASS. Nothing here modifies production runtime, I1/I2/I3/P0
semantics, declares qualification PASS, or starts P2.

## Qualification semantics (read first)

Two classes, never conflated:

- **HARNESS_MECHANICS** (this harness): 7 controlled scenarios proving the
  adapter/replan/ledger/restart/verifier/checkpoint *paths* work with real
  production classes and real injected faults. The provider TimeoutError is
  **CONTROLLED** — it qualifies the ReliableProviderAdapter failover path,
  NOT a real external provider outage.
- **FINAL_REAL_QUALIFICATION** (NOT_RUN here): the post-I4 1800–3600 s run
  on the **canonical production path** (not scenario objects calling each
  other), reporting REAL_PROVIDER_CALLS / REAL_TOOL_EXECUTIONS /
  REAL_CONTEXT_CYCLES / REAL_RESTARTS / REAL_CHECKPOINTS. Blocked on
  I4_A_ACTION_PROTOCOL=PASS + I4_CANONICAL_E2E=PASS, then rebase + regression
  first.

Every report records `controlled_provider_failover` (PASS/FAIL),
`real_provider_failover: NOT_RUN`, and a `final_real_qualification` block
with all REAL_* counters at NOT_RUN.

## Run

Full (30 min floor, real work only):

```bash
venv/bin/python evals/p1_qualification/run_qualification.py \
  --target-seconds 1800 --work-root .veya/qualification/p1q-<date>
```

or via pytest (marked slow; excluded from the default fast suite):

```bash
P1Q_TARGET_SECONDS=1800 venv/bin/python -m pytest -q \
  tests/qualification/test_p1_qualification.py -m slow
```

Smoke (harness self-check, short clock, same code paths):

```bash
P1Q_TARGET_SECONDS=60 venv/bin/python -m pytest -q \
  tests/qualification/test_p1_qualification.py
```

## Duration rule

`--target-seconds` (default 1800, max 3900) is filled by executed
hash/IO/tool/provider/context cycles. There is no `time.sleep` anywhere in
`evals/p1_qualification/` — verified by `test_no_fake_sleep`.

## Coverage (7 scenarios, one SAME GoalRun)

| # | Scenario | Driver | Acceptance |
|---|----------|--------|------------|
| 1 | Context pressure | `scenarios.scenario_context_pressure` | multi-round real tool traffic, ContextEngine pressure+compaction, goal/spec/failure/artifact refs preserved, CONTEXT_DRIFT=0 |
| 2 | Provider failover | `scenarios.scenario_provider_failover` | real primary TimeoutError, ReliableProviderAdapter failover, SAME_GOALRUN, context/tool schema preserved, SEMANTIC_DRIFT=0 |
| 3 | Tool failure + replan | `scenarios.scenario_tool_failure_replan` | one real controlled failure, evidence preserved, LongRunningHarness replan, corrected action, recovery |
| 4 | PersistentComputer restart | `scenarios.scenario_supervisor_restart` | Supervisor A session ended, B restores same computer + same GoalRun, execution continues |
| 5 | SideEffectLedger | `scenarios.scenario_side_effect_ledger` | committed before restart, resume hits committed row, DUPLICATE_SIDE_EFFECTS=0 |
| 6 | Verification convergence | `scenarios.scenario_verification_convergence` | verifier FAIL (>=1) → replan + new evidence → PASS; finalize only after PASS |
| 7 | Checkpoint/resume | `scenarios.scenario_checkpoint_resume` | >=3 checkpoints, latest valid restored, no lost progress (+ missing-checkpoint control) |

## Outputs (per run dir)

- `events.jsonl` — full event/metric stream (collector)
- `metrics.json` — counters
- `report.json` / `report.md` — acceptance checklist + disclaimer
- `failure_trace.json` — written on any crash (traceback + metrics + event tail)

The report status is `HARNESS_COMPLETED`, never a qualification PASS verdict.
PASS requires I4 PASS + human review of the report and preserved traces.
