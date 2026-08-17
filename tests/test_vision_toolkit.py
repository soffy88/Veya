"""tests/test_vision_toolkit.py — 3O 视觉工具链 smoke 测试。

只测 L1/L2/L3 纯逻辑 (PIL 本地操作 + 文本合并), 不碰真实视觉 API:
- 区域解析 / 裁剪 / 像素差分 / 主色 / 前景提取 / 描摹 / 长图切分 / 文本合并。
外部 API 的路径走 unittest.mock (vision_chat 打桩)。
"""

from __future__ import annotations

import pytest

from veya.oprim import vision_ops as ops
from veya.oprim.vision_ops import (
    _HAS_PIL,
    Image,
)

pytestmark = pytest.mark.skipif(not _HAS_PIL, reason="需要 Pillow")


def _img(w=64, h=32, color=(200, 200, 200)):
    return Image.new("RGB", (w, h), color)


# ── 区域解析 ────────────────────────────────────────────────────────
def test_parse_region_clamps():
    assert ops.parse_region("10,10,40,30", 64, 32) == (10, 10, 40, 30)
    assert ops.parse_region("-5,-5,99,99", 64, 32) == (0, 0, 64, 32)
    with pytest.raises(ValueError):
        ops.parse_region("5,5,5,5", 64, 32)


# ── 裁剪 ────────────────────────────────────────────────────────────
def test_crop_bytes_scale(tmp_path):
    img = _img()
    data = ops.crop_bytes(img, (0, 0, 32, 16), scale=2)
    f = tmp_path / "c.png"
    f.write_bytes(data)
    assert ops.load_rgb(f).size == (64, 32)


