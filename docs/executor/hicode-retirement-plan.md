# Hicode Retirement Plan (design only — NOT implemented)

**Status:** design only. Nothing in this document is implemented. `hicode` remains
a live runtime executor today; see "Current state" for the measured inventory.

**Scope note:** this document records a required scope expansion. Retiring
`hicode` cannot be done inside the authority-closure scope, because the live call
graph runs through files that scope excludes (`veya/remote/tool_adapter.py`,
`server/hicode_*.py`).

## 1. Current callers

Measured at `3eb095e1` (repo-wide, excluding `platform/3O`):

| Category | Files | Notes |
|---|---|---|
| `hicode` references | 142 | 995 refs total |
| Production files | 64 | includes live execution paths |
| Test files | 142 | many assert on live hicode behavior |
| Markdown | 29 | migration/historical documentation |

### Live runtime execution (must reach zero)

```
veya/remote/tool_adapter.py:3357   async def _call_direct_hicode
veya/remote/tool_adapter.py:1490     return await self._call_direct_hicode(
server/hicode_agent.py:401          async def _run_hicode
server/hicode_agent.py:802            r = await _run_hicode(args, **run_kwargs)
server/project_ask.py:127           async def _run_hicode
```

### Registry / routing references (must stay deny-only)

```
server/goal_run/leaf.py:98           _run_builtin/_run_hicode/_run_dsh dispatch comment
server/capability_model.py:892       HarnessRegistry routes through these runners
veya/remote/action_gateway.py        action surface references
veya/remote/__init__.py              export surface
scripts/qualify_remote_mcp.py        qualification script
```

`veya/remote/executor_registry.py` keeps `hicode` only as a deny marker:

```python
_RETIRED_EXECUTORS = frozenset({"hicode"})

def identity(...):
    if key in _RETIRED_EXECUTORS:
        raise ValueError(f"Executor retired: {key}")
```

That deny marker is **not** a runtime reference and is counted separately as
`HICODE_DENY_MARKER_REFERENCE`.

## 2. Migration graph

```
caller
  → _run_hicode / _call_direct_hicode      (to be replaced)
      → subprocess / provider              (to be deleted)

caller
  → resolve_executor()
      → ExecutorRegistry.ordered_ids()    (authority, already in place)
          → WorkerRuntime capability gate
              → HarnessRegistry
                  → adapter / execution
```

Every current `_run_hicode` caller must end up routed through the
ExecutorRegistry → HarnessRegistry path, so that admission, ordering, and
qualification decisions stay in one place.

## 3. Replacement path

```
hicode
 ↓  (remove; do not silently reroute)
ExecutorRegistry          owns WHO is admitted + canonical order
 ↓
HarnessRegistry           owns HOW execution is dispatched
 ↓
WorkerRuntime             owns runtime lifecycle capability
```

**Constraint:** the legacy adapter must never silently route to another
executor. A retired executor that receives a request must fail closed with an
explicit retired error, not be substituted.

## 4. Required scope expansion

Retirement cannot proceed without modifying:

```
server/hicode_agent.py            _run_hicode definition + live caller
server/project_ask.py             _run_hicode definition
server/goal_run/leaf.py           builtin/hicode/dsh dispatch
veya/remote/tool_adapter.py       _call_direct_hicode, hicode.execute tool surface
server/capability_model.py        HarnessRegistry wiring to the legacy runner
```

These are currently excluded by the authority-closure scope. Retirement is
therefore a separate project with its own scope.

## 5. Deprecation stages

**Stage 1 — shadow.** Keep the legacy path executable but add a deny marker and
telemetry so real call sites are observed, not guessed. Instrument
`_run_hicode` and `_call_direct_hicode` entry points; assert in tests that the
retired marker rejects admission.

**Stage 2 — deny new usage.** Make `hicode` unreachable for new work:
`ExecutorRegistry.identity("hicode")` and any probe must fail closed; the legacy
runner raises an explicit retired error rather than executing. No silent
substitution to another executor. Existing tests asserting live hicode behavior
must be migrated or explicitly retired in the same change — never weakened.

**Stage 3 — remove runtime.** Delete `_run_hicode`, `_call_direct_hicode`, the
`hicode.execute` tool surface, and the `direct_hicode` execution mode. Migrate
remaining callers to the ExecutorRegistry → HarnessRegistry path. Keep only the
`_RETIRED_EXECUTORS` deny marker so a stale request still fails closed with a
clear error.

## 6. Verification for each stage

```
HICODE_RUNTIME_REFERENCE=0        # stages 2-3
HICODE_EXECUTION_PATH=0            # stage 3
HICODE_FALLBACK=0                  # stage 2 onward
HICODE_GOALRUN_DEFAULT=0           # stage 2 onward
HICODE_DENY_MARKER_REFERENCE       # may remain; counted separately
```

Falsification for each stage: restoring a `hicode` execution branch must make the
corresponding boundary test fail. A test that cannot fail when the branch is
restored does not count as verification.

## 7. Known technical debt (recorded, not fixed here)

1. `resolve_executor()` reads `WORKER_CAPABILITIES` membership to gate requested
   executors. Today this is not a live divergence — every capability key is
   registry-admitted — but it is a second read of executor identity. Retiring it
   requires deciding how an admitted-but-unroutable executor such as `acp` must
   behave: reject, or substitute. Pinned by
   `test_worker_capabilities_gate_is_not_an_admission_authority`.
2. `WORKER_CAPABILITIES` has no `acp` entry, so `acp` is admitted and visible in
   `ExecutorRegistry.ordered_ids()` but rejected by `resolve_executor`. The
   health snapshot filters it out, so the visible universe equals the routable
   set, but the registry still reports it.
3. `tests/remote/test_execution_contract_p1.py::test_cli_execution_manifest_json`
   asserts `HICODE` status `READY`, which contradicts the frozen architecture.
   It fails today and is deliberately left failing rather than weakened.
