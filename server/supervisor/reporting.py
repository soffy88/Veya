"""W12 Supervisor — progress report builder (spec A3, A4, A5).

No new scheduler. Reports are built from the existing GoalRun state and
posted via the routine turn pipeline. The only state added is in the
binding record (server.supervisor.binding).
"""

from __future__ import annotations

import re
import time
from typing import Any

from server.goal_run.models import GoalStatus
from server.goal_run.store import load_goal_run


def _evidence_report_enabled() -> bool:
    from server.feature_flags import enabled
    try:
        return bool(enabled("VEYA_EVIDENCE_REPORT"))
    except Exception:
        return False


def _extract_sources(state) -> list[str]:
    sources = set()
    url_re = re.compile(r"https?://[^\s]+")
    file_re = re.compile(r"\b[a-zA-Z0-9_/.-]+\.[a-zA-Z0-9]+\b")
    for task in state.tasks.values():
        texts = []
        if task.execute_result:
            texts.append(task.execute_result)
        for ev in task.evidence:
            if isinstance(ev, dict):
                texts.append(str(ev.get("content", "") or ev.get("locator", "")))
            else:
                texts.append(str(ev))
        for t in texts:
            for url in url_re.findall(t):
                sources.add(url)
            for f in file_re.findall(t):
                if "/" in f or f.endswith((".py", ".md", ".json", ".txt", ".yml", ".yaml")):
                    sources.add(f)
    return sorted(list(sources))


def _extract_guesses(state) -> list[str]:
    guesses = []
    for task in state.tasks.values():
        if task.execute_result and "assume" in task.execute_result.lower():
            guesses.append(task.execute_result[:100])
    if not guesses:
        guesses.append("（无未证实猜测）")
    return guesses


def _extract_skips(state) -> list[str]:
    skips = []
    for task in state.tasks.values():
        if task.status.value in ("blocked", "pending", "running") and task.status.value != "completed":
            skips.append(task.title)
    if not skips:
        skips.append("（无跳过项）")
    return skips


def build_progress_report(
    *,
    goal_run_id: str,
    project_root: str,
    binding,  # SupervisorBinding — import avoided to prevent cycle
    verifier_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a progress report from GoalRun state only.

    Returns a dict with keys:
      - goal_run_id, objective, status, report_seq, timestamp
      - tasks_done, tasks_total, tasks_blocked
      - last_activity_at, last_evidence
      - is_stalled, stall_reason
      - is_done
      - body: the human-readable report text

    Must not invent progress. If nothing changed since last report, say so.
    """

    state = load_goal_run(project_root, goal_run_id)
    if state is None:
        return {
            "goal_run_id": goal_run_id,
            "body": "找不到该任务的执行状态。",
            "is_done": False,
            "is_stalled": False,
            "report_seq": binding.report_seq,
        }

    total = len(state.tasks)
    done = sum(1 for t in state.tasks.values() if t.status.value == "completed")
    blocked = sum(1 for t in state.tasks.values() if t.status.value == "blocked")
    running = sum(1 for t in state.tasks.values() if t.status.value == "running")

    # Last evidence: from the most recently completed task
    last_evidence = None
    for task in sorted(state.tasks.values(), key=lambda t: t.id):
        if task.status.value == "completed" and task.execute_result:
            last_evidence = task.execute_result[:200]
            break

    # Last activity: when was the last task completed/started
    now = time.time()
    last_activity = binding.last_activity_at or now
    stall_after_s = binding.stall_after_minutes * 60

    is_stalled = False
    stall_reason = None
    if (
        state.status in (GoalStatus.running, GoalStatus.planning)
        and now - last_activity > stall_after_s
    ):
        is_stalled = True
        stall_reason = f"任务已 {binding.stall_after_minutes} 分钟无活动"

    # Check if verification passed (all tasks completed)
    all_completed = total > 0 and done == total and blocked == 0
    is_done = all_completed or state.status in (GoalStatus.completed, GoalStatus.finalizing)
    if verifier_evidence is not None:
        if verifier_evidence.get("usable") is True:
            is_done = True
        v_summary = verifier_evidence.get("summary") or verifier_evidence.get("details")
        if v_summary:
            last_evidence = str(v_summary)[:200]

    # Build body
    if _evidence_report_enabled():
        sources = _extract_sources(state)
        guesses = _extract_guesses(state)
        skips = _extract_skips(state)
        src_text = ", ".join(sources) if sources else "（无记录来源）"
        guess_text = "; ".join(guesses)
        skip_text = ", ".join(skips)
        body = (
            f"任务状态: {state.status.value}\n\n"
            f"【来源】\n- {src_text}\n\n"
            f"【猜测】\n- {guess_text}\n\n"
            f"【跳过】\n- {skip_text}"
        )
    elif is_done:
        body = f"任务完成！已完成 {done}/{total} 个子任务。"
        if last_evidence:
            body += f"\n\n最新证据：{last_evidence[:100]}..."
    elif is_stalled:
        body = f"任务卡住了：{stall_reason}"
        if state.last_stop_reason:
            body += f"\n阻塞原因：{state.last_stop_reason}"
    elif done == 0:
        body = f"任务刚开始（{total} 个子任务），尚未完成任何子任务。"
    else:
        body = f"进度：已完成 {done}/{total} 个子任务，运行中 {running} 个，阻塞 {blocked} 个。"

    return {
        "goal_run_id": goal_run_id,
        "objective": binding.objective,
        "status": state.status.value,
        "report_seq": binding.report_seq,
        "timestamp": now,
        "tasks_done": done,
        "tasks_total": total,
        "tasks_blocked": blocked,
        "last_activity_at": last_activity,
        "last_evidence": last_evidence,
        "is_stalled": is_stalled,
        "stall_reason": stall_reason,
        "is_done": is_done,
        "nudges_sent": binding.nudges_sent,
        "repairs_remaining": max(0, binding.max_repairs - binding.nudges_sent),
        "verifier": verifier_evidence,
        "deepeval": _deepeval_finish_check(is_done, binding.objective, last_evidence),
        "body": body,
    }


def _deepeval_finish_check(
    is_done: bool, objective: str, last_evidence: str | None
) -> dict[str, Any] | None:
    """W18: 完成时的可选 DeepEval 检查(flag VEYA_DEEPEVAL, 默认关)。

    关时(默认)返回 None, 报告结构与今天完全一致; 开时对「目标 vs 最新证据」
    跑一次 task_completion 作为**附加**字段。纯附加, 不改变 is_done。
    """
    if not is_done:
        return None
    try:
        from server.deepeval_adapter import supervisor_finish_score

        return supervisor_finish_score(objective, last_evidence)
    except Exception:
        return None
