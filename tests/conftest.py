"""Shared test fixtures and configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# 测试套件内禁止 test_gate 递归启动 pytest(agent 执行分队 → H4 hook → pytest → 死循环)
os.environ.setdefault("HICODE_SKIP_TEST_GATE", "1")

# Ensure project root is on sys.path (needed for CI environments)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_project_dir(tmp_path: Path) -> Path:
    """Create a temporary project directory with a minimal hicode.yml."""
    cfg = tmp_path / "hicode.yml"
    cfg.write_text("project:\n  name: test\n  version: 0.0.1\n")
    return tmp_path


@pytest.fixture
def mock_env(tmp_path: Path):
    """Set minimal environment variables for LLM provider config."""
    env = {
        "HICODE_MODELS_DIR": str(tmp_path / "models"),
        "HICODE_CONFIG": str(tmp_path / "hicode.yml"),
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
