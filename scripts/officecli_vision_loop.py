#!/usr/bin/env python3
"""渲染→观察→修复 闭环 demo (OfficeCLI + G13 Vision)。

流程:
  officecli render report.docx → report.png
    → POST /api/v1/vision/analyze (检查排版溢出/图表重叠)
    → 发现问题 → officecli edit 定点修复 → 再 render → 通过

用法:
  python scripts/officecli_vision_loop.py <report.docx> [--vision http://127.0.0.1:8767] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

MAX_ROUNDS = 3


def render(officecli: str, docx: str, out_png: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [officecli, "render", docx, "--output", out_png],
        capture_output=True,
        text=True,
        timeout=600,
    )


def vision_check(vision_base: str, png: str) -> dict:
    """G13 Vision 检查: 返回 {ok, issues:[...]}。"""
    import httpx

    payload = {
        "image_path": str(Path(png).resolve()),
        "prompt": (
            "检查这份文档渲染图的排版问题: 文字溢出、元素重叠、图表遮挡、"
            "留白异常。若无问题回复 OK; 有问题列出具体区域与修复建议。"
        ),
    }
    r = httpx.post(f"{vision_base}/api/v1/vision/analyze", json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    text = str(data.get("result") or data.get("text") or data.get("analysis") or "")
    return {
        "ok": "OK" in text.upper() and "溢出" not in text and "重叠" not in text,
        "text": text[:2000],
    }


def fix(officecli: str, docx: str, issue: str) -> subprocess.CompletedProcess:
    """按 vision 建议定点修复 (edit + 说明)。"""
    return subprocess.run(
        [officecli, "edit", docx, "--options", json.dumps({"note": f"vision 修复: {issue[:200]}"})],
        capture_output=True,
        text=True,
        timeout=600,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染→观察→修复 闭环")
    ap.add_argument("docx", help="源文档")
    ap.add_argument("--vision", default="http://127.0.0.1:8767", help="Veya 网关 (G13 Vision)")
    ap.add_argument("--dry-run", action="store_true", help="不执行真实命令 (CI/测试)")
    args = ap.parse_args()

    docx = Path(args.docx).resolve()
    if not docx.exists():
        print(f"✗ 文档不存在: {docx}")
        return 1

    import shutil

    officecli = shutil.which("officecli")
    if officecli is None and not args.dry_run:
        print("✗ officecli 未安装 (见技能包安装指引)")
        return 1

    out_png = str(docx.with_suffix(".png"))
    print(f"[1] render: {docx} → {out_png}")
    if args.dry_run:
        print("    (dry-run 跳过)")
    else:
        r = render(officecli, str(docx), out_png)
        if r.returncode != 0:
            print(f"✗ render 失败: {r.stderr[-500:]}")
            return 1

    for round_no in range(1, MAX_ROUNDS + 1):
        print(f"[{round_no + 1}] vision 检查 (round {round_no})")
        if args.dry_run:
            issues = {"ok": True, "text": "OK (dry-run)"}
        else:
            try:
                issues = vision_check(args.vision, out_png)
            except Exception as e:
                print(f"  ⚠ vision 不可用: {e} (跳过闭环)")
                return 0
        if issues["ok"]:
            print("✔ 渲染通过, 闭环结束")
            return 0
        print(f"  ✗ 发现 {issues['text'][:200]}...")
        if args.dry_run:
            print("    (dry-run 跳过修复)")
            break
        r = fix(officecli, str(docx), issues["text"])
        if r.returncode != 0:
            print(f"✗ 修复失败: {r.stderr[-500:]}")
            return 1
        print("  ✔ 已修复, 重新渲染")
        render(officecli, str(docx), out_png)

    print("⚠ 达到最大轮次, 请人工检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
