"""server.coordinator_master 长程任务装配测试。

验证: MasterCoordinator(long_task_factory=...) 把驱动传给主库
chat_stream 的长程钩子 (配额暂停 → paused_by_quota; 默认无 factory
行为与原来一致)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.coordinator_master import MasterCoordinator

# ---------------------------------------------------------------------------
# stubs
# ---------------------------------------------------------------------------


class FakeTools:
    def get_all_schemas(self) -> list[dict]:
        return []

    def list_tools(self) -> list[str]:
        return []

    def describe(self, name: str) -> str:
        return name

    def has(self, name: str) -> bool:
        return False

    async def execute(self, name: str, kwargs: dict) -> str:
        return "[tool executed]"


class FakeSkillHub:
    def get_all_schemas(self) -> list[dict]:
        return []

    def list_skills(self) -> list[str]:
        return []

    def describe(self, name: str) -> str:
        return name

    def reload_skills(self) -> dict:
        return {"loaded": 0, "skipped": 0}

    async def execute(self, name: str, kwargs: dict) -> str:
        return "[skill executed]"


class FakeMemory:
    def inject_subconscious(self) -> str:
        return ""

    def add_preference(self, **kwargs: Any) -> str:
        return "ok"

    def remove_preference(self, **kwargs: Any) -> str:
        return "ok"


class FakeSwarm:
    async def run_swarm(self, overarching_goal: str, sub_tasks: list[dict]) -> str:
        return "swarm done"


class FakeVault:
    async def execute_secure_tool(self, **kwargs: Any) -> str:
        return "vault done"


class FakeAutomata:
    def register_cron_task(
        self, cron_expr: str, task_prompt: str, task_id: str | None = None
    ) -> str:
        return "job-1"

    def remove_task(self, task_id: str) -> str:
        return "removed"

    def get_jobs(self) -> list[dict]:
        return []


class FakeRag:
    def search_context(self, query: str, top_k: int = 3) -> str:
        return ""

    def reindex_workspace(self, force: bool = False) -> str:
        return "ok"


class FakeOmni:
    def get_llm_schema(self) -> dict:
        return {}

    async def execute_dispatch(self, targets: list[str], title: str, content: str) -> str:
        return "dispatched"


class FakeLongTask:
    def __init__(self, *, quota_ok: bool = True) -> None:
        self.quota_ok = quota_ok
        self.pre_calls = 0

    async def pre_round(self):
        self.pre_calls += 1
        return SimpleNamespace(
            quota_ok=self.quota_ok,
            remaining_usd=0.0 if not self.quota_ok else 5.0,
            prompt_suffix="",
            next_action=None,
            goal_summary="todo 1/2 done",
        )

    async def post_round(self, outcome: dict) -> None:
        pass


async def _fake_llm(messages: list, **kwargs: Any) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": "done"}}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _make_coordinator(long_task_factory=None) -> MasterCoordinator:
    return MasterCoordinator(
        llm_fn=_fake_llm,
        tools=FakeTools(),
        skill_hub=FakeSkillHub(),
        memory_bank=FakeMemory(),
        automata=FakeAutomata(),
        swarm_engine=FakeSwarm(),
        rag_engine=FakeRag(),
        vault=FakeVault(),
        omni_gateway=FakeOmni(),
        long_task_factory=long_task_factory,
        max_rounds=4,
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coordinator_long_task_quota_pause_wired():
    """long_task_factory 装配: 配额暂停 → paused_by_quota, 主脑返回对应状态。"""
    fake = FakeLongTask(quota_ok=False)
    coord = _make_coordinator(long_task_factory=lambda: fake)
    result = await coord.chat_stream("写代码", session_id="s1")
    assert result["status"] == "paused_by_quota"
    assert fake.pre_calls >= 1
    assert "quota" in result["final_answer"].lower()


@pytest.mark.asyncio
async def test_coordinator_default_no_long_task():
    """默认无 factory: 行为与原来一致 (直答 success)。"""
    coord = _make_coordinator()
    result = await coord.chat_stream("hi", session_id="s2")
    assert result["status"] == "success"
    assert result["final_answer"] == "done"
