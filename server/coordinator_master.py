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
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("coordinator.master")

from server.events import _on_step_ctx, fire_step


# ── 编程任务强信号判定 (收尾兜底用, 非前置路由) ────────────────────────
# 模型自主路由优先; 此处仅用于「模型没动手且结果无代码」时收尾交给 reasonix。
_CODE_STRONG = (
    "写一个", "写一段", "写个", "写代码", "编写", "实现", "创建", "新建",
    "修复", "修一下", "修个", "重构", "改成", "改一下", "跑测试", "运行测试",
    "跑一下", "编译", "调试", "加个功能", "新增功能", "搭一个", "构建",
    "写个脚本", "写个程序", "python 脚本", "写个函数",
)
_CODE_EXCLUDE = (
    "解释", "讲解", "为什么", "是什么", "啥是", "区别", "对比", "教程",
    "原理", "怎么理解", "说说", "分析一下原因", "帮我看看这段", "帮我看看这个",
)


def _is_code_execution_task(user_prompt: str) -> bool:
    """强编码意图判定 (排除纯解释类)。"""
    t = user_prompt.lower()
    if any(k in t for k in _CODE_EXCLUDE):
        return False
    return any(k in t for k in _CODE_STRONG)


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
# CODE EXECUTION (reasonix) — CRITICAL:
`reasonix_run` is the REAL CODE EXECUTOR (Reasonix — a dedicated coding agent
with its own planner/executor/sandbox/checkpoints, working in an isolated
workspace). ROUTE any task that needs actual code changes here:
[写代码/实现功能/修改代码/修 bug/跑测试/重构/搭项目/读代码库并动手改].
- If the task will touch or create code files, call `reasonix_run` directly with
  a clear task + acceptance criteria. Do NOT hand-write code in chat.
- `mcp_codebase_*` tools are for UNDERSTANDING code (index/search/call-graph/
  blast-radius). Use them to scope a task, but hand the actual editing to reasonix.
- `reasonix_run` is a long task (may take minutes). It returns the execution
  summary + cost. `reasonix_status` checks availability first if unsure.
- Cross-turn: user says 「继续上次/接着做」→ `reasonix_run(continue_=true)` resumes
  the previous session. `reasonix_sessions` lists history (machine ids);
  `reasonix_run(session_id=<id>)` resumes a specific one.
- Rollback: user asks 「回滚/撤销最近一次」→ `reasonix_rollback()` restores the
  workspace to the pre-task git snapshot (auto-created before each run).
- If reasonix is unavailable, state the limitation instead of faking edits.

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
4. **reasonix** (reasonix_run) — the CODE EXECUTOR (writes/edits/runs code).

# ROUTING RULES (什么问题找谁):
- 视频/动画/分镜/漫画/转场 → hevi (mcp_hevi_*) + Open Design 项目载体
- 查资料/知识检索/翻译/摘要/笔记/PDF/网页/RSS → stratum (mcp_stratum_*)
- 代码/文件/浏览器/办公文档 → codebase tools / 本地技能 (理解代码: mcp_codebase_*)
- 需要实际写/改代码 → reasonix_run (编码执行器, 在隔离工作区动手)
- 跨领域任务 → 先用 stratum 检索背景知识, 再决定是否进入 hevi 生产管线
- Do not invent tools that do not exist; if stratum/hevi are unavailable,
  state the limitation instead of fabricating results.
"""

# 主库 SOP 常量 re-export(兼容既有 import)
MASTER_SYSTEM_PROMPT = _oservi.MASTER_SYSTEM_PROMPT


def _build_reasonix_spec(user_prompt: str) -> str:
    """规范指令生成: 主脑理解用户话术 → 结构化任务书 (Reasonix 纯执行)。

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


