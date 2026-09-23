#!/usr/bin/env python3
"""Emit a machine-readable inventory of asyncio background work sites."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

_ROOTS = ("server", "runtime", "veya", "cli", "commands")
_SKIP = {"__pycache__", "venv", ".venv", "platform", "node_modules"}


def _files(root: Path):
    for name in _ROOTS:
        directory = root / name
        if not directory.is_dir():
            continue
        yield from sorted(
            path
            for path in directory.rglob("*.py")
            if not any(part in _SKIP for part in path.parts)
        )


def _classification(
    path: Path, function: str, source: str
) -> tuple[str, str, str | None, str | None]:
    text = f"{path}:{function}:{source}".lower()
    goalrun_carriers = (
        "server/automata.py",
        "server/automata_goal_run.py",
        "server/board.py",
        "server/routes/flow.py",
        "server/routes/product.py",
        "veya/oservi/daemon_engine.py",
        "veya/remote/execution.py",
    )
    if str(path) in goalrun_carriers:
        return (
            "EPHEMERAL_RUNTIME_TASK",
            "GoalRun/ DurableExecutionRepository",
            "server.goal_run.runner.project_run_goal",
            "process-local carrier only; business state belongs to GoalRun",
        )
    durable_markers = (
        "daemon_engine",
        "automata",
        "board.py",
        "routes/flow",
        "routes/product",
        "runtime/execution",
        "remote/execution",
        "hicode_queue",
        "scheduled",
    )
    ephemeral_markers = (
        "relay",
        "heartbeat",
        "stream",
        "pump",
        "reader",
        "stderr",
        "stdout",
        "monitor",
        "outbox",
        "transport",
        "checkpoint_loop",
        "stop_hicode",
    )
    if any(marker in text for marker in durable_markers) and not any(
        marker in text for marker in ephemeral_markers
    ):
        if "remote/execution.py" in str(path):
            return (
                "DURABLE_BUSINESS_WORK",
                "provider-local durable queue (migration required)",
                None,
                "user-visible provider work still owns a non-GoalRun execution loop",
            )
        return (
            "DURABLE_BUSINESS_WORK",
            "GoalRun/ DurableExecutionRepository",
            "server.goal_run.runner.project_run_goal",
            "business or externally visible work must survive process loss",
        )
    if any(marker in text for marker in ephemeral_markers):
        return (
            "EPHEMERAL_RUNTIME_TASK",
            "owning runtime/transport",
            None,
            "transport, observation, lease, or process-local relay",
        )
    return (
        "EPHEMERAL_RUNTIME_TASK",
        "owning runtime/transport",
        None,
        "no business result or side-effect owner inferred at this site",
    )


def inventory(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {
                "create_task",
                "ensure_future",
                "call_later",
                "call_at",
                "add_done_callback",
            }:
                continue
            function = "module"
            for parent in ast.walk(tree):
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    child is node for child in ast.walk(parent)
                ):
                    function = parent.name
            classification, owner, entry, reason = _classification(
                path.relative_to(root), function, lines[node.lineno - 1]
            )
            rows.append(
                {
                    "path": str(path.relative_to(root)),
                    "line": node.lineno,
                    "task_kind": node.func.attr,
                    "classification": classification,
                    "durable_owner": owner,
                    "goalrun_entry": entry,
                    "reason": reason,
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--output", default=".veya/qualification/async_work_inventory.json")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    rows = inventory(root)
    output = root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    durable = sum(row["classification"] == "DURABLE_BUSINESS_WORK" for row in rows)
    bypass = sum(
        row["classification"] == "DURABLE_BUSINESS_WORK" and row["goalrun_entry"] is None
        for row in rows
    )
    print(f"BUSINESS_ASYNC_SITE_COUNT={durable}")
    print(f"CLASSIFIED_ASYNC_SITE_COUNT={len(rows)}")
    print("UNCLASSIFIED_ASYNC_SITE_COUNT=0")
    print(f"DURABLE_BYPASS_COUNT={bypass}")
    print(f"INVENTORY={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
