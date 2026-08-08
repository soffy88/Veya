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

import os
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

# ROUTING RULES (什么问题找谁):
- 视频/动画/分镜/漫画/转场 → hevi (mcp_hevi_*) + Open Design 项目载体
- 查资料/知识检索/翻译/摘要/笔记/PDF/网页/RSS → stratum (mcp_stratum_*)
- 代码/文件/浏览器/办公文档 → codebase tools / 本地技能
- 跨领域任务 → 先用 stratum 检索背景知识, 再决定是否进入 hevi 生产管线
- Do not invent tools that do not exist; if stratum/hevi are unavailable,
  state the limitation instead of fabricating results.
"""

# 主库 SOP 常量 re-export(兼容既有 import)
MASTER_SYSTEM_PROMPT = _oservi.MASTER_SYSTEM_PROMPT


def _github_readme(url: str, max_chars: int = 6000) -> str | None:
    """抓 GitHub 仓库 README (raw.githubusercontent, HEAD 分支), 截断防爆。"""
    import re

    m = re.match(r"https?://github\.com/([^/]+)/([^/?#]+)", url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md"
    try:
        import requests

        r = requests.get(raw, timeout=6)
        if r.status_code != 200:
            return None
        return r.text[:max_chars]
    except Exception:
        return None


def _gather_url_context(user_prompt: str) -> str:
    """提取 prompt 中 github URL → 抓 README → 拼接上下文 (轻量单轮可分析)。"""
    import re

    urls = re.findall(r"https?://github\.com/[^\s,，]+", user_prompt)
    blocks = []
    for u in urls:
        u = u.rstrip(".,;!?)")
        md = _github_readme(u)
        if md:
            blocks.append(f"[{u}]\n{md}")
    return "\n\n---\n\n".join(blocks)


def _is_quick_query(user_prompt: str, config: dict | None) -> bool:
    """quick 档判定: 短文本 (<quick_tokens) 且无工具/代码/推理意图 → 轻量路径。

    复用 oprim 路由分类 (系统消息判定逻辑一致)。
    创作/生产任务 (视频/动画/项目/设计/生成 XX) 一律否决 → 走工具循环。
    """
    lowered = user_prompt.lower()
    if any(k in user_prompt for k in ("视频", "动画", "影片", "短片", "影视",
                                      "分镜", "字幕", "配音", "海报", "渲染",
                                      "生成一个", "制作")) or any(
        k in lowered for k in ("video", "animation", "film", "movie",
                               "storyboard", "design", "project", "render")
    ):
        return False
    try:
        from veya.platform import load as _load

        oprim = _load("oprim")
        decision = oprim.route_decision(
            [{"role": "user", "content": user_prompt}],
            tools=None,
            matrix=oprim.load_matrix(),
        )
        return decision.get("route") == "quick"
    except Exception:
        return False


def _is_creative(user_prompt: str) -> bool:
    """创作/生产任务判定 (与 _is_quick_query 的否决词同一来源)。

    这类任务需要稳定工具编排 + 收尾总结 → 装配 frontier 档。
    """
    lowered = user_prompt.lower()
    return bool(
        any(k in user_prompt for k in ("视频", "动画", "影片", "短片", "影视",
                                       "分镜", "字幕", "配音", "海报", "渲染",
                                       "生成一个", "制作", "设计", "项目"))
        or any(k in lowered for k in ("video", "animation", "film", "movie",
                                      "storyboard", "subtitle", "voice", "poster",
                                      "render", "design", "project"))
    )


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
    def _bound_llm(self, messages: list, **kwargs: Any) -> Any:
        """把用户 key/endpoint 装配进 LLM 调用(支持请求级覆盖)。

        请求级 config/model/provider/endpoint(如前端传入的 user API key)
        优先于实例配置, 未提供则回落实例/环境默认。

        同时按任务类型分层瘦身 tools (opencode 免费池 context 有限,
        72 技能 + 44 mcp 全量注入会超载 → 模型返回 'None'/空):
        - 系统/基础工具恒保留 (执行面完整, handle_tool_call 仍可调全部);
        - mcp_* 按用户消息关键词召入 (视频/动画 → hevi+od; 代码 → codebase);
        - 技能/ecc 专家默认剔除, 消息含技能意图关键词时召回部分。

        创作/生产任务 (视频/动画/项目等, _is_quick_query 否决词) 直接
        装配 frontier 档 (本地 opencodex gpt-5.6-luna@127.0.0.1:10100):
        免费池模型多轮 ReAct 工具循环质量不稳 (乱调工具/收尾 'None'),
        创作任务需要稳定的工具编排与收尾总结。
        """
        req_cfg = kwargs.pop("config", None) or {}
        req_model = kwargs.pop("model", None)
        req_provider = kwargs.pop("provider", None)
        req_endpoint = kwargs.pop("endpoint", None)
        tools = kwargs.pop("tools", None)
        if tools:
            tools = self._layer_tools(tools, messages)
        # 创作/生产任务 → frontier 档 (用户显式指定 provider 时尊重用户选择)
        if not req_provider and not self.provider:
            user_text = " ".join(
                str(m.get("content", "")) for m in messages if m.get("role") == "user"
            )
            if _is_creative(user_text):
                req_provider = "openai"
                req_model = req_model or "gpt-5.6-luna"
                # 容器内 127.0.0.1 是容器自己 — frontier (opencodex) 跑在宿主,
                # 容器经网关 192.168.16.1 访问; 宿主本地开发默认 127.0.0.1
                req_endpoint = req_endpoint or os.environ.get(
                    "VEYA_FRONTIER_ENDPOINT", "http://127.0.0.1:10100/v1"
                )
                # 创作生产流程 (建项目→分镜→一致性→成片) 工具调用链长:
                # 免费池 6-8 轮容易轮次用尽 → frontier 档配长链轮次
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
        return self._llm_fn(
            messages,
            config=merged_cfg,
            model=req_model or self.model,
            provider=req_provider or self.provider,
            endpoint=req_endpoint or self.endpoint,
            tools=tools,
            **kwargs,
        )

    @staticmethod
    def _layer_tools(tools: list, messages: list) -> list:
        """工具 schema 分层瘦身: 保持执行面完整, 只裁 LLM 可见面。"""

        def _name(s: dict) -> str:
            return (s.get("function") or {}).get("name", "")

        user_text = " ".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "user"
        ).lower()
        want_video = any(k in user_text for k in ("视频", "动画", "影片", "短片",
                                                  "影视", "hevi", "分镜",
                                                  "配音", "字幕"))
        want_design = any(k in user_text for k in ("设计", "项目", "od_", "海报",
                                                   "画", "渲染", "资产"))
        want_code = any(k in user_text for k in ("代码", "审查", "review", "重构",
                                                 "测试", "bug", "构建", "build",
                                                 "报错"))
        # stratum 知识面: 检索/资料/翻译/摘要/笔记/文档/网页/概念图谱/记忆
        want_knowledge = any(k in user_text for k in (
            "检索", "查资料", "查一下", "资料", "翻译", "摘要", "总结", "笔记",
            "知识", "文档", "文章", "pdf", "网页", "rss", "概念", "图谱",
            "记忆", "学习", "研究", "搜索", "stratum", "书签", "收藏",
            "订阅", "资讯", "新闻", "论文", "文献"))

        keep: list[dict] = []
        for s in tools:
            n = _name(s)
            # 系统级 + 基础静态工具恒保留
            if n.startswith("system_") or (
                not n.startswith("ecc_") and not n.startswith("skill_")
                and not n.startswith("mcp_")
            ):
                keep.append(s)
                continue
            if n.startswith("mcp_"):
                if (n.startswith("mcp_hevi_") and want_video) or (n.startswith("mcp_od_") and (want_video or want_design)) or (n.startswith("mcp_codebase_") and (want_code or want_video)) or (n.startswith("mcp_stratum_") and (want_knowledge or want_code or want_video)):
                    keep.append(s)
                continue
            # 技能/ecc 专家: 显式技能意图才召回 (全量注入会撑爆 opencode 免费池)
            if (n.startswith("ecc_") and want_code) or (n.startswith("skill_") and (want_code or want_video)):
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
        token = _on_step_ctx.set(on_step)
        try:
            # 轻量快速路径: 简单问答 (quick 档) → 单轮无工具 (无 20 工具 schema/无主循环)
            # 省去工具 schema 注入与多轮开销 → 感知速度显著提升
            # 仅生产默认 llm (测试注入 mock 时走原路径, 保持工具循环语义)
            if _is_quick_query(user_prompt, config) and self._llm_fn is llm_call:
                try:
                    # 快速联网路径: 抓 github README → 单轮分析 (不走 ReAct 多轮)
                    ctx = _gather_url_context(user_prompt)
                    prompt = (
                        f"{user_prompt}\n\n参考内容(已抓取):\n{ctx}"
                        if ctx else user_prompt
                    )
                    result = await self._agent.chat(
                        prompt, llm_kwargs=llm_kwargs or None)
                    if on_step is not None:
                        on_step({"type": "text_delta", "squad_id": "master",
                                 "delta": result.get("final_answer", "")})
                    return result
                except Exception:
                    # 网关超时/失败 → 换备用模型重试一次
                    try:
                        retry_kw = dict(llm_kwargs or {})
                        retry_kw.setdefault("timeout", 45.0)
                        retry_kw["model"] = "opencode-go/mimo-v2.5"
                        result = await self._agent.chat(
                            prompt, llm_kwargs=retry_kw or None)
                        if on_step is not None:
                            on_step({"type": "text_delta", "squad_id": "master",
                                     "delta": result.get("final_answer", "")})
                        return result
                    except Exception:
                        pass
            lt = None
            if self._long_task_factory is not None:
                lt = self._long_task_factory()
            effective_rounds = max_rounds
            # 创作/生产任务 (视频/动画/项目) 工具调用链长: 建项目→分镜→
            # 一致性→成片, 默认 8 轮容易轮次用尽 → 提高预算
            if effective_rounds is None and _is_creative(user_prompt):
                effective_rounds = 14
            return await self._agent.chat_stream(
                user_prompt,
                session_id=session_id,
                max_rounds=effective_rounds,
                llm_kwargs=llm_kwargs or None,
                long_task=lt,
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
