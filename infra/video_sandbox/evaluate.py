#!/usr/bin/env python3
"""veya-video-sandbox 质检执行器 —— 只读 ffprobe 规则质检。

输入: stdin JSON {video_path, spec}
输出: stdout JSON (VideoEvalResult 对齐):
    {passed, duration_s, width, height, fps, has_audio, size_mb, issues, metrics, stderr}

硬性规则 (v1): FILE_MISSING / DURATION_TOO_SHORT / DURATION_TOO_LONG /
RESOLUTION_LOW / ASPECT_NOT_ALLOWED(±0.05) / NO_AUDIO / FILE_TOO_LARGE /
PROBE_FAILED。v2 扩展 (黑帧/响度/OCR) 走同一 issues 数组。

安全约束: 只读输入文件; 不发起任何网络请求; 无文件写权限预期。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _probe(video_path: str) -> dict:
    """ffprobe → {format, streams}。失败抛 RuntimeError。"""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe exit={result.returncode}: {result.stderr[-500:]}")
    data = json.loads(result.stdout or "{}")
    return data


def _aspect_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "0:0"
    import math

    g = math.gcd(width, height)
    return f"{width // g}:{height // g}"


def _aspect_allowed(ratio: str, allowed: list[str], tolerance: float = 0.05) -> bool:
    """比例是否在白名单 (数值比较, 容差 ±0.05)。"""
    if not ratio or ":" not in ratio:
        return False
    try:
        r_num, r_den = ratio.split(":")
        actual = float(r_num) / float(r_den)
    except (ValueError, ZeroDivisionError):
        return False
    for candidate in allowed:
        try:
            c_num, c_den = candidate.split(":")
            target = float(c_num) / float(c_den)
        except (ValueError, ZeroDivisionError):
            continue
        if abs(actual - target) <= tolerance:
            return True
    return False


def evaluate(request: dict) -> dict:
    video_path = str(request.get("video_path") or "")
    spec = request.get("spec") or {}

    issues: list[dict] = []
    path = Path(video_path)

    # FILE_MISSING
    if not path.exists() or not path.is_file() or os.access(path, os.R_OK) is False:
        issues.append({"code": "FILE_MISSING",
                       "message": f"文件不存在或不可读: {video_path}",
                       "severity": "high"})
        return _result(False, issues=issues)

    # FILE_TOO_LARGE
    size_mb = path.stat().st_size / (1024 * 1024)
    max_size_mb = float(spec.get("max_size_mb") or 100.0)
    if size_mb > max_size_mb:
        issues.append({"code": "FILE_TOO_LARGE",
                       "message": f"{size_mb:.1f}MB > {max_size_mb:.1f}MB",
                       "severity": "high"})

    # PROBE_FAILED (吞掉内部错误, 记录到 stderr)
    stderr = ""
    try:
        data = _probe(video_path)
    except Exception as exc:
        stderr = str(exc)
        issues.append({"code": "PROBE_FAILED", "message": f"ffprobe 失败: {exc}",
                       "severity": "high"})
        return _result(False, issues=issues, stderr=stderr)

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    duration_s = float(fmt.get("duration") or 0.0)
    size_fmt = float(fmt.get("size") or 0.0) / (1024 * 1024)
    size_mb = size_fmt or size_mb

    # 视频流 / 音频流
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    width = int(video_stream.get("width") or 0) if video_stream else 0
    height = int(video_stream.get("height") or 0) if video_stream else 0
    fps_raw = (video_stream.get("r_frame_rate") or "0/1") if video_stream else "0/1"
    try:
        fps_num, fps_den = fps_raw.split("/")
        fps = float(fps_num) / float(fps_den) if float(fps_den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    # 时长
    min_dur = float(spec.get("min_duration_s") or 0.0)
    max_dur = float(spec.get("max_duration_s") or 0.0)
    if min_dur > 0 and duration_s < min_dur:
        issues.append({"code": "DURATION_TOO_SHORT",
                       "message": f"{duration_s:.2f}s < min {min_dur:.1f}s",
                       "severity": "high"})
    if max_dur > 0 and duration_s > max_dur:
        issues.append({"code": "DURATION_TOO_LONG",
                       "message": f"{duration_s:.2f}s > max {max_dur:.1f}s",
                       "severity": "high"})

    # 分辨率
    min_w = int(spec.get("min_width") or 0)
    min_h = int(spec.get("min_height") or 0)
    if min_w > 0 and width < min_w:
        issues.append({"code": "RESOLUTION_LOW",
                       "message": f"{width} < min_width {min_w}",
                       "severity": "high"})
    if min_h > 0 and height < min_h:
        issues.append({"code": "RESOLUTION_LOW",
                       "message": f"{height} < min_height {min_h}",
                       "severity": "high"})

    # 比例白名单
    allowed = spec.get("aspect_ratios") or []
    ratio = _aspect_ratio(width, height)
    if allowed and not _aspect_allowed(ratio, allowed):
        issues.append({"code": "ASPECT_NOT_ALLOWED",
                       "message": f"比例 {ratio} 不在 {allowed} (±0.05)",
                       "severity": "high"})

    # 音频
    if spec.get("require_audio", True) and audio_stream is None:
        issues.append({"code": "NO_AUDIO", "message": "require_audio 但无音轨",
                       "severity": "high"})

    metrics = {"duration_s": round(duration_s, 3), "width": width, "height": height,
               "fps": round(fps, 3), "has_audio": audio_stream is not None,
               "size_mb": round(size_mb, 3), "ratio": ratio}

    return _result(not issues, duration_s=duration_s, width=width, height=height,
                   fps=fps, has_audio=audio_stream is not None, size_mb=size_mb,
                   issues=issues, metrics=metrics, stderr=stderr)


def _result(passed: bool, **kw: Any) -> dict:
    base = {"passed": passed, "duration_s": 0.0, "width": 0, "height": 0,
            "fps": 0.0, "has_audio": False, "size_mb": 0.0,
            "issues": [], "metrics": {}, "stderr": ""}
    base.update(kw)
    return base


def main() -> None:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"passed": False,
                          "issues": [{"code": "PROBE_FAILED",
                                      "message": f"stdin JSON 解析失败: {exc}",
                                      "severity": "high"}],
                          "stderr": str(exc)}))
        return
    result = evaluate(request)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
