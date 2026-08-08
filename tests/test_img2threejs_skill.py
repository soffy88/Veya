"""img2threejs 技能 (templates/skills/img2threejs/run.py) 状态机/门控测试。

验证技能对 3O 主库原语的装配: init 状态机 → status → gate (确定性视觉门)
→ review (VLM 共识, 无 sampler 明确提示) → html (无 factory 明确错误)
→ mark 越序被状态机拒绝。使用临时工作目录, 不污染技能目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1] / "templates" / "skills" / "img2threejs"
sys.path.insert(0, str(SKILL_DIR))

from run import main  # noqa: E402


@pytest.fixture()
def ref_image(tmp_path):
    from PIL import Image

    img = Image.new("RGB", (128, 128), (240, 240, 240))
    px = img.load()
    for y in range(32, 96):
        for x in range(32, 96):
            px[x, y] = (180, 40, 40)
    path = tmp_path / "ref.png"
    img.save(path)
    return str(path)


def test_init_creates_pipeline_state(ref_image, tmp_path):
    r = main(reference=ref_image, goal="重建方块", action="init", workdir=str(tmp_path))
    assert r["ok"] is True
    assert r["currentStep"] == "image-analysis"
    assert (tmp_path / ".img2threejs" / "state.json").is_file()


def test_init_refuses_overwrite(ref_image, tmp_path):
    main(reference=ref_image, goal="g", action="init", workdir=str(tmp_path))
    r = main(reference=ref_image, goal="g", action="init", workdir=str(tmp_path))
    assert r["note"] and "拒绝覆盖" in r["note"]


def test_status_reports_next_step(ref_image, tmp_path):
    main(reference=ref_image, goal="g", action="init", workdir=str(tmp_path))
    r = main(reference=ref_image, action="status", workdir=str(tmp_path))
    assert r["currentStep"] == "image-analysis"
    assert r["nextCommand"]


def test_status_without_state_fails(ref_image, tmp_path):
    r = main(reference=ref_image, action="status", workdir=str(tmp_path))
    assert r["ok"] is False and "状态不存在" in r["error"]


def test_gate_deterministic_vision_pass(ref_image, tmp_path):
    main(reference=ref_image, goal="g", action="init", workdir=str(tmp_path))
    # 渲染图 = 参考图 → IoU 1.0 pass
    r = main(reference=ref_image, action="gate", render=ref_image, workdir=str(tmp_path))
    assert r["passed"] is True
    assert r["checks"]["silhouetteIoU"] >= 0.8


def test_review_requires_sampler(ref_image, tmp_path):
    main(reference=ref_image, goal="g", action="init", workdir=str(tmp_path))
    r = main(reference=ref_image, action="review", render=ref_image, workdir=str(tmp_path))
    assert r["skipped"] is True
    assert "vlm_sampler" in r["reason"]


def test_html_requires_factory(ref_image, tmp_path):
    main(reference=ref_image, goal="g", action="init", workdir=str(tmp_path))
    r = main(reference=ref_image, action="html", workdir=str(tmp_path))
    assert r["ok"] is False and "factory 不存在" in r["error"]


def test_mark_out_of_order_rejected(ref_image, tmp_path):
    main(reference=ref_image, goal="g", action="init", workdir=str(tmp_path))
    r = main(reference=ref_image, action="mark", step_id="spec-authoring", evidence="x", workdir=str(tmp_path))
    assert r["ok"] is False and "out-of-order" in r["error"]


def test_mark_in_order_with_evidence(ref_image, tmp_path):
    main(reference=ref_image, goal="g", action="init", workdir=str(tmp_path))
    r = main(reference=ref_image, action="mark", step_id="image-analysis", evidence="analysis.md", workdir=str(tmp_path))
    assert r["ok"] is True
    assert r["currentStep"] == "reference-suitability"
