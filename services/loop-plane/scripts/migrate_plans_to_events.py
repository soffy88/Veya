#!/usr/bin/env python3
"""loop-plane 迁移脚本（SPEC §3.2 / Phase 1）: 旧 plan JSON → DomainEvent。

扫描 ~/.veya/plans/**/*.json（旧 plan_todo 存储）：
    每个 plan → GoalCreated + Todo 快照事件，写入 loop-plane EventStore。
一次性脚本；重复运行幂等（按 goal_id 去重，已存在则跳过）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def migrate_plans(plans_dir: Path, store, *, dry_run: bool = False) -> int:
    """迁移 ~/.veya/plans/*.json → GoalCreated/TodoUpdated 事件。返回迁移数。"""
    migrated = 0
    existing = set(store.aggregates("Goal"))
    if not plans_dir.exists():
        return 0
    for path in sorted(plans_dir.glob("*.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[skip] {path}: {exc}")
            continue
        goal_id = plan.get("plan_id") or plan.get("id") or path.stem
        if goal_id in existing:
            print(f"[skip] {path}: Goal {goal_id} 已迁移")
            continue
        todos = [
            {
                "id": t.get("id") or str(i),
                "title": t.get("title", ""),
                "status": t.get("status", "open"),
                "depends_on": list(t.get("depends_on") or []),
            }
            for i, t in enumerate(plan.get("todos") or [])
        ]
        if dry_run:
            print(f"[dry] would migrate {path} → Goal {goal_id} ({len(todos)} todos)")
            migrated += 1
            continue
        store.append(
            aggregate_type="Goal",
            aggregate_id=goal_id,
            event_type="GoalCreated",
            payload={"objective": plan.get("objective", ""), "todos": todos},
        )
        for todo in todos:
            if todo["status"] not in ("open",):
                store.append(
                    aggregate_type="Goal",
                    aggregate_id=goal_id,
                    event_type="TodoUpdated",
                    payload={"todo_id": todo["id"], "status": todo["status"]},
                )
        existing.add(goal_id)
        migrated += 1
        print(f"[ok] {path} → Goal {goal_id} ({len(todos)} todos)")
    return migrated


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="旧 plan JSON → loop-plane 事件")
    ap.add_argument("--plans-dir", default="~/.veya/plans", help="旧 plan_todo 目录")
    ap.add_argument("--data-dir", default="~/.veya/loop", help="loop-plane 数据根")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.infra.event_store import EventStore

    store = EventStore(Path(args.data_dir).expanduser(), tenant_id="default")
    n = migrate_plans(Path(args.plans_dir).expanduser(), store, dry_run=args.dry_run)
    print(f"完成: 迁移 {n} 个 plan" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
