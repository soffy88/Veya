# PHASE 2 — State Authority Contract

## Hard Invariants

| ID | Statement |
|---|---|
| **SA-01** | `history_store` is append-oriented historical authority. |
| **SA-02** | `MemoryController / Personal Runtime` is the semantic memory authority. |
| **SA-03** | `session_tree` is a reconstructable projection. |
| **SA-04** | `session_tree` loss must not lose historical truth. |
| **SA-05** | `session_tree` loss must not lose approved memory. |
| **SA-06** | Semantic memory loss must not be silently reconstructed from `session_tree`. |
| **SA-07** | `history_store` cannot be rewritten from `session_tree`. |
| **SA-08** | One logical projection namespace has exactly one authoritative projector/writer. |
| **SA-09** | Cross-store propagation uses IDs/references/events, not full-object blind copy. |
| **SA-10** | Startup recovery direction is explicit and acyclic. |

---

## Namespace Ownership

`session_tree` has two logical projection namespaces with strict single-writer ownership:

| Namespace | Authoritative Writer | Implementation |
|---|---|---|
| `conversation` | `SessionProjector` | `coordinator_master._mirror_to_session_tree` |
| `execution` | `GoalRunProjection` | `goal_run/runner._record_retry_branch` |

These are declared in `runtime/state_authority/ownership.py::StateWriterOwnership`.

---

## Memory Domains

| Domain | Authority | Writer |
|---|---|---|
| `semantic` | `MemoryController / Personal Runtime` | `coordinator_master._distill_and_store` → `personal.create_memory_candidate` |
| `preference` | `memory_bank` (`VeyaMemoryBank`) | `memory_bank.add_preference` (tool-driven) |
| `distillation` | `memory_hub` (`VeyaMemoryHub`) | derived distillation/retrieval adapter/cache; not an authority (0 production callers) |

---

## MemoryRef Semantics

`runtime/state_authority/memory_refs.py::MemoryRef` provides typed, stable references across memory domains:

```python
@dataclass(frozen=True)
class MemoryRef:
    domain: Literal["semantic", "preference"]
    id: str
    version: int | None = None
    source: str | None = None
```

Resolution dispatches by `domain` to the owning authority. This module does NOT wrap history events (already referenced by `(session_id, revision)`) or GoalRuns (already referenced by `goal_run_id`) or Artifacts (already referenced by `ArtifactRef`).

---

## Enforcement

The contract is enforced via:
- `runtime/state_authority/ownership.py` — namespace owner declarations + `assert_writer` guard
- `runtime/state_authority/session_projection.py` — thin guarded adapter used by both existing writers
- `tests/state_authority/` — recovery/guard tests (SA-01 through SA-15)
- `runtime/state_authority/doctor.py` — `state doctor` command for live verification

The doctor reports the three memory roles separately. `memory_hub` may persist
pipeline material, but `DURABLE_SEMANTIC_AUTHORITY = False` and it is not a
semantic winner/supersession store.
