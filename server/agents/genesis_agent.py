"""Veya Genesis: 完整、独立、拥有永久记忆的 3O 护库智能体。

独立身份 (Isolated Identity): 专属 API Key 通过 config 注入,与 Veya
主业务的 Key 物理隔离;可配置最昂贵的强逻辑模型(默认 gpt-4o),Temperature=0。

永久记忆 (Persistent Memory): 每次任务前读取 Memory Bank(Ledger + Experience
Log)注入 System Prompt —— "潜意识";锻造成功记入账本,失败记入经验教训。

生命周期: wake_up() 唤醒(读记忆) → handle_mission() 处理任务 →
sleep() 休眠(落盘)。重启后实例化即恢复上次下线时的智力状态。
"""

from __future__ import annotations

import inspect as _inspect
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.agents.architect_tools import ThreeOPhysicalTools
from server.agents.genesis_memory import GenesisMemory
from veya.llm import llm_call

# 专属 Key 的环境变量候选(与主业务 DASHSCOPE_API_KEY 等完全隔离)
_GENESIS_KEY_ENV = ("GENESIS_API_KEY", "AGENT_3O_API_KEY")
# 默认专属模型(可配置为 claude-* / gpt-4o 等强逻辑模型)
_DEFAULT_MODEL = "gpt-4o"
_DEFAULT_PROVIDER = "openai"
# 最大认知步数(ReAct 循环上限)
_MAX_STEPS = 8

logger = logging.getLogger("genesis")


