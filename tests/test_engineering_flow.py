"""engineering-flow skill 包测试 (mattpocock idea→ship 编排)。

skill 包由 skill_hub 按路径加载 (非包导入), 测试同样按路径 importlib 加载。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_RUN_PATH = Path(__file__).resolve().parents[1] / "templates" / "skills" / "engineering-flow" / "run.py"


def _load_main() -> object:
    spec = importlib.util.spec_from_file_location("engineering_flow_run", _RUN_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


@pytest.fixture()
def run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """把 ~/.veya/loops 重定向到 tmp, 返回 main 调用器。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return _load_main()


def test_full_flow(run) -> None:  # noqa: ANN001
    r = run("start", idea="做个平台")
    fid = r["flow_id"]
    assert r["stage"] == "IDEA"
    assert fid

    r = run("advance", flow_id=fid, event="run_interview",
            interview_questions_json=json.dumps([
                {"id": "q1", "title": "目标", "body": "做什么?", "options": ["A"], "recommended": "A"},
                {"id": "q2", "title": "深度", "body": "做多深?", "depends_on": ["q1"]},
            ]))
    assert r["stage"] == "GRILLING"
    assert run("next", flow_id=fid)["payload"][0]["id"] == "q1"

    run("advance", flow_id=fid, event="record_answers", answers_json='{"q1":"A"}')
    assert run("next", flow_id=fid)["payload"][0]["id"] == "q2"
    run("advance", flow_id=fid, event="record_answers", answers_json='{"q2":"浅"}')
    assert run("next", flow_id=fid)["action"] == "write_spec"

    run("advance", flow_id=fid, event="spec_written", spec="spec")
    assert run("next", flow_id=fid)["action"] == "split_tickets"

    run("advance", flow_id=fid, event="tickets_split",
        tickets_json='[{"id":"t1","title":"a"},{"id":"t2","title":"b","blocked_by":["t1"]}]')
    assert run("next", flow_id=fid)["payload"]["ticket_id"] == "t1"
    run("ticket_done", flow_id=fid, ticket_id="t1")
    assert run("next", flow_id=fid)["payload"]["ticket_id"] == "t2"
    run("ticket_done", flow_id=fid, ticket_id="t2")

    review = run("review", flow_id=fid, diff='+    api_key = "sk-123456789"\n')
    assert review["fail"] == ["hardcoded_secret"]


def test_state_persists_across_calls(run, tmp_path: Path) -> None:  # noqa: ANN001
    r = run("start", idea="persist")
    fid = r["flow_id"]
    run("advance", flow_id=fid, event="run_interview",
        interview_questions_json=json.dumps([{"id": "q1", "title": "t", "body": "b"}]))
    run("advance", flow_id=fid, event="record_answers", answers_json='{"q1":"x"}')
    run("advance", flow_id=fid, event="spec_written", spec="s")
    state_file = tmp_path / "home" / ".veya" / "loops" / f"{fid}.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["stage"] == "TICKETS"
    assert data["spec"] == "s"


def test_cycle_rejected(run) -> None:  # noqa: ANN001
    r = run("start", idea="cycle")
    fid = r["flow_id"]
    run("advance", flow_id=fid, event="run_interview",
        interview_questions_json=json.dumps([{"id": "q1", "title": "t", "body": "b"}]))
    run("advance", flow_id=fid, event="record_answers", answers_json='{"q1":"x"}')
    run("advance", flow_id=fid, event="spec_written", spec="s")
    r = run("advance", flow_id=fid, event="tickets_split",
            tickets_json='[{"id":"t1","blocked_by":["t2"]},{"id":"t2","blocked_by":["t1"]}]')
    assert r["ok"] is False
    assert "依赖环" in r["error"]
