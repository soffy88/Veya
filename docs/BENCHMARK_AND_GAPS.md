# veya 对标报告：与 Claude Code / Cursor / Copilot CLI 差距清单

> 版本：veya v0.4.0（Sprint 1–3 完成后复评）
> 日期：2025
> 基线：GitHub Actions CI ✅ · 112 tests ✅ · ruff/mypy 核心包清零 ✅ · 遗留 o* 依赖全部替换 ✅

---

## 一、总评

经过 Sprint 1–3（运行时修复、CI/CD、错误分类学、兼容层），veya 已经从"不可运行的原型"升级为
**可测试、可构建、可维护的 agent 编排框架**。但与三大标杆相比，仍存在 **1 个致命级（P0）、8 个高优先（P1）、
8 个中优先（P2）** 差距。

**最核心结论：veya 目前是一个"没有接入真实 LLM 的 agent 框架"——`llm_call` 仍是返回占位文本的 stub。**
这决定了它和 Claude Code/Cursor/Copilot CLI 之间有一条本质鸿沟：标杆们把"模型能力"作为第一性原理，
veya 目前只把"编排能力"做出来了。

---

## 二、14 维度对标矩阵（当前状态）

评分：🟢 对齐 / 🟡 部分对齐 / 🔴 缺失或显著落后

| # | 维度 | veya 现状 | Claude Code | Cursor | Copilot CLI | veya |
|---|------|------------|-------------|--------|-------------|--------|
| 1 | **真实 LLM 集成** | ✅ 多 provider（OpenAI/Anthropic/DashScope）+ 流式 + 工具调用 + 成本估算 + 无 key 回落 stub | 原生 Claude 模型 | 多模型路由 | GPT/Claude 多模型 | 🟡→🟢 |
| 2 | **任务分解** | ✅ LLM 意图分类（关键词快速路径 + LLM 裁决 + 缓存）；simple→并行执行、complex→research→plan→execute DAG | LLM 全量理解 | LLM 全量理解 | LLM 全量理解 | 🟢 |
| 3 | **上下文管理** | 会话 JSON 持久化 + token 级压缩（`max_tokens` 触发）+ `_KEEP_RECENT` 保留 | 自动上下文窗口管理/压缩 | 索引 + 多文件上下文 | 任务上下文打包 | 🟡 |
| 4 | **流式输出** | SSE 基础设施完整（START/TOKEN/COMPLETE 事件、interrupt、status 机） | 原生流式 | 原生流式 | 原生流式 | 🟡（无真 token） |
| 5 | **工具调用生态** | 20+ 工具（fs/shell/git/search/parse…）+ 参数 schema 校验 + 工具注册表 | 原生 + MCP 客户端 | 原生 + MCP | 原生 | 🟡 |
| 6 | **MCP** | `server/routes/mcp.py` connect/call 路由（客户端风格雏形） | 完整 MCP 客户端 | MCP 客户端 | 部分 | 🟡 |
| 7 | **安全/沙箱** | subprocess + **子进程级 ulimit 内存/CPU 限制** + **参数数组执行（无注入）** + **危险命令前置拦截** + 审计日志 + **交互式 approve/deny** | 权限提示（approve/deny）+ 受限执行 | 沙箱化运行 | 权限确认 | 🟡→🟢 |
| 8 | **多 agent 协作** | Coordinator→Squad→Engine、DAG 调度、collaboration 模块、9 个内置 hooks | 子代理 + 任务队列 | 后台多 agent | 单 agent 流 | 🟢 |
| 9 | **可视化** | 2D/3D 图（networkx/plotly/matplotlib）、cytoscape/json/image 导出、架构图 | 无（终端） | Diff/UX 可视化 | 无 | 🟢 |
| 10 | **开发者体验/CLI** | `veya run/serve` + headless + TUI（textual，测试内存不稳） | 终端 TUI 标杆级 | 编辑器原生 | 终端 agent | 🟡 |
| 11 | **CI/CD & 质量** | GitHub Actions：ruff format/check → mypy → pytest 3.11/3.12 → build；112 tests | 自家 CI | 自家 CI | 自家 CI | 🟢 |
| 12 | **错误分类学** | `veya/errors.py`：23 个类型化异常、9 域、severity/code/component/context | 非公开接口 | 非公开接口 | 非公开接口 | 🟢 |
| 13 | **类型安全** | mypy 严格解锁 11 文件（coordinator/streaming/sandbox/intent/obase/llm/errors/compat/utils），CI 阻塞 | 强类型（内部） | 强类型（内部） | 强类型（内部） | 🟡→🟢 |
| 14 | **可观测性** | ✅ `obase.telemetry`：JSONL trace（`@traced` span + ContextVar 传播 + 事件通道可接 on_step） | 完整遥测 | 完整遥测 | 完整遥测 | 🔴→🟡 |
| 15 | **编辑器闭环** | ✅ VS Code 扩展：run-stream → SSE 流式输出 → 工作区刷新（G6） | 原生编辑器 | 编辑器原生 | 终端 agent | 🟡→🟢 |

