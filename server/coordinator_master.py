"""Veya Master Coordinator — 主脑编排器(薄适配层)。

3O 单一来源 (§1.4): 主脑 ReAct 引擎已固化为主库
oservi.master_agent.MasterAgent(SOP/系统工具/路由/循环)。
本层职责:
1. 装配 veya 具体组件(tool_registry / skill_hub / memory_bank / automata /
   swarm / rag / vault)为主库协议实现(鸭子类型, 零包装);
2. 注入 veya.llm.llm_call(带用户 key/endpoint) 与 fire_step 事件桥接;
3. 保留既有 API(MasterCoordinator / MASTER_SYSTEM_PROMPT / chat_stream ...)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from server.events import _on_step_ctx, fire_step
from server.memory_bank import VeyaMemoryBank
from server.memory_bank import memory_bank as _default_memory_bank
from server.skill_hub import VeyaSkillHub
from server.skill_hub import skill_hub as _default_skill_hub
from server.swarm_manager import SwarmOrchestrator
from server.tool_registry import master_tools
from server.workspace_rag import get_rag_engine as _default_rag_factory
from veya.llm import get_provider_config, llm_call
from veya.platform import oservi as _load_oservi

_oservi = _load_oservi()

# 主库 SOP 常量 re-export(兼容既有 import)
MASTER_SYSTEM_PROMPT = _oservi.MASTER_SYSTEM_PROMPT


class MasterCoordinator:
    """主脑: 把用户请求路由到后端工具 / 子 Agent (Genesis),汇总最终回答。

    工具分三层:
    1. 系统级 (不可卸载): 热重载 / 跨会话记忆读写 / 自动化 / 蜂群 / RAG / Vault
    2. 静态能力: master_tools 注册表 (browser / genesis / ast / sandbox ...)
    3. 动态技能: skill_hub (~/.veya/skills 技能包, 可热重载, 运行时自生长)

    引擎本体委托主库 oservi.master_agent.MasterAgent(§1.4 单一来源)。
    """

    def __init__(
        self,
        user_api_key: str | None = None,
        *,
        model: str | None = None,
        provider: str | None = None,
        endpoint: str | None = None,
        tools: Any | None = None,
        skill_hub: VeyaSkillHub | None = None,
        memory_bank: VeyaMemoryBank | None = None,
        automata: Any | None = None,
        swarm_engine: SwarmOrchestrator | None = None,
        rag_engine: Any | None = None,
        vault: Any | None = None,
        omni_gateway: Any | None = None,
        llm_fn: Callable | None = None,
        max_rounds: int = 8,
        temperature: float = 0.2,
    ):
        """初始化主脑(装配 veya 组件 → 委托主库引擎)。

        Args:
            user_api_key: 驱动主脑的用户侧 Key(可选; 缺省读 provider 环境变量,
                          仍无则 llm_call 自动 stub 回落, 便于离线测试)。
            tools: 静态能力注册表(默认全局 master_tools 单例)。
            skill_hub: 动态技能枢纽(默认全局单例; 测试注入独立实例)。
            memory_bank: 全局偏好账本(默认全局单例; 测试注入独立实例)。
            automata: 后台自动化引擎(默认全局单例; 测试注入独立实例)。
            swarm_engine: 蜂群引擎(默认全局单例; 测试注入独立实例)。
            rag_engine: 工作区语义检索引擎(默认全局单例; 测试注入独立实例)。
            vault: 零信任密钥金库(默认全局单例; 测试注入独立实例)。
            omni_gateway: 全渠道分发网关(默认全局单例; 测试注入独立实例)。
            llm_fn: LLM 调用函数(默认 veya.llm.llm_call; 测试注入用)。
        """
        self.api_key = user_api_key or ""
        self.model = model
        self.provider = provider
        self.endpoint = endpoint
        self.tools = tools if tools is not None else master_tools
        self.skill_hub = skill_hub if skill_hub is not None else _default_skill_hub
        self.memory_bank = memory_bank if memory_bank is not None else _default_memory_bank
        # automata / rag 惰性: 模块级单例构造时无 event loop / 免建索引, 首次使用才创建
        self._automata = automata
        self._rag_engine = rag_engine
        self.swarm_engine = swarm_engine if swarm_engine is not None else _default_swarm_engine()
        if vault is not None:
            self.vault = vault
        else:
            from server.zero_trust_vault import global_vault

            self.vault = global_vault
        # 全渠道分发网关(宿主注入 → 主脑 system_dispatch_omni_channel)
        if omni_gateway is not None:
            self.omni_gateway = omni_gateway
        else:
            from server.omni_gateway import omni_gateway as _default_omni_gateway

            self.omni_gateway = _default_omni_gateway
        self._llm_fn = llm_fn or llm_call
        self.max_rounds = max_rounds
        self.temperature = temperature

        # 用户侧 Key 只注入本实例 config,不影响全局环境
        self._llm_config: dict[str, Any] = {}
        if self.api_key:
            resolved_provider = provider or get_provider_config(None, provider=provider)[0]
            self._llm_config["providers"] = {resolved_provider: {"api_key": self.api_key}}
        if endpoint:
            resolved_provider = provider or get_provider_config(None, provider=provider)[0]
            self._llm_config["endpoints"] = {resolved_provider: endpoint}

        # 主库引擎装配(§1.4)
        self._agent = _oservi.MasterAgent(
            llm_caller=self._bound_llm,
            tools=self.tools,
            skill_hub=self.skill_hub,
            memory=self.memory_bank,
            swarm=self.swarm_engine,
            vault=self.vault,
            automata_factory=lambda: self.automata,
            rag_factory=lambda: self.rag_engine,
            omni_gateway=self.omni_gateway,
            notify=fire_step,
            max_rounds=max_rounds,
            temperature=temperature,
            cost_calculator=self._cost_calculator,
        )

        # 零信任金库物理工具接线: 大模型只传 vault_id + 意图, 审批通过后
        # 真实密钥经 _injected_secret 隐式注入物理回调(feishu_webhook 等)。
        from server.vault_physical_tools import register_vault_physical_tools

        register_vault_physical_tools(self)

    # ── 宿主注入 ─────────────────────────────────────────────────────
    def _bound_llm(self, messages: list, **kwargs: Any) -> Any:
        """把用户 key/endpoint 装配进 LLM 调用(支持请求级覆盖)。

        请求级 config/model/provider/endpoint(如前端传入的 user API key)
        优先于实例配置, 未提供则回落实例/环境默认。
        """
        req_cfg = kwargs.pop("config", None) or {}
        req_model = kwargs.pop("model", None)
        req_provider = kwargs.pop("provider", None)
        req_endpoint = kwargs.pop("endpoint", None)
        merged_cfg = {**self._llm_config, **req_cfg}
        if req_cfg.get("providers"):
            merged_cfg["providers"] = {**self._llm_config.get("providers", {}), **req_cfg["providers"]}
        if req_cfg.get("endpoints"):
            merged_cfg["endpoints"] = {**self._llm_config.get("endpoints", {}), **req_cfg["endpoints"]}
        return self._llm_fn(
            messages,
            config=merged_cfg,
            model=req_model or self.model,
            provider=req_provider or self.provider,
            endpoint=req_endpoint or self.endpoint,
            **kwargs,
        )

    def _cost_calculator(self, response: dict) -> float:
        usage = response.get("usage") or {}
        if not usage:
            return 0.0
        try:
            from veya.llm import calc_cost

            provider, _ = get_provider_config(None, provider=self.provider, model=self.model)
            return calc_cost(provider, usage)
        except Exception:
            return 0.0

    # ── 惰性子系统 ───────────────────────────────────────────────────
    @property
    def automata(self) -> Any:
        if self._automata is None:
            from server.automata import get_automata as _factory

            self._automata = _factory()
        return self._automata

    @automata.setter
    def automata(self, engine: Any) -> None:
        self._automata = engine

    @property
    def rag_engine(self) -> Any:
        if self._rag_engine is None:
            self._rag_engine = _default_rag_factory()
        return self._rag_engine

    # ── 主脑 API(委托主库引擎) ──────────────────────────────────────
    def get_system_prompt(self) -> str:
        return self._agent.get_system_prompt()

    def get_system_schemas(self) -> list[dict]:
        return self._agent.get_system_schemas()

    def get_all_tool_schemas(self) -> list[dict]:
        return self._agent.get_all_tool_schemas()

    def register_secure_tool(self, tool_name: str, callback: Callable) -> None:
        self._agent.register_secure_tool(tool_name, callback)

    async def handle_tool_call(self, tool_name: str, tool_args: dict) -> str:
        return await self._agent.handle_tool_call(tool_name, tool_args)

    async def chat_stream(
        self,
        user_prompt: str,
        *,
        session_id: str | None = None,
        on_step: Callable | None = None,
        max_rounds: int | None = None,
        config: dict | None = None,
        provider: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        """主脑主入口(委托主库 ReAct 循环)。

        on_step 经 contextvar 桥接: 主库 notify=fire_step 会自动命中。
        config/provider/model/endpoint 为请求级 LLM 覆盖(前端传入的 user key)。
        """
        llm_kwargs = {}
        if config:
            llm_kwargs["config"] = config
        if provider:
            llm_kwargs["provider"] = provider
        if model:
            llm_kwargs["model"] = model
        if endpoint:
            llm_kwargs["endpoint"] = endpoint
        token = _on_step_ctx.set(on_step)
        try:
            return await self._agent.chat_stream(
                user_prompt,
                session_id=session_id,
                max_rounds=max_rounds,
                llm_kwargs=llm_kwargs or None,
            )
        finally:
            _on_step_ctx.reset(token)

    async def chat(self, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
        return await self._agent.chat(user_prompt, **kwargs)


# 蜂群引擎全局单例(构造无副作用, eager 安全)
_swarm_engine: SwarmOrchestrator | None = None


def _default_swarm_engine() -> SwarmOrchestrator:
    global _swarm_engine
    if _swarm_engine is None:
        _swarm_engine = SwarmOrchestrator()
    return _swarm_engine


# 模块级单例(server 复用)
master_coordinator = MasterCoordinator()
