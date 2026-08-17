"""Shared test fixtures and configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# 测试套件内禁止 test_gate 递归启动 pytest(agent 执行分队 → H4 hook → pytest → 死循环)
os.environ.setdefault("VEYA_SKIP_TEST_GATE", "1")

# Ensure project root is on sys.path (needed for CI environments)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 3O 主库路径注入: 少数模块 (goal_run / boss_entrypoint) 用裸 ``import obase`` 等,
# 依赖 veya.platform 已把 platform/3O/* 注入 sys.path。生产由 app 启动链自然触发,
# 测试须在收集前显式触发一次 —— 否则孤立导入这些模块会 ModuleNotFoundError。
# 一次 load("obase") 即调用 _ensure_paths() 把全部主库目录加进 sys.path。
try:
    from veya import platform as _veya_platform

    if _veya_platform.available("obase"):
        _veya_platform.load("obase")
except Exception:  # pragma: no cover — 子库缺失时优雅降级 (依赖它的测试自会跳过/报错)
    pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project_dir(tmp_path: Path) -> Path:
    """Create a temporary project directory with a minimal veya.yml."""
    cfg = tmp_path / "veya.yml"
    cfg.write_text("project:\n  name: test\n  version: 0.0.1\n")
    return tmp_path


@pytest.fixture
def mock_env(tmp_path: Path):
    """Set minimal environment variables for LLM provider config."""
    env = {
        "VEYA_MODELS_DIR": str(tmp_path / "models"),
        "VEYA_CONFIG": str(tmp_path / "veya.yml"),
    }
    (tmp_path / "models").mkdir(exist_ok=True)
    return env


@pytest.fixture
def mock_async_llm():
    """Return an AsyncMock that simulates an LLM response."""
    mock = AsyncMock()
    mock.achat.return_value = {
        "choices": [{"message": {"content": "Test response"}}],
    }
    return mock


@pytest.fixture
def tmp_sandbox_dir(tmp_path: Path) -> Path:
    """Create a temporary sandbox directory."""
    sb = tmp_path / "sandbox"
    sb.mkdir()
    return sb
