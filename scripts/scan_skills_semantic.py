#!/usr/bin/env python3
"""独立技能语义安全扫描(补 server/skill_scan.py AST 层的盲区)。

参照 K-Dense scientific-agent-skills 的 scan_skills.py: 内容哈希缓存(未变的
技能不重扫)、并发扫描、产出 JSON + Markdown 报告。直接遍历技能目录, 不经过
VeyaSkillHub——AST 扫描器已拒载的技能反而最该被语义审查(不能因为已经被
strict 挡了就跳过, 那样永远看不出"是不是真的恶意"), dispatcher 模式也不该
藏名字。

用法:
    python scripts/scan_skills_semantic.py                    # 增量扫描
    python scripts/scan_skills_semantic.py --full              # 强制全量重扫
    python scripts/scan_skills_semantic.py --skill officecli   # 只扫一个
    python scripts/scan_skills_semantic.py --workers 5

环境变量:
    VEYA_SKILLS_DIR   技能目录(默认 ~/.veya/skills)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.skill_scan_semantic import scan_skill_semantics  # noqa: E402
from veya.llm import llm_call  # noqa: E402

_DEFAULT_SKILLS_DIR = str(Path.home() / ".veya" / "skills")
_REPORT_JSON = Path.home() / ".veya" / "skills-security-report.json"
_REPORT_MD = Path.home() / ".veya" / "skills-security-report.md"


def _content_hash(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(f for f in skill_dir.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
    for f in files:
        digest.update(str(f.relative_to(skill_dir)).encode())
        digest.update(b"\0")
        digest.update(f.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _discover_skills(skills_dir: Path, only: str | None) -> list[Path]:
    out = []
    for manifest_path in sorted(skills_dir.glob("*/manifest.json")):
        skill_dir = manifest_path.parent
        if only and skill_dir.name != only:
            continue
        out.append(skill_dir)
    return out


def _load_cache() -> dict[str, dict]:
    if not _REPORT_JSON.exists():
        return {}
    try:
        data = json.loads(_REPORT_JSON.read_text(encoding="utf-8"))
        return {r["name"]: r for r in data.get("results", [])}
    except (json.JSONDecodeError, OSError):
        return {}


async def _scan_one(skill_dir: Path, sem: asyncio.Semaphore) -> dict:
    name = skill_dir.name
    try:
        manifest = json.loads((skill_dir / "manifest.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "name": name,
            "verdict": "unscanned",
            "concerns": [],
            "reasoning": f"manifest 读取失败: {exc}",
        }
    entry_file = skill_dir / str(manifest.get("entrypoint", "run.py"))
    try:
        source = entry_file.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "name": name,
            "verdict": "unscanned",
            "concerns": [],
            "reasoning": f"源码读取失败: {exc}",
        }

    async with sem:
        result = await scan_skill_semantics(
            name=name,
            source=source,
            manifest_description=manifest.get("description", ""),
            llm_call_fn=llm_call,
        )
    return {"name": name, **result}


async def _run(skills_dir: Path, *, only: str | None, full: bool, workers: int) -> dict:
    skill_dirs = _discover_skills(skills_dir, only)
    cache = {} if full else _load_cache()

    to_scan: list[Path] = []
    reused: list[dict] = []
    for skill_dir in skill_dirs:
        content_hash = _content_hash(skill_dir)
        cached = cache.get(skill_dir.name)
        if cached and cached.get("content_hash") == content_hash:
            reused.append(cached)
        else:
            to_scan.append(skill_dir)

    sem = asyncio.Semaphore(workers)
    scanned = await asyncio.gather(*(_scan_one(d, sem) for d in to_scan))
    for skill_dir, result in zip(to_scan, scanned):
        result["content_hash"] = _content_hash(skill_dir)
        result["last_scanned"] = datetime.now(timezone.utc).isoformat()

    all_results = reused + scanned
    all_results.sort(key=lambda r: r["name"])
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "skills_scanned": len(scanned),
        "skills_reused": len(reused),
        "results": all_results,
    }


def _write_reports(report: dict) -> None:
    _REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 技能语义安全扫描报告",
        "",
        f"生成时间: {report['generated']}",
        f"本次扫描: {report['skills_scanned']} 个 · 沿用缓存: {report['skills_reused']} 个",
        "",
    ]
    lines.append("| 技能 | 结论 | 关注点 |")
    lines.append("|---|---|---|")
    badge = {
        "safe": "🟢 safe",
        "suspicious": "🟡 suspicious",
        "malicious": "🔴 malicious",
        "unscanned": "⚪ unscanned",
    }
    for r in report["results"]:
        concerns = "; ".join(r.get("concerns") or []) or "-"
        lines.append(f"| {r['name']} | {badge.get(r['verdict'], r['verdict'])} | {concerns} |")

    flagged = [r for r in report["results"] if r["verdict"] in ("suspicious", "malicious")]
    if flagged:
        lines += ["", "## 需要人工复核", ""]
        for r in flagged:
            lines.append(f"### {r['name']} — {badge.get(r['verdict'], r['verdict'])}")
            lines.append(r.get("reasoning", ""))
            lines.append("")
    _REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--full", action="store_true", help="忽略缓存, 强制全量重扫")
    parser.add_argument("--skill", help="只扫这一个技能名")
    parser.add_argument("--workers", type=int, default=5, help="并发数(默认 5)")
    args = parser.parse_args()

    skills_dir = Path(os.environ.get("VEYA_SKILLS_DIR", _DEFAULT_SKILLS_DIR)).expanduser()
    report = asyncio.run(_run(skills_dir, only=args.skill, full=args.full, workers=args.workers))
    _write_reports(report)

    print(f"扫描 {report['skills_scanned']} 个, 沿用缓存 {report['skills_reused']} 个")
    flagged = [r for r in report["results"] if r["verdict"] in ("suspicious", "malicious")]
    for r in flagged:
        print(f"  [{r['verdict']}] {r['name']}: {r.get('reasoning', '')[:120]}")
    print(f"报告: {_REPORT_MD}")
    if any(r["verdict"] == "malicious" for r in report["results"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