def _format_reasonix_result(res: dict) -> str:
    """serve 执行结果 → 主脑可读摘要。"""
    if res.get("status") == "error":
        return f"⚠ reasonix 执行失败: {res.get('error')}"
    result = (res.get("result") or "").strip()
    turns = res.get("turns") or 0
    tools = res.get("tool_calls") or []
    usage = res.get("usage") or {}
    head = f"✅ reasonix 执行完成 (轮次={turns}, 工具调用={len(tools)})"
    if usage.get("promptTokens") or usage.get("completionTokens"):
        head += (f", in={usage.get('promptTokens', 0)} "
                 f"out={usage.get('completionTokens', 0)}")
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
            system_prompt=_oservi.MASTER_SYSTEM_PROMPT + _HOST_SOP_APPEND,
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

        原生智能优先: 路由决策权在模型 — 但工具面对 free 池模型做
        「诱惑管理」: 全量 173 工具会让它被领域工具带偏 (设计任务去查
        行情 → 死循环 → 空回复)。_layer_tools 按任务领域召入工具面
        (非路由判断, 只是可见性管理): 核心执行工具恒在, mcp/技能按
        领域意图召回, 量化数据工具仅真实操作意图召回。

        绝不静默 (LLM 边界最后一环): opencode-go free 池网关间歇性返回
        空/'None' → 带温和原生提示退避重试; 仍空则返回可见提示。
        """
        req_cfg = kwargs.pop("config", None) or {}
        req_model = kwargs.pop("model", None)
        req_provider = kwargs.pop("provider", None)
        req_endpoint = kwargs.pop("endpoint", None)
        tools = kwargs.pop("tools", None)
        if tools:
            tools = self._layer_tools(tools, messages)
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
                msg = ((resp.get("choices") or [{}])[0].get("message") or {})
                content = msg.get("content") or ""
                if msg.get("tool_calls") or (
                    content.strip()
                    and content.strip().lower() not in ("none", "null")
                ):
                    return resp
                if attempt < len(backoffs):
                    # 不污染会话历史: 仅本次调用附加温和提示
                    messages = messages + [{
                        "role": "user",
                        "content": (
                            "(系统提示: 你刚才返回了空/无效内容。请直接用中文"
                            "回答用户, 或调用你判断需要的工具; 不要输出 "
                            "None/空/null。)"
                        ),
                    }]
            return {
                "choices": [{"message": {"role": "assistant", "content": (
                    "⚠ 模型连续返回空内容 (网关抖动)。请重试, "
                    "或在上方更换模型。"
                )}}],
                "usage": {},
            }
        return await _call(messages)

    @staticmethod
    def _layer_tools(tools: list, messages: list) -> list:
        """工具 schema 分层 (诱惑管理, 非路由判断): 只裁 LLM 可见面。

        路由决策权在模型; 但 free 池模型在全量 173 工具下会被领域工具
        带偏 (设计任务去查行情 → 失败重试 → 空回复)。分层后模型看到
        「核心执行工具 + 当前任务领域工具」:
        - 系统级 + 基础执行工具 (fetch_url/reasonix/browser/sandbox/...) 恒保留;
        - mcp_* 按领域意图召入 (视频/设计 → hevi+od, 代码 → codebase,
          知识 → stratum);
        - 量化数据工具 (get_market_data_schema/run_backtest_coprocessor)
          仅**操作意图**召回 (回测/行情数据/实盘/策略验证) — 指标名
          (MACD/均线/RSI) 不触发, 否则「设计一个 MACD 拦截器」会被
          带进查行情的死胡同;
        - 技能/ecc 专家默认剔除, 显式技能/代码意图才召回。
        """

        def _name(s: dict) -> str:
            return (s.get("function") or {}).get("name", "")

        user_text = " ".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        ).lower()
        want_video = any(k in user_text for k in ("视频", "动画", "影片", "短片",
                                                  "影视", "hevi", "分镜",
                                                  "配音", "字幕"))
        want_design = any(k in user_text for k in ("设计", "项目", "od_", "海报",
                                                   "画", "渲染", "资产",
                                                   "方案", "架构", "规划"))
        want_code = any(k in user_text for k in ("代码", "审查", "review", "重构",
                                                 "测试", "bug", "构建", "build",
                                                 "报错", "写一个", "写个", "实现",
                                                 "修复", "编程", "函数", "脚本",
                                                 "开发", "code", "coding",
                                                 "compile", "error"))
        # 量化**操作**意图 (指标名 MACD/均线/RSI 不触发 — 设计任务含指标名
        # 不代表要查行情; 只有真实操作词才召回数据工具)
        want_quant = any(k in user_text for k in ("回测", "行情数据", "实盘",
                                                  "策略验证", "量化分析",
                                                  "backtest", "market data",
                                                  "run_backtest", "获取行情",
                                                  "交易信号", "下单", "k线数据",
                                                  "数据文件", "parquet"))
        want_knowledge = any(k in user_text for k in (
            "检索", "查资料", "查一下", "资料", "翻译", "摘要", "总结", "笔记",
            "知识", "文档", "文章", "pdf", "网页", "rss", "概念", "图谱",
            "记忆", "学习", "研究", "搜索", "stratum", "书签", "收藏",
            "订阅", "资讯", "新闻", "论文", "文献"))
        want_skill = any(k in user_text for k in ("技能", "skill", "专家",
                                                  "审查", "review", "代码审查",
                                                  "code review", "架构师"))

        keep: list[dict] = []
        for s in tools:
            n = _name(s)
            # 系统级 + 基础静态执行工具恒保留
            if n.startswith("system_") or (
                not n.startswith("ecc_") and not n.startswith("skill_")
                and not n.startswith("mcp_")
                and n not in ("get_market_data_schema",
                              "run_backtest_coprocessor")
            ):
                keep.append(s)
                continue
            if n.startswith("mcp_"):
                if ((n.startswith("mcp_hevi_") or n.startswith("mcp_od_"))
                        and (want_video or want_design)) or (
                    n.startswith("mcp_codebase_") and (want_code or want_video)
                ) or (
                    n.startswith("mcp_stratum_") and (want_knowledge or want_code)
                ):
                    keep.append(s)
                continue
            # 量化数据依赖工具: 仅真实操作意图 (指标名不触发)
            if n in ("get_market_data_schema", "run_backtest_coprocessor"):
                if want_quant:
                    keep.append(s)
                continue
            # 技能/ecc 专家: 显式技能/代码意图才召回 (全量注入撑爆免费池)
            if (n.startswith("ecc_") and (want_code or want_skill)) or (
                n.startswith("skill_") and (want_code or want_skill or want_video)
            ):
                keep.append(s)
        return keep

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
            # ── 原生智能优先: 不设任何程序化前置路由 ──
            # 长文本 / URL / 编程 / 视频 / 知识检索……全部交给大模型原生理解,
            # 由模型自主决定: 直接回答, 或调用哪个工具 (reasonix_run /
            # fetch_url / browser_run / mcp_* 都是模型可自主选择的工具面)。
            # 唯一保留的护栏是轮次上限 (防物理死循环, 不限制智能)。
            #
            # URL 原生上下文供给 (可靠性辅助, 非路由判断): 用户消息含 URL
            # 时预抓内容注入上下文 — 模型可直接原生回答, 也可继续自主调用
            # fetch_url/browser_run 深入。修复「发 GitHub 地址不回复」:
            # 内容已在上下文里, 不再依赖工具轮次的网关稳定性。
            if self._llm_fn is llm_call:
                try:
                    import re as _re

                    _urls = _re.findall(r"https?://[^\s,，]+", user_prompt)
                    if _urls:
                        from server.tool_registry import _tool_fetch_url

                        _u = _urls[0].rstrip(".,;!?)")
                        _ctx = await _tool_fetch_url(_u, 6000)
                        if _ctx and not _ctx.startswith(
                            ("抓取失败", "错误:", "GitHub 仓库")
                        ):
                            # 清洗 HTML + 收紧容量: free 池网关上下文在临界
                            # (system 50KB + 164 工具 schema), 预抓内容过大会
                            # 触发空响应 (quality-gate 误判 → 升级 → 仍空)。
                            # 干净的 2500 字足够模型直接概括, 深挖仍可自主调工具。
                            from server.tool_registry import _html_to_text

                            _ctx = _html_to_text(_ctx, 2500)
                            user_prompt = (
                                f"{user_prompt}\n\n[URL 内容已预抓, 可直接使用; "
                                f"如需更深入仍可自主调用 fetch_url/browser_run]\n"
                                f"{_ctx}"
                            )
                except Exception:  # noqa: BLE001 — 预抓失败不阻塞, 模型仍可自主调用工具
                    pass
            lt = None
            if self._long_task_factory is not None:
                lt = self._long_task_factory()
            effective_rounds = max_rounds or self.max_rounds
            result = await self._agent.chat_stream(
                user_prompt,
                session_id=session_id,
                max_rounds=effective_rounds,
                llm_kwargs=llm_kwargs or None,
                long_task=lt,
            )
            # ── 编程任务收尾兜底 (原生智能的护栏, 非前置拦截) ──
            # 模型自主路由优先; 但若编程强信号任务模型没调 reasonix_run 且
            # 没做实质工作 (只调了探索类工具/空回复/手写代码未执行) → 收尾
            # 直接交给 Reasonix 执行器 (serve), 保证任务不落空。
            # 配额暂停/失败等**有意结果**一律尊重; 模型已用其他工具做过
            # 实质工作 (如蜂群/沙箱) 也绝不覆盖。仅生产默认 llm 启用
            # (测试注入 mock 时保持工具循环语义, 不触发真实执行器)。
            _EXPLORE_ONLY = {
                "reasonix_status", "reasonix_sessions", "list_files",
                "grep", "read_file_ast", "search_genesis_ledger",
                "system_workspace_search", "mcp_codebase_search_code",
                "mcp_codebase_get_graph_schema",
            }
            if (_is_code_execution_task(user_prompt)
                    and self._llm_fn is llm_call
                    and result.get("status") not in ("paused_by_quota", "failed")):
                _done = {t.get("tool", "") for t in (result.get("tool_calls") or [])}
                final0 = str(result.get("final_answer") or "").strip()
                _real_work = _done - _EXPLORE_ONLY - {"reasonix_run"}
                if not _real_work and (not final0 or "```" not in final0):
                    from server.reasonix_agent import reasonix_run

                    def _prog(ev: dict) -> None:
                        fire_step({"type": "reasonix_progress",
                                   "squad_id": "master", **ev})

                    try:
                        exec_summary = await reasonix_run(
                            user_prompt, timeout_sec=900, on_event=_prog,
                        )
                        result["reasonix_execution"] = exec_summary
                        result["final_answer"] = (
                            exec_summary
                            + "\n\n(主脑已将此编程任务交给 Reasonix 编码执行器完成)"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("reasonix 兜底执行失败: %s", exc)
            # 绝不静默: 模型返回空/'None' 且无工具执行 → 可见兜底话术
            final = str(result.get("final_answer") or "").strip()
            if not final or final.lower() in ("none", "null"):
                if result.get("tool_calls"):
                    done = ", ".join(
                        t.get("tool", "?") for t in result["tool_calls"]
                    )
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

    async def chat(self, user_prompt: str, **kwargs: Any) -> dict[str, Any]:
        result = await self._agent.chat(user_prompt, **kwargs)
        # 绝不静默: 模型返回空/'None' → 换成可见兜底话术 (空白 = 用户感知「不回复」)
        final = str(result.get("final_answer") or "").strip()
        if not final or final.lower() in ("none", "null"):
            result["final_answer"] = (
                "⚠ 主脑未生成有效回答 (模型返回空内容 / 网关抖动)。"
                "请重试, 或在上方更换模型/引擎。"
            )
        return result


# 蜂群引擎全局单例(构造无副作用, eager 安全)
_swarm_engine: SwarmOrchestrator | None = None


def _default_swarm_engine() -> SwarmOrchestrator:
    global _swarm_engine
    if _swarm_engine is None:
        _swarm_engine = SwarmOrchestrator()
    return _swarm_engine


# 模块级单例(server 复用)
master_coordinator = MasterCoordinator()
