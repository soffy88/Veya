"""Regression test for P1-3 stage environment isolation and alignment."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    res = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            res[k.strip()] = v.strip().strip("'\"")
    return res


def test_stage_db_isolation():
    """Stage database must not share host, db name, or superuser with production."""
    stage_env_file = Path("/home/soffy/.config/veya/stage.env")
    assert stage_env_file.is_file(), f"{stage_env_file} must exist"
    env_vars = parse_env_file(stage_env_file)
    dsn = env_vars.get("VEYA_EXECUTION_DATABASE_URL", "")
    assert dsn, "VEYA_EXECUTION_DATABASE_URL must be defined for stage"

    parsed = urlparse(dsn)
    # Stage host must be isolated (e.g. veya-stage-db)
    assert parsed.hostname == "veya-stage-db", f"Expected stage DB host 'veya-stage-db', got {parsed.hostname}"
    # Stage user must NOT be production superuser
    assert parsed.username != "veya_runtime", "Stage must not use prod 'veya_runtime' superuser"
    assert parsed.username == "veya_stage_user", f"Expected stage user 'veya_stage_user', got {parsed.username}"
    # Database name
    assert parsed.path.lstrip("/") == "veya_stage"


def test_stage_flags_aligned_with_prod():
    """Stage feature flags must align with deploy/bot_landing_flags.env."""
    flags_file = Path("/data/soffy/projects/veya/deploy/bot_landing_flags.env")
    assert flags_file.is_file()
    flags = parse_env_file(flags_file)

    # Critical landing flags that must be active
    expected_on = [
        "VEYA_TURN_QUEUE",
        "VEYA_WAKE_ROUTER",
        "VEYA_LOOP_MODE",
        "VEYA_FINISH_CHECKPOINT",
        "VEYA_LIVE_STREAM",
        "VEYA_PROMPT_CACHE",
        "VEYA_CONTEXT_STEWARD",
        "VEYA_MEMORY_REVIEW",
        "VEYA_READ_OWN_CHAT",
        "VEYA_BOT_MEMORY_MD",
        "VEYA_PERSISTENT_WORKERS",
        "VEYA_AUTO_REVIEW",
        "VEYA_GROUP_ROLES",
        "VEYA_BOT_SECRETS",
        "VEYA_A11Y_FIRST",
        "VEYA_DOM_READER",
        "VEYA_CHANNEL_LADDER",
        "VEYA_TOOL_ERROR_FRIENDLY",
        "VEYA_SEARCH_FALLBACK",
        "VEYA_CHANNEL_STATUS",
        "VEYA_APPROVAL_GATES",
        "VEYA_ROUTINE_CLOCK",
        "VEYA_SUPERVISOR",
        "VEYA_BRAIN_BREAKER",
        "VEYA_MCP_PRESETS",
        "VEYA_CHANNELS",
        "VEYA_PROVIDER_REGISTRY",
    ]
    for key in expected_on:
        assert flags.get(key) == "1", f"Flag {key} must be enabled ('1') in prod landing set"


def test_stage_compose_file_schema():
    """Stage compose file exists, references veya-stage and veya-stage-db, and does not mount /app code."""
    compose_file = Path("/data/soffy/projects/veya/deploy/docker-compose.stage.yml")
    assert compose_file.is_file()
    text = compose_file.read_text(encoding="utf-8")

    assert "veya-stage-db" in text
    assert "veya-stage" in text
    assert "127.0.0.1:8793:8765" in text
    # Verify immutable: no bind-mounts of /app/server, /app/veya, etc.
    assert "/app/server" not in text
    assert "/app/veya" not in text
    assert "/app/runtime" not in text
