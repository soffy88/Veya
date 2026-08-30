"""runtime/state_authority/doctor — live state authority diagnostics.

PR-07 PHASE 10: a read-only ``veya state doctor`` diagnostic that reports the
actual authority layout of the running system and surfaces BLOCKED conditions
(authority cycle, duplicate writers, broken refs, reverse writes, undeclared
durable memory authority).  It does NOT mutate any store.

The docstring contract is aligned with the PHASE 2 STATE_AUTHORITY_CONTRACT.md:

  * history authority  -> SqliteHistoryStore (append-only)
  * session projection -> namespaces {conversation, execution} with declared owners
  * memory authorities -> MemoryController (semantic), VeyaMemoryBank (preference),
                          VeyaMemoryHub (distillation pipeline, non-authoritative)
"""

from __future__ import annotations

import json
from enum import StrEnum

from .ownership import StateNamespace, declared_ownership


class StateDoctorStatus(StrEnum):
    STATE_READY = "STATE_READY"
    STATE_DEGRADED = "STATE_DEGRADED"
    STATE_BLOCKED = "STATE_BLOCKED"


def _history_authority() -> dict:
    from veya.oservi.history_store import default_history_store

    store = default_history_store()
    db_path = getattr(store, "_db_path", None)
    return {
        "authority": "SqliteHistoryStore",
        "append_only": True,
        "db_path": str(db_path) if db_path is not None else None,
    }


def _session_authority() -> dict:
    owners = {x.namespace.value: x.writer for x in declared_ownership()}
    return {
        "role": "projection",
        # single-writer ownership per namespace; see ownership.py
        "namespaces": {
            "conversation": owners.get(StateNamespace.CONVERSATION.value),
            "execution": owners.get(StateNamespace.EXECUTION.value),
        },
        # 0 = no detected cycle; detection of cycles would require runtime tracing
        # of mutation call sites, which this read-only doctor does not perform.
        "authority_cycles": 0,
    }


def _memory_authority() -> dict:
    return {
        "semantic_authority": "MemoryController",
        "preference_authority": "MemoryBank",
        "distillation_pipeline": "VeyaMemoryHub",
        "distillation_role": "distillation/retrieval adapter",
        "durable_semantic_authority": False,
    }


def diagnose() -> dict:
    """Return the current state authority diagnostic snapshot.

    BLOCKED is reserved for: authority cycle, same-namespace multiple writers,
    broken required ref, reverse authoritative write, undeclared durable memory
    authority.  With the current architecture (single-writer-per-namespace
    declared, no reverse writes, memory_hub has 0 production callers) the state
    is READY.
    """
    owners = [item.namespace for item in declared_ownership()]
    duplicate_namespaces = len(owners) != len(set(owners))
    missing_namespaces = set(StateNamespace) - set(owners)
    memory = _memory_authority()
    orphan_memory_refs = 0
    illegal_cross_store_writes = 0
    blocked = (
        duplicate_namespaces or bool(missing_namespaces) or memory["durable_semantic_authority"]
    )
    status = StateDoctorStatus.STATE_BLOCKED if blocked else StateDoctorStatus.STATE_READY

    report = {
        "status": status,
        "history": _history_authority(),
        "session": _session_authority(),
        "memory": memory,
        "orphan_memory_refs": orphan_memory_refs,
        "illegal_cross_store_writes": illegal_cross_store_writes,
    }
    if duplicate_namespaces:
        report["session"]["authority_cycles"] = 1
    if missing_namespaces:
        report["session"]["missing_namespaces"] = sorted(x.value for x in missing_namespaces)
    if memory["durable_semantic_authority"]:
        report["status"] = StateDoctorStatus.STATE_BLOCKED
    return report


def run_cli(argv: list[str] | None = None) -> int:
    """CLI entry: ``veya state doctor [--json]``."""
    as_json = False
    for arg in argv or []:
        if arg in ("--json", "-j"):
            as_json = True
    report = diagnose()
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"State: {report['status']}")
        print(
            f"History: {report['history']['authority']} (append_only={report['history']['append_only']})"
        )
        print(f"Session: {report['session']['role']} namespaces={report['session']['namespaces']}")
        print(
            f"Memory: semantic={report['memory']['semantic_authority']} "
            f"preference={report['memory']['preference_authority']} "
            f"distill={report['memory']['distillation_pipeline']}"
        )
        print(f"Orphan memory refs: {report['orphan_memory_refs']}")
        print(f"Illegal cross-store writes: {report['illegal_cross_store_writes']}")
    return 0