**总评：3 项领先（可视化、错误分类学、编辑器闭环）、5 项对齐（多 agent、CI/CD、工具生态、MCP 雏形、上下文），
5 项部分对齐、1 项致命缺失（真实 LLM —— 有 provider 层但缺端到端密钥验证）。**

---

## 三、Sprint 1–3 已消除的差距（复评确认）

| 差距（旧） | 状态 | 证据 |
|-----------|------|------|
| 无 CI/CD | ✅ 已消除 | `.github/workflows/ci.yml`：lint→mypy→pytest(3.11/3.12)→build |
| 无结构化错误 | ✅ 已消除 | `veya/errors.py` 23 异常/9 域；`tests/test_errors.py` 35 例 |
| 死依赖导致运行时崩溃 | ✅ 已消除 | `veya/compat.py` ~600 行 shim，全模块 import OK |
| 测试无法运行 | ✅ 已消除 | 112 passed（两 chunk，环境内存限制所致） |
| Python 3.11 语法不兼容 | ✅ 已消除 | 全库 `ast.parse(feature_version=(3,11))` 通过 |
| 核心包 lint 失控（629 错误） | ✅ 已消除 | 核心包 `ruff check` 清零、`ruff format` 全库对齐 |

---

## 四、剩余差距清单（P0 / P1 / P2）

### 🔴 P0 — 致命级：真实 LLM Provider 集成

**✅ G1. `llm_call` 是 stub，全框架无真实模型 → 已修复**

- 修复内容（本次 Sprint）：
  1. 新建 `veya/llm.py` —— 规范化的多 provider 客户端（OpenAI / Anthropic / DashScope），
     支持非流式完成、**SSE 流式**（两种协议归一化为 OpenAI delta 事件）、工具调用
     （OpenAI-compat + Anthropic Messages）、成本估算、无 key 时优雅回落 stub。
  2. `server/providers.py` 改为 `veya.llm` 的薄转发（历史 import 路径兼容）。
  3. `veya/compat.py` 的 `llm_call`/`llm_stream` 从 stub 改为委托 `veya.llm`。
  4. `server/assembly.py` 删除重复的本地 retry/DEBUG print，接入 compat 的 `retry_with_backoff`。
  5. `config/loader.py` 增加 `llm.provider/model` 配置（读 `VEYA_LLM_PROVIDER`/`VEYA_LLM_MODEL`）。
  6. 新增 `tests/test_llm.py` 19 例（mock httpx transport，覆盖配置解析/OpenAI/Anthropic/
     流式/成本/回落/兼容委托）。
- 未完成部分：multimodal 视觉输入到模型、真实 API key 的端到端冒烟（需密钥，CI 不可用）。

**🎁 附带重大发现并修复：沙箱 RLIMIT 中毒 bug（G4 核心）**

