"""Layer 4 boss entrypoint uses injected workers, not a live LLM."""

from __future__ import annotations

import pytest

from server.boss_entrypoint import project_run_goal_boss_mode


async def _caller(*, messages, max_tokens):
    joined = " ".join(str(m.get("content") or "") for m in messages)
    if "意图分诊官" in joined:
        return {
            "ok": True,
            "action": "plan",
            "interpretation": "add foo to a.py",
            "in_scope_files": ["a.py"],
            "out_of_scope_files": ["b.py"],
            "acceptance_draft": ["git diff contains foo"],
            "questions": [],
            "reasons": ["clear"],
        }
    if "Acceptance" in joined or "QA" in joined:
        return {"ok": True, "passed": True, "reasoning": "ok"}
    return {
        "ok": True,
        "tasks": [
            {
                "id": "T1",
                "title": "Patch",
                "files": ["a.py"],
                "logic": "add foo",
                "forbidden": ["do not touch b.py"],
                "instruction": "edit a.py; add foo; do not touch b.py",
                "acceptance": ["git diff contains foo"],
                "depends_on": [],
                "assignee": "hicode",
            }
        ],
    }


async def _leaf(*, project_root, instruction, assignee="hicode", **kwargs):
    return {"status": "completed", "git_diff": "+def foo", "stdout": "ok"}


@pytest.mark.asyncio
async def test_boss_mode_completes(tmp_path) -> None:
    rec = await project_run_goal_boss_mode(
        str(tmp_path),
        "add foo",
        goal_id="g-layer4",
        llm_caller=_caller,
        leaf_executor=_leaf,
        output_dir=tmp_path / "out",
    )
    assert rec["status"] == "completed"
    assert rec["completed"] == ["T1"]
