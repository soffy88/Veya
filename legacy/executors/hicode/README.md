# Retired: Hicode executor

Hicode was retired as a Veya executor. Nothing in the runtime may select,
dispatch, fall back to, or route through this code.

Moved here (not deleted) so the historical implementation stays reviewable.

Nothing in this directory is imported by the product. In particular:

- there is no `hicode.execute` MCP tool binding;
- `veya/executor_retirement.py` lists `hicode` in `RETIRED_EXECUTORS`, and
  `ExecutorRegistry` refuses it at admission, discovery, and identity lookup;
- `resolve_executor(requested="hicode")` raises rather than substituting;
- the master prompt no longer advertises `hicode_run` / `hicode_rollback`;
- `server/goal_run/leaf.py` fails closed on any unadmitted assignee.

These modules still import each other as `server.hicode_*`, so they are not
importable from their new location. That is intentional: it makes any attempt
to reintroduce them fail loudly instead of silently working.

## tests/

These test modules moved with the implementation they covered. They are not
collected by the product test suite: Hicode is retired, and a retired executor
must not gate CI. They are kept for historical reference only.
