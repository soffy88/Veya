# Executor qualification matrix (spec P4.8.1)

Read from the single authority, `get_executor_registry().snapshot()`.
No worker-type literal, CLI worker table or hardcoded list is consulted.

Generated 2026-10-02 from the running registry.

| executor | availability | capability | ops | selectable | write | priority | provider |
|---|---|---|---|---|---|---|---|
| `acp` | UNAVAILABLE | **no** | 0 | no | no | 7 | acp |
| `antigravity` | DEGRADED | yes | 7 | yes | yes | 0 | gemini |
| `claude_code` | AVAILABLE | yes | 8 | yes | yes | 2 | anthropic |
| `codex` | AVAILABLE | yes | 7 | yes | yes | 6 | openai |
| `dsh` | DEGRADED | yes | 7 | yes | no | 5 | dsh |
| `grok` | UNAVAILABLE | yes | 7 | no | no | 4 | xai |
| `opencode` | AVAILABLE | yes | 4 | yes | no | 1 | opencode |
| `pi` | AVAILABLE | yes | 7 | yes | no | 3 | local-cliproxy-google |

## Reading the table

* `acp` is registered with no capability record: **registered but not
  selectable**. That is the intended distinction — existence and runnability are
  different dimensions, and merging them would make `acp` either disappear or
  crash on dispatch.
* `grok` has a capability record but is `UNAVAILABLE`, so it is not selectable.
  Priority does not override availability.
* Only `antigravity`, `claude_code` and `codex` are write-qualified.

## Real execution results (live, via MCP)

| executor | status | failure_class | admission | target |
|---|---|---|---|---|
| `opencode` | TIMED_OUT | `TOOL_TIMEOUT` | ACCEPTED | NEW_ISOLATED_WORKTREE |
| `pi` | FAILED | `PROVIDER_CONFIGURATION_FAILURE` | ACCEPTED | NEW_ISOLATED_WORKTREE |

Both were dispatched through the real chain — MCP client, HTTP, tool registry,
`ExecutorRegistry`, `resolve_executor`, CLI worker runtime, receipt — with no
mock and no injected executor.

`pi` is the significant result: its provider answers HTTP 400
`FAILED_PRECONDITION` ("User location is not supported for the API use"). That
is a provider policy refusal, and before this phase it was reported as
`WORKER_CRASH` because the process exited non-zero. See
`LOCAL2_PHASE4_REPORT.md`.

`opencode` reached the provider and then hit the runner's 300s inactivity
timeout; the receipt carries `timeout_kind=INACTIVITY_TIMEOUT`, so the two
timeout layers are distinguishable.