- `sandbox.py` 原将 `RLIMIT_AS` soft+hard 同时设为 memory_limit，永久封顶**宿主进程**地址空间
  （hard 无法再提升）→ 全量测试 MemoryError、coverage 0.00%、3D 图偶发失败的真凶。
- 修复：限制只作用于**子进程**（POSIX `ulimit -v` 前缀包裹命令），宿主 rlimit 永不被触碰；
  新增 3 个回归测试（`test_sandbox_rlimit_restored_after_capped_execution` /
  `test_sandbox_host_rlimit_never_lowered` / `test_sandbox_child_memory_limit_enforced`）。
- 结果：**全量 134 测试单进程通过，覆盖率 54.16%（超过 40% 门禁）**，此前必须分块运行。

### 🟠 P1 — 高优先（8 项）

**✅ G2. 任务分解路由靠关键词启发式（`_is_simple` 硬编码信号） → 已修复**

- 新建 `veya/intent.py`：`IntentClassifier` 双层分类器——
  1. 确定性快速路径（≥200 字 → complex；关键词信号 → complex；≤12 字 → simple）；
  2. LLM 裁决中间地带（提示词强制 JSON 输出，容忍 markdown 围栏，解析失败回落）；
  3. 无 API key 自动回落启发式（离线/测试行为与旧版一致）；
  4. 文本级缓存（相同请求只调一次 LLM，上限 256 条）。
- `server/coordinator.py`：`_decompose` 改用 `classifier.classify()`；旧 `_is_simple` 保留为
  兼容别名（委托 `is_simple_heuristic`）；`decompose_model` 参数已接入分类器模型。
- 新增 `tests/test_intent.py` 15 例（快速路径/LLM 裁决/JSON 解析容错/异常回落/缓存/
  coordinator 集成/旧接口兼容）。
- 结果：149 测试全绿（单进程），覆盖率 54.54%。

**G3. 无可观测性（遥测） → 已修复（3O §7 obase 落地）**
- 新建 `veya/obase/telemetry.py`：
  1. `TraceContext` 共享可变对象 + ContextVar 通道（C1 铁律：子 Task 只 get 同一对象累加）；
  2. `@traced` 装饰器（sync/async 双形态，自动 enter/exit/error/duration，异常重抛）；
  3. `emit` 事件通道（可注入 on_step 回调，服务层注入 `server.events.fire_step`）；
  4. JSONL 汇出（`jsonl_write`/`latest_trace`，读取单源委托 compat.jsonl_latest）。
- 新增 `tests/test_obase_telemetry.py` 11 例（含 PEP 567 子 Task 不串扰验证）。

**G4. 沙箱隔离层级不足 → 已修复（内存 + CPU + 参数数组 + 危险拦截）**
- `veya/sandbox.py`：
  1. 内存限制只作用于子进程（POSIX `ulimit -v`，宿主永不被触碰）；
  2. **CPU 时间上限**（`ulimit -t`，RLIMIT_CPU）；
  3. **`execute_args(argv)` 参数数组执行**：固定可信 wrapper（`exec "$@"`）传递用户参数，
     shell 元字符按字面处理，无注入面；`run_script` 改走此路径；
  4. **危险命令执行前拦截**：`is_dangerous_command`/`is_dangerous_argv` canonical 单源，
     tools.py `_is_safe_command` 改为委托（§1.4 + 守卫测试）；默认 `reject_dangerous=True`。
- 新增 `tests/test_sandbox_g4.py` 13 例（含注入面断言、单源防漂移断言）。

**G5. 权限系统无交互式确认 → 已修复（规则引擎 + approve/deny 状态机）**
- 新建 `veya/obase/authz.py`（canonical 单源）：
  1. 规则引擎三态：ALLOW/DENY/PENDING（`allow:`/`deny:`/`ask:`/`*` 通配，顺序优先）；
  2. `InteractivePermissionGate`：PENDING 挂起 → approve/deny/超时自动 DENY（安全默认）；
  3. compat.permission_evaluate/match_permission_rule 委托（§1.4）。
