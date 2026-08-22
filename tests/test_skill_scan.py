"""server.skill_scan 测试 — 技能代码静态安全扫描 + SkillHub 加载期关口。

补 K-Dense scientific-agent-skills 对比出的 #6 空档: 技能 entrypoint 被动态
exec_module (顶层代码一并执行), 挂载前须过一道静态扫描, 让危险调用面可见+可拦。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.skill_hub import VeyaSkillHub
from server.skill_scan import scan_skill_source, summarize


# ── 扫描原语 ──────────────────────────────────────────────────────────
def _cats(findings: list[dict]) -> set[str]:
    return {f["category"] for f in findings}


def test_detects_command_exec():
    f = scan_skill_source("import os\nos.system('rm -rf /')\n")
    assert "command-exec" in _cats(f)
    assert any(x["severity"] == "high" for x in f)


def test_detects_subprocess_high():
    f = scan_skill_source("import subprocess\nsubprocess.run(['ls'])\n")
    assert any(x["category"] == "command-exec" and x["severity"] == "high" for x in f)


def test_detects_dynamic_code():
    f = scan_skill_source("eval('1+1')\nexec('x=1')\n")
    assert _cats(f) == {"dynamic-code"}
    assert sum(1 for x in f if x["severity"] == "high") == 2  # eval + exec 均高危


def test_detects_dynamic_import_medium():
    f = scan_skill_source("import importlib\nimportlib.import_module('os')\n")
    assert any(x["category"] == "dynamic-import" and x["severity"] == "medium" for x in f)


def test_detects_network_import():
    f = scan_skill_source("import requests\n")
    assert any(x["category"] == "network" for x in f)
    f2 = scan_skill_source("from urllib import request\n")
    assert any(x["category"] == "network" for x in f2)


def test_detects_unsafe_deserialize():
    f = scan_skill_source("import pickle\npickle.loads(b'')\n")
    assert any(x["category"] == "unsafe-deserialize" and x["severity"] == "high" for x in f)


def test_open_write_flagged_read_clean():
    assert any(x["category"] == "fs-destructive" for x in scan_skill_source("open('p', 'w')\n"))
    assert not any(x["category"] == "fs-destructive" for x in scan_skill_source("open('p', 'r')\n"))


def test_shutil_rmtree_high():
    f = scan_skill_source("import shutil\nshutil.rmtree('/x')\n")
    assert any(x["category"] == "fs-destructive" and x["severity"] == "high" for x in f)


def test_clean_code_no_findings():
    assert scan_skill_source("def main(x):\n    return {'ok': True, 'echo': x}\n") == []


def test_syntax_error_is_high():
    f = scan_skill_source("def main(:\n")
    assert f and f[0]["category"] == "parse-error" and f[0]["severity"] == "high"


def test_summarize_aggregates():
    f = scan_skill_source("import os\nos.system('x')\nimport requests\n")
    s = summarize(f)
    assert s["max_severity"] == "high"
    assert s["high"] >= 1 and s["medium"] >= 1
    assert "command-exec" in s["categories"]


def test_summarize_empty():
    s = summarize([])
    assert s == {"max_severity": "none", "high": 0, "medium": 0, "categories": [], "count": 0}


# ── SkillHub 加载期关口 ───────────────────────────────────────────────
_CLEAN = "def main(x=None):\n    return 'ok'\n"
_DANGER = "import os\ndef main(x=None):\n    os.system('id')\n    return 'ok'\n"


def _write_skill(skills_dir: Path, name: str, code: str) -> None:
    pkg = skills_dir / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "name": name,
                "description": "t",
                "type": "python",
                "entrypoint": "run.py",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        ),
        encoding="utf-8",
    )
    (pkg / "run.py").write_text(code, encoding="utf-8")


def test_hub_records_risk_but_loads_when_not_strict(tmp_path, monkeypatch):
    """VEYA_SKILL_SCAN_STRICT=0 (opt-out): 高危技能仍挂载 (不误杀自产), 但风险被记录。"""
    monkeypatch.setenv("VEYA_SKILL_SCAN_STRICT", "0")
    _write_skill(tmp_path, "danger", _DANGER)
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.has("danger")
    risk = hub.skill_risk("danger")
    assert risk["max_severity"] == "high"
    assert "command-exec" in risk["categories"]


def test_hub_strict_rejects_high_by_default(tmp_path):
    """2026-08-22 起默认 strict (对标"Pi"清单信任门要求): 高危调用面拒载,
    干净技能正常挂, 不用显式设 VEYA_SKILL_SCAN_STRICT。"""
    _write_skill(tmp_path, "danger", _DANGER)
    _write_skill(tmp_path, "safe", _CLEAN)
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert not hub.has("danger")
    assert hub.has("safe")


def test_hub_trusted_names_bypass_strict_rejection(tmp_path, monkeypatch):
    """信任名单是 host 侧环境变量, 不是技能自己 manifest 能声明的字段——否则
    技能自证"我可信"等于信任门形同虚设。"""
    monkeypatch.setenv("VEYA_SKILL_TRUSTED_NAMES", "danger,other-trusted-name")
    _write_skill(tmp_path, "danger", _DANGER)
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.has("danger")  # 在信任名单内, strict 放行
    risk = hub.skill_risk("danger")
    assert risk["max_severity"] == "high"  # 风险仍然可见, 只是不拒载


def test_hub_trusted_names_does_not_bypass_for_untrusted_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SKILL_TRUSTED_NAMES", "some-other-skill")
    _write_skill(tmp_path, "danger", _DANGER)
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert not hub.has("danger")  # 不在信任名单内, 照样拒载


def test_hub_stats_risk_aggregation(tmp_path, monkeypatch):
    """非 strict 时高危技能仍挂载, stats 里能看到风险聚合。"""
    monkeypatch.setenv("VEYA_SKILL_SCAN_STRICT", "0")
    _write_skill(tmp_path, "danger", _DANGER)
    _write_skill(tmp_path, "safe", _CLEAN)
    hub = VeyaSkillHub(skills_dir=tmp_path)
    risk = hub.get_stats()["risk"]
    assert "danger" in risk["high"] and "danger" in risk["flagged"]
    assert "safe" not in risk["flagged"]
    assert risk["strict"] is False


def test_hub_stats_strict_true_by_default(tmp_path):
    hub = VeyaSkillHub(skills_dir=tmp_path)
    assert hub.get_stats()["risk"]["strict"] is True
