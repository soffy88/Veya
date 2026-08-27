"""engineering-flow 技能包 — 工程工作流编排 (mattpocock idea→ship 3O 内化)。

把 oskill.workflow_pipeline 阶段机 + requirements_interview + review_double_axis
串成 skill_hub 可调用的动作面。状态 JSON 持久化到 ~/.veya/loops/<flow_id>.json
(纯投影; 若需事件溯源后端可换 veya_loop GoalKernel + AppendOnlyEventStore)。

动作面 (见 manifest.json):
  start(idea) → next → advance(...) → ticket_done(...) → review(diff) → status
零 veya 反向依赖: 经 veya_loop 装配 oskill 原语 (3O 主库, 缺失时报错降级)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from veya_loop import (
    InterviewQuestion,
    InterviewState,
    WorkflowState,
    pipeline_next_action,
    pipeline_transition,
    record_interview_answers,
    review_diff,
    select_rulebooks,
    standards_rules,
    ticket_set_status,
    tickets_check_cycles,
    workflow_from_dict,
    workflow_to_dict,
)

_LOOPS_DIR = Path.home() / ".veya" / "loops"


def _state_path(flow_id: str) -> Path:
    return _LOOPS_DIR / f"{flow_id}.json"


def _load(flow_id: str) -> WorkflowState:
    path = _state_path(flow_id)
    if not path.exists():
        return WorkflowState()
    return workflow_from_dict(json.loads(path.read_text(encoding="utf-8")))


def _save(flow_id: str, state: WorkflowState) -> None:
    path = _state_path(flow_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(workflow_to_dict(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w]+", "-", text.lower()).strip("-")
    return slug[:48] or "flow"


def _interview_from_json(raw: str) -> InterviewState:
    data = json.loads(raw)
    state = InterviewState()
    for qd in data:
        state.add_question(
            InterviewQuestion(
                id=qd["id"],
                title=qd.get("title", qd["id"]),
                body=qd.get("body", ""),
                options=qd.get("options"),
                recommended=qd.get("recommended", ""),
                depends_on=list(qd.get("depends_on", [])),
                facts_needed=list(qd.get("facts_needed", [])),
            )
        )
    return state


def main(
    action: str,
    flow_id: str = "",
    idea: str = "",
    event: str = "",
    spec: str = "",
    tickets_json: str = "",
    ticket_id: str = "",
    diff: str = "",
    interview_questions_json: str = "",
    answers_json: str = "",
    **_: Any,
) -> dict[str, Any]:
    """执行工程工作流动作, 返回结构化结果。"""
    if action == "start":
        fid = flow_id or _slug(idea)
        state = WorkflowState(idea=idea)
        _save(fid, state)
        return {"flow_id": fid, "stage": state.stage, "next": pipeline_next_action(state).action}

    fid = flow_id
    if not fid:
        return {"ok": False, "error": "flow_id 必填 (start 返回的 flow_id)"}
    state = _load(fid)

    if action == "status":
        return {
            "ok": True,
            "stage": state.stage,
            "idea": state.idea,
            "next_action": pipeline_next_action(state).action,
            "tickets": [
                {"id": t.id, "title": t.title, "status": t.status, "blocked_by": t.blocked_by}
                for t in state.tickets
            ],
        }

    if action == "next":
        nxt = pipeline_next_action(state)
        payload = None
        if nxt.action == "ask_questions" and state.interview is not None:
            from veya_loop import interview_frontier

            payload = [
                {
                    "id": q.id,
                    "title": q.title,
                    "body": q.body,
                    "options": q.options,
                    "recommended": q.recommended,
                }
                for q in interview_frontier(state.interview)
            ]
        elif nxt.action == "implement_ticket" and nxt.payload is not None:
            payload = {"ticket_id": nxt.payload.id, "title": nxt.payload.title}
        return {
            "ok": True,
            "stage": state.stage,
            "action": nxt.action,
            "hint": nxt.hint,
            "payload": payload,
        }

    if action == "advance":
        try:
            if event == "run_interview":
                state = pipeline_transition(state, "run_interview")
                if interview_questions_json:
                    state.interview = _interview_from_json(interview_questions_json)
            elif event == "record_answers":
                if state.interview is None:
                    return {"ok": False, "error": "先 run_interview 注入问题"}
                record_interview_answers(state.interview, json.loads(answers_json or "{}"))
                from veya_loop import is_interview_complete

                if is_interview_complete(state.interview):
                    state = pipeline_transition(state, "interview_done")
            elif event == "spec_written":
                state = pipeline_transition(state, "spec_written", spec=spec)
            elif event == "tickets_split":
                tickets = [
                    {
                        "id": t["id"],
                        "title": t.get("title", t["id"]),
                        "blocked_by": list(t.get("blocked_by", [])),
                    }
                    for t in json.loads(tickets_json or "[]")
                ]
                cycles = tickets_check_cycles(
                    [
                        type("T", (), {"id": t["id"], "blocked_by": t["blocked_by"]})()
                        for t in tickets
                    ]  # type: ignore[attr-defined]
                )
                if cycles:
                    return {"ok": False, "error": f"tickets 依赖环: {cycles}"}
                from veya_loop import Ticket

                state = pipeline_transition(
                    state,
                    "tickets_split",
                    tickets=[Ticket(**t) for t in tickets],
                )
            elif event == "review_done":
                state = pipeline_transition(state, "review_done", review={})
            else:
                return {"ok": False, "error": f"未知事件: {event}"}
        except Exception as exc:
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
        _save(fid, state)
        nxt = pipeline_next_action(state)
        return {"ok": True, "stage": state.stage, "next": nxt.action, "hint": nxt.hint}

    if action == "ticket_done":
        state = pipeline_transition(state, "ticket_done", ticket_id=ticket_id)
        _save(fid, state)
        nxt = pipeline_next_action(state)
        return {
            "ok": True,
            "stage": state.stage,
            "next": nxt.action,
            "payload": {"ticket_id": getattr(nxt.payload, "id", None)},
        }

    if action == "review":
        baseline = standards_rules(task=state.idea or "code review", top_k=2)
        report = review_diff(diff, standards_rules=str(baseline))
        return {
            "ok": report.ok,
            "stage": state.stage,
            "fail": [f.rule for f in report.fails()],
            "warn": [f.rule for f in report.warns()],
            "rulebooks": baseline["books"],
            "findings": [
                {"axis": f.axis, "severity": f.severity, "rule": f.rule, "detail": f.detail}
                for f in report.findings
            ],
        }

    return {"ok": False, "error": f"未知 action: {action}"}