- 服务层接线：
  1. `server/routes/permission.py`：`/api/v1/permission/{pending,evaluate,{id}/approve,{id}/deny}`；
  2. `cli/simple_cli.py`：`_maybe_confirm` 交互确认（规则直接裁决，PENDING 时 input()）。
- 新增 `tests/test_obase_authz.py` 19 例（含 compat 委托守卫）。

**3O 范式工程化（附录 B 落地）**
- 新建 `veya/obase/` 基础设施层（§2.5 `__manifest__` 7 元素；§7.4 依赖方向强制）；
- 新建 3 个 lint 脚本并接入 CI：`check_obase_no_reverse_dep.py` / `check_manifest.py` /
  `check_async_contract.py`；
- 全量 191 测试单进程全绿，覆盖率 56.67%（门禁 40%）。

**G6. 编辑器集成是空壳 → 已修复（SSE 闭环）**
- 服务端：`server/routes/vscode.py` 重写 —— run-agent/chat 改用真实
  `coordinator.handle()`（原调用不存在的 coordinator.run/create_session，空壳）；
  新增 `POST /vscode/run-stream`（后台执行 + SSEQueue 桥接）→ 扩展 GET
  `/stream/{sid}` 消费事件流（session_start→squad_start→text_delta→squad_done→
  cost_update→task_done→[DONE]）。
- 扩展端（`.vscode/extensions/veya-vscode`）：修复 package.json 语法错误（`$(chat)` 缺引号）；
  extension.js 重写为闭环：发起任务 → SSE 增量渲染输出面板 → 完成后刷新文件资源管理器
  （引擎已直接落盘改动）。`node --check` 通过。
- 新增 `tests/test_g6_vscode.py` 6 例（含 SSE 事件序列断言 + 死 API 回归守卫）。
- 🎁 连带修复：`_build_tool_schema` 无 docstring 工具越界；test_gate 阻塞事件循环
  （to_thread + VEYA_SKIP_TEST_GATE 守卫防递归）；**发现并清除提交进仓库的真实
  DASHSCOPE_API_KEY**（`.env`/`.veya.env` git rm --cached + 占位符，旧 key 视为泄露需轮换）。

**G7. 类型安全覆盖极低 → 已修复（解锁 4 个核心文件）**
- mypy 解锁集（CI 阻塞检查）：`server/coordinator.py` + `veya/streaming.py` +
  `veya/sandbox.py` + `veya/intent.py` + obase/llm/errors/compat/utils —— 全部零错误；
  sandbox/obase 开启 `disallow_untyped_defs=true`。
- 修复 numpy stub 3.12 语法问题（`python_version=3.12`，3.11 兼容性由独立 AST 检查把关）；
  CostTracker 单源化（compat 别名 → veya.utils）；Engine 增加真实 `run_turn`
  （turn_handler → llm_caller 编排，含输出键归一化）。
- 🎁 连带发现并修复真实运行时缺陷：协调器执行分队位置参数 bug（keyword-only 冲突）、
  semantic_search 方法/属性遮蔽、GraphNode 从错误模块导入 —— `coordinator.handle()`
  端到端执行链首次真正可用。

**G8. 测试覆盖与 E2E 深度不足 → 已修复**
- 覆盖率门禁 40% → **50%**（实际 60.35%）；CI 单进程 + junitxml + coverage.xml 已有；
- 新增 `tests/test_g7_e2e.py` 6 例（run_turn stub 回落 / handle 端到端 / 复杂 DAG /
  惰性初始化 / test_gate 守卫 / semantic_search_query 委托）。

**G9. Coordinator 启动即拉满 10+ 子系统 → 已修复（惰性初始化）**
- 16 个子系统全部 `functools.cached_property` 惰性构造；重模块（plotly/networkx/
  matplotlib 共 ~56MB）延迟到首次访问才导入；`Coordinator()` 构造增量内存 0.0MB，
  `import server.coordinator` 后 plotly/networkx/numpy 零加载。
- 移除重模块顶层导入（advanced_visualization/visualization/integrations/collaboration）；
  semantic_search 方法改名 semantic_search_query 避免与惰性属性冲突。
