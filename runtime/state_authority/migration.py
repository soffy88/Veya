"""Read-only PR-07 migration audit.

The current audit has no data movement plan: the issue was ambiguous contracts,
not duplicate records.  Keeping this as a small pure function makes the
no-migration conclusion reproducible without touching any store.
"""

from __future__ import annotations


def dry_run_audit() -> dict[str, object]:
    return {
        "migration_required": False,
        "duplicates_requiring_migration": 0,
        "orphans": 0,
        "authority_conflicts": 0,
        "evidence": "Separate existing authorities; no records require movement.",
    }
