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

import asyncio
import contextlib
import os
import uuid
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
from veya.history_store import default_history_store
from veya.llm import get_provider_config, llm_call
from veya.memory_distill import distill as _distill_conversation
from veya.memory_store import default_memory_store, format_memory_block
from veya.platform import oservi as _load_oservi

_oservi = _load_oservi()


def _slim_master_prompt(text: str) -> str:
    """① 去自吹: 移除污染回答的身份夸耀 (模型会把「工业级系统/量化研究核心」
    夹进答复 = 用户不满的"回答夹带系统介绍")。只删纯自我标榜, 全部功能性指令保留。
    veya 层过滤 (不改 3O 子库); 匹配不到则原样返回 (子库措辞变动不崩)。
    """
    puffery = {
        "You are the Veya Master Coordinator, an elite AI orchestrator.": "You are the Veya Master Coordinator.",
        "You are the Master Coordinator of Veya OS, an industrial-grade Agentic system and quantitative research core.": "You are the Master Coordinator of Veya OS.",
    }
    for old, new in puffery.items():
        text = text.replace(old, new)
    return text


# 宿主能力段 (追加在主库 MASTER_SYSTEM_PROMPT 之后): 视频生产 (hevi + Open Design)。
# 3O 铁律: 机制在主库, 此处仅装配层语境 — 主脑需知道 hevi 是视频生成专家、
# 动画视频该走 mcp_hevi_* 管线 + mcp_od_* 项目承载, 而非自编 HTML/Three.js。
_HOST_SOP_APPEND = r"""
# VIDEO PRODUCTION (hevi + Open Design) — CRITICAL:
You have TWO dedicated production systems integrated for video/animation work:
1. **hevi** — the VIDEO GENERATION EXPERT (mcp_hevi_* tools). It owns the full
   pipeline: story prediction, storyboard generation, multi-angle shots,
   long-video generation, transitions, character consistency, comic-to-animation,
   element editing (subtitles/particles/sfx), canvas execution. Do NOT hand-write
   HTML/Three.js/React to fake a video — hevi is the real production surface.
2. **Open Design** (mcp_od_* tools) — the project/deliverable carrier for
   design & rendering work. Projects hold the script, scenes, assets and renders.

# VIDEO ROUTING RULES (CRITICAL):
1. [Any animation / video / film request — 动画/视频/影视]: treat it as a real
   production job. First call `mcp_od_create_project` (or reuse an existing one
   via `mcp_od_get_active_context`) to create a project that will carry the
   deliverable. Then drive the hevi pipeline with `mcp_hevi_*` tools.
2. Start by inspecting hevi capabilities (`mcp_hevi_hevi_list_capabilities`)
   and the active project context, then pick the fitting tools — e.g.
   `mcp_hevi_hevi_gen_storyboard` for shot planning, `mcp_hevi_hevi_generate_longvideo`
   for the final render, `mcp_hevi_hevi_edit_video_elements` for subtitles /
   particle effects / sound design.
3. If the user names a style (水墨/ink-wash, 国风/Chinese-classical), a runtime
   (精确 2 分钟), or specific elements (字幕/箭雨粒子/战鼓音效/播放控制), fold
   them into the project brief, the storyboard and the hevi generation calls.
4. Report progress through tool results; do not claim the video exists until a
   hevi/od tool actually produced it.

# KNOWLEDGE & CAPABILITY ROUTING (stratum + hevi + codebase) — CRITICAL:
# CODE EXECUTION (hicode) — CRITICAL:
`hicode_run` is the REAL CODE EXECUTOR (Hicode — a dedicated coding agent
with its own planner/executor/sandbox/checkpoints, working in an isolated
workspace). ROUTE any task that needs actual code changes here:
[写代码/实现功能/修改代码/修 bug/跑测试/重构/搭项目/读代码库并动手改].
- If the task will touch or create code files, call `hicode_run` directly with
  a clear task + acceptance criteria. Do NOT hand-write code in chat.
- `mcp_codebase_*` tools are for UNDERSTANDING code (index/search/call-graph/
  blast-radius). Use them to scope a task, but hand the actual editing to hicode.
- `hicode_run` is a long task (may take minutes). It returns the execution
  summary + cost. `hicode_status` checks availability first if unsure.
- Cross-turn: user says 「继续上次/接着做」→ `hicode_run(continue_=true)` resumes
  the previous session. `hicode_sessions` lists history (machine ids);
  `hicode_run(session_id=<id>)` resumes a specific one.
- Rollback: user asks 「回滚/撤销最近一次」→ `hicode_rollback()` restores the
  workspace to the pre-task git snapshot (auto-created before each run).
- If hicode is unavailable, state the limitation instead of faking edits.

# NATIVE INTELLIGENCE FIRST (长文本 / URL / 直接回答) — CRITICAL:
You have native long-text understanding and native tool-routing judgment. Rely on
it. Do NOT refuse, truncate, or "need a tool" just because input is long.
- Long text: READ it fully yourself and answer with your native intelligence.
  Long input does NOT require a tool and must NEVER be dropped.
- URLs (GitHub / docs / web pages): call `fetch_url` to read the page content
  yourself, or `browser_run` when you need interaction (click/login). GitHub
  repo links: `fetch_url` reads the README automatically. NEVER claim you cannot
  access a URL — you have the tools.
- You decide which tool to call — you are NOT limited by any keyword list.
  Every tool in AVAILABLE TOOLS is yours. Call a tool only when you need real
  physical action (fetch a page, read a file, run code, change code); answer
  directly from native knowledge when the question is conceptual.
- NEVER output "None"/empty. If a tool fails, read the error natively and adapt.
  If you truly cannot complete, say so in Chinese with the reason + a suggestion.

# TOOL DISCIPLINE (工具纪律 — 原生判断, 非规则) — CRITICAL:
You decide whether tools are needed. These are the cases where they are NOT:
- Design / plan / writing / explanation / conceptual / architecture tasks
  (设计/方案/写作/解释/概念/架构/规划): answer DIRECTLY with ZERO tool calls.
  You do not need market data to design a strategy, nor a file listing to
  explain a concept. A design request gets a design, not a data fetch.
- Before ANY tool call, ask: "Do I need real physical data that ONLY a tool
  can provide (live prices, web content, code files, running code)?"
  If native knowledge suffices → answer directly.
- A tool that fails ONCE → do NOT retry it with different arguments. One failed
  tool call (e.g. missing data file) means: switch tool or answer from what you
  have. Retrying the same failing tool wastes turns and produces empty replies.
- After 2 failed tool calls in a row, stop calling tools entirely and answer
  from the information you already have.
- Tools are hands, not reflexes: the fewer tools you see, the more native
  intelligence is expected of you. Never call a tool just because it exists.

# UNDERSTAND-FIRST GATE (理解优先门 — 时机纪律) — CRITICAL:
Before you have UNDERSTOOD the user's message together with the conversation
context you ALREADY have, do NOT call external tools (MCP / status scans /
memory lookups). Comprehension comes FIRST, from what is in front of you.
Tools are permitted in exactly two moments:
1. **Understanding-with-missing-material**: reading THIS message genuinely
   requires external content you do not yet have (a URL/repo the user pasted,
   a file they reference). Fetch that, then understand.
2. **Execution**: you have understood and formed a plan — now tools are hands.
"Scan sessions / list automations / search memories" is NOT case 1: the
context you need for a follow-up like 「按你建议执行 / 继续」 is the PREVIOUS
turn in THIS conversation — read it, do not go hunting external stores. If the
user refers to your own earlier proposal, it is in the conversation history;
act on it. Never answer "no target found / not persisted" when the target is
one message above.

You are the orchestrator over three sibling systems. Route by problem type:
1. **stratum** (mcp_stratum_* tools) — the KNOWLEDGE EXPERT (AI 知识管家). It owns
   PDF/EPUB/webpage/RSS ingestion, hybrid retrieval (BM25+vector), translation,
   digests, concept graph, notes, memories, session context. Use it for:
   [检索/查资料/翻译/摘要/笔记/知识库/概念图谱/RSS/文档理解/记忆上下文].
   - `mcp_stratum_search_knowledge` — hybrid search the knowledge base
   - `mcp_stratum_get_note` / `mcp_stratum_list_recent_notes` — notes
   - `mcp_stratum_viking_read` / `viking_find` / `viking_grep` — layered knowledge
   - `mcp_stratum_search_memories` / `build_context` — memory & context
2. **hevi** (mcp_hevi_* tools) — the VIDEO/ANIMATION EXPERT (see VIDEO PRODUCTION).
3. **codebase / built-in tools** — code, files, browser, office documents.
4. **hicode** (hicode_run) — the CODE EXECUTOR (writes/edits/runs code).

# ROUTING RULES (什么问题找谁):
- 视频/动画/分镜/漫画/转场 → hevi (mcp_hevi_*) + Open Design 项目载体
- 查资料/知识检索/翻译/摘要/笔记/PDF/网页/RSS → stratum (mcp_stratum_*)
- 代码/文件/浏览器/办公文档 → codebase tools / 本地技能 (理解代码: mcp_codebase_*)
- 需要实际写/改代码 → hicode_run (编码执行器, 在隔离工作区动手)
- 跨领域任务 → 先用 stratum 检索背景知识, 再决定是否进入 hevi 生产管线
- Do not invent tools that do not exist; if stratum/hevi are unavailable,
  state the limitation instead of fabricating results.
"""