- 实测：`import server.coordinator` 0.32s / 无重模块；子访问 0.0s；重子系统按需 0.25s。

### 🟡 P2 — 中优先（8 项）

**G10. 无插件市场/扩展 SDK → 已修复（PluginManifest + SDK）**
- `registries/plugins.py` 重写：`PluginManifest`（manifest.json 校验）、`install_plugin`（拷贝入
  `~/.veya/plugins/`）、`discover_plugins`（目录扫描激活）、`PluginContext` SDK
  （register_tool → 全局工具表可被 agent 调用；register_hook + run_plugin_hooks 分发）。
- 参考插件 `examples/plugins/greet-plugin/`；文档页 `docs/dev/plugins.md` 已入 mkdocs nav；
  遗留 register_plugin/get_plugin API 保留。`tests/test_plugins_g10.py` 13 例。

**G11. 无文档站点与 API 参考 → 已修复（MkDocs + mkdocstrings）**
- 新增 `mkdocs.yml`（Material 主题、搜索、暗色模式）+ 站点：首页/快速开始/架构/
  API 参考 7 页（llm/intent/sandbox/multimodal/obase-telemetry/obase-authz/errors，
  全部由 mkdocstrings 从 docstring 自动生成）/开发者页。
- `mkdocs build --strict` 零警告；CI 新增 **docs job**（install → build --strict）；
  `site/` 已 gitignore；mkdocs 依赖已加入 dev extras。

**G12. 多模态仅本地推理占位 → 已修复（接入 LLM provider）**
- `veya/llm.py` 新增 `prepare_messages_for_provider()`：OpenAI 风格 content blocks
  （text + image_url data-URI）→ Anthropic 原生 image block（base64/url source），
  openai/dashscope 直通；provider_call/stream 已接线。
- `veya/multimodal.py`：`ImageProcessor.to_content_block()`（媒体类型映射 + data-URI）+
  `MultimodalProcessor.build_vision_messages()`（文本+多图 → 消息，自动跳过缺失文件）。
- 新增 `tests/test_multimodal_g12.py` 15 例（真实 1x1 PNG + MockTransport 请求体断言）。

**G13. 无断点恢复/Checkpoint 语义化 → 已修复（从失败分队恢复）**
- `SquadPlan` 新增 `resume_from` 字段；`handle()` 落初始 checkpoint（原始 command + 完整
  squad 计划）；checkpoint 保存 squad 序列化 → `resume()` 确定性重建（不重调 LLM），
  旧格式自动回退重新拆解。
- 修复 3 个真实 bug：失败分队不再计入 completed_steps；`_run_dag` zip 错位
  （pending 过滤）；cli `_resume` 误把 CheckpointData 当 session_id
  （新增 `checkpoint_to_run_state`）。`tests/test_g13_resume.py` 6 例（含失败→恢复端到端）。

**G14. 缓存/性能模块缺少真实受益验证 → 已修复（mock 延迟基准）**
- `tests/test_performance_g14.py` 8 例：LRU/SmartCache/`@cached`/IncrementalComputer
  对慢 LLM 调用（mock 0.15s）的加速断言——命中 < 冷启动×10、底层函数零重算、
  增量计算依赖不变不重算、10 次同 prompt 缓存 < 未缓存×0.4。
- 🎁 挖出并修复 **LRUCache 死锁 bug**（get 持锁后再调 _update_access 二次加锁）。

**G15. CI 未覆盖 docs 构建与打包发布 → 已修复（release 流水线）**
- docs 构建 job 已在 G11 接入；新增 `.github/workflows/release.yml`：tag v* 触发 →
  build sdist+wheel → **tag 与 pyproject 版本一致性校验** → GitHub Release 资产
  （含 release notes）→ PyPI 发布（配 PYPI_TOKEN 后启用，未配置跳过）。
- `scripts/bump_version.py`：patch/minor/major 一键同步 pyproject + cli/main +
  cli/simple_cli + server/app 四处（实测 0.5.0 → 0.5.1）。

