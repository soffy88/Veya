"""
veya/oprim/vision_ops.py — Atomic vision/image operations (Layer 1).

视觉工具链的原子层: 区域解析 / 裁剪 / 长图能量切分 / 像素差分 / 主色提取 /
前景抠图 / 标注预览 / SVG 描摹 / OCR 文本去重合并。全部是无状态纯函数:
bytes/路径进 → bytes/结构出, 不读环境变量、不碰网络。

Dependencies: stdlib only。Optional: PIL/Pillow (无 PIL 时相关操作抛可读
错误, 与 oprim/video.py 同一降级口径)。

算法内化自 Anionex/agent-vision-toolkit (dsh-vision-toolkit 上游, MIT):
- pixel_diff        : 逐像素差 + 网格排名 (ImageChops/ImageStat)
- dominant_colors   : MEDIANCUT 量化 + Chebyshev 合并 + 候选打分
- extract_foreground: 8 邻域连通分量, 最大分量=前景 (抗锯齿保留)
- split_long_image  : 行边缘能量 + 前景占用率 → 低内容切割带
坐标契约: 一律原图像素 x1,y1,x2,y2 (裁切/描摹可直接互喂)。
"""

from __future__ import annotations

import contextlib
import io
import itertools
import math
import os
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

    _HAS_PIL = True
except ImportError:  # 与 oprim/video.py 同口径: 可选依赖
    Image = None  # type: ignore
    ImageChops = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFilter = None  # type: ignore
    ImageStat = None  # type: ignore
    _HAS_PIL = False

# 高保真 raster→SVG 引擎 = vtracer 独立 CLI (visioncortex/vtracer, 上游同源)。
# 不用 pip 包: 0.6.15 的 Python 扩展在 CPython 3.14 下段错误; CLI 走子进程
# 天然隔离 (崩了也只丢子进程)。二进制获取: 见 docs/ops/TOOLCHAIN_SETUP.md。
_vtracer_bin_cache: str | None = None


def _find_vtracer_bin() -> str | None:
    """解析 vtracer CLI: VEYA_VTRACER_BIN → ~/.veya/bin/vtracer → PATH。"""
    global _vtracer_bin_cache
    if _vtracer_bin_cache is not None:
        return _vtracer_bin_cache or None
    candidates = []
    env_bin = os.environ.get("VEYA_VTRACER_BIN", "").strip()
    if env_bin:
        candidates.append(Path(env_bin).expanduser())
    candidates.append(Path.home() / ".veya" / "bin" / "vtracer")
    candidates.append(Path("vtracer"))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            _vtracer_bin_cache = str(candidate)
            return _vtracer_bin_cache
        if str(candidate) == "vtracer":
            import shutil

            found = shutil.which("vtracer")
            if found:
                _vtracer_bin_cache = found
                return found
    _vtracer_bin_cache = ""
    return None


def _need_pil(op: str) -> None:
    if not _HAS_PIL:
        raise RuntimeError(f"vision_ops.{op}: 需要 Pillow (pip install pillow)")


# ---------------------------------------------------------------------------
# 区域解析 / 图像加载
# ---------------------------------------------------------------------------

