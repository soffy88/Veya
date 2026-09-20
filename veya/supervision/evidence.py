"""Evidence layer — project the execution authority's state into ExecutionReport.

The Evidence Layer never executes anything and never trusts an executor's own
"PASS": it normalizes observable state (task graph, artifacts, verify results,
block reasons) into the canonical report the reviewer reads (spec §6/§10).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .models import ExecutionReport, Mission

# ── text signals (the canonical dispatch returns text, not a task graph) ──
_CREATE_VERB_RE = re.compile(
    r"(creat|wrote|written|saved|added|generated|\u521b\u5efa|\u5199\u5165|\u751f\u6210|\u4ea7\u51fa)",
    re.I,
)
_TEST_LINE_RE = re.compile(
    r"^\s*\$?\s*(python\s+-m\s+pytest|pytest|unittest|ruff|mypy|npm|pnpm|yarn|jest|go\s+test|cargo\s+test)\b.*$",
    re.I | re.M,
)
_FAILURE_RE = re.compile(
    r"(\u26d4|\bblocked\b|VERDICT:\s*failed|Traceback \(most recent call last\)|^\s*ERROR[: ]|\bcommand not found\b|\bPermission denied\b|\bfailed\b)",
    re.I | re.M,
)
_PATH_RE = re.compile(r"((?:/|\.{1,2}/)?[\w.-]+(?:/[\w.-]+)*\.[A-Za-z0-9]{1,8})")
_PATH_EXT = {
    "txt",
    "md",
    "py",
    "json",
    "yml",
    "yaml",
    "toml",
    "ts",
    "js",
    "tsx",
    "jsx",
    "sh",
    "cfg",
    "ini",
    "log",
    "csv",
    "html",
    "css",
    "sql",
    "rs",
    "go",
    "java",
    "lock",
}
_MAX_ITEMS = 50


def _text_of(state: Any) -> str:
    for attr in ("final_summary", "executor_summary", "summary"):
        value = getattr(state, attr, None)
        if value:
            return str(value)
    return ""


def _check_path(raw: str, workspace: str) -> bool:
    """Does this path really exist on disk? (no trust in the executor's claim)"""
    candidate = Path(raw)
    if not candidate.is_absolute():
        if not workspace:
            return False
        candidate = Path(workspace) / candidate
    try:
        return candidate.is_file()
    except OSError:
        return False


def _project_text_evidence(
    text: str, workspace: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract artifacts / tests / failures / runtime evidence from dispatch output.

    An artifact only counts when it exists on disk; a path that the executor
    *claims* to have created but which is absent is recorded as a failure, never
    as evidence. That is what keeps the report from carrying false evidence.
    """
    artifacts: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    runtime: list[dict[str, Any]] = []
    if not text.strip():
        return artifacts, tests, failures, runtime

    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if not _TEST_LINE_RE.match(line.strip()):
            continue
        entry: dict[str, Any] = {"command": line.strip()[:300], "source": "executor_text"}
        # the pass/fail summary usually lands on one of the following lines
        window = "\n".join(lines[idx : idx + 4])
        counts = re.search(r"(\d+)\s+passed", window, re.I)
        if counts:
            entry["passed"] = int(counts.group(1))
        failed = re.search(r"(\d+)\s+failed", window, re.I)
        if failed:
            entry["failed"] = int(failed.group(1))
        tests.append(entry)

    seen: set[str] = set()
    for line in text.splitlines():
        for raw in _PATH_RE.findall(line):
            ext = raw.rsplit(".", 1)[-1].lower()
            if "/" not in raw and ext not in _PATH_EXT:
                continue
            if raw in seen or len(seen) >= _MAX_ITEMS:
                continue
            seen.add(raw)
            exists = _check_path(raw, workspace)
            if exists:
                artifacts.append({"path": raw, "verified": True, "source": "executor_text"})
            elif _CREATE_VERB_RE.search(line):
                failures.append(
                    {
                        "kind": "artifact_missing",
                        "path": raw,
                        "reason": "executor claimed this file but it is absent on disk",
                    }
                )

    if _FAILURE_RE.search(text):
        for line in text.splitlines():
            if _FAILURE_RE.search(line):
                failures.append({"kind": "text_signal", "detail": line.strip()[:300]})
                if len(failures) > 20:
                    break

    runtime.append(
        {
            "source": "canonical_runner",
            "kind": "executor_text",
            "chars": len(text),
            "excerpt": text[:400],
        }
    )
    return artifacts, tests, failures, runtime


def _git_changes(workspace: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Objective change set: what actually changed in the workspace (read-only)."""
    if not workspace or not (Path(workspace) / ".git").exists():
        return [], {}
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return [], {}
    if proc.returncode != 0:
        return [], {}
    changes: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines()[:_MAX_ITEMS]:
        code, _, path = line.partition(" ")
        path = path.strip()
        # `.veya-project/` is supervision bookkeeping, not the mission's work product.
        if not path or path.startswith(".veya-project/") or "/.veya-project/" in path:
            continue
        changes.append({"path": path, "status": code.strip(), "source": "git"})
    summary = {"changed": len(changes)} if changes else {}
    return changes, summary


def _status_of(node: Any) -> str:
    raw = getattr(node, "status", None)
    return str(getattr(raw, "value", raw) or "").lower()


def _iter_tasks(goalrun_state: Any) -> dict[str, Any]:
    tasks = getattr(goalrun_state, "tasks", None)
    return dict(tasks) if isinstance(tasks, dict) else {}


def build_execution_report(
    mission: Mission,
    goalrun_state: Any,
    *,
    iteration: int,
    checkpoint_id: str | None = None,
    objective: str | None = None,
    jev_decisions: list[dict[str, Any]] | None = None,
    proposed_next_action: str | None = None,
    git_diff_summary: dict[str, Any] | None = None,
) -> ExecutionReport:
    """Build one canonical report from the durable GoalRun state."""

    tasks = _iter_tasks(goalrun_state)
    changes: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    runtime_evidence: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    risks: list[dict[str, Any]] = []
    deviations: list[dict[str, Any]] = []

    for task_id, node in tasks.items():
        status = _status_of(node)
        title = str(getattr(node, "title", "") or "")
        assignee = str(getattr(node, "assignee", "") or "")
        entry = {"task_id": task_id, "title": title, "assignee": assignee, "status": status}
        changes.append(entry)

        for path in getattr(node, "artifacts", []) or []:
            artifacts.append({"task_id": task_id, "path": str(path)})

        acceptance = list(getattr(node, "acceptance", []) or [])
        verify_summary = getattr(node, "verify_summary", None)
        if acceptance or verify_summary:
            tests.append(
                {
                    "task_id": task_id,
                    "acceptance": [str(a) for a in acceptance],
                    "verify_summary": verify_summary,
                    "status": status,
                }
            )

        runtime_evidence.extend(
            {"task_id": task_id, **dict(item)}
            if isinstance(item, dict)
            else {"task_id": task_id, "value": item}
            for item in (getattr(node, "evidence", []) or [])
        )

        block_reason = getattr(node, "block_reason", None)
        if status in {"failed", "blocked", "error"}:
            failures.append({"task_id": task_id, "reason": block_reason or status})
        if status == "blocked" and block_reason:
            blocked.append({"task_id": task_id, "reason": block_reason})
        if getattr(node, "retries", 0):
            deviations.append({"task_id": task_id, "retries": int(getattr(node, "retries", 0))})
        if getattr(node, "unfinished_work", None):
            risks.append({"task_id": task_id, "unfinished_work": list(node.unfinished_work)})

    status = str(getattr(goalrun_state, "status", "") or "")
    status = str(getattr(status, "value", status) or status)
    summary = getattr(goalrun_state, "final_summary", None) or ""
    unfinished = list(getattr(goalrun_state, "unfinished_work", []) or [])
    blocked.extend({"task_id": "", "reason": str(item)} for item in unfinished)

    # ── dispatch projection: the canonical runner returns text, not a task graph ──
    text = _text_of(goalrun_state)
    extra_artifacts, extra_tests, extra_failures, extra_runtime = _project_text_evidence(
        text, str(mission.workspace or "")
    )
    artifacts.extend(extra_artifacts)
    tests.extend(extra_tests)
    failures.extend(extra_failures)
    runtime_evidence.extend(extra_runtime)

    git_changes, git_summary = _git_changes(str(mission.workspace or ""))
    if git_changes:
        known = {str(entry.get("path")) for entry in changes}
        changes.extend(item for item in git_changes if item.get("path") not in known)
    blocked.extend(
        {"task_id": "", "reason": str(item.get("detail") or item.get("reason") or "")}
        for item in extra_failures
        if item.get("kind") == "text_signal" and "block" in str(item.get("detail", "")).lower()
    )

    return ExecutionReport(
        mission_id=mission.mission_id,
        goalrun_id=str(getattr(goalrun_state, "goal_id", "") or "") or None,
        iteration=iteration,
        checkpoint_id=checkpoint_id,
        objective=objective or mission.goal,
        status=status,
        changes=changes,
        tests=tests,
        artifacts=artifacts,
        runtime_evidence=runtime_evidence,
        git_diff_summary=dict(git_diff_summary or git_summary),
        failures=failures,
        unresolved_risks=risks,
        deviations=deviations,
        blocked_items=blocked,
        jev_decisions=list(jev_decisions or []),
        executor_summary=str(summary) or f"{len(tasks)} task(s), status={status}",
        proposed_next_action=proposed_next_action
        or (None if not failures and not blocked else "revise"),
    )


__all__ = ["build_execution_report"]