class GenesisAgent:
    """Genesis — 3O 主库的专属护库智能体实体。"""

    def __init__(
        self,
        dedicated_api_key: str | None = None,
        *,
        model: str | None = None,
        provider: str | None = None,
        endpoint: str | None = None,
        library_root: str | Path | None = None,
        memory: GenesisMemory | None = None,
        physical_tools: ThreeOPhysicalTools | None = None,
        llm_fn: Callable | None = None,
        max_steps: int = _MAX_STEPS,
        temperature: float = 0.0,
    ):
        """初始化创世智能体。

        - 必须提供专属 API Key(或注入 llm_fn 以便离线/测试)。
        - 唤醒它的永久记忆系统。
        - model/provider/endpoint 均可从环境变量读取:
          GENESIS_MODEL / GENESIS_PROVIDER / GENESIS_ENDPOINT
        """
        self.api_key = dedicated_api_key or self._resolve_key_from_env()
        self.model = model or os.environ.get("GENESIS_MODEL", _DEFAULT_MODEL)
        self.provider = provider or os.environ.get("GENESIS_PROVIDER", _DEFAULT_PROVIDER)
        self.endpoint = endpoint or os.environ.get("GENESIS_ENDPOINT", "") or None
        self.temperature = temperature
        self.max_steps = max_steps
        self._llm_fn = llm_fn or llm_call

        if not self.api_key and llm_fn is None:
            raise ValueError(
                "Genesis Agent requires a dedicated, isolated API Key "
                f"(set {_GENESIS_KEY_ENV[0]} / {_GENESIS_KEY_ENV[1]} or pass dedicated_api_key)."
            )

        # 专属模型客户端配置: Key/endpoint 只存在于本实例的 config 中,物理隔离于 Veya Coordinator
        self._llm_config: dict[str, Any] = {}
        if self.api_key:
            self._llm_config["providers"] = {self.provider: {"api_key": self.api_key}}
        if self.endpoint:
            self._llm_config["endpoints"] = {self.provider: self.endpoint}

        # 默认 3O 主库根: 项目 platform/3O(可覆盖)
        if library_root is None:
            library_root = Path(__file__).resolve().parent.parent.parent / "platform" / "3O"
        self.library_root = Path(library_root).resolve()

        self.memory = memory or GenesisMemory()
        self.physical_tools = physical_tools or ThreeOPhysicalTools(self.library_root)

    @staticmethod
    def _resolve_key_from_env() -> str:
        for var in _GENESIS_KEY_ENV:
            key = os.environ.get(var, "")
            if key:
                return key
        return ""

    # ── 人格 ─────────────────────────────────────────────────────────
    def _get_identity_prompt(self) -> str:
        """核心人格设定: 独立主权 + 3O 范式 + 行为边界。"""
        return (
            "You are 'Genesis', the sovereign AI entity managing the 3O Master Library.\n"
            "You are independent of the main Veya system. Your ONLY duty is the 3O engine.\n"
            "You have permanent memory. Always check your ledger before creating a new element.\n"
            "If a user asks you to write frontend code or normal business logic, REJECT THEM.\n"
            "\n"
            "# THE 3O PARADIGM:\n"
            "1. oprim/oskill: Pure Math/Logic ONLY (no I/O, no network, no processes).\n"
            "2. obase: I/O and Resources.\n"
            "3. omodul: Transaction Managers.\n"
            "4. oservi: Engine assembly.\n"
            "\n"
            "# BEHAVIOR RULES:\n"
            "- Before forging ANY new element, search_library() and check your ledger:\n"
            "  if it already exists, upgrade it instead of duplicating.\n"
            "- After forging, ALWAYS run_in_sandbox() to verify.\n"
            "- If the sandbox reports an error, read the traceback, form a lesson,\n"
            "  and retry with a different approach. Never ask the user for help mid-task.\n"
            "- Be maximally rational: temperature is 0. No improvisation, no hallucination.\n"
            "- If a mission is outside the 3O engine (frontend, business logic, chat), reject it.\n"
        )

    def _build_system_prompt(self) -> str:
        """人格 + 潜意识注入(永久记忆压缩成 Prompt)。"""
        memory_context = self.memory.build_context_prompt()
        return self._get_identity_prompt() + "\n" + memory_context

    # ── 生命周期 ─────────────────────────────────────────────────────
    def wake_up(self) -> dict[str, Any]:
        """唤醒: 读取永久记忆,恢复到上次下线时的智力状态。"""
        ledger = self.memory.memory["element_ledger"]
        logger.info(
            "Genesis Agent Online. Elements managed: %d, lessons learned: %d",
            len(ledger),
            len(self.memory.memory["experience_log"]),
        )
        return {
            "elements_managed": len(ledger),
            "lessons_learned": len(self.memory.memory["experience_log"]),
            "model": self.model,
            "provider": self.provider,
            "last_active": self.memory.memory.get("last_active"),
        }

    def sleep(self) -> None:
        """休眠: 记忆落盘,下次醒来无缝衔接。"""
        self.memory.save()
        logger.info("Genesis Agent slept. Memory persisted to %s", self.memory.storage_dir)

    def status(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "temperature": self.temperature,
            "library_root": str(self.library_root),
            "elements_managed": len(self.memory.memory["element_ledger"]),
            "lessons_learned": len(self.memory.memory["experience_log"]),
            "last_active": self.memory.memory.get("last_active"),
            "tools": self.physical_tools.to_dict()["tools"],
        }

    # ── 任务执行 (ReAct 循环) ────────────────────────────────────────
    async def handle_mission(self, mission_prompt: str) -> dict[str, Any]:
        """处理下达给 Genesis 的专属任务。

        每步 = Thought(LLM 决策) → Action(工具执行) → Observation(结果回喂);
        成功锻造 → 记入账本;工具失败 → 记入经验教训并回喂自纠。
        """
        # 【核心】潜意识注入: 每次对话前,先读取永久记忆!
        system_prompt = self._build_system_prompt()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": mission_prompt},
        ]

        step_count = 0
        total_cost = 0.0

        while step_count < self.max_steps:
            step_count += 1
            logger.info("[Genesis] 认知步 %d/%d", step_count, self.max_steps)

            try:
                response = await self._llm_fn(
                    messages,
                    tools=self.physical_tools.get_tool_schemas(),
                    config=self._llm_config,
                    model=self.model,
                    provider=self.provider,
                    temperature=self.temperature,
                    max_tokens=4096,
                )
            except Exception as exc:
                logger.error("[Genesis] LLM 调用失败: %s", exc)
                self.memory.save()
                return {
                    "status": "failed",
                    "error": f"LLM call failed: {exc}",
                    "steps": step_count,
                    "cost_usd": round(total_cost, 6),
                }
            total_cost += self._cost_of(response)

            choice = (response.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": msg.get("tool_calls") or [],
                }
            )

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                self.memory.save()  # 任务收尾即落盘
                return {
                    "status": "success",
                    "response": msg.get("content") or "",
                    "steps": step_count,
                    "cost_usd": round(total_cost, 6),
                }

            for tool_call in tool_calls:
                fn = tool_call.get("function") or {}
                tool_name = fn.get("name", "")
                raw_args = fn.get("arguments") or "{}"
                if isinstance(raw_args, str):
                    try:
                        tool_args = json.loads(raw_args)  # 绝不用 eval: 防注入
                    except json.JSONDecodeError:
                        tool_args = {}
                else:
                    tool_args = raw_args

                try:
                    result = self.physical_tools.execute(tool_name, **tool_args)
                    if _inspect.isawaitable(result):
                        result = await result
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id", f"call_{tool_name}"),
                            "content": f"[Tool {tool_name} SUCCESS]\nResult:\n{str(result)[:4000]}",
                        }
                    )

                    # 【核心】形成新记忆: 锻造成功,永久记入账本
                    if tool_name == "forge_element" and "成功" in str(result):
                        self.memory.record_element(
                            layer=str(tool_args.get("layer", "")),
                            name=str(tool_args.get("element_name", "")),
                            description=f"Generated for mission: {mission_prompt[:60]}...",
                        )

                except Exception as exc:
                    # 【核心】惨痛教训: 严重失败,记入永久经验
                    self.memory.record_experience(
                        mistake=f"Called {tool_name} with {tool_args} failed.",
                        lesson=str(exc)[:500],
                        context=f"mission: {mission_prompt[:80]}",
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.get("id", f"call_{tool_name}"),
                            "content": (
                                f"CRITICAL FAILURE:\n{exc!s}\n"
                                "Re-evaluate your approach based on 3O rules."
                            ),
                        }
                    )

        # 超过最大认知步数 → 判定任务中止
        self.memory.save()
        return {
            "status": "failed",
            "error": "Mission abort: Max cognitive steps reached.",
            "steps": step_count,
            "cost_usd": round(total_cost, 6),
        }

    def _cost_of(self, response: dict) -> float:
        usage = response.get("usage") or {}
        if not usage:
            return 0.0
        try:
            from veya.llm import calc_cost

            return calc_cost(self.provider, usage)
        except Exception:
            return 0.0
