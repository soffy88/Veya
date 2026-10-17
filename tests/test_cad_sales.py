"""Unit and regression tests for CAD sales bot (闲鱼CAD代画销转)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.cad_sales.engine import CadSalesEngine
from server.cad_sales.models import CadSalesRequest
from server.cad_sales.requirements import generate_order_folder
from server.cad_sales.trainer import CadSalesTrainer
from server.feature_flags import enabled
from server.role_skill_presets import load_roles, load_skills


def test_cad_sales_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_CAD_SALES_BOT", raising=False)
    assert enabled("VEYA_CAD_SALES_BOT") is False


def test_cad_sales_role_and_skill_presets_loaded() -> None:
    roles = load_roles()
    cad_roles = [r for r in roles if r.preset_id == "cad-sales"]
    assert len(cad_roles) == 1
    assert cad_roles[0].name == "CAD Sales Specialist"

    skills = load_skills()
    cad_skills = [s for s in skills if s.preset_id == "cad-sales"]
    assert len(cad_skills) == 1
    assert "7大要素" in cad_skills[0].body or "要素" in cad_skills[0].body


def test_http_endpoint_gated_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEYA_CAD_SALES_BOT", raising=False)
    client = TestClient(app, base_url="http://localhost")

    # Status works even when off
    res = client.get("/api/v1/cad-sales/status")
    assert res.status_code == 200
    assert res.json()["enabled"] is False

    # POST /reply fails with 409 when off
    res_off = client.post(
        "/api/v1/cad-sales/reply",
        json={"conversation": [{"role": "buyer", "content": "画图"}]},
    )
    assert res_off.status_code == 409
    assert "disabled" in res_off.json()["detail"]


def test_http_endpoint_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_CAD_SALES_BOT", "1")
    monkeypatch.setenv("VEYA_CAD_SALES_TOKEN", "t0k")
    client = TestClient(app, base_url="http://localhost", headers={"X-Veya-Token": "t0k"})

    res = client.get("/api/v1/cad-sales/status")
    assert res.status_code == 200
    assert res.json()["enabled"] is True

    # Tier A request
    body = {
        "conversation": [
            {
                "role": "buyer",
                "content": (
                    "我在万达租了个50平轻食奶茶店，物业催我交商铺平面布置报备图。"
                    "我有商场给的租赁红线图和手绘的吧台设备摆放尺寸，单位都是毫米标注清楚了，"
                    "需要转成dwg格式AutoCAD 2007版，后天上午前要交。麻烦看下能画吗？"
                ),
            }
        ],
        "attachments": ["rental_line.dwg"],
    }
    res_reply = client.post("/api/v1/cad-sales/reply", json=body)
    assert res_reply.status_code == 200
    data = res_reply.json()
    assert data["tier"] == "A"
    assert data["quote_range"] is not None
    assert "亲" in data["reply_draft"]
    assert data["missing_fields"] == []


def test_tier_c_out_of_scope_rejection() -> None:
    engine = CadSalesEngine()
    req = CadSalesRequest(
        conversation=[
            {
                "role": "buyer",
                "content": "帮我画消防图，需要甲级设计院盖章和注册消防工程师签字报审。",
            }
        ]
    )
    res = engine.analyze(req)
    assert res.tier == "C"
    assert res.quote_range is None
    assert res.needs_human is True
    assert "设计院资质盖章" in res.reply_draft or "盖章" in res.reply_draft
    assert "无法承接" in res.reply_draft


def test_tier_b_missing_fields_and_no_premature_quote() -> None:
    engine = CadSalesEngine()
    req = CadSalesRequest(
        conversation=[
            {
                "role": "buyer",
                "content": "老板发你4张毛坯房照片，你看下画平面图多少钱？",
            }
        ]
    )
    res = engine.analyze(req)
    assert res.tier == "B"
    assert res.quote_range is None
    assert "尺寸与单位" in res.missing_fields
    assert "照片" in res.reply_draft


def test_requirements_folder_generation() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        conv = [
            {
                "role": "buyer",
                "content": "我在万达租了个50平轻食奶茶店，物业催我交商铺平面布置报备图。我有商场给的租赁红线图和手绘的吧台设备摆放尺寸，单位都是毫米标注清楚了，需要转成dwg格式AutoCAD 2007版，后天上午前要交。",
            }
        ]
        res = generate_order_folder(
            conversation=conv,
            attachments=["floorplan.pdf"],
            output_root=tmpdir,
        )
        order_dir = Path(res.order_dir)
        assert (order_dir / "原始材料").is_dir()
        assert (order_dir / "需求分析").is_dir()
        assert (order_dir / "工程分析").is_dir()
        assert (order_dir / "制作过程").is_dir()
        assert (order_dir / "交付结果").is_dir()

        req_file = Path(res.requirements_file)
        assert req_file.is_file()
        text = req_file.read_text(encoding="utf-8")
        assert "## 1. 已确认项 (Confirmed)" in text
        assert "## 2. 有据可依项 (Evidenced)" in text
        assert "## 3. 严禁脑补项 (Must-Not-Guess)" in text
        assert "严禁脑补现场未量尺寸" in text


def test_simulation_trainer_exam() -> None:
    trainer = CadSalesTrainer()
    exam = trainer.run_exam()
    assert exam["total_cases"] == 15
    assert exam["average_score"] >= 90.0
    assert exam["passed_90"] is True


def test_http_requires_token_and_rejects_output_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEYA_CAD_SALES_BOT", "1")
    monkeypatch.delenv("VEYA_CAD_SALES_TOKEN", raising=False)
    client = TestClient(app, base_url="http://localhost")
    body = {"conversation": [{"role": "buyer", "content": "画个户型图"}]}
    assert client.post("/api/v1/cad-sales/reply", json=body).status_code == 503
    monkeypatch.setenv("VEYA_CAD_SALES_TOKEN", "t0k")
    assert client.post("/api/v1/cad-sales/reply", json=body).status_code == 401
    ok = client.post("/api/v1/cad-sales/reply", json=body, headers={"Authorization": "Bearer t0k"})
    assert ok.status_code == 200
    bad = client.post(
        "/api/v1/cad-sales/requirements",
        json={**body, "output_root": "/tmp/x"},
        headers={"X-Veya-Token": "t0k"},
    )
    assert bad.status_code == 400
    with pytest.raises(ValueError):
        from server.cad_sales.requirements import generate_order_folder

        generate_order_folder(conversation=[], order_id="../etc", output_root="/tmp")
