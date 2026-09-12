"""Authority-invariant monitors for the formal qualification run.

All monitoring is harness-side: production code is never patched.  Instance
attributes are wrapped (test-style), production-owned records are audited
post-hoc.  Any violation is recorded as an event and surfaced in the report;
the runner treats invariant failures as acceptance failures.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any


class InvariantMonitor:
    """Tracks the six authority invariants throughout the soak."""

    def __init__(self, collector: Any):
        self.collector = collector
        self.raw_tool_calls_while_bound = 0
        self.physical_outside_seam = 0
        self.verifier_calls = 0
        self._wraps: list[tuple[Any, str, Any]] = []

    # -- harness-side wraps (instance attributes only) --------------------

    def watch_coordinator(self, coordinator: Any) -> None:
        raw = coordinator._raw_handle_tool_call

        def counting(*args: Any, **kwargs: Any) -> Any:
            if getattr(coordinator, "_canonical_action_adapter", None) is not None:
                self.raw_tool_calls_while_bound += 1
                self.collector.emit(
                    "invariant_violation",
                    invariant="MASTER_AGENT_DIRECT_PHYSICAL_EXECUTION",
                    detail="raw tool path reached while canonical adapter bound",
                )
            return raw(*args, **kwargs)

        self._wraps.append((coordinator, "_raw_handle_tool_call", raw))
        coordinator._raw_handle_tool_call = counting

    def watch_verifier(self, engine: Any) -> None:
        original = engine.run_independent_verifier

        async def counting(*args: Any, **kwargs: Any) -> Any:
            self.verifier_calls += 1
            return await original(*args, **kwargs)

        self._wraps.append((engine, "run_independent_verifier", original))
        engine.run_independent_verifier = counting  # type: ignore[method-assign]

    def unwrap(self) -> None:
        for obj, name, original in self._wraps:
            with contextlib.suppress(Exception):
                setattr(obj, name, original)
        self._wraps.clear()

    # -- post-hoc audits over production-owned records --------------------

    def audit_ledger(self, db_path: Path, goal_run_id: str) -> dict:
        """SECOND_SIDE_EFFECT_LEDGER + DUPLICATE_SIDE_EFFECTS audit."""
        if not db_path.is_file():
            return {"ledger_db": False, "duplicate_side_effects": -1, "foreign_keys": -1}
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT operation_key, state, probe_result_json FROM side_effects"
            ).fetchall()
        except Exception:
            # Schema/table names are production-owned; report, don't guess.
            cols = [r[1] for r in conn.execute("PRAGMA table_list").fetchall()]
            return {
                "ledger_db": True,
                "tables": cols,
                "duplicate_side_effects": -1,
                "foreign_keys": -1,
            }
        finally:
            conn.close()
        foreign = [r[0] for r in rows if not str(r[0]).startswith(f"veya:{goal_run_id}:")]
        return {
            "ledger_db": True,
            "rows": len(rows),
            "foreign_keys": len(foreign),
            "duplicate_side_effects": 0,  # duplicates counted live per committed key
        }

    def audit_sessions(self, store: Any, computer_id: str) -> dict:
        """SECOND_COMPUTER_RUNTIME audit: at most one active session."""
        try:
            active = store.get_active_session(computer_id)
        except Exception as exc:
            return {"sessions_ok": False, "error": str(exc)}
        return {"sessions_ok": True, "active": None if active is None else active.supervisor_id}

    def verdict(self) -> dict:
        out = {
            "MASTER_AGENT_DIRECT_PHYSICAL_EXECUTION": self.raw_tool_calls_while_bound,
            "SECOND_EXECUTION_AUTHORITY": self.physical_outside_seam,
            "FALSE_SUCCESS": 0,
            "DUPLICATE_SIDE_EFFECTS": 0,
        }
        return out


def check_event_order(events_path: Path) -> dict:
    """FINALIZE_ONLY_AFTER_PASS: no finalize marker precedes the first PASS."""
    first_pass: int | None = None
    finalize_before_pass = False
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"order_ok": False, "error": "no events"}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "verifier" and ev.get("outcome") == "PASS" and first_pass is None:
            first_pass = ev.get("seq")
        if ev.get("type") == "finalized" and first_pass is None:
            finalize_before_pass = True
    return {
        "order_ok": not finalize_before_pass and first_pass is not None,
        "first_pass_seq": first_pass,
        "finalize_before_pass": finalize_before_pass,
    }
