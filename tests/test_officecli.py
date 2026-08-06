"""OfficeCLI 集成门禁 — 技能包 / sidecar 管理器 / 渲染-观察-修复闭环。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from server.sidecar_manager import CIRCUIT_FAIL_THRESHOLD, SidecarManager

SKILLS_DIR = Path(__file__).resolve().parent.parent / "templates" / "skills"
OFFICECLI_DIR = SKILLS_DIR / "officecli"


# =========================================================================
# 技能包
# =========================================================================

def test_officecli_manifest_valid():
    manifest = json.loads((OFFICECLI_DIR / "manifest.json").read_text())
    assert manifest["name"] == "officecli"
    assert manifest["type"] == "python"
    props = manifest["parameters"]["properties"]
    assert set(props["op"]["enum"]) == {
        "add", "edit", "read", "convert", "merge", "dump", "batch", "render", "watch"}
    assert (OFFICECLI_DIR / "run.py").exists()
    assert (OFFICECLI_DIR / "cheat_sheet.md").exists()


def test_skill_hub_loads_officecli():
    """skill_hub 热载三件套 (browser_use/agent_reach/officecli)。"""
    from server.skill_hub import VeyaSkillHub

    hub = VeyaSkillHub(skills_dir=str(SKILLS_DIR))
    result = hub.reload_skills()
    assert result["loaded"] >= 3
    assert "officecli" in hub.list_skills()


def test_officecli_write_path_whitelist():
    """写操作路径白名单: workspace 外拒绝 (零信任)。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "officecli_run", OFFICECLI_DIR / "run.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    # 白名单外 → PermissionError
    with pytest.raises(PermissionError):
        mod._check_write_path("/etc/passwd", "add")

    # 白名单内 (workspace / templates) → 通过
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    mod.WORKSPACE = tmp  # monkeypatch workspace 指向临时目录
    ok = mod._check_write_path(str(tmp / "out.docx"), "add")
    assert ok == (tmp / "out.docx").resolve()
    ok2 = mod._check_write_path(str(mod.TEMPLATES_DIR / "t.docx"), "add")
    assert ok2 == (mod.TEMPLATES_DIR / "t.docx").resolve()


def test_officecli_missing_binary_structured():
    """officecli 未装 → 结构化安装指引。"""
    import importlib.util
    import shutil

    if shutil.which("officecli"):
        pytest.skip("officecli 已安装, 跳过")

    spec = importlib.util.spec_from_file_location(
        "officecli_run2", OFFICECLI_DIR / "run.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    with pytest.raises(RuntimeError) as e:
        mod.main("read", input="x.docx")
    assert "未安装" in str(e.value) and "install.sh" in str(e.value)


def test_template_render_placeholder():
    """模板库: {{key}} 占位符替换。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "officecli_run3", OFFICECLI_DIR / "run.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    out = mod.render_template("Hello {{name}}, period={{period}}",
                              {"name": "Veya", "period": "W34"})
    assert out == "Hello Veya, period=W34"


# =========================================================================
# Sidecar 管理器
# =========================================================================

def test_sidecar_missing_binary_structured():
    mgr = SidecarManager()
    with pytest.raises(RuntimeError) as e:
        mgr.start("ghost", ["definitely-not-installed-xyz", "daemon"])
    assert "不可用" in str(e.value)


def test_sidecar_start_health_stop():
    """健康检查通过 → running; stop → stopped。"""
    mgr = SidecarManager()
    rec = mgr.start(
        "sleepy", [sys.executable, "-c", "import time; time.sleep(300)"],
        health=lambda: True, ready_timeout_s=5)
    assert rec.state == "running"
    assert rec.proc is not None and rec.proc.poll() is None

    mgr.stop("sleepy")
    assert mgr.get("sleepy").state == "stopped"


def test_sidecar_unhealthy_then_circuit_breaker():
    """连续失败达到阈值 → 熔断 (circuit open)。"""
    mgr = SidecarManager()
    with pytest.raises(RuntimeError):
        mgr.start(
            "flaky", [sys.executable, "-c", "import time; time.sleep(300)"],
            health=lambda: False, ready_timeout_s=1)
    rec = mgr.get("flaky")
    assert rec is not None and rec.failures >= 1

    # 手动累加失败到阈值 → 熔断打开
    rec.failures = CIRCUIT_FAIL_THRESHOLD
    mgr._maybe_open_circuit("flaky")
    assert rec.circuit_open_until > 0
    assert rec.state == "open"
    assert "熔断" in rec.last_error
    mgr.stop_all()


def test_sidecar_status_shape():
    mgr = SidecarManager()
    assert mgr.status() == []
    mgr.start("probe", [sys.executable, "-c", "import time; time.sleep(60)"],
              health=lambda: True)
    statuses = mgr.status()
    assert statuses[0]["name"] == "probe"
    assert statuses[0]["state"] in ("running",)
    assert "pid" in statuses[0]
    mgr.stop_all()


# =========================================================================
# 渲染→观察→修复 闭环 (dry-run)
# =========================================================================

def test_vision_loop_dry_run():
    """闭环脚本 dry-run: 不执行真实命令, 输出通过。"""

    r = subprocess.run(
        [sys.executable, "scripts/officecli_vision_loop.py",
         "templates/office/weekly_report.md.tpl", "--dry-run"],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "render" in r.stdout
    assert "闭环" in r.stdout or "OK" in r.stdout