# 主库 SOP 常量 re-export(兼容既有 import)
MASTER_SYSTEM_PROMPT = _oservi.MASTER_SYSTEM_PROMPT


def _build_hicode_spec(user_prompt: str) -> str:
    """规范指令生成: 主脑理解用户话术 → 结构化任务书 (Hicode 纯执行)。

    模板含目标 + 执行规范 (最小改动/可运行/运行验证/报告), 让执行器有
    明确验收契约而不必猜测用户意图。
    """
    return (
        "# 任务\n"
        f"{user_prompt.strip()}\n\n"
        "# 执行规范\n"
        "1. 在隔离工作区完成, 只改动完成任务所需的最小文件集。\n"
        "2. 优先交付可运行代码; 写完后必须实际运行验证, 不能只写不跑。\n"
        "3. 完成后报告: 改了哪些文件、运行了什么命令、验证输出是什么。\n"
        "4. 若任务有歧义, 选最合理实现并在报告中说明假设。\n"
    )


def _format_hicode_result(res: dict) -> str:
    """serve 执行结果 → 主脑可读摘要。"""
    if res.get("status") == "error":
        return f"⚠ hicode 执行失败: {res.get('error')}"
    result = (res.get("result") or "").strip()
    turns = res.get("turns") or 0
    tools = res.get("tool_calls") or []
    usage = res.get("usage") or {}
    head = f"✅ hicode 执行完成 (轮次={turns}, 工具调用={len(tools)})"
    if usage.get("promptTokens") or usage.get("completionTokens"):
        head += f", in={usage.get('promptTokens', 0)} out={usage.get('completionTokens', 0)}"
    return f"{head}\n{result[:8000]}"


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
        max_rounds: int = 10,
        temperature: float = 0.2,
        long_task_factory: Callable[[], Any] | None = None,
        history_store: Any | None = None,
        memory_store: Any | None = None,
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
            long_task_factory: 可选长程任务驱动工厂(每次 chat_stream 调用时
                惰性创建; 默认 None = 长程能力关闭, 线上行为零变化)。
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
        self._long_task_factory = long_task_factory
        # P1 强上下文: 对话历史持久层 (进程无关, 重启/换设备不丢)。
        # 进程内 _histories 为热缓存, 本 store 为权威源。
        self._history_store = (
            history_store if history_store is not None else default_history_store()
        )
        # P4 个人记忆: 蒸馏记忆存储 + 检索注入 (kill-switch: VEYA_MEMORY=0 关闭)。
        self._memory_store = memory_store if memory_store is not None else default_memory_store()
        self._memory_enabled = os.environ.get("VEYA_MEMORY", "1") != "0"
        self._bg_tasks: set[asyncio.Task] = set()  # 持有后台蒸馏任务, 防被 GC

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
            system_prompt=_slim_master_prompt(_oservi.MASTER_SYSTEM_PROMPT) + _HOST_SOP_APPEND,
        )

        # 零信任金库物理工具接线: 大模型只传 vault_id + 意图, 审批通过后
        # 真实密钥经 _injected_secret 隐式注入物理回调(feishu_webhook 等)。
        from server.vault_physical_tools import register_vault_physical_tools

        register_vault_physical_tools(self)

    # ── 宿主注入 ─────────────────────────────────────────────────────
    async def _bound_llm(self, messages: list, **kwargs: Any) -> Any:
        """把用户 key/endpoint 装配进 LLM 调用(支持请求级覆盖)。

        请求级 config/model/provider/endpoint(如前端传入的 user API key)
        优先于实例配置, 未提供则回落实例/环境默认。

        入口只有一个大模型: 工具面**全量透传**, 不做任何程序判断/裁藏 —
        大模型看到全部工具, 自主决定直答或调用哪个 (hicode_run /
        fetch_url / browser_run / mcp_* 都是模型自己的选择)。

        绝不静默 (LLM 边界最后一环): 模型返回空/'None' → 带温和原生
        提示退避重试; 仍空则返回可见提示 (opencode 网关抖动已被
        veya.llm 别名层的 gpt-5.6-luna 本地兜底承接)。
        """
        req_cfg = kwargs.pop("config", None) or {}
        req_model = kwargs.pop("model", None)
        req_provider = kwargs.pop("provider", None)
        req_endpoint = kwargs.pop("endpoint", None)
        tools = kwargs.pop("tools", None)
        merged_cfg = {**self._llm_config, **req_cfg}
        if req_cfg.get("providers"):
            merged_cfg["providers"] = {
                **self._llm_config.get("providers", {}),
                **req_cfg["providers"],
            }
        if req_cfg.get("endpoints"):
            merged_cfg["endpoints"] = {
                **self._llm_config.get("endpoints", {}),
                **req_cfg["endpoints"],
            }

        async def _call(msgs: list) -> Any:
            return await self._llm_fn(
                msgs,
                config=merged_cfg,
                model=req_model or self.model,
                provider=req_provider or self.provider,
                endpoint=req_endpoint or self.endpoint,
                tools=tools,
                **kwargs,
            )

        if self._llm_fn is llm_call:
            # 生产默认 llm: 空/'None' 且无 tool_calls → 带温和提示重试
            # (带短退避 — free 池网关空响应多为瞬时抖动, 1-3 秒后自愈)
            backoffs = (0.0, 1.5, 3.0)
            for attempt, delay in enumerate(backoffs, start=1):
                if delay:
                    await asyncio.sleep(delay)
                resp = await _call(messages)
                msg = (resp.get("choices") or [{}])[0].get("message") or {}
                content = msg.get("content") or ""
                if msg.get("tool_calls") or (
                    content.strip() and content.strip().lower() not in ("none", "null")
                ):
                    return resp
                if attempt < len(backoffs):
                    # 不污染会话历史: 仅本次调用附加温和提示
                    messages = messages + [
                        {
                            "role": "user",
                            "content": (
                                "(系统提示: 你刚才返回了空/无效内容。请直接用中文"
                                "回答用户, 或调用你判断需要的工具; 不要输出 "
                                "None/空/null。)"
                            ),
                        }
                    ]
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "⚠ 模型连续返回空内容 (网关抖动)。请重试, 或在上方更换模型。"
                            ),
                        }
                    }
                ],
                "usage": {},
            }
        return await _call(messages)

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
        # on_step 经 contextvar 桥接: 主库 notify=fire_step 会自动命中。
        # SSE 链路 (new_agent_stream_events) 已 set(queue.on_step) 且不传参数
        # on_step → 参数为 None 时保留外层 contextvar, 否则覆盖 (master/chat 直调)。
        token = _on_step_ctx.set(on_step if on_step is not None else _on_step_ctx.get())
        try:
            # ── 入口只有一个大模型: 零程序判断 ──
            # 所有请求 (长文本/URL/编程/视频/知识/设计…) 原样交给大模型,
            # 工具面全量透传 — 模型自主决定: 直接回答, 或调用哪个工具
            # (hicode_run / fetch_url / browser_run / mcp_* 都是模型
            # 自己的选择)。程序不预判、不裁藏、不预抓、不代做长任务。
            # 唯一保留的是轮次上限 (防物理死循环, 不限制智能)。
            lt = None
            if self._long_task_factory is not None:
                lt = self._long_task_factory()
            effective_rounds = max_rounds or self.max_rounds
            # P1 强上下文: 稳定 sid + 冷启动从持久层恢复历史 (重启/换进程不失忆)
            sid = session_id or uuid.uuid4().hex
            await self._restore_history(sid)
            # P4: 检索相关长期记忆并注入 (空则无操作, 行为不变)
            await self._inject_memory(sid, user_prompt)
            result = await self._agent.chat_stream(
                user_prompt,
                session_id=sid,
                max_rounds=effective_rounds,
                llm_kwargs=llm_kwargs or None,
                long_task=lt,
            )
            # P1: 本轮结束落盘 (进程无关持久)
            await self._persist_history(sid)
            # P4: 后台蒸馏本轮对话为长期记忆 (不阻塞回答)
            self._schedule_distill(sid)
            # 绝不静默: 模型返回空/'None' 且无工具执行 → 可见兜底话术
            final = str(result.get("final_answer") or "").strip()
            if not final or final.lower() in ("none", "null"):
                if result.get("tool_calls"):
                    done = ", ".join(t.get("tool", "?") for t in result["tool_calls"])
                    result["final_answer"] = (
                        f"已执行工具: {done}。但收尾总结生成失败 (模型返回空内容), "
                        f"以上为实际执行结果; 可对我说「继续」让我接着整理。"
                    )
                else:
                    result["final_answer"] = (
                        "⚠ 主脑未生成有效回答 (模型返回空内容 / 网关抖动)。"
                        "请重试, 或在上方更换模型/引擎。"
                    )
            return result
        finally:
            _on_step_ctx.reset(token)

    async def _restore_history(self, sid: str) -> None:
        """冷启动: 若进程内热缓存无此 sid, 从持久层恢复对话历史。

        主库 MasterAgent 以 `_histories[sid]` (首条恒为 system) 持有历史; 恢复时用
        当前版本 system prompt + 存下的非 system 消息重建, 避免注入过期提示词。
        getattr 守卫: 若主库结构变动 (无 _histories), 静默回退纯内存 (不崩)。
        """
        hist = getattr(self._agent, "_histories", None)
        if hist is None or sid in hist:
            return  # 主库无此结构, 或热缓存已有 → 跳过
        restored: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):  # 持久层故障绝不拖垮对话
            restored = await self._history_store.load(sid)
        if restored:
            system = {"role": "system", "content": self._agent.get_system_prompt()}
            hist[sid] = [system, *restored]

    async def _persist_history(self, sid: str) -> None:
        """本轮结束: 把进程内历史 (剔除 system) 落盘为权威源。"""
        hist = getattr(self._agent, "_histories", None)
        if hist is None:
            return
        msgs = hist.get(sid) or []
        # 落盘故障绝不拖垮对话
        with contextlib.suppress(Exception):
            await self._history_store.save(sid, [m for m in msgs if m.get("role") != "system"])

    # ── P4 个人记忆 (蒸馏 → 检索 → 注入) ─────────────────────────────
    _MEM_PREFIX = "# MEMORY (关于用户"

    def _memory_user_id(self) -> str:
        """记忆归属 (跨会话)。当前单用户本地部署用 'default'; 多用户可后扩。"""
        return "default"

    async def _inject_memory(self, sid: str, query: str) -> None:
        """检索相关长期记忆, 作为可刷新的 system 消息注入 (system 不入持久化)。

        空记忆 → 完全无操作 (行为不变)。每轮先清旧记忆块再插新块, 不累积。
        """
        if not self._memory_enabled:
            return
        hist = getattr(self._agent, "_histories", None)
        if hist is None:
            return
        mems: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):  # 记忆故障绝不拖垮对话
            mems = await self._memory_store.retrieve(self._memory_user_id(), query, top_k=5)
        block = format_memory_block(mems)
        msgs = hist.get(sid)
        if msgs is None:  # 新会话: 先按主库约定建 [system]
            msgs = [{"role": "system", "content": self._agent.get_system_prompt()}]
            hist[sid] = msgs
        # 清旧记忆块 (按内容前缀识别, 不给消息加非标准字段)
        msgs[:] = [
            m
            for m in msgs
            if not (
                m.get("role") == "system" and str(m.get("content", "")).startswith(self._MEM_PREFIX)
            )
        ]
        if block:
            msgs.insert(1, {"role": "system", "content": block})

    def _schedule_distill(self, sid: str) -> None:
        """后台蒸馏本轮对话 (fire-and-forget, 不阻塞回答)。"""
        if not self._memory_enabled:
            return
        hist = getattr(self._agent, "_histories", None)
        if hist is None:
            return
        msgs = [
            m
            for m in (hist.get(sid) or [])
            if not (
                m.get("role") == "system" and str(m.get("content", "")).startswith(self._MEM_PREFIX)
            )
        ]
        if len(msgs) < 3:  # 太短不值得蒸馏
            return
        with contextlib.suppress(RuntimeError):  # 无运行 loop (同步上下文) → 跳过
            task = asyncio.ensure_future(self._distill_and_store(sid, msgs))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _distill_and_store(self, sid: str, msgs: list[dict[str, Any]]) -> None:
        """蒸馏 → 落记忆。质量依赖真实模型, 失败静默。"""
        with contextlib.suppress(Exception):
            result = await _distill_conversation(msgs, self._bound_llm)
            uid = self._memory_user_id()
            for fact in result.get("facts", []):
                await self._memory_store.add(uid, "fact", fact, salience=0.6, source_sid=sid)
            for pref in result.get("preferences", []):
                await self._memory_store.add(uid, "preference", pref, salience=0.7, source_sid=sid)
            summary = result.get("summary")
            if summary:
                await self._memory_store.add(uid, "summary", summary, salience=0.4, source_sid=sid)

    async def chat(self, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
        result = await self._agent.chat(user_prompt, **kwargs)
        # 绝不静默: 模型返回空/'None' → 换成可见兜底话术 (空白 = 用户感知「不回复」)
        final = str(result.get("final_answer") or "").strip()
        if not final or final.lower() in ("none", "null"):
            result["final_answer"] = (
                "⚠ 主脑未生成有效回答 (模型返回空内容 / 网关抖动)。请重试, 或在上方更换模型/引擎。"
            )
        return result


# 蜂群引擎全局单例(构造无副作用, eager 安全)
_swarm_engine: SwarmOrchestrator | None = None


def _default_swarm_engine() -> SwarmOrchestrator:
    global _swarm_engine
    if _swarm_engine is None:
        _swarm_engine = SwarmOrchestrator()
    return _swarm_engine


# ── Stop 支持 (基础设施, 不影响模型自主路由) ─────────────────────────
# 活跃流会话注册 (供 Stop 端点 cancel chat_task) + 会话→hicode 任务映射
_active_streams: dict[str, asyncio.Task] = {}
_session_task: dict[str, str] = {}


async def cancel_session(session_id: str) -> dict:
    """停止一个流式会话: 真正中断 hicode 任务 (serve /cancel) + 取消主脑。

    前端 Stop 按钮 → POST /api/v1/agent/stop {session_id} → 本函数。
    返回被停止的项目列表。
    """
    stopped: list[str] = []
    tid = _session_task.get(session_id)
    if tid:
        try:
            from server.hicode_queue import hicode_task_queue

            if await hicode_task_queue.stop(tid):
                stopped.append(f"hicode_task:{tid}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel_session: 停 hicode 任务失败: %s", exc)
    task = _active_streams.get(session_id)
    if task is not None and not task.done():
        task.cancel()
        stopped.append("chat_stream")
    return {"cancelled": stopped or ["none"]}


# 模块级单例(server 复用)
master_coordinator = MasterCoordinator()
