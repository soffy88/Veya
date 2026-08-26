# Personal Agent Runtime eval fixtures

These fixtures define deterministic acceptance contracts for the durable
Personal Agent Runtime. They are intentionally facts-and-gates tests, not an
LLM judge:

- `memory_recall`: active records have provenance and retrieval excludes
  forgotten/superseded facts.
- `memory_conflict`: conflicting values are flagged; correction supersedes
  the old record without deleting its source.
- `memory_correction`: replacement is active and auditable.
- `memory_forget`: forget is a soft lifecycle state and normal search omits it.
- `skill_teaching`: teaching creates a candidate and explicit confirmation is
  required before activation.
- `skill_reuse`: the selected immutable version records result/acceptance and
  updates run statistics.
- `skill_regression`: an inferior candidate version cannot replace the active
  version; rollback restores a prior trusted version.
- `continuity`: CLI/Web/TUI projections agree on task, checkpoint, artifact,
  and memory references.
- `long_term_learning`: one failure is never enough; three independent task
  IDs create a candidate, replay/baseline evaluation gates validation, and
  application is explicit.

The source of truth is the PostgreSQL execution authority in production. These
fixtures may use isolated SQLite only for unit tests.
