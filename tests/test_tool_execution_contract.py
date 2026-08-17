"""工具注册与动态技能执行边界的回归契约。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from server.skill_hub import VeyaSkillHub
from server.tool_registry import MasterToolRegistry, ToolExecutionError

STRICT_PARAMETERS = {
    "type": "object",
    "properties": {
        "count": {"type": "integer", "minimum": 1},
        "mode": {"type": "string", "enum": ["safe", "fast"]},
    },
    "required": ["count", "mode"],
    "additionalProperties": False,
}


@pytest.mark.asyncio
async def test_registry_validates_registered_schema_before_callback():
    called = False

    def callback(**kwargs):
        nonlocal called
        called = True
        return kwargs

    registry = MasterToolRegistry()
    registry.register("strict", "strict callback", STRICT_PARAMETERS, callback)

    with pytest.raises(ToolExecutionError, match="JSON schema validation"):
        await registry.execute("strict", {"count": 0, "mode": "unsafe", "extra": True})

    assert called is False
    assert json.loads(
        await registry.execute("strict", {"count": 2, "mode": "safe"})
    ) == {"count": 2, "mode": "safe"}


@pytest.mark.asyncio
async def test_registry_runs_sync_callback_off_loop_and_awaits_its_result():
    event_loop_thread = threading.get_ident()
    registry = MasterToolRegistry()

    def sync_callback() -> int:
        return threading.get_ident()

    def awaitable_callback(value: int):
        async def finish() -> int:
            await asyncio.sleep(0)
            return value * 2

        return finish()

    empty_schema = {"type": "object", "properties": {}}
    registry.register("thread_id", "thread id", empty_schema, sync_callback)
    registry.register(
        "awaitable",
        "sync callback returning awaitable",
        {"type": "object", "properties": {"value": {"type": "integer"}}},
        awaitable_callback,
    )

    assert int(await registry.execute("thread_id", {})) != event_loop_thread
    assert await registry.execute("awaitable", {"value": 21}) == "42"


@pytest.mark.asyncio
async def test_registry_timeout_is_optional_and_configurable(monkeypatch):
    async def slow() -> str:
        await asyncio.sleep(0.03)
        return "done"

    schema = {"type": "object", "properties": {}}

    monkeypatch.delenv("VEYA_TOOL_TIMEOUT_S", raising=False)
    unlimited = MasterToolRegistry()
    unlimited.register("slow", "slow", schema, slow)
    assert await unlimited.execute("slow", {}) == "done"

    monkeypatch.setenv("VEYA_TOOL_TIMEOUT_S", "0.005")
    configured = MasterToolRegistry()
    configured.register("slow", "slow", schema, slow)
    with pytest.raises(ToolExecutionError, match=r"timed out after 0\.005s"):
        await configured.execute("slow", {})

    no_limit = MasterToolRegistry()
    no_limit.register("slow", "slow", schema, slow, timeout_s=0)
    assert await no_limit.execute("slow", {}) == "done"

    with pytest.raises(ToolExecutionError, match="timed out"):
        await no_limit.execute("slow", {}, timeout_s=0.005)


def _write_strict_skill(skills_dir: Path) -> None:
    package = skills_dir / "strict_skill"
    package.mkdir()
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "name": "strict_skill",
                "description": "A schema-strict test skill",
                "type": "python",
                "entrypoint": "run.py",
                "parameters": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                    "additionalProperties": False,
                },
            }
        ),
        encoding="utf-8",
    )
    (package / "run.py").write_text(
        "def main(count):\n    return {'count': count}\n", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_skill_dispatcher_validates_real_manifest_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SKILL_DISPATCHER", "1")
    _write_strict_skill(tmp_path)
    hub = VeyaSkillHub(skills_dir=tmp_path)

    with pytest.raises(ToolExecutionError, match=r"Skill 'strict_skill'.*schema validation"):
        await hub.execute(
            "run_skill", {"skill_name": "strict_skill", "args": {"count": "one"}}
        )

    result = await hub.execute(
        "run_skill", {"skill_name": "strict_skill", "args": {"count": 2}}
    )
    assert json.loads(result) == {"count": 2}


@pytest.mark.asyncio
async def test_non_dispatcher_skill_validates_manifest_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYA_SKILL_DISPATCHER", "0")
    _write_strict_skill(tmp_path)
    hub = VeyaSkillHub(skills_dir=tmp_path)

    with pytest.raises(ToolExecutionError, match=r"Skill 'strict_skill'.*schema validation"):
        await hub.execute("strict_skill", {})

    assert json.loads(await hub.execute("strict_skill", {"count": 3})) == {"count": 3}
