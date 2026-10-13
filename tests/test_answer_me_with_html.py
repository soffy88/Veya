"""answer-me-with-html 技能集成测试 — 技能包 / SkillHub 加载 / 渲染交付。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).resolve().parent.parent / "templates" / "skills"
AMH_DIR = SKILLS_DIR / "answer-me-with-html"
AM_DRAFT = """---
title: Test Page
---

## A Setup
SYN: client to server.

## B Ack
SYN-ACK: server to client.

## C Done
ACK: client to server.
"""


def _load_run():
    spec = importlib.util.spec_from_file_location("amh_run", AMH_DIR / "run.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


# =========================================================================
# 技能包
# =========================================================================


def test_answer_me_with_html_manifest_valid():
    manifest = json.loads((AMH_DIR / "manifest.json").read_text())
    assert manifest["name"] == "answer-me-with-html"
    assert manifest["type"] == "python"
    assert manifest["entrypoint"] == "run.py"
    props = manifest["parameters"]["properties"]
    assert "draft" in props
    assert manifest["parameters"]["required"] == ["draft"]
    assert (AMH_DIR / "run.py").is_file()
    assert (AMH_DIR / "SKILL.md").is_file()
    # vendored upstream CLI
    assert (AMH_DIR / "scripts" / "am.mjs").is_file()


def test_skill_hub_loads_answer_me_with_html(monkeypatch):
    """skill_hub 热载 (subprocess 是它的职责, 需进信任名单)。"""
    monkeypatch.setenv("VEYA_SKILL_TRUSTED_NAMES", "answer-me-with-html")
    from server.skill_hub import VeyaSkillHub

    hub = VeyaSkillHub(skills_dir=str(SKILLS_DIR))
    result = hub.reload_skills()
    assert result["loaded"] >= 3
    assert hub.has("answer-me-with-html")


def test_answer_me_with_html_draft_validation():
    mod = _load_run()
    with pytest.raises(ValueError, match="draft is empty"):
        mod.main(draft="")
    with pytest.raises(ValueError, match="at least one heading"):
        mod.main(draft="no headings here")


def test_answer_me_with_html_theme_validation():
    mod = _load_run()
    with pytest.raises(ValueError, match="theme"):
        mod.main(draft="## x", theme="nope")


# =========================================================================
# 端到端渲染 (需要 Node 20+)
# =========================================================================


def test_answer_me_with_html_render_e2e(tmp_path, monkeypatch):
    """渲染 TCP 三次握手草稿 → 单文件 HTML + 可访问 URL。"""
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node 未安装, 跳过")

    monkeypatch.setenv("VEYA_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("VEYA_BASE_URL", "http://127.0.0.1:8767")
    mod = _load_run()

    out = mod.main(draft=AM_DRAFT, style="off")

    assert out["ok"] is True
    html_path = Path(out["html_path"])
    assert html_path.is_file()
    assert html_path.parent == tmp_path / "answer-me-html"
    assert html_path.stat().st_size > 1000
    # 交付方式: 服务链接 (非 file://)
    assert out["file_url"] == f"http://127.0.0.1:8767/api/v1/h/{html_path.name}"
    content = html_path.read_text(encoding="utf-8")
    assert "TCP" in content or "三次握手" in content or "Setup" in content


def test_answer_me_with_html_render_no_base_url_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("VEYA_BASE_URL", "")
    mod = _load_run()
    out = mod.main(draft=AM_DRAFT, style="off")
    assert out["ok"] is True
    assert out["file_url"].startswith("file://")


# =========================================================================
# HTTP 交付 (server.routes.html_pages)
# =========================================================================


def test_html_pages_route_serves_rendered_page(tmp_path, monkeypatch):
    """GET /api/v1/h/<name> 返回渲染好的页面。"""
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node 未安装, 跳过")

    monkeypatch.setenv("VEYA_ARTIFACTS_DIR", str(tmp_path))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.routes.html_pages import public_router

    app = FastAPI()
    app.include_router(public_router)

    mod = _load_run()
    out = mod.main(draft=AM_DRAFT, style="off")
    assert out["ok"] is True

    client = TestClient(app)
    name = Path(out["html_path"]).name
    resp = client.get(f"/api/v1/h/{name}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert resp.text


def test_html_pages_route_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_ARTIFACTS_DIR", str(tmp_path))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.routes.html_pages import public_router

    app = FastAPI()
    app.include_router(public_router)
    client = TestClient(app)
    resp = client.get("/api/v1/h/../etc/passwd")
    assert resp.status_code in (400, 404)