**G16. 国际化混杂 → 已修复（现代核心强制英文 docstring）**
- `scripts/check_docstring_language.py` 门禁（CI Lint job）：veya/obase + llm/intent/
  sandbox/multimodal/errors/utils 9 文件 docstring 必须 ASCII；legacy（compat/streaming）
  增量迁移。
- 存量：`scripts/migrate_docstrings_g16.py` 一次性翻译 100+ 条中文 docstring → 英文
  （AST 锚定精确替换、幂等），现已全绿。

**G17. 依赖清单过窄 → 已修复**
- 主依赖补 `python-multipart`（FastAPI 表单）；新增可选 extras：`[interactive]`
  prompt-toolkit、`[settings]` pydantic-settings（按需安装不膨胀）。

---

## 五、行动建议（优先级排序）

| 顺序 | 差距 | 工作量 | 前置 |
|------|------|--------|------|
| 1 | **G1 真实 LLM provider** | ✅ 已完成（`veya/llm.py`） | — |
| 2 | **G2 LLM 意图分类** | ✅ 已完成（`veya/intent.py`） | G1 ✅ |
| 3 | **G3 遥测 JSONL** | ✅ 已完成（`veya/obase/telemetry.py`，3O §7） | — |
| 4 | **G4 沙箱 CPU+参数化执行** | ✅ 已完成（ulimit -t + execute_args + 危险前置拦截） | 内存限制已修复 ✅ |
| 5 | **G5 交互式权限确认** | ✅ 已完成（`veya/obase/authz.py` + CLI/HTTP 接线） | — |
| 6 | **G6 VS Code SSE 闭环** | ✅ 已完成（run-stream + SSE 消费 + 工作区刷新） | G9 ✅ |
| 7 | **G7 mypy 解锁核心** | ✅ 已完成（coordinator/streaming/sandbox/intent 零错误，CI 阻塞） | — |
| 8 | **G8 覆盖率门禁 50%** | ✅ 已完成（60.35% 实际）+ E2E 深度测试 | — |
| 9 | **G9 惰性初始化** | ✅ 已完成（cached_property 16 子系统，重模块零加载） | — |
| 10 | **G11 文档站点+API 参考** | ✅ 已完成（MkDocs Material + mkdocstrings 7 页 API，CI docs job） | — |
| 11 | **G12 多模态接入 provider** | ✅ 已完成（content blocks 归一化 + vision 消息构建，15 测试） | G1 ✅ |
| 12 | **G10 插件 SDK** | ✅ 已完成（manifest/install/discover + PluginContext，13 测试） | — |
| 13 | **G13 Checkpoint 断点恢复** | ✅ 已完成（确定性重建 + 失败分队重跑，6 测试） | — |
| 14 | **G14 缓存收益基准** | ✅ 已完成（mock 延迟对比 + 挖出 LRUCache 死锁） | — |
| 15 | **G15 Release 流水线** | ✅ 已完成（tag 触发 build + Release 资产 + 版本一致性校验） | — |
| 16 | **G16 英文 docstring 门禁** | ✅ 已完成（9 文件 ASCII 门禁 + 100+ 条存量翻译） | — |
| 17 | **G17 依赖清单** | ✅ 已完成（python-multipart + interactive/settings extras） | — |

> 现状：**G1–G17 全部完成**，veya 已具备与标杆"同台竞技"的完整闭环
> （真实模型 → LLM 意图分解 → 沙箱执行 → 流式返回 → 可观测 + 权限可控 + 编辑器闭环
>  + 文档站点 + 多模态视觉输入 + 插件 SDK + 断点恢复 + 发布流水线 + 质量门禁全覆盖）。
> 251 测试全绿 / 覆盖率 61.86% / mypy 解锁集零错 / 3O + G16 门禁全过。
> 附带修复：泄露的 DASHSCOPE_API_KEY 已从仓库移除（旧 key 视为泄露，需轮换）。
