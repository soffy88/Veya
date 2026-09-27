# B remote-only capability inventory (origin/main not on local)

Total commits: 411

## MasterAgent / GoalRun / ActionGateway (8)

- d1763922 feat(cognitive-policy): MasterAgent 减少无谓工具调用 — ANSWER FIRST + evidence framing
- f3d7358e feat(wayfinder): 打通 wayfinding → spec-pack → goal_run 三条线
- afdb6952 feat(eval): 独立 Eval 夹具接入 MasterCoordinator 真实运行结果
- dc222c73 feat(wayfinding): 接入 wayfind_gh_* — GitHub Issues 后端的 MasterAgent 工具面
- fb31b143 feat(wayfinding): 接入 MasterAgent 工具面 — wayfind_*/stateful_* 16 个工具
- d4b4edb8 feat(3o): 主链重构 — master_coordinator 统一入口 + 计划模式/批准
- 8a281fc9 feat(coordinator-master): 主脑三系统能力路由提示 (stratum 知识专家 / hevi 视频专家 / codebase 工具)
- e6b18247 feat(server): coordinator_master 长程任务可选装配 — 主脑路径接线

## Remote MCP / Local2 (13)

- 170627fa fix(product): close legacy MCP governance bypass
- 62b62c56 feat: add MCP tool governance and credential references
- de548682 fix(graph): rows 元素为 list (非 dict) + 去 WHERE <> (MCP 解析错) — 图谱数据修正
- 56448e93 test: 适配 skills dispatcher + mcp 网关新契约 — 修 Claude Code 优化未同步的 10 个测试
- 1da73314 ②-B mcp 67→4 网关: 主脑工具面 85→22 (全 agent 面 ~168→~35) (#5)
- b1bf3f2a fix(stratum): stratum MCP 连接修正 + 主脑知识路由 (三系统能力注册收尾)
- 6df25b19 feat(open_design): 设计/渲染智能接入 — mcp_od_* 22 工具 (四智能面合流)
- bfaabee1 feat(hevi): 视频管线 MCP 接入 — veya 主脑视频生产面 (mcp_hevi_* 14 工具)
- d28ef65f feat(stratum): 知识库 MCP 接入 — veya 主脑知识面 (mcp_stratum_* 18 工具)
- 7a0dc16e feat(veya_loop): StreamableHttpMcpClient/HttpMcpError 装配
- 80c65bdc feat(codebase_memory): 主脑工具面接线 (mcp_codebase_* 8 工具) + 每日增量索引 cron
- 6b777d45 feat: codebase-memory-mcp 集成 — LSP 调用链/blast radius/Cypher 精度层
- 5a09eba0 feat(veya_loop): StdioMcpClient/StdioMcpError 装配 (obase 机制转发)

## L0 / L1 / L2 (2)

- f8355209 feat(officecli): 场景层 + 基座升级 — 动态 help / L1-L3 分层 / 4 场景资产
- 5bd5dfa0 feat(runtimes): L1→L3 三框架运行时实施 (prime-agent / pi / agentscope)

## permissions / approvals (8)

- e810adc5 fix(browser): update takeover policy dependency
- 819f44cd test(coding): gate restricted sandbox checks on bubblewrap
- 7b3a0911 fix(ci): use pnpm 11 allow-builds policy
- daafb6be feat(10-of-10): PR-09 安全契约最小步骤 — SandboxProfile 分级 + 对抗性路径测试
- 98528c7c fix(cognitive-policy): ANSWER FIRST 补优先级声明 + WORKSPACE RAG 收窄 + Anthropic tool_choice 修复
- ba820d0f fix(master): 主脑文件操作分工 — 新增 write_file 工具 + 收紧 run_in_sandbox
- 620e5554 fix(sandbox): audit_dir cwd 不可写时 fallback 到系统临时目录
- 90f17b4d feat(compat): legacy L4 gateway bridge — agent verify/run/stream/history, sandbox execute, kanban

## provider routing (31)

- fe3d7758 fix(product): restore coding workbench fallback
- 138ef88a feat(provider): assemble 3O provider router and usage
- 6f382bfd feat(llm): reroute veya1.2 pools to paid providers + harden free pool
- b122a40f style(obase): ruff format llm.py routing branch
- cba39c50 feat: switch master brain to veya1.2 OpenRouter pool
- 902cb09f fix(llm): opencode-go 网关抖动整轮重试, 不再一次失败即报错
- 17610bac fix(llm): opencode-go frontier 兜底自愈 + 修正误导性报错来源
- 9bb972d4 feat(3o): 记忆注入桥进新主链 — context_providers + on_finish 钩子
- 2898eebd fix(llm): veya1.1 直接用 opencode-go API — 绕开 oskill 复杂路由器
- c1893a79 fix(llm): opencode-go 空回复自动降级本地 gpt-5.6-luna — 模型层彻底闭环
- ec15926e feat(reasonix): 独立 oservi (serve HTTP+SSE) + opencode-go 云端独立可用
- 25cfed56 fix(llm): opencode-go 无效响应兜底 — 'None'/空重试换模型, 绝不静默
- a7b7d00c test(llm): get_provider_config config.json 兜底防回归 (+2)
- f7cb183b feat(model-routing): freellmapi 四机制内化 — 统一模型 fallover / 用量跟踪 / 粘性会话 / 工具救援
- a941ba84 fix(identity): opencode-go key 直连 — 主脑人格回归 veya (去掉 opencode agent 包装)
- 03ff9304 fix(identity): opencode 常驻会话注入 veya system prompt — 恢复 veya 人格与能力
- d6ea76cc feat(perf): opencode serve 常驻化 + SSE 真流式 + CRG 语义搜索原语
- 3b463743 feat(llm-router-v3): ChatGPT 订阅接入 opencodex 翻译层 (127.0.0.1:10100)
- 8d3f01c6 feat(llm-router): 线上打通 opencode-go/deepseek-v4-flash 主模型 (容器内真实执行)
- 2477242b feat(llm-router): 主模型接入 opencode-go/deepseek-v4-flash (系统内已有凭据)
- 02dfe682 fix(llm): custom provider 双通道 — 直连失败自动切代理兜底 (GFW 间歇重置)
- fee430e3 fix(llm): endpoint 归一化 — custom provider base URL 自动补 /chat/completions
- e8321318 docs: 引擎账号修复记录 (四引擎容器内全通 + 代理桥/opencodex 自举)
- 0f2a7f66 feat(engine): 容器内 claude/codex 放行 — 代理桥 + opencodex 自举 (四引擎全通)
- 13a33d45 feat(P1+P2): SessionLineage + ProviderCatalog + Kanban 多 Agent 编排
- 01eea93d fix(llm): DeepSeek 400 空 tool_calls 数组 — 发送前剥键 + 源头不写空数组
- 9d22519c fix(master): route frontend-supplied provider/model/config into master brain
- f4083ee6 feat(llm): support VEYA_LLM_ENDPOINT env for default provider endpoint
- aeef01c3 fix(llm): provider auth/network errors crashed the whole request as a raw 500
- e61b18d6 feat(web): multi-provider LLM config + sidebar nav + plugin marketplace
- 42b48c76 G11: MkDocs site + mkdocstrings API reference (CI docs job); G12: multimodal vision wired into LLM providers

## HICODE / AGY / Pi / Codex / OpenCode (16)

- ee6d1d51 fix(vision): 容器内视觉模型走 hicode 反代 (Host 改写) + 路径白名单
- cfb808fc fix(deploy): 主进程 CWD 挪出 hicode-workspace, 防同名目录遮蔽真实包
- 39e17ef2 docs: 冻结文档 reasonix → hicode 更名同步 (AGENTS.md + ARCHITECTURE_STABLE)
- 518d4abc refactor(hicode): Reasonix → hicode 全链路更名
- ba3ba11d fix(server): reasonix 进度实时透传 SSE (reasonix_progress) + omodul P0 合入
- 845b5c89 feat(web): Agent Dashboard — 四引擎工作台 (Claude Code/Codex/Grok Build/Pi) 自适应四块并行
- d5101caf feat(reasonix): AI 代码评审工具 + 后台任务队列 + 前端 Stop 真正中断
- c6a22951 fix(master): 入口只有一个大模型 — 删除全部程序判断 (工具分层/URL预抓/reasonix收尾兜底)
- 04d2cde4 feat(master): 编程任务收尾兜底 — 模型自主未执行时交 reasonix serve
- afa9c075 feat(reasonix): 会话 resume / 实时进度流 / checkpoint 回滚
- 788e7cb3 feat(reasonix): 集成 Reasonix 编码执行器 — 编程任务确定性路由执行
- 405f250b feat(replica): 二期 pi-workbench 层落地 (G1/G2)
- a82d536a docs(prd): 三框架集成 PRD (prime-agent/pi/agentscope) + delegate_to_genesis 立档
- 3fe2e039 feat(engine): 容器内 pi 精确放行 — 凭据探测而非一刀切禁用
- cc1cb700 feat: 多引擎执行 — 聊天框选引擎 (Master/Claude/Codex/Pi) 直连对应 CLI
- d42697c9 web: 右下角模型快捷选择器 (Claude/Codex/Pi + 模型列表) — cindy 风格

## supervision (1)

- 46479ed6 feat: add 3O computer supervisor assembly

## context engine (5)

- 28e92356 fix(personal): enforce gold-gated memory and skill correctness
- e28e76ba feat(10-of-10): PR-07 收尾 — session_tree 核实无需改, memory_store 补 provenance
- 979f7aa6 feat(vaom): VEYA 3.0 VAOM 迁移 P0-P6 — Trust Plane/Capability/Memory/Learning/Harness
- 0089e7b9 feat(memory-hub): VEYA 记忆中枢 — TencentDB 三机制装配 + 跨会话持久化
- f087474c docs: codebase-memory 工具面接线与 cron 生产验证记录

## verification OS (7)

- 58790d42 fix(product): complete coding task verification loop
- 2cf1ebd7 feat(issue): add verified patch to draft PR flow
- 02592efb docs(release): record personal runtime gold v1 baseline
- 35ce13f1 test(eval): record personal runtime release freeze verification
- 9e4d4ca6 chore(eval): record post-commit personal gold audit
- 7ceba503 feat(eval): add personal agent gold benchmark
- 2028e22a feat(graph-engineer): P1 三件套 — PRE-FLIGHT/机械质量门/真 VERIFY

## persistent computer (6)

- 3a43810b test(browser): cover sensitive confirmation takeover
- 3d49d956 feat(browser): add computer takeover assembly
- 94995550 feat(execution): 执行路由 + 生命周期 + 同步括号 (cloudflare/computer runtime 内化)
- 38d87a5c fix(desktop-ci): playwright 浏览器路径跨平台统一 (PLAYWRIGHT_BROWSERS_PATH+PW_PACK_DIR)
- 3fc2935d feat(integrations): browser-use + Agent-Reach 集成 (Skill Hub 技能包 + browser_run 引擎升级)
- c678c594 fix(browser): 远端抓取通道 — Playwright 浏览器二进制 + 容器 Chromium 沙箱/共享内存

## knowledge (3)

- 2645c6f7 ci: preserve full coverage gate for optional suite
- 801449ab ci: aggregate coverage across test suites
- 3c0b0115 feat(skill-opt): paper/knowledge skill 机械+LLM 复合 scorer (接 optimize_skill) (#13)

## channels / integrations (13)

- c165c9b4 docs(graveyard): 修正批次C残留清单 — CLI/IM/automata 早已迁移完成
- 17d98234 chore(3o): 子模块指针推进 — omodul/oprim/oskill 沙箱 hosted profile
- b3af65a7 fix(notify): dismiss() 元组未解包崩溃 — HITL toast 批准/拒绝/超时路径 (723053cf 引入)
- 5db10317 feat(3o): 阶段 3 — oprim 物理触手原子层 (6 组 21 原子) + 禁止业务直接 I/O
- 661f1298 feat(state-kernel): 状态内核 Phase 1 补全 — Quota/Claim/Gate 控制面工具 (主脑零改动)
- e0b857f2 chore(3O): oservi 指针 → 270e4fa (收尾无效响应兜底 + oprim 909cfca)
- 5349ebaa chore(3O): oprim 指针 → 909cfca (quality_gate 工具调用兼容)
- c068560f chore(3o): oprim 指针 → 9517a49 (雕刻管线三原语)
- 71fd0ea2 feat(3O): L3 反事实升级为精确 twin-network 枚举 — 更新 submodule 指针 (oprim/omodul)
- 9ad21a74 chore: submodule 指针更新 (oprim/oskill llm-router)
- 4171529c chore: 登记 3O 子模块指针 (oprim c7929ba · obase a0ccd79)
- 19fabe7b feat(veya_loop): 优化工程化+walk_forward+生命周期 装配 (shim+ELEMENT_MAP+15 测试)
- 0ecb484a feat(veya_loop): optimize_loop 装配 — 多目标效用优化循环 (shim+ELEMENT_MAP+17 测试)

## 3O compatibility (69)

- d80530ab feat: add 3O action gateway assembly
- f016037c fix(ci): install 3o source dependencies
- 26dc7f33 fix(ci): include 3o runtime dependencies
- f8023bb5 fix(ci): install 3o runtime parser dependencies
- 5940bec2 fix(ci): restore 3o async and dependency checks
- 07b0853c fix(ci): restore baseline checkout and obase type safety
- 4d99dd7c chore: update 3O submodule revisions
- dac21154 chore(3O): 更新 submodule 指针 — obase Runbook 图状态机
- e0c926d8 chore(3o): oservi submodule URL 迁移 oservice → oservi (远端 repo 已改名) (#10)
- cfbb6bd2 chore(3o): 更新 oskill/oservi gitlink — drawio 原语 + 主脑透传落地 (#9)
- 085cbe6e feat(goal-run): Boss 编排长时闭环接入主脑 + G2 忙等自旋修复 + 3O 指针推进
- dc4688f7 refactor(3o): 双轨收敛 + 主脑执行并发/异步安全加固
- 16b80741 feat(vision): 3O 内化 dsh-vision-toolkit — 10 个 vision_* 工具入主脑工具面
- 3dc5f75e feat(3o): 内化集成 Semantica 决策账本/上下文图 + OpenMausBot 提问卡片
- d9f19bbe feat(3o): 服务端新管线 — openrsi/unified_pipeline + graft 上下文 + user_control + wechat
- acc1ec04 feat(3o): 会话归属与安全加固 — checkpoint owner + session auth
- 048f7480 feat(3o): project_ask 项目任务入口 + Understand 门禁（U5 真机验证）
- 95362de0 fix(3o): 会话树 KV 自动建父目录（容器 ~/.veya/loop 不存在导致 500）
- 7bd132c5 fix(3o): 工具结果消息补齐 tool_call_id + assistant tool_calls 还原 OpenAI 协议形态
- d476ea8f fix(3o): 新心脏 LLM 调用携带 tools 声明 — 模型返回结构化 tool_calls
- 0688c5fe fix(3o): 主链切换上线修复 — 外部 session_id 自动建树 + 空输入友好响应
- d23a6824 feat(3o): 主链切换桥 — chat_stream 接入 VEYA_AGENT_LOOP=strict（默认关闭）
- de7cf4c4 feat(3o): 阶段 5 — oservi 长时守护引擎 + 统一网关（五阶段迁移完成）
- e68698f1 feat(3o): 阶段 4 — omodul 注入式流程控制核心 (session_tree/tool_pipeline/agent_loop/evidence_refine) + 双轨
- 2b11e37b feat(3o): 阶段 2 — oskill 纯函数层 (8 元素) + 幻觉拦截防线
- fd7dfcc8 feat(3o): 阶段 1 — 严格句柄层合同 (5 Protocol) + 薄适配器 + 全局单例句柄
- 266cdd4b feat(3o): 阶段 0 冻结基线 — 严格 3O 迁移双强制检查 + 能力映射文档
- cc1b9315 refactor(3o): obase 归位 — 8 顶层平铺模块落入 veya/obase (阶段 B)
- c6ff1c41 feat(3o): veya/ 包纳入 3O lint (veya/<layer> 布局) + oservi 骨架
- 89bdc761 chore(submodule): oservi 指针更新 — 重建 .gitignore + 清 pycache
- 48ab4125 chore(submodule): oskill 升 v4.37.0 (Agentic-RL + 框架适配)
- 8fce72a7 chore(submodule): oskill 升 v4.36.0 (hello-agents 三机制)
- d76b630b chore(submodule): oskill 升 v4.35.0 (第七批补全)
- de7bec9d chore(submodule): oskill 升 v4.34.0 (第六批补全)
- a8aa0f4b chore(submodule): oskill 升 v4.33.0 (六项目机制补全)
- 80d07942 chore(submodule): oskill 升 v4.32.0 (七项目机制补全)
- 7be75bfa chore(submodule): oskill 升 v4.31.0 (Agent 三模式编排)
- 68edd726 chore(3O): oservi 指针 → rounds exhausted 摘要兜底
- 65d40094 chore(submodule): oskill 升 v4.30.0 (工作流 DSL + 模板 + 插件)
- dea1c9dc chore(submodule): oskill 升 v4.29.0 (非函数调用 LLM 适配)
- 6ff52f57 chore(submodule): oskill → v4.28.0 (MVD + 打法手册 + 产品化四问)
- 9d747bf1 chore(submodule): oskill → v4.27.0 (SVG 拟合工艺)
- 857bcd7d chore(submodule): oskill → v4.26.0 (多平台发布 + 变现结算)
- 999eb1b7 chore(submodule): oskill → v4.25.0 (四层健康分 + 预警干预)
- 3286a540 docs(agents): 吸收 Cypress 工程规范 5 条 + submodule → oskill v4.24.0
- 92f300f7 feat(agent-os): optimize_parameters 工具 — Agentic HPO 装配层 (3O _hp_search)
- ae091a09 chore(submodule): oskill → v4.22.0 (图遍历查询 + 渗透闭环)
- 5d75c432 chore(submodule): oskill → v4.21.0 (语义节点图 + 多 agent 接线)
- 52dcdc30 fix(submodule): oskill 指针 → v4.19.0 + caller-error 修复 (5deb070)
- 0fee5cbc fix(submodule): oskill 指针 → 3a4653c (v4.18.0 + caller-error 修复)
- 8ff897b6 fix(submodule): oskill 指针回正 e5bb3d8 — 恢复 caller error 闸门修复
- 208d7c15 chore: oservi submodule 更新 — 清理跟踪的 __pycache__ 构建产物
- ce64bef9 feat(loop): 工程工作流 3O 装配 — veya_loop 0.6.0 + engineering-flow skill 包
- 2c02ac88 feat(doctor): veya doctor 集成 oskill.env_doctor 工具链自检 (3O 主库)
- acddb0e0 feat(goal-driven): while 循环编排事务 — Goal-Driven 3O 内化 (W1-W4)
- 454838df feat(crg): code-review-graph 3O 复刻 — 代码审查知识图谱 (持久增量图谱)
- 4a2d956f chore(3O): 长程任务状态内核 — 更新 submodule 指针 (obase/omodul/oservi)
- c098dbbd chore(submodule): oservi 指针更新 (移除 Zone.Identifier)
- b1e63088 feat(spec-ecc): 可执行 Spec + ECC 领域目录 + 硬规则 (spec-kit/ECC 3O 内化)
- 01019998 feat(llm-router): veya1.1 智能路由别名 + 长文并行快速回答 (RouteLLM 3O 内化)
- 2206a2d3 refactor(runtimes): 按 3O 单一来源迁移 — 协议与适配器进主库 oservi.runtime_bridge
- dbd6685c chore: oservi submodule 指针更新 (主脑零限制)
- b7d3bd12 fix(deploy): Dockerfile — full deps from pyproject, correct uvicorn entry, healthcheck, 3O PYTHONPATH
- 885a0b9b feat(agent-os): Veya Agent OS — 9-capability industrial stack over 3O main libraries
- 9c225db1 fix: structlog must be a main dependency (obase core requires it at import)
- 56c835af ci: checkout submodules in Test job (guardian tests need the mounted main libraries)
- a41ab639 Harden 3O lint suite against unparseable main-library files
- 00e29d6f Mount 3O main libraries as submodules + single-source assembly layer
- cd8eac07 Add 3O SPEC v3.0 Appendix B CI lint suite (9 checks) + fix full-repo ruff

## web / frontend (29)

- 65db8143 feat(product): wire real task entry to workbench
- 6387a553 feat(product): add Veya Bot product shell
- 5485710c feat(workbench): unify canonical task controls and state
- 4e52dcac feat(voice/gateway/web): 语音链路 + drawio + 前端组件 + 主脑瘦身 (#8)
- ec5c2355 feat(web): 提问卡片前端 — agent_question 事件 → 卡片 → /agent/answer 回填
- e946e9ae chore(web/deploy): 前端交互 + dsh 网关配置 + serve 脚本
- 62863e00 fix(upload): octet-stream 上传绕过 SvelteKit CSRF — PDF/文件上传修复
- a0256672 feat(web): 聊天框文件/图片上传 — 附件 + 视觉消息链路
- 717e51fe feat(web): 项目图谱 + 语音听写 — 借鉴 ccgui 剩余差距
- ffed976b feat(web): P3 文件树 + P4 Git 面板 — 借鉴 ccgui 工程面板
- 72381df4 feat(web): P2 上下文用量 + P5 对话流活跃计划条
- b658b0c4 feat(web): 计划看板 PlanBoard — 状态内核控制面 UI (P1)
- 8eba0440 docs: Dashboard 增强设计 — 借鉴 ccgui, 差异化在状态内核 UI
- 8662e125 fix(engine): grok 单轮模式用 -p/--single (裸 prompt 会进交互 TUI 无输出); compose 挂载 ~/.grok
- 74e16e2f fix(web): 移除「任务开始/思考…」噪音徽章 — 只保留真实执行轨迹
- 818d40eb fix(web): 移动端侧边栏抽屉化 — 手机不再占半屏
- 34d6834f feat(web): 看板页面 (KanbanPanel) 与插件/自动化并列 — 多 Agent 编排可视化
- 131e49ad fix(web): 模型菜单点击外部关闭 + 不可用引擎禁用 (520 根因)
- 2b87542a style(web): pure-black minimal theme + Claude-style composer layout
- 1d97f8db feat(web): Claude-grade chat console — streaming Markdown, sessions, tools trace
- d2c37f65 fix(web): chat locked up after one message and "新对话" destroyed history
- 427985a3 fix(web): API key/model had no save button or success feedback
- 496325fe chore: remove dead/orphaned frontend surfaces
- 93dc2cdb feat(web): artifact cards in chat/assembly flows + ARTIFACTS PROTOCOL SOP
- bd6e0cd4 fix(web): SSE proxy never flushed headers on an idle upstream
- 454cfb63 fix(web): SSE proxy never flushed headers on an idle upstream
- 1e2aeba0 feat(web): settings drawer (model/plugins/automation) + readability pass
- 36369fb0 deploy: add systemd unit for the SvelteKit frontend (veya-web.service)
- a37a3bc8 feat(web): rebuild veya.aiinote.com as single-flow Claude-style HITL console

## deployment / Docker / systemd (15)

- fcef7d5a fix(deploy): expose public health endpoint safely
- c788b3f3 docs: make compose load the root environment file
- 18ff9ad6 feat(gateway): veya1.2 模型别名 + OpenAI 兼容 LLM 网关
- f1506657 fix(desktop-ci): Linux 产物改 deb (AppImage linuxdeploy 1.4GB 资源打包不稳; deb/rpm 已成功)
- 70430a3d fix(desktop-ci): AppImage 打包 linuxdeploy 自解压 (CI 无 fuse → APPIMAGE_EXTRACT_AND_RUN=1)
- 7299dfcb fix(524): SSE 心跳防 Cloudflare Tunnel 100s 掐断 + 工具健壮性
- 5fc72074 fix(deploy): 容器构建免网络下载 — .dockerignore (context 2GB→77MB) + chromium 二进制走 Release 附件
- fe5d12e0 docs: 生产事故修复记录 (520/524) — 容器网络恢复 + 宿主 gateway 让位 + key 遗留
- 34b9e4c2 docs(ops): 线上部署与故障排查手册落记忆 — AGENTS.md 运维章节 + ONLINE_DEPLOYMENT.md
- 8f31f8b9 deploy: 多引擎容器化 — uid 1000 用户 + 引擎二进制/凭据挂载 + veya-data chown
- f2261baf deploy: 容器双端口 8767(gateway)+9120(legacy) — 前端主脑 legacy 代理 404 根因修复
- cc334e6b deploy: 端口 8767 (8765/8766 被 systemd 与 hevi 占用)
- f021bf5a deploy: 修复容器部署链路 — 逐目录 COPY/build-essential/requirements/端口冲突防御
- 1c56cac9 refactor(gateway): single-process Agent OS — merge legacy L4 gateway onto root server.app
- 499cc71d feat(gateway): unify legacy gateway with Agent OS master brain (text contract)

## tests / CI / chore (34)

- 1fd31eef chore(ci): close coding workflow lint and format regressions
- e61ac982 fix(ci): stabilize direct io and reverse dependency checkers
- 01cf1be9 chore(repo): add executable Veya agent guide
- 85cc3850 docs(ci): correct direct io audit evidence
- 82df58fc ci: complete release preflight dependencies
- 2f45e12d ci: align release test dependencies
- a47140f4 ci: support isolated release smoke tag
- 7bc56b92 fix(ci): baseline and enforce direct io release check
- e839bc6d fix(ci): align release and desktop ancillary workflows
- 8e175f30 docs(release): record green ci and production health baseline
- 1312f9f1 ci: split required and optional pytest suites
- 297419a1 fix(ci): restore portable smoke dependencies
- 067c098f fix(ci): satisfy modern core docstring gate
- b4d72c24 fix(lint): resolve ruff import and unused diagnostics
- 48027110 style: apply ruff formatting baseline
- 24699313 fix(ci): clear chat kernel mypy baseline
- 7aa818e9 feat(10-of-10): mypy CI 收口 + 架构文档补齐 + 供应链/依赖卫生检查
- d1820cb2 feat(10-of-10): architecture manifest CI 接入 + graveyard 补充 + ToolSpec v1 落地
- b4bedad8 feat(10-of-10): PR-01 architecture manifest + 主链 mypy 清零 + smoke test 真崩溃修复
- 617c6095 chore: 提交工作区全部改动
- dde2d785 chore: 补提交 ChatConsole query 上传方案 (8deba164 遗漏)
- c21a6dbd fix(desktop-ci): Tauri build 步骤 shell: bash (Windows 默认 PowerShell 不认 bash if)
- 3af4d81e fix(desktop-ci): 产物 glob 对齐 productName (veya-desktop_*)
- 56887410 fix(desktop-ci): Windows npm 不认 /dev/null 重定向 → 2>&1
- 477708b0 fix(desktop-ci): -m PyInstaller (包名大写, -m pyinstaller 找不到模块)
- defeee41 fix(desktop-ci): Windows pip 升级需 python -m pip (pip.exe 自升级被拒)
- 7470b1a2 fix(desktop-ci): Windows 跨平台 — venv/pip/python 路径平台化 (RUNNER_OS 分支) + 步骤合并
- ca6a8a2a fix(desktop-ci): playwright 路径改用步骤内相对路径 (runner context 在 job env 不可用)
- 3a6c069f fix: legacy_agent 缺 json import (ruff F821)
- d7ce0230 feat(desktop): Tauri 桌面版与网页版对齐 — 静态前端 + PyInstaller 后端 + 三平台 CI
- b1a7e9a0 chore: remove dead nginx reverse-proxy configs
- c5be8e1f fix pyproject: restore [project.optional-dependencies] header + close dependencies list
- 7f4a6407 ruff format
- 8217f362 Fix G14 LRU benchmark timing flake on CI (3.12): slow() now simulates 1ms compute

## docs (16)

- 9ec3643d docs(release): record final required release readiness
- edeac198 docs(release): record required release candidate baseline
- 13830e3a docs(release): record public health probe
- 9f7989d5 docs(audit): record Grok Bot reconstruction review
- 56f62569 docs(10-of-10): PR-12 产品定位重写 — Agent Runtime, coding 是第一应用面
- 7bcc99f5 docs(10-of-10): PR-07 State Authority — 决策记录, 不动代码
- 1191e7ba docs(10-of-10): PR-20 开源成熟度 — 补齐 LICENSE/CONTRIBUTING/治理文档
- cae7e4bc docs: add Veya 10/10 engineering upgrade plan
- 2ca5f5a2 docs: 项目级 CLAUDE.md — andrej-karpathy-skills 工程纪律
- 03ccec6b docs: code-review-graph 全量探查 veya 实现 (CRG_EXPLORATION.md)
- 1e560e57 docs: 状态内核综合方案 — Prime 执行面 × LoopX 控制面
- 54ed4132 docs: Prime Agent 架构对照评审 — veya 三层边界差距 + 可借鉴点
- 1ba89df3 docs: 冻结主链路架构 — 用户确认稳定, 任何改动须经用户同意
- d558d8df docs(ops)+script: 主脑 LLM 配置固化 — 同步脚本 + 部署手册 §4.1
- a351180f docs(ops): 工具链免 root 安装手册 (typst/xelatex/drawio/pdftoppm) + AGENTS 引用
- 58bf83e7 docs: veya_loop 交接文档 HANDOVER.md

## other (135)

- 72e73b1b fix(product): harden task tool governance
- e87e948c feat(review): add GitHub pull request review flow
- ed3c271f feat(state): enforce PR-07 authority boundaries
- 8025f0df fix(cli): read coding task result from durable authority
- 9db97695 feat(llm): rewire veya1.2 free/primary rotation pools
- 80f92286 feat(coding): connect coding tasks to durable goal runs
- fed98ad7 feat(harness): close sensor-backed readiness loop
- 38f7d237 feat(harness): add guides sensors and ratchet contract
- 9009e8aa feat(coding): add isolated workspace coding tools
- ed44431a style: normalize llm base formatting
- 2f84bf16 Merge remote-tracking branch 'origin/main'
- 0f1f3672 feat(eval): add personal intelligence audit
- 9fa6515c feat(runtime): add personal agent continuity and learning
- 40e0c08b feat(runtime): activate crash-safe PostgreSQL durable execution 1.0
- 1a5fdab3 feat: make gmi minimax the veya1.2 default
- 664d4b8d feat: complete P1-P3 runtime implementation
- 9807a713 feat(10-of-10): PR-07 State Authority — history_store 改成不可变追加日志
- 6b5dcc1d feat(10-of-10): PR-06/PR-11 可观测性 — tool_execute span 埋点(零风险子集)
- fed21eb9 feat(10-of-10): PR-10 Eval Harness — 现有用例诚实分类 + 派生指标(不凑数量)
- 9f5f747f fix(10-of-10): PR-08 收尾第三轮 — 补上自己制造的 server.app 启动回归
- 24a19926 fix(10-of-10): PR-08 收尾 — 纠正上轮误判, 7 个重包全部挪 extras
- 71e4b77d feat(10-of-10): ToolSpec side_effect 21/21 补齐 + PR-08 依赖卫生调研 + textual 挪 extras
- 6e22374e feat(goal-run): [P] 并行任务标记 — smart-ralph 内化, 修复并发会话遗留代码
- 122c79c3 feat(team): 点对点协作工具面 — oh-my-openagent Team Mode 内化
- b6e6141f feat(goal-run): 计划前置双轴审查门禁 — oh-my-openagent orchestration 内化
- b29bf087 feat(session-tree): list_branches — 验证并暴露 Compaction 原文的找回入口
- b952c066 feat(goal-run): 双轴独立代码审查 — mattpocock/skills code-review 内化
- 46196cc3 feat(graft): 讲解层 — 补 nanonets/graft "资深工程师讲解" 的内化缺口
- 20cd2d37 feat(skills): LLM 语义安全扫描 — 补 AST 层看不见指令内容的盲区
- f7b8f51d fix(tests): 隔离 test_skill_hub_loads_both_packs 到 tmp 目录, 不依赖真实技能数量
- 97b5cd24 feat(goal-run): 验收失败重试改用 SessionTree 分支记录
- 9e7ac95d feat(skills): 信任门默认转严 — 高危调用面默认拒载
- a7556c6b feat(harness): 长会话可靠性四项落地 — Compaction/SessionTree镜像/EventIR/生命周期
- 48dc6b37 feat(long-task): 安全接线 long_task_factory — GoalKernel 真正接主链
- 537a8bf3 feat(wayfinding): stateful_goto 到达终态时自动投影 trajectory
- 4de8a6af feat(agent-safety): 托管沙箱多租户 + 编辑安全/审计原语 + spec-pack 技能包
- 21577da9 feat(skill-catalog): SKILL.md 目录索引/搜索/晋降 (补 SkillsGate 管理空档) (#14)
- 52b1e49b feat(skill-opt): skill 文档验证门控迭代优化 (补 SkillOpt 空档) (#12)
- 28dc6117 feat(skill-hub): 技能代码加载期静态安全扫描 (补 K-Dense skill-scanner 空档) (#11)
- 9ef01777 feat(openrsi): holdout 反 reward-hacking 守卫 (#7)
- 5574d21d feat(cognition): Orchard 内化 — 决策边界信用分配 + 共享前缀分支 (#6)
- 9194f875 feat(cognition): 前沿范式内化 — 技能契约/元路由 + 懒加载瘦身 + best-of-N 共识 + 代码图置信
- 8ba4714f fix(vision): 反代探活超时 0.5s→2s+重试 (首请求冷启动)
- 25425d76 feat(vision): vision_trace 高保真矢量化 — vtracer CLI 主引擎 + PIL 降级
- 9324e652 feat(sync): 多端逐字实时同步 — 电脑执行时手机跟随同一会话
- 41cdbe52 fix(llm): frontier 兜底接受 tool_call+空content, 不再确定性误杀
- 7ec11980 fix(llm): 网关+frontier 重试预算拉长到约 90s (原 20s 扛不住分钟级抖动)
- b46502dc fix(agent): 网络抖动重连续接 + frontier 兜底同样退避重试
- 32b71119 test: 跟进近期主链路改动 — dsh 引擎探测/网关模式/鉴权路由/history 重构 + 基线重生成
- 4c56eab1 feat(engine): dsh 引擎 — engine_runner 路由 + 容器探测 + 前端引擎选项
- ab0ea0f3 test(e2e): 内化能力端到端测试入库 — master 工具面 24 断言 (决策账本/上下文图/提问卡片)
- b8c2c5b8 fix(loop): 正常短回复被误判疲劳 — _INVALID_CONTENTS 移除 ok/done/完成
- 5a31204f fix(llm): 候选重试弃用 mimo 系列，改 kimi-k2.7-code（用户指示）
- b0b1a511 fix(loop-plane): flag 关闭时 loop_* 工具返回明确提示（不隐式启用进程内模式）
- a3dcc70f feat(loop-plane): Loop Plane 微服务 — Phase 0-3 + Sched 门面 + Skills stub
- b0c21b3b security: 修复确认的攻击面 + 关键面鉴权 + 状态内核接入
- fa4cb12e fix(tool_guard): terminal 闸门只按工具名分类 + allowlist 豁免 — 消除误伤风险
- df4558b8 feat(stratum): 主脑系统提示词接入决策智能工具
- c81800fa fix(llm): default_content stub 被当有效回答返回 — 429 限流期间跳过兜底
- 0f100e0f feat(master): 工具发现增强 — description 触发条件 + SOP GRAPH-ENGINEER PROTOCOL
- e6b3c323 feat(graph-engineer): P2 三模式 + Elevated assurance 3 lens
- 4a38e677 fix(agency): 转换器 SYSTEM_PROMPT 用 repr 转义 — 修复源 md 含三引号截断
- 1254ded5 feat(graph-engineer): P0 防振荡三增强 — CRITIQUE 连续性/DEBATE 三分类/Anti-loop cutoff
- 1a142544 feat(agency): agency-agents 专家角色库接入 — 转换器 + run_skill 参数兼容
- 7cb7548d feat(graph-engineer): 多引擎编排自纠正循环 — system_graph_cycle 工具
- dcbbd6b9 feat(master): SOP 加 SCANNED-PDF PROTOCOL — 阻止扫描 PDF 瞎装 OCR 库
- 6bc5bfa2 fix(upload): PDF 上传全链路修复 — CSRF/body 限制/二进制无损/adapter
- e45a08d3 fix(upload): fs_read 支持 uploads/ 前缀 + PDF 豁免 200KB 限制
- 649839fd feat(upload): PDF 支持 — 上传工作区 + fs_read 提取文本 (pypdf)
- cbdcd149 fix(upload): 上传目录改 ~/.veya/uploads (veya-data rw) — /app 只读挂载不可写
- 616c6698 feat(upload): 文件上限放宽到 100MB — 大文本存工作区 @引用, 不撑爆上下文
- a8505680 feat(auth): 三项收尾 — automations 用户隔离 / 刷新自动同步 / 移动端布局
- e8d9b7c6 fix(stream): _run_chat 返回结果 — 修 auth 重构引入的兜底假象
- 08826746 feat(auth): 会话列表多端同步 (P1)
- 2f93278e feat(auth): 用户注册/登录 + 多用户隔离 + 跨端同步通知
- 9c33e1d6 fix(graph): File→File 依赖边查询去关系名限制 + 未索引自动 ensure_indexed
- 05f043d2 feat(git-panel): 容器挂载宿主仓库 /repo — Git 面板操作真实 veya 仓库
- a1b43e6e fix(plan-board): quota 摘要改 async await (asyncio.run 在 event loop 内报错被吞 → unknown)
- 3557b45c test: 状态内核 + 认知增强 (plan_todo/long_read) 单元测试 — 867 全绿
- d7e74a82 feat(state-kernel): Phase 2+3 — Spend 记账 / Terminal Gate / 公私边界扫描 (主脑零改动)
- 6560ddf8 feat(cognition): 主脑认知增强 — plan_todo 计划看板 + long_read 长文导航
- e2690212 主脑瘦身: ① 提示去自吹 + ②-A skills 72→2 (工具面 93→23, 提示 39KB→18.7KB) (#4)
- 84647650 强上下文 + 个人记忆 (P1–P4): 主脑不再失忆 + 理解优先门 + 跨设备 + 蒸馏记忆 (#2)
- 1a6d1c2d fix(engine): claude stream-json 整条 assistant 消息解析 — 修 claude 引擎零输出
- c3634717 fix(engine): stream_engine 超时用 asyncio.timeout — 修 async for wait_for 500; 测试补 grok mock
- 61e3f140 fix(engine): asyncio.TimeoutExpired → TimeoutError — 修复引擎流 500
- 708e1e4d feat(engine): 容器 grok 引擎探测 — ~/.grok 挂载后自动放行
- 01396943 整改 A+C: 认知债清零 + CLI 统一主脑 + 全量审计记录 (#1)
- 937fa0c9 fix(llm): 空回复外环兜底 — 本地 gpt-5.6-luna + 核心工具面, 覆盖 quality-gate 升级路径
- 38022fb2 fix(master): 恢复工具面分层 (诱惑管理) — 设计任务不再被行情工具带偏
- 65fa1913 fix(master): URL 预抓内容清洗+收紧至 2500 字 — 避免撑爆 free 池网关上下文
- 2413e9e5 fix(master): GitHub 链接可靠回复 + 网关空响应 3 次退避重试
- adb64f3e fix(master): LLM 边界绝不静默 — 空/'None' 响应温和重试一次
- d894fe11 fix(master): 编程任务收尾兜底不覆盖有意结果 — 配额暂停/失败一律尊重
- 47f04f64 feat(master): 主脑回归原生智能 — 模型自主路由, 删程序化前置判断, 四层绝不静默
- f3142f52 fix(master): 创作任务走 frontier 全链路 + hevi 视频生成端到端打通
- 08f2cb94 fix(master): hevi/Open Design 视频管线全链路打通 + 多层'不回复'根因修复
- 42efd104 fix(skill): img2threejs 预览 HTML 修复 + guardian SkillMeta 登记 + g7 隔离
- f430a69e feat(skill-hub): capabilities 能力发现命令 (Discovery-First, md2wechat 语义)
- f66a33b6 feat(img2threejs): 图片→3D 雕刻技能 + 前端 three.js artifact 预览
- 6b2afea0 feat(agent-project): 文件系统优先的 Agent 定义 (vercel/eve 机制内化)
- b2b7cae3 test(llm): 隔离宿主 ~/.veya/config.json 对无参调用测试的污染 (+3)
- 69bcff77 fix(brain): 无参调用默认走 veya1.1 别名路由 — 修复线上不回答
- 44e2b0c3 fix(guardians): ExecResult 登记 KNOWN_SYMBOLS — 契约差异非双实现
- f7b038bc fix(master): 轻量单轮 chat() 注入 system prompt — 修复 quick 档人格丢失
- b50415f3 feat(loop): rulebooks 装配 + engineering-flow review 自动注入规则书基线
- 87a4b62a perf: veya 回答提速 — 轻量快速路径 + URL 快速联网 + 网关超时防护
- 53168384 fix(llm-router): 特征提取误扫 system prompt → frontier 误判 → 主脑空回答
- fd3ee207 feat(llm-router-v3): 长程/复杂任务深度理解与规划层 (强模型)
- 57fb11b9 fix(desktop): bundle.targets 明确列表 (deb/rpm/dmg/nsis) — targets all 强制 appimage 打包失败拖垮全平台
- 25cf0b69 fix(desktop): tauri resources 目录映射 + lib.rs bundled 路径 + upload 小写目录 (appimage/dmg/nsis)
- 90755275 fix(pyinstaller): playwright 浏览器改 headless shell 方案 (macOS .app 兼容)
- c89ab62b feat(llm-router-v2): 分层路由落地 — 动态成本阈值 + Frontier 档 + 质量闸门 + traces 分析
- 47dae800 test(replica): 8 算子实战验证 23/23 通过 + 验证脚本入库
- bc65f909 feat(replica): 三期 KiroCrew 层落地 (G7/G8) — 三平台复刻 8 算子全部完成
- dd26ffad fix(llm): endpoint 归一化提前到 llm_call — 错误信息显示真实请求 URL
- 29c66369 feat(replica): 一期 Vigla 层 4 算子落地 (G3/G4/G5/G6)
- b84395d2 feat(prd): 4 算子正式 PRD + delegate_to_genesis 账本固化
- 31c68020 feat(officecli): OfficeCLI 集成 — 技能包 + sidecar 管理器 + 渲染-观察-修复闭环
- 1b4b52a8 feat(openhands): ACP 客户端 + 多 backend 挂载 + Issue 自动拆解
- d3b5d946 fix(engine): 容器环境只允许 master — 外部 CLI 引擎 520/502 根治
- 8165b555 test(veya_loop): P2/P3 行为测试矩阵 (231→256) + 蜜罐超时取证防回归
- 37724178 fix(online): 插件市场/定时任务 404 — Cindy 端点挂载根 app + 前端网关探活诊断
- cf6ce8db feat: P1 神经符号 API (allocate+VCG/deadlock/game) + 会话持久化 CheckpointStore
- f0166e9f opt(veya_loop): P1 神经符号能力面装配 + CLI 修复 + 守护测试 (78→230)
- 9dd8bc3f fix: /api/v1/agent/stream 500 — StreamingResponse 模块级导入 (master 分支 UnboundLocalError)
- b7e98650 fix: legacy /api/v1/agent/run dry_run 补 user_ref 伪匿名 (旧协议兼容)
- bade1ffe test: 连续对话历史测试 + HITL 断言更新
- 8572da44 feat: 旧 L4 网关协议兼容路由 /api/v1/agent/run|stream — 域名主脑 404 根因修复
- a651ef04 feat: veya-loop 全套件 — 因果闭环/可靠性/神经符号/审计 + 主脑原生智能
- 432c817f fix(llm): network retry + master brain rounds/execution fixes
- de6fcf2e refactor: retire legacy engine layer — all endpoints on Agent OS master brain
- 2a027aac Complete G10-G17: plugins SDK, checkpoint resume, cache benchmarks, release pipeline, i18n gate, deps
- d6b05cca Rename project to Veya; bump to 0.5.0
- da844ba7 Complete G6-G9 benchmark gaps; remove leaked API key

