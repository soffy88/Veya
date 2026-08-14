"""严格 3O 迁移 · 阶段 0 守护测试: 依赖方向 + oskill 纯净度强制检查。

保证两条红线在 CI 里持续生效 (基线模式: 只拦增量违规, 存量入基线逐步清零):

1. check_no_reverse_dep.py — 3O 层单向依赖 (obase ← oprim ← oskill ← omodul ← oservi),
   且 3O 任何层不得 import 业务根 (server/agents/cli/...)。
2. check_oskill_pure.py — oskill 无 I/O / 全局状态 / 非确定性调用;
   /pure/ 目录或 3O-PURE 标记的文件强制纯净 (基线不豁免)。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
BASELINE_REV = SCRIPTS / "baseline_reverse_dep.txt"
BASELINE_PURE = SCRIPTS / "baseline_oskill.txt"


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), str(ROOT), *args],
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_reverse_dep_checker_passes_with_baseline():
    """反依赖检查在基线模式下必须 0 违规 (存量 12 条已入基线)。"""
    assert BASELINE_REV.is_file(), "缺少基线 scripts/baseline_reverse_dep.txt"
    r = _run("check_no_reverse_dep.py", "--baseline", str(BASELINE_REV), "--quiet")
    assert r.returncode == 0, f"反依赖检查失败:\n{r.stdout}\n{r.stderr}"


def test_reverse_dep_checker_detects_new_violation(tmp_path: Path):
    """新引入的上层依赖必须被拦下 (基线不豁免新增)。"""
    pkg = tmp_path / "veya" / "oskill"
    pkg.mkdir(parents=True)
    (pkg / "bad_module.py").write_text("import server\n", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_no_reverse_dep.py"), str(tmp_path), "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 1
    assert "bad_module.py" in r.stdout
    assert "R2" in r.stdout


def test_oskill_pure_checker_passes_with_baseline():
    """oskill 纯净度检查在基线模式下必须 0 新增违规。"""
    assert BASELINE_PURE.is_file(), "缺少基线 scripts/baseline_oskill.txt"
    r = _run("check_oskill_pure.py", "--baseline", str(BASELINE_PURE), "--quiet")
    assert r.returncode == 0, f"oskill 纯净度检查失败:\n{r.stdout}\n{r.stderr}"


def test_oskill_pure_strict_flags_impure_file(tmp_path: Path):
    """/pure/ 目录下的文件必须强制纯净 — open() 直接失败。"""
    pkg = tmp_path / "pure"
    pkg.mkdir(parents=True)
    (pkg / "impure.py").write_text(
        "def f():\n    return open('x')\n", encoding="utf-8"
    )
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_oskill_pure.py"), str(tmp_path),
         "--targets", "pure", "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 1
    assert "IO call open" in r.stdout


def test_oskill_pure_accepts_clean_module(tmp_path: Path):
    """纯函数模块 (无 I/O/全局/非确定性) 必须通过严格检查。"""
    pkg = tmp_path / "pure"
    pkg.mkdir(parents=True)
    (pkg / "clean.py").write_text(
        "import math\n\ndef clamp(x: float, lo: float, hi: float) -> float:\n"
        "    return max(lo, min(x, hi))\n\nSCALE: float = 2.0\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_oskill_pure.py"), str(tmp_path),
         "--targets", "pure", "--quiet"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f"干净模块被误报:\n{r.stdout}\n{r.stderr}"