def parse_region(text: str, width: int, height: int) -> tuple[int, int, int, int]:
    """'X1,Y1,X2,Y2' → 夹紧到图像内的合法框; 空框抛 ValueError。"""
    try:
        x1, y1, x2, y2 = (int(v.strip()) for v in str(text).split(","))
    except ValueError:
        raise ValueError(f"region 需四个整数 X1,Y1,X2,Y2, got {text!r}") from None
    box = (
        max(0, min(x1, x2)),
        max(0, min(y1, y2)),
        min(width, max(x1, x2)),
        min(height, max(y1, y2)),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError(f"region {text!r} 夹紧到 {width}x{height} 后为空")
    return box


def load_rgb(path: str | Path) -> Any:
    """载入图像为 RGB。透明像素合到白底 (查看器视角), 防 alpha 读成黑色。"""
    _need_pil("load_rgb")
    image = Image.open(path)
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        canvas = Image.new("RGB", image.size, "white")
        canvas.paste(image, mask=image.split()[-1])
        image = canvas
    else:
        image = image.convert("RGB")
    return image


def image_size(path: str | Path) -> tuple[int, int]:
    """不完整解码, 只读尺寸 (magic bytes 头)。"""
    _need_pil("image_size")
    with Image.open(path) as probe:
        return probe.size


def encode_png(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 裁剪
# ---------------------------------------------------------------------------

def crop_bytes(
    image: Any,
    box: tuple[int, int, int, int],
    scale: int = 1,
    out_format: str = "png",
) -> bytes:
    """裁剪像素框并输出 bytes。scale 1-8 (LANCZOS 放大); 格式 png/jpeg。"""
    _need_pil("crop_bytes")
    if not 1 <= int(scale) <= 8:
        raise ValueError("scale 须在 1-8")
    crop = image.crop(box)
    if int(scale) > 1:
        crop = crop.resize(
            (crop.width * int(scale), crop.height * int(scale)), Image.LANCZOS
        )
    fmt = "JPEG" if str(out_format).lower() in ("jpg", "jpeg") else "PNG"
    buffer = io.BytesIO()
    crop.save(buffer, format=fmt)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 长截图能量切分 (内化自 upstream long_screenshot_ocr.py)
# ---------------------------------------------------------------------------

ANALYSIS_WIDTH = 900
SAFE_OCCUPANCY_LEVEL = 20.0


def _percentile(values: Sequence[float], percent: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * percent / 100
    lower, upper = math.floor(pos), math.ceil(pos)
    if lower == upper:
        return float(ordered[lower])
    frac = pos - lower
    return float(ordered[lower] * (1 - frac) + ordered[upper] * frac)


def _rolling_mean(values: Sequence[float], radius: int) -> list[float]:
    if radius <= 0 or len(values) <= 1:
        return [float(v) for v in values]
    padded = [float(values[0])] * radius
    padded.extend(float(v) for v in values)
    padded.extend([float(values[-1])] * radius)
    window = radius * 2 + 1
    total = sum(padded[:window])
    result = []
    for i in range(len(values)):
        result.append(total / window)
        if i + window < len(padded):
            total += padded[i + window] - padded[i]
    return result


def row_energy(image: Any) -> tuple[list[float], list[float], float]:
    """每行边缘能量 + 前景占用率 (降采样到 ANALYSIS_WIDTH 分析)。"""
    scale = min(1.0, ANALYSIS_WIDTH / image.width)
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    analysis = image.convert("RGB")
    if scale < 1.0:
        analysis = analysis.resize((width, height), Image.BILINEAR)
    gray = analysis.convert("L")

    shifted_x = Image.new("L", gray.size)
    shifted_x.paste(gray, (1, 0))
    shifted_x.paste(gray.crop((0, 0, 1, height)), (0, 0))
    horizontal = ImageChops.difference(gray, shifted_x)

    shifted_y = Image.new("L", gray.size)
    shifted_y.paste(gray, (0, 1))
    shifted_y.paste(gray.crop((0, 0, width, 1)), (0, 0))
    vertical = ImageChops.difference(gray, shifted_y)

    combined = Image.blend(horizontal, vertical, 0.32)
    collapsed = combined.resize((1, height), Image.BOX)
    edges = [float(v) for v in collapsed.tobytes()]

    border_width = max(1, min(24, width // 18))
    left = analysis.crop((0, 0, border_width, height)).resize((1, height), Image.BOX)
    right = analysis.crop((width - border_width, 0, width, height)).resize(
        (1, height), Image.BOX
    )
    edge_reference = Image.new("RGB", (2, height))
    edge_reference.paste(left, (0, 0))
    edge_reference.paste(right, (1, 0))
    background = edge_reference.resize((width, height), Image.BILINEAR)
    foreground_difference = ImageChops.difference(analysis, background)
    r_diff, g_diff, b_diff = foreground_difference.split()
    foreground_distance = ImageChops.lighter(ImageChops.lighter(r_diff, g_diff), b_diff)
    foreground_mask = foreground_distance.point(
        lambda v: 255 if v >= 14 else 0, mode="L"
    )
    occupancy_column = foreground_mask.resize((1, height), Image.BOX)
    occupancy = [float(v) for v in occupancy_column.tobytes()]

    radius = max(1, round(3 * scale))
    smoothed_edges = _rolling_mean(edges, radius)
    smoothed_occupancy = _rolling_mean(occupancy, radius)
    energy = [
        e + o * 0.55 for e, o in zip(smoothed_edges, smoothed_occupancy, strict=True)
    ]
    return energy, smoothed_occupancy, scale


def resolve_split_sizes(
    width: int,
    mode: str,
    target_height: int | None,
    min_height: int | None,
    max_height: int | None,
    overlap: int | None,
) -> tuple[int, int, int, int]:
    auto_target = round(
        max(1400 if mode == "chat" else 1200,
            min(2400, width * (1.75 if mode == "chat" else 1.45)))
    )
    target = target_height or auto_target
    minimum = min_height or max(600, round(target * 0.58))
    maximum = max_height or min(3400, round(target * 1.42))
    resolved_overlap = overlap if overlap is not None else (64 if mode == "chat" else 40)
    if min(target, minimum, maximum) <= 0:
        raise ValueError("split heights must be greater than zero")
    if not minimum <= target <= maximum:
        raise ValueError("min-height <= target-height <= max-height 不成立")
    if resolved_overlap < 0 or resolved_overlap * 2 >= minimum:
        raise ValueError("overlap 须 < min-height/2")
    return target, minimum, maximum, resolved_overlap


def choose_cut(
    energy: Sequence[float],
    occupancy: Sequence[float],
    start: int,
    target: int,
    minimum: int,
    maximum: int,
    mode: str,
    safe_radius: int,
) -> tuple[int, float, float, int]:
    image_height = len(energy)
    lower = min(image_height - 1, start + minimum)
    upper = min(image_height - minimum, start + maximum)
    desired = min(image_height - 1, start + target)
    if lower >= upper:
        return upper, float(energy[upper]), 0.0, 0

    local = [float(v) for v in energy[lower : upper + 1]]
    low = _percentile(local, 8)
    high = _percentile(local, 92)
    normalized = [(v - low) / max(0.001, high - low) for v in local]

    threshold = _percentile(local, 32 if mode == "chat" else 25)
    low_rows = [
        1.0 if e <= threshold and occupancy[i] <= SAFE_OCCUPANCY_LEVEL else 0.0
        for i, e in enumerate(energy)
    ]
    blank_ratio = _rolling_mean(low_rows, safe_radius)[lower : upper + 1]
    distance_weight = 0.20 if mode == "chat" else 0.30

    selected_offset = min(
        range(len(local)),
        key=lambda offset: (
            normalized[offset]
            + abs((lower + offset) - desired) / max(1, maximum - minimum) * distance_weight
            + occupancy[lower + offset] / 255 * 0.75
            - blank_ratio[offset] * 0.48
        ),
    )
    selected = lower + selected_offset

    band_threshold = _percentile(local, 40)
    band_left = band_right = selected
    while (
        band_left > lower
        and energy[band_left - 1] <= band_threshold
        and occupancy[band_left - 1] <= SAFE_OCCUPANCY_LEVEL
    ):
        band_left -= 1
    while (
        band_right < upper
        and energy[band_right + 1] <= band_threshold
        and occupancy[band_right + 1] <= SAFE_OCCUPANCY_LEVEL
    ):
        band_right += 1
    if band_right - band_left >= max(4, safe_radius // 2):
        selected = (band_left + band_right) // 2
        safe_margin = min(selected - band_left, band_right - selected)
    else:
        safe_margin = 0

    selected_energy = float(energy[selected])
    percentile_rank = sum(v <= selected_energy for v in local) / len(local)
    quality = max(0.0, min(1.0, 1.0 - percentile_rank))
    return selected, selected_energy, quality, safe_margin


def split_long_image(
    image: Any,
    mode: str = "general",
    target_height: int | None = None,
    min_height: int | None = None,
    max_height: int | None = None,
    overlap: int | None = None,
) -> dict[str, Any]:
    """能量切分长图 → 块清单 (core/crop 区间 + 每块 PNG bytes + 切割质量审计)。

    返回 {"chunks": [{index, core_top, core_bottom, crop_top, crop_bottom,
    top_overlap, bottom_overlap, cut_energy, cut_quality, top_safe_margin,
    bottom_safe_margin, image_bytes}], "analysis_scale", "safe_band_radius_px"}。
    图高 <= max_height 时单块直出 (零切分)。
    """
    _need_pil("split_long_image")
    if image.height <= (max_height or 3400):
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return {
            "chunks": [{
                "index": 1,
                "core_top": 0,
                "core_bottom": image.height,
                "crop_top": 0,
                "crop_bottom": image.height,
                "top_overlap": 0,
                "bottom_overlap": 0,
                "cut_energy": None,
                "cut_quality": None,
                "top_safe_margin": None,
                "bottom_safe_margin": None,
                "image_bytes": buffer.getvalue(),
            }],
            "analysis_scale": 1.0,
            "safe_band_radius_px": 0.0,
        }

    target, minimum, maximum, resolved_overlap = resolve_split_sizes(
        image.width, mode, target_height, min_height, max_height, overlap
    )
    energy, occupancy, scale = row_energy(image)
    t = max(1, round(target * scale))
    mn = max(1, round(minimum * scale))
    mx = max(mn + 1, round(maximum * scale))
    safe_radius = max(2, round(image.width * 0.012 * scale))

    cuts = [0]
    cut_details: list[tuple[float, float, int]] = []
    analysis_height = len(energy)
    while analysis_height - cuts[-1] > mx:
        if analysis_height - cuts[-1] < mn * 2:
            break
        cut, selected_energy, quality, safe_margin = choose_cut(
            energy, occupancy, cuts[-1], t, mn, mx, mode, safe_radius
        )
        if cut <= cuts[-1]:
            cut = min(analysis_height, cuts[-1] + t)
        cuts.append(cut)
        cut_details.append((selected_energy, quality, safe_margin))
    cuts.append(analysis_height)

    original_cuts = [0]
    for cut in cuts[1:-1]:
        mapped = max(original_cuts[-1] + 1, min(image.height - 1, round(cut / scale)))
        original_cuts.append(mapped)
    original_cuts.append(image.height)

    chunks: list[dict[str, Any]] = []
    for index, (top, bottom) in enumerate(itertools.pairwise(original_cuts), 1):
        detail = cut_details[index - 1] if index - 1 < len(cut_details) else (None, None, None)
        top_safe_margin = (
            round(cut_details[index - 2][2] / scale) if index > 1 and cut_details else None
        )
        bottom_safe_margin = (
            round(detail[2] / scale) if detail[2] is not None else None
        )
        top_overlap = (
            0 if not top or (top_safe_margin is not None and top_safe_margin > 0)
            else resolved_overlap
        )
        bottom_overlap = (
            0 if bottom >= image.height
            or (bottom_safe_margin is not None and bottom_safe_margin > 0)
            else resolved_overlap
        )
        crop_top = max(0, top - top_overlap)
        crop_bottom = min(image.height, bottom + bottom_overlap)
        buffer = io.BytesIO()
        image.crop((0, crop_top, image.width, crop_bottom)).save(buffer, format="PNG")
        chunks.append({
            "index": index,
            "core_top": top,
            "core_bottom": bottom,
            "crop_top": crop_top,
            "crop_bottom": crop_bottom,
            "top_overlap": top_overlap,
            "bottom_overlap": bottom_overlap,
            "cut_energy": detail[0],
            "cut_quality": detail[1],
            "top_safe_margin": top_safe_margin,
            "bottom_safe_margin": bottom_safe_margin,
            "image_bytes": buffer.getvalue(),
        })
    return {
        "chunks": chunks,
        "analysis_scale": scale,
        "safe_band_radius_px": safe_radius / scale,
    }


# ---------------------------------------------------------------------------
# 像素差分
# ---------------------------------------------------------------------------

def pixel_diff(
    original_path: str | Path,
    rebuilt_path: str | Path,
    grid: int = 6,
    top: int = 5,
) -> dict[str, Any]:
    """重建图 vs 参考图: 真实像素对比, 返回总差百分比 + 最差网格区排名 + 热力图。

    尺寸不一致时重建图缩放到参考图尺寸 (scaled=true)。盒子是参考图坐标。
    """
    _need_pil("pixel_diff")
    if not 1 <= int(grid) <= 32:
        raise ValueError("grid 须在 1-32")
    original = load_rgb(original_path)
    with Image.open(rebuilt_path) as probe:
        raw_size = probe.size
    rebuilt = load_rgb(rebuilt_path)
    if rebuilt.size != original.size:
        rebuilt = rebuilt.resize(original.size, Image.LANCZOS)
        scaled = True
    else:
        scaled = False

    diff = ImageChops.difference(original, rebuilt)
    grey = diff.convert("L")
    overall = ImageStat.Stat(grey).mean[0] / 255 * 100

    width, height = grey.size
    scores: list[tuple[float, tuple[int, int, int, int]]] = []
    for row in range(grid):
        for column in range(grid):
            box = (
                round(column * width / grid), round(row * height / grid),
                round((column + 1) * width / grid), round((row + 1) * height / grid),
            )
            if box[2] > box[0] and box[3] > box[1]:
                mean = ImageStat.Stat(grey.crop(box)).mean[0]
                scores.append((mean / 255 * 100, box))
    # 负 key 稳定排序: 相等分数保持先发现者在前 (reverse=True 会翻转 tie 顺序)
    scores.sort(key=lambda item: -item[0])
    return {
        "overall_difference_pct": round(overall, 2),
        "scaled": scaled,
        "rebuilt_original_size": {"width": raw_size[0], "height": raw_size[1]},
        "worst_regions": [
            {
                "index": i,
                "difference_pct": round(score, 2),
                "box": {"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]},
            }
            for i, (score, box) in enumerate(scores[: int(top)], 1)
        ],
        "heatmap_bytes": encode_png(diff),
    }


# ---------------------------------------------------------------------------
# 主色提取 / 候选色打分
# ---------------------------------------------------------------------------

def _hex_of(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _parse_hex(text: str) -> tuple[int, int, int]:
    value = text.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"invalid colour {text!r}: 需 #RRGGBB")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _chebyshev(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def dominant_colors(
    image: Any,
    box: tuple[int, int, int, int],
    top: int = 6,
    quantize: int = 16,
    max_pixels: int = 200000,
    merge_tolerance: int = 12,
) -> dict[str, Any]:
    """区域主色: 降采样 → MEDIANCUT 量化 → Chebyshev 合并近邻簇 → 按占比排序。"""
    _need_pil("dominant_colors")
    crop = image.crop(box)
    width, height = crop.size
    scale = min(1.0, max_pixels / max(width, height))
    if scale < 1.0:
        crop = crop.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS
        )
    quantized = crop.quantize(colors=int(quantize), method=Image.MEDIANCUT)
    palette = quantized.getpalette() or []
    clusters: list[tuple[list[int], int]] = []  # [r,g,b], count
    for count, index in sorted(quantized.getcolors(maxcolors=int(quantize)), reverse=True):
        rgb = [palette[index * 3], palette[index * 3 + 1], palette[index * 3 + 2]]
        for existing in clusters:
            if _chebyshev(tuple(rgb), tuple(existing[0])) <= int(merge_tolerance):
                total = existing[1] + count
                existing[0] = [
                    round((existing[0][i] * existing[1] + rgb[i] * count) / total)
                    for i in range(3)
                ]
                existing[1] = total
                break
        else:
            clusters.append([rgb, count])
    clusters.sort(key=lambda c: c[1], reverse=True)
    total = sum(c[1] for c in clusters) or 1
    return {
        "colors": [
            {"color": _hex_of(tuple(c[0])), "share_pct": round(c[1] / total * 100, 2)}
            for c in clusters[: int(top)]
        ],
        "sampled_pixels": total,
        "requested_top": int(top),
        "cluster_count": len(clusters),
    }


def score_color_candidates(
    image: Any,
    box: tuple[int, int, int, int],
    candidates: Sequence[str],
    tolerance: int = 14,
    max_pixels: int = 200000,
) -> dict[str, Any]:
    """候选 #RRGGBB 调色板打分: 按像素距离过滤 → 像素背书的胜者 (标签来自模型, 数值来自像素)。"""
    _need_pil("score_color_candidates")
    crop = image.crop(box)
    width, height = crop.size
    scale = min(1.0, max_pixels / max(width, height))
    if scale < 1.0:
        crop = crop.resize(
            (max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS
        )
    pixels_raw = crop.convert("RGB").tobytes()
    pixels = [
        (pixels_raw[i], pixels_raw[i + 1], pixels_raw[i + 2])
        for i in range(0, len(pixels_raw), 3)
    ]
    parsed = [_parse_hex(c) for c in candidates][:32]
    rows = []
    best_score = -1.0
    winner = ""
    for rgb in parsed:
        distances = [_chebyshev(p, rgb) for p in pixels]
        within = sum(d <= int(tolerance) for d in distances)
        mean_distance = sum(d for d in distances) / len(distances)
        share = within / len(pixels) * 100
        weighted = share / max(1.0, mean_distance + 1.0)
        rows.append({
            "color": _hex_of(rgb),
            "share_pct": round(share, 2),
            "mean_distance": round(mean_distance, 2),
            "weighted_score_pct": round(weighted, 2),
            "winner": False,
        })
        if weighted > best_score:
            best_score = weighted
            winner = _hex_of(rgb)
    for row in rows:
        row["winner"] = row["color"] == winner
    matched = rows[0] if rows else None
    return {
        "mode": "candidates",
        "winner": winner or None,
        "matched_within_tolerance": bool(matched and matched["share_pct"] > 0),
        "closest_candidate": matched["color"] if matched else None,
        "candidates": rows,
    }


# ---------------------------------------------------------------------------
# 前景提取 (连通分量)
# ---------------------------------------------------------------------------

def connected_components(ink: set, w: int, h: int) -> list[list[tuple[int, int]]]:
    """8 邻域连通分量, 按大小降序。"""
    seen: set = set()
    comps: list[list[tuple[int, int]]] = []
    for p in ink:
        if p in seen:
            continue
        stack = [p]
        seen.add(p)
        comp = []
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    q = (cx + dx, cy + dy)
                    if q in seen or q not in ink:
                        continue
                    seen.add(q)
                    stack.append(q)
        comps.append(comp)
    comps.sort(key=len, reverse=True)
    return comps


def extract_foreground(
    image: Any,
    box: tuple[int, int, int, int],
    mode: str = "color",
    saturation: int = 40,
    dark_threshold: int = 90,
    exclude_color: str | None = None,
    exclude_tolerance: int = 24,
    padding: int = 0,
    keep_whites: bool = True,
) -> tuple[bytes, dict[str, Any]]:
    """搜索区内取满足判定的像素 → 8 邻域连通分量 → 前景 = 保留分量 (透明 PNG)。

    mode=color: 饱和差 > saturation (彩色图标); mode=dark: 亮度 < dark_threshold
    (灰/黑线条)。exclude_color 排除背景色 (#RRGGBB)。噪点散点 (远小于最大分量)
    自动出局; 与最大分量 bbox 重叠的分离子形状 (如 '>_' 与云朵轮廓) 保留。
    """
    _need_pil("extract_foreground")
    im = image.convert("RGB")
    w, h = im.size
    x1, y1, x2, y2 = box
    excl = _parse_hex(exclude_color) if exclude_color else None

    ink: set = set()
    px = im.load()
    for y in range(y1, y2):
        for x in range(x1, x2):
            r, g, b = px[x, y]
            mx, mn = max(r, g, b), min(r, g, b)
            if mode == "color":
                if mx - mn <= int(saturation):
                    continue
            else:
                if mx >= int(dark_threshold):
                    continue
            if excl is not None:
                d = math.sqrt((r - excl[0]) ** 2 + (g - excl[1]) ** 2 + (b - excl[2]) ** 2)
                if d <= int(exclude_tolerance):
                    continue
            ink.add((x - x1, y - y1))

    if not ink:
        raise RuntimeError("区域内未找到前景像素 (调 saturation/dark_threshold 或换区域)")

    comps = connected_components(ink, x2 - x1, y2 - y1)
    min_size = max(len(comps[0]) * 0.02, 8)
    main_box = (min(p[0] for p in comps[0]), min(p[1] for p in comps[0]),
                max(p[0] for p in comps[0]), max(p[1] for p in comps[0]))

    def overlaps_main(c: list) -> bool:
        cx = [p[0] for p in c]
        cy = [p[1] for p in c]
        return not (max(cx) < main_box[0] or min(cx) > main_box[2]
                    or max(cy) < main_box[1] or min(cy) > main_box[3])

    kept = [c for c in comps if len(c) >= min_size or overlaps_main(c)]
    if not keep_whites:
        # 丢弃被最大分量包住的白洞/白色子形状 (饱和度极低)
        kept = [c for c in kept if _component_saturation(c, px, x1, y1) > 12]
    best = [p for c in kept for p in c]
    pad = int(padding)

    bx1 = x1 + min(p[0] for p in best) - pad
    by1 = y1 + min(p[1] for p in best) - pad
    bx2 = x1 + max(p[0] for p in best) + 1 + pad
    by2 = y1 + max(p[1] for p in best) + 1 + pad
    bx1, by1 = max(0, bx1), max(0, by1)
    bx2, by2 = min(w, bx2), min(h, by2)

    out = Image.new("RGBA", (bx2 - bx1, by2 - by1), (0, 0, 0, 0))
    src = im.load()
    dst = out.load()
    for cx, cy in best:
        ax, ay = x1 + cx, y1 + cy
        if bx1 <= ax < bx2 and by1 <= ay < by2:
            dst[ax - bx1, ay - by1] = (*src[ax, ay], 255)
    return encode_png(out), {
        "box": {"x1": bx1, "y1": by1, "x2": bx2, "y2": by2},
        "foreground_pixels": len(best),
        "kept_components": len(kept),
        "total_components": len(comps),
        "largest_component_pct": round(len(comps[0]) / max(1, len(ink)) * 100, 2),
        "width": bx2 - bx1,
        "height": by2 - by1,
    }


def _component_saturation(comp: Sequence[tuple[int, int]], px: Any, ox: int, oy: int) -> float:
    rs = gs = bs = 0
    for x, y in comp:
        r, g, b = px[ox + x, oy + y]
        rs += r
        gs += g
        bs += b
    n = len(comp)
    return max(rs / n, gs / n, bs / n) - min(rs / n, gs / n, bs / n)


# ---------------------------------------------------------------------------
# 标注预览
# ---------------------------------------------------------------------------

def draw_labeled_preview(
    image: Any,
    items: Sequence[dict[str, Any]],
    numbered: bool = False,
) -> bytes:
    """在图上画框 (红) + 编号徽章。编号数字 ASCII 无需字体; 标签随 JSON 返回。"""
    _need_pil("draw_labeled_preview")
    out = image.convert("RGB")
    draw = ImageDraw.Draw(out)
    for i, item in enumerate(items, 1):
        box = item["box"]
        rect = (box["x1"], box["y1"], box["x2"], box["y2"])
        draw.rectangle(rect, outline=(220, 38, 38), width=3)
        if numbered:
            label = str(item.get("index", i))
            draw.rectangle(
                (rect[0], rect[1], rect[0] + 7 + 6 * len(label), rect[1] + 14),
                fill=(220, 38, 38),
            )
            draw.text((rect[0] + 4, rect[1] + 2), label, fill=(255, 255, 255))
    return encode_png(out)


# ---------------------------------------------------------------------------
# SVG 描摹 (高保真: vtracer 主引擎 + PIL 边界追踪降级; 零视觉 API)
# ---------------------------------------------------------------------------

# 小图直接二值化会全部死于斑点过滤 (上游实测): 自动放大到短边 ≥ TARGET_MIN_SIDE。
TARGET_MIN_SIDE = 256
_WHITE_FILLS = {"#ffffff", "#fff", "white"}

def _strip_background(svg: str) -> str:
    """去掉 vtracer 为背景生成的全画布白色首路径 (上游同款后处理)。"""
    match = re.search(r"<path [^>]*/>", svg)
    if match:
        fill = re.search(r'fill="([^"]+)"', match.group(0))
        if fill and fill.group(1).strip().lower() in _WHITE_FILLS:
            return svg.replace(match.group(0), "", 1)
    return svg


def _truncate_decimals(svg: str, places: int = 2) -> str:
    """压缩浮点位数 (SVG 体积/精度折中, 上游同款)。"""
    return re.sub(
        r"-?\d+\.\d{3,}", lambda m: f"{float(m.group()):.{places}f}", svg
    )


def _douglas_peucker(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) < 3:
        return points
    start, end = points[0], points[-1]
    dx, dy = end[0] - start[0], end[1] - start[1]
    norm = math.hypot(dx, dy) or 1.0
    max_dist = 0.0
    index = 0
    for i in range(1, len(points) - 1):
        dist = abs(dy * (points[i][0] - start[0]) - dx * (points[i][1] - start[1])) / norm
        if dist > max_dist:
            max_dist = dist
            index = i
    if max_dist > epsilon:
        left = _douglas_peucker(points[: index + 1], epsilon)
        right = _douglas_peucker(points[index:], epsilon)
        return left[:-1] + right
    return [start, end]


def _gradient_edges(grey: Any) -> Any:
    """位移差分梯度 (与 row_energy 同款): 均匀图 → 全零, 无 FIND_EDGES 边框伪影。"""
    w, h = grey.size
    shifted_x = Image.new("L", grey.size)
    shifted_x.paste(grey, (1, 0))
    shifted_x.paste(grey.crop((0, 0, 1, h)), (0, 0))
    grad_x = ImageChops.difference(grey, shifted_x)
    shifted_y = Image.new("L", grey.size)
    shifted_y.paste(grey, (0, 1))
    shifted_y.paste(grey.crop((0, 0, w, 1)), (0, 0))
    grad_y = ImageChops.difference(grey, shifted_y)
    return ImageChops.lighter(grad_x, grad_y)


def trace_to_svg(
    image: Any,
    box: tuple[int, int, int, int],
    scale: int | None = None,
    color: bool = False,
    polygon: bool = False,
) -> tuple[str, dict[str, Any]]:
    """把扁平高对比栅格图描摹成高保真可编辑 SVG (本地, 零视觉 API, 像素级精确几何)。

    主引擎 = vtracer CLI (上游同源, filter_speckle=8 / corner_threshold=40 /
    spline|polygon / bw|color); 无二进制时降级为 PIL 边界追踪 + Douglas-Peucker
    (保真度较低, geometry.engine 标注 "pil-fallback")。

    scale=None 时自动放大: 区域裁剪 ≥2x, 保证短边 ≥256px 以活过斑点过滤
    (小图标直接描会二值化成空 — 上游实测教训)。"""
    _need_pil("trace_to_svg")
    if scale is not None and not 1 <= int(scale) <= 16:
        raise ValueError("scale 须在 1-16")
    crop = image.crop(box)
    if _find_vtracer_bin() is not None:
        return _trace_vtracer(crop, scale, color, polygon)
    return _trace_pil_fallback(crop, int(scale or 1), color, polygon)


def _trace_vtracer(
    crop: Any, scale: int | None, color: bool, polygon: bool
) -> tuple[str, dict[str, Any]]:
    """vtracer CLI 引擎 (子进程隔离): 上游 bin/trace 同款契约 + 白底剥离 + 小数截断。"""
    if scale is None:
        shortest = max(min(crop.width, crop.height), 1)
        scale = max(2, -(-TARGET_MIN_SIDE // shortest))  # ceil 除法
    if scale != 1:
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    svg_src_fd, src_path = tempfile.mkstemp(suffix=".png")
    svg_out_fd, out_path = tempfile.mkstemp(suffix=".svg")
    os.close(svg_src_fd)
    os.close(svg_out_fd)
    src_path_p = Path(src_path)
    out_path_p = Path(out_path)
    try:
        crop.save(src_path_p, format="PNG")
        import subprocess

        completed = subprocess.run(
            [
                _find_vtracer_bin() or "vtracer",
                "--input", src_path,
                "--output", out_path,
                "--colormode", "color" if color else "bw",
                "--filter_speckle", "8",
                "--corner_threshold", "40",
                "--mode", "polygon" if polygon else "spline",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"vtracer 退出 {completed.returncode}: "
                f"{(completed.stderr or completed.stdout or '').strip()[:200]}"
            )
        svg = _truncate_decimals(_strip_background(out_path_p.read_text(encoding="utf-8")))
    finally:
        for handle in (src_path_p, out_path_p):
            with contextlib.suppress(OSError):
                handle.unlink(missing_ok=True)
    path_count = svg.count("<path")
    if not path_count:
        # 二值化后什么都没活下来 — 给出最便宜的恢复路径 (上游同款提示)
        hint = (
            "0 paths: 二值化后无形状存活。试更大 scale、把 region 裁到形状附近、"
            "或浅底深图先反色; color 是最后手段 (抗锯齿图会把每个灰阶拆成一条路径)。"
        )
        return "", {
            "status": "empty", "engine": "vtracer", "path_count": 0,
            "traced_scale": scale, "bytes": 0, "hint": hint,
        }
    return svg, {
        "status": "generated", "engine": "vtracer", "path_count": path_count,
        "traced_scale": scale, "bytes": len(svg.encode("utf-8")),
    }


def _trace_pil_fallback(
    crop: Any, scale: int, color: bool, polygon: bool
) -> tuple[str, dict[str, Any]]:
    """PIL 降级引擎 (vtracer 缺失时): 梯度墨迹 → 连通分量 → Moore 边界 → DP 简化。"""
    if not 1 <= scale <= 16:
        raise ValueError("scale 须在 1-16")
    if scale > 1:
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    grey = crop.convert("L")
    edges = _gradient_edges(grey).point(lambda v: 255 if v > 16 else 0, mode="L")
    rgb = crop.convert("RGB")

    ink: set = set()
    w, h = edges.size
    edge_px = edges.load()
    for y in range(h):
        for x in range(w):
            if edge_px[x, y]:
                ink.add((x, y))
    if not ink:
        return "", {"status": "empty", "engine": "pil-fallback", "path_count": 0,
                    "traced_scale": scale, "bytes": 0}

    comps = connected_components(ink, w, h)
    min_size = max(len(comps[0]) * 0.02, 6)
    kept = [c for c in comps if len(c) >= min_size]
    if not kept:
        kept = comps[:1]

    color_px = rgb.load()
    paths: list[str] = []
    for comp in kept:
        boundary = _trace_boundary(comp, w, h)[:-1]  # 去掉闭合重复点, DP 后再闭合
        if len(boundary) < 4:
            continue
        epsilon = (1.2 if polygon else 0.5) * (4 / scale + 0.6)
        simplified = _douglas_peucker(boundary, epsilon)
        if len(simplified) < 3:
            continue
        d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in simplified) + " Z"
        if color:
            rs = gs = bs = n = 0
            for x, y in comp:
                r, g, b = color_px[x, y]
                rs += r
                gs += g
                bs += b
                n += 1
            fill = _hex_of((round(rs / n), round(gs / n), round(bs / n)))
        else:
            fill = "currentColor"
        paths.append(f'<path d="{d}" fill="{fill}" fill-rule="evenodd"/>')

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}">{"".join(paths)}</svg>'
    )
    return svg, {
        "status": "generated", "engine": "pil-fallback",
        "path_count": len(paths),
        "traced_scale": scale,
        "bytes": len(svg.encode("utf-8")),
    }


def _trace_boundary(comp: Sequence[tuple[int, int]], w: int, h: int) -> list[tuple[float, float]]:
    """Moore 邻域边界追踪: 输出逆时针边界点列 (含首尾闭合)。"""
    comp_set = set(comp)
    min_y = min(y for _, y in comp_set)
    start = min((x, y) for x, y in comp_set if y == min_y)
    # 方向表: 8 邻域 (从上方起顺时针)
    dirs = [(0, -1), (1, -1), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1)]
    boundary: list[tuple[float, float]] = []
    cx, cy = start
    # 起始方向: 从正左 (即上一格) 开始顺时针找下一个墨迹邻居
    start_dir = 6
    px_, py_ = cx, cy
    dir_idx = start_dir
    first = True
    while first or (px_, py_) != start or dir_idx != start_dir:
        first = False
        boundary.append((float(px_), float(py_)))
        found = False
        for k in range(8):
            d = (dir_idx + k) % 8
            dx, dy = dirs[d]
            nx, ny = px_ + dx, py_ + dy
            if 0 <= nx < w and 0 <= ny < h and (nx, ny) in comp_set:
                px_, py_ = nx, ny
                # 下一轮从当前方向的逆时针前一个方向开始
                dir_idx = (d + 6) % 8
                found = True
                break
        if not found:
            break
        if len(boundary) > len(comp_set) * 8 + 8:  # 兜底防死循环
            break
    boundary.append(boundary[0])
    return boundary


# ---------------------------------------------------------------------------
# OCR 文本合并 (块重叠去重)
# ---------------------------------------------------------------------------

def trim_outer_blank_lines(text: str) -> list[str]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def normalized_line(line: str) -> str:
    normalized = unicodedata.normalize("NFKC", line)
    return " ".join(normalized.casefold().split())


def find_text_overlap(previous: Sequence[str], current: Sequence[str]) -> tuple[int, str]:
    """相邻块重叠行数 (exact 全等 / fuzzy ≥0.96 相似), 供合并去重。"""
    maximum = min(24, len(previous), len(current))
    for count in range(maximum, 0, -1):
        left = [normalized_line(line) for line in previous[-count:]]
        right = [normalized_line(line) for line in current[:count]]
        if left == right and any(left):
            return count, "exact"
    for count in range(min(3, maximum), 0, -1):
        left = [normalized_line(line) for line in previous[-count:]]
        right = [normalized_line(line) for line in current[:count]]
        joined_length = sum(len(line) for line in left + right)
        if joined_length < 24 or not all(left) or not all(right):
            continue
        ratios = [SequenceMatcher(None, a, b).ratio() for a, b in zip(left, right, strict=True)]
        if min(ratios) >= 0.92 and sum(ratios) / len(ratios) >= 0.96:
            return count, "fuzzy"
    return 0, "none"


def merge_text_transcripts(texts: Sequence[str]) -> tuple[str, list[dict[str, Any]]]:
    """顺序合并各块 OCR 文本: 只去相邻块重复的重叠行。返回 (merged, merge_audit)。"""
    merged_lines: list[str] = []
    audit: list[dict[str, Any]] = []
    for i, text in enumerate(texts):
        lines = trim_outer_blank_lines(text)
        if not lines:
            audit.append({"chunk": i + 1, "overlap": 0, "method": "none", "lines": 0})
            continue
        if not merged_lines:
            merged_lines = lines
            audit.append({"chunk": i + 1, "overlap": 0, "method": "none", "lines": len(lines)})
            continue
        overlap, method = find_text_overlap(merged_lines, lines)
        merged_lines.extend(lines[overlap:] if overlap else lines)
        audit.append({
            "chunk": i + 1, "overlap": overlap, "method": method, "lines": len(lines),
        })
    return "\n".join(merged_lines) + "\n", audit