# ── 像素差分 ────────────────────────────────────────────────────────
def test_pixel_diff_identical_and_changed(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    _img().save(a)
    result = ops.pixel_diff(a, a)
    assert result["overall_difference_pct"] == 0.0
    # 右半变红
    img = _img()
    img.paste((255, 0, 0), (32, 0, 64, 32))
    img.save(b)
    result = ops.pixel_diff(a, b, grid=2, top=2)
    assert result["scaled"] is False
    assert 25 < result["overall_difference_pct"] < 35
    assert len(result["worst_regions"]) == 2
    assert result["worst_regions"][0]["box"] == {"x1": 32, "y1": 0, "x2": 64, "y2": 16}
    assert len(result["heatmap_bytes"]) > 100


# ── 主色 ────────────────────────────────────────────────────────────
def test_dominant_colors():
    img = Image.new("RGB", (60, 40), (10, 10, 10))
    img.paste((250, 0, 0), (0, 0, 30, 40))
    result = ops.dominant_colors(img, (0, 0, 60, 40), top=3)
    assert result["colors"][0]["color"] == "#0A0A0A"
    assert result["colors"][0]["share_pct"] > 49
    assert result["colors"][1]["color"] == "#FA0000"


def test_score_candidates_winner():
    img = Image.new("RGB", (50, 50), (245, 245, 245))
    result = ops.score_color_candidates(
        img, (0, 0, 50, 50), ["#F5F5F5", "#000000"], tolerance=14
    )
    assert result["winner"] == "#F5F5F5"
    assert result["matched_within_tolerance"] is True


# ── 前景提取 ────────────────────────────────────────────────────────
def test_extract_foreground_manual():
    img = Image.new("RGB", (80, 60), (240, 240, 240))
    for x, y in [(30, 20), (31, 20), (32, 20), (32, 21), (32, 22), (31, 22), (30, 22), (30, 21)]:
        img.putpixel((x, y), (200, 30, 30))  # 红色 8 邻域连通小环
    png, stats = ops.extract_foreground(img, (0, 0, 80, 60), mode="color", saturation=20)
    assert len(png) > 50
    assert stats["foreground_pixels"] == 8
    assert stats["kept_components"] == 1
    assert stats["width"] >= 3 and stats["height"] >= 3


# ── SVG 描摹 ────────────────────────────────────────────────────────
def test_trace_to_svg_square():
    img = Image.new("RGB", (50, 50), (255, 255, 255))
    img.paste((0, 0, 0), (10, 10, 40, 40))  # 实心方块
    svg, geometry = ops.trace_to_svg(img, (0, 0, 50, 50), scale=4, polygon=True)
    assert geometry["status"] == "generated"
    assert geometry["path_count"] >= 1
    assert "<svg" in svg and "<path" in svg and svg.rstrip().endswith("</svg>")


def test_trace_to_svg_empty():
    _svg, geometry = ops.trace_to_svg(_img(), (0, 0, 64, 32))
    assert geometry["status"] == "empty"


def test_trace_vtracer_engine_used():
    """vtracer CLI 装好时默认走主引擎; 未装时降级 pil-fallback (保真度标注可见)。"""
    img = Image.new("RGB", (48, 48), (255, 255, 255))
    img.paste((0, 0, 0), (6, 6, 42, 42))
    _svg, geometry = ops.trace_to_svg(img, (0, 0, 48, 48))
    assert geometry["status"] == "generated"
    assert geometry["engine"] in ("vtracer", "pil-fallback")
    if ops._find_vtracer_bin():
        assert geometry["engine"] == "vtracer"
        assert geometry["traced_scale"] >= 2  # 48px 小图 → 自动放大到短边≥256


def test_trace_pil_fallback_engine(monkeypatch):
    monkeypatch.setattr(ops, "_vtracer_bin_cache", "")
    img = Image.new("RGB", (60, 60), (255, 255, 255))
    img.paste((0, 0, 0), (10, 10, 50, 50))
    _svg, geometry = ops.trace_to_svg(img, (0, 0, 60, 60), scale=1, polygon=True)
    assert geometry["engine"] == "pil-fallback"
    assert geometry["path_count"] >= 1


def test_trace_color_mode_keeps_fill():
    """color 模式应保留前景色而非黑白。"""
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    img.paste((220, 40, 40), (8, 8, 56, 56))
    svg, geometry = ops.trace_to_svg(img, (0, 0, 64, 64), scale=2, color=True)
    assert geometry["status"] == "generated"
    if ops._find_vtracer_bin():
        assert "#" in svg and 'fill="#ffffff"' not in svg.replace("#FFFFFF", "#ffffff")[:200]


def test_trace_fidelity_circle_vs_pil():
    """高保真: 圆形描摹走样条曲线 (C 命令) 而非 PIL 降级的纯 L 多边形。"""
    from PIL import ImageDraw

    img = Image.new("RGB", (200, 200), (255, 255, 255))
    ImageDraw.Draw(img).ellipse((20, 20, 180, 180), fill=(0, 0, 0))
    if not ops._find_vtracer_bin():
        pytest.skip("需要 vtracer CLI")
    svg, geometry = ops.trace_to_svg(img, (0, 0, 200, 200), scale=2)
    assert geometry["engine"] == "vtracer"
    assert geometry["path_count"] == 1  # 单个圆形 = 单路径 (白底已剥离)
    # 样条模式: 曲线命令 C/Q 存在 (vs PIL 降级的纯 L 多边形)
    assert " C " in svg or "C" in svg


def test_strip_background_and_truncate():
    svg = '<svg><path fill="#ffffff" d="M0 0H100V100H0Z"/><path fill="#000000" d="M1.234567 2.345678"/></svg>'
    stripped = ops._strip_background(svg)
    assert "#ffffff" not in stripped and "#000000" in stripped
    assert "1.23" in ops._truncate_decimals(svg) and "2.35" in ops._truncate_decimals(svg)


# ── 长图切分 ────────────────────────────────────────────────────────
def test_split_long_image_single_chunk():
    img = _img(64, 100)
    spec = ops.split_long_image(img, max_height=3400)
    assert len(spec["chunks"]) == 1
    assert spec["chunks"][0]["core_bottom"] == 100


def test_split_long_image_multi_chunk_safety():
    # 三段文字行 + 中间大空白 → 切分应落在空白带 (safe_band 检测)
    img = Image.new("RGB", (200, 1200), (255, 255, 255))
    for band in ((50, 350), (800, 1100)):
        for y in range(band[0], band[1], 20):
            img.paste((0, 0, 0), (20, y, 180, y + 8))  # 密集文字行
    spec = ops.split_long_image(
        img, mode="general", target_height=400, min_height=250, max_height=700, overlap=40
    )
    assert len(spec["chunks"]) == 2
    cut_y = spec["chunks"][1]["core_top"]
    # 切割线必须在两段内容之间 (350..800)
    assert 350 <= cut_y <= 800
    # 块内区间一致 + 重叠映射正确
    first = spec["chunks"][0]
    assert first["core_top"] == 0 and first["core_bottom"] == cut_y
    assert first["crop_bottom"] <= cut_y + 40  # 无安全带时加 overlap


# ── 文本合并 ────────────────────────────────────────────────────────
def test_merge_text_transcripts_overlap():
    texts = [
        "标题\n第一行\n第二行\n",
        "第二行\n第三行\n第四行\n",
    ]
    merged, audit = ops.merge_text_transcripts(texts)
    assert merged.strip().splitlines() == ["标题", "第一行", "第二行", "第三行", "第四行"]
    assert audit[1]["overlap"] == 1 and audit[1]["method"] == "exact"


def test_find_text_overlap_fuzzy():
    prev = ["foo", "line one with small typo"]
    cur = ["line one with smal typo", "bar"]  # 内容级小差异 (非空白), 走 fuzzy 分支
    count, method = ops.find_text_overlap(prev, cur)
    assert count == 1 and method == "fuzzy"


# ── 标注预览 ────────────────────────────────────────────────────────
def test_draw_labeled_preview():
    img = _img()
    png = ops.draw_labeled_preview(
        img, [{"index": 1, "box": {"x1": 1, "y1": 1, "x2": 10, "y2": 10}}], numbered=True
    )
    assert len(png) > 100


# ── L2: 定位 JSON 解析 (打桩视觉模型) ──────────────────────────────
@pytest.mark.asyncio
async def test_ground_with_mocked_vision(monkeypatch):
    from veya.oskill import vision_toolkit as skill

    calls = {}

    async def fake_chat(blocks, **kwargs):
        calls["blocks"] = blocks
        return '[{"box_2d": [100, 200, 300, 500], "label": "send"}]'

    monkeypatch.setattr(skill, "vision_chat", fake_chat)
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    path = d / "shot.png"
    Image.new("RGB", (1000, 1000), (255, 255, 255)).save(path)
    result = await skill.ground(str(path), "the send button")
    assert result["matches"][0]["box"] == {"x1": 200, "y1": 100, "x2": 500, "y2": 300}
    # region 命中映射回原图坐标: 0-1000 网格相对 300x300 裁剪区缩放 + (100,100) 偏移
    result2 = await skill.ground(str(path), "send", region="100,100,400,400")
    assert result2["matches"][0]["box"] == {"x1": 160, "y1": 130, "x2": 250, "y2": 190}
