"""officecli 场景层 + 基座升级测试 (L1/L2/L3 分层 + help + 场景继承)。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_BASE_RUN = Path(__file__).resolve().parents[1] / "templates" / "skills" / "officecli" / "run.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_scene(name: str):
    path = Path(__file__).resolve().parents[1] / "templates" / "skills" / name / "run.py"
    return _load_module(path, name)


@pytest.fixture(scope="module")
def base_run():
    return _load_module(_BASE_RUN, "officecli_base")


# ── 基座: L1/L2/L3 分层 ──────────────────────────────────────────────

def test_layer_mapping(base_run):
    assert base_run._OP_LAYER["read"] == "L1"
    assert base_run._OP_LAYER["render"] == "L1"
    assert base_run._OP_LAYER["add"] == "L2"
    assert base_run._OP_LAYER["edit"] == "L2"
    assert base_run._OP_LAYER["help"] == "L1"


def test_help_op_in_all(base_run):
    assert "help" in base_run.ALL_OPS
    assert "help" not in base_run.WRITE_OPS


def test_missing_binary_error(base_run):
    """未装 officecli 时错误清晰, 不崩。"""
    import shutil

    if shutil.which("officecli"):
        pytest.skip("officecli 已安装")
    with pytest.raises(RuntimeError, match="officecli 未安装"):
        base_run.main("read", input="x.docx")


def test_unknown_op_raises(base_run):
    with pytest.raises(ValueError, match="未知 op"):
        base_run.main("nope")


# ── 基座: 动态 help (mock 二进制) ────────────────────────────────────

def test_help_schema_cache(base_run, monkeypatch, tmp_path):
    """help 动态 schema: 调二进制 → 缓存; 二次命中缓存。"""
    calls = {"n": 0}

    def fake_which(name):
        return "/usr/bin/fake-officecli"

    def fake_run(cmd, **kw):
        calls["n"] += 1
        class _P:
            returncode = 0
            stdout = '{"paragraph": {"props": ["bold", "size"]}}'
            stderr = ""
        return _P()

    monkeypatch.setattr(base_run.shutil, "which", fake_which)
    monkeypatch.setattr(base_run.subprocess, "run", fake_run)
    monkeypatch.setattr(base_run, "HELP_CACHE_DIR", tmp_path / "cache")

    r1 = base_run._help_cmd("/usr/bin/fake-officecli", "docx", "paragraph")
    base_run._help_cmd("/usr/bin/fake-officecli", "docx", "paragraph")
    assert r1["ok"] and "paragraph" in r1["schema"]
    assert calls["n"] == 1  # 第二次走缓存
    assert (tmp_path / "cache" / "officecli-help.json").exists()


# ── 场景层: 继承基座 ─────────────────────────────────────────────────

@pytest.mark.parametrize("scene", [
    "officecli-pitch-deck",
    "officecli-academic-paper",
    "officecli-financial-model",
    "officecli-data-dashboard",
])
def test_scene_loads_and_has_rules(scene):
    mod = _load_scene(scene)
    rules = mod.scene_rules()
    assert len(rules) > 100
    assert "继承 officecli 基座" in rules
    # manifest 合法
    manifest_path = Path(__file__).resolve().parents[1] / "templates" / "skills" / scene / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == scene
    assert manifest["type"] == "python"


def test_scene_delegates_to_base():
    """场景层 main 委托基座: 未装二进制时报基座错误 (委托成立)。"""
    scene_mod = _load_scene("officecli-pitch-deck")
    import shutil

    if shutil.which("officecli"):
        pytest.skip("officecli 已安装")
    with pytest.raises(RuntimeError, match="officecli 未安装"):
        scene_mod.main(op="add", input="deck.pptx", output="/tmp/deck.pptx")
