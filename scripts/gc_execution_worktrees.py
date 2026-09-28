#!/usr/bin/env python3
"""Inventory and reclaim leaked Local2 execution worktrees.

The worktree lifecycle is: admit -> acquire -> run -> terminal -> release.
A gateway that dies between "terminal persisted" and "worktree removed" leaves
the worktree behind, and a child execution used to never release at all.  This
tool reports what is reclaimable, and reclaims it using Git's canonical
``worktree remove`` / ``worktree prune`` semantics via ``teardown_worktree``.

It never touches a non-Local2 worktree: ``teardown_worktree`` only resolves
paths under ``.veya/worktrees`` and refuses dirty, locked, and
/proc-referenced worktrees.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def worktree_inventory(repo: Path) -> dict[str, object]:
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    entries = [
        line.removeprefix("worktree ").strip()
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]
    local2 = [p for p in entries if "/.veya/worktrees/" in p]
    user = [p for p in entries if p not in local2]
    meta = repo / ".git" / "worktrees"
    total = 0
    if meta.is_dir():
        for path in meta.rglob("*"):
            if path.is_file():
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
    return {
        "total": len(entries),
        "local2": len(local2),
        "user": len(user),
        "user_worktrees": user,
        "worktree_admin_bytes": total,
    }


def _histogram(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        key = str(value)
        out[key] = out.get(key, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(ROOT), help="canonical repository root")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually reclaim; without this flag the run is a dry run",
    )
    parser.add_argument("--prune", action="store_true", help="also run git worktree prune")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    os.environ.setdefault("VEYA_WORKSPACE_ROOT", str(repo))

    from veya.remote.execution import DurableJobManager, ExecutionStore

    store = ExecutionStore.from_env(default_persistent=True)
    manager = DurableJobManager(store)

    before = worktree_inventory(repo)
    result = manager.reconcile_worktrees(dry_run=not args.apply)

    pruned = None
    if args.apply and args.prune:
        from runtime.coding.worktree import WorktreeManager

        pruned = WorktreeManager(repo).prune(verbose=False)

    after = worktree_inventory(repo)
    report = {
        "repo": str(repo),
        "applied": bool(args.apply),
        "before": before,
        "after": after,
        "reconciled": {
            "considered": result["considered"],
            "released": result["released"],
            "retained_count": len(result["retained"]),
            "errors": result["errors"],
        },
        "retained_histogram": _histogram(x.get("reason") for x in result["retained"]),
        "retained_sample": result["retained"][:20],
        "pruned": pruned,
    }
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
