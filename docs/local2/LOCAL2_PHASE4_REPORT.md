# Local2 Phase 4 report — L1 ExecutorRegistry

| gate | status |
|---|---|
| P4.1 registry authority | PASS |
| P4.2 executor record | PASS |
| P4.3 hicode legacy | PASS |
| P4.4 selection pipeline | PASS |
| P4.5 failure taxonomy | PASS |
| P4.6 provider boundary | PASS |
| P4.7 admission migration | PASS |
| P4.8 real qualification | **PARTIAL — real chain proven, provider side blocked** |

## What P4.8 proved, and what it did not

The full chain was exercised for real, twice, with no mock and no injected
executor: MCP client → HTTP :8790 → tool registry → `ExecutorRegistry` →
`resolve_executor` → CLI worker runtime → receipt.

| executor | outcome |
|---|---|
| `opencode` | reached its provider, then `TIMED_OUT` / `TOOL_TIMEOUT`, `timeout_kind=INACTIVITY_TIMEOUT` |
| `pi` | `FAILED` / `PROVIDER_CONFIGURATION_FAILURE` |

Neither produced a provider *success* in this window. That is a provider-side
condition, not a failure of the executor chain — the receipts prove the chain
ran to the provider boundary and classified the outcome correctly.

## The defect this phase found

`pi` returns HTTP 400 `FAILED_PRECONDITION` — "User location is not supported
for the API use". The process exits non-zero, and
`classify_executor_failure` mapped any non-zero exit to `WORKER_CRASH`, so a
provider policy refusal was reported as a worker fault. Verified live through
MCP before and after the fix:

```
before:  pi -> FAILED / WORKER_CRASH
after:   pi -> FAILED / PROVIDER_CONFIGURATION_FAILURE
```

Also split `PROVIDER_RATE_LIMIT` out of the `PROVIDER_UNAVAILABLE` catch-all,
because "unreachable" and "slow down" call for different operator responses.

## Honest status

`P4.8_PROVIDER_DEPENDENCY_BLOCKED`: an upstream quota/region condition on the
executor providers. No gate was lowered, no provider was mocked, no test was
skipped. Registry authority, selection, failure boundary and receipt are all
qualified and green; only a *successful* provider round trip is outstanding, and
that depends on the provider, not on this code.

## Commit chain

```
74373e19  refactor(l1): derive the worker-type labels from the registry
3139b864  feat(l1): complete the executor registry contract
aec022e9  feat(l1): add the executor-plane failure classes
6286c0d9  feat(l1): separate admission rejection from execution lifecycle
6518320e  feat(l1): migrate admission rejection from blocked state
e8c6bbf4  fix(l0): restore suspend resume lineage completion
```
