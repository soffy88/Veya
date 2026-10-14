# OMB 交付报告：Clean-Room 借鉴 OpenMinis 架构特性

- **工程序号**：OMB (`<W>=OMB`)
- **工作区**：`/data/soffy/projects/veya-omb`
- **分支**：`feat/openminis-borrow`
- **交付日期**：2026-10-14
- **任务定义**：`/tmp/brief_omb.md`
- **进度日志**：`/tmp/OMB_progress.log`

---

## 一、概述与 Clean-Room 合规说明

本任务基于 Clean-Room（干净室）原则，借鉴 OpenMinis（GPL-3 许可）优秀设计思想，**完全独立自主重构实现**，严禁并做到零代码拷贝。所有设计细节与抽象接口均无缝接入 Veya 现有架构，未新增任何第二套调度器、注册表、权限网关或持久化引擎。

交付的三大功能项均通过特征开关（Feature Flag）进行管控，**默认全部处于关闭状态 (`default=False`)**：
1. `VEYA_SKILL_PROGRESSIVE`：技能渐进式加载（按需读取）
2. `VEYA_WORKSPACE_URI`：虚拟工作区 URI (`veya://workspace/<path>`)
3. `VEYA_MEMORY_UI`：记忆可视化与全局背景层（只读背景 + 逐条撤回）

---

## 二、功能详细架构与实现

### 1. 技能渐进式加载 (`VEYA_SKILL_PROGRESSIVE`)

- **背景与痛点**：传统模式下，Bot 提示词会将所有可用技能的完整 `SKILL.md` 源码全部塞入上下文，导致单轮 Prompt 占用上万 token（实测 11,753 tokens），极大侵占有效上下文并拖慢首包延迟。
- **架构设计**：
  - 特征开关：`server/feature_flags.py` 中注册 `VEYA_SKILL_PROGRESSIVE`（owner: `learning`，默认 `False`）。
  - 核心模块：`server/skill_progressive.py`。
  - 技能描述提取：启动/调用时复用 W15 presets（`config/presets/skills/**/SKILL.md`）和 Skills V2（`SkillSpec`），动态扫描并提取技能名称、ID 及前 200 字符简要描述，生成紧凑的 `<available_skills>` 列表注入 Prompt。
  - 按需加载工具：新增模型可调用工具 `read_skill(skill_id)`，纯只读无副作用，模型在真正需要执行某技能时按需读取完整 `SKILL.md` 内容。
  - Prompt 注入点：`server/bot_team/turns.py:build_objective`。当 flag 为 True 时，跳过全量技能正文 dump，仅渲染 `<available_skills>` 块。
- **Token 节约实测数据**：
  - **开启前（全量注入）**：**11,753 tokens**
  - **开启后（渐进摘要）**：**161 tokens**
  - **Token 降幅**：**98.6%**（单轮节省 11,592 tokens）

### 2. 虚拟工作区 URI (`VEYA_WORKSPACE_URI`)

- **背景与痛点**：模型在工具调用与文本输出中暴露服务器物理绝对路径（如 `/data/soffy/projects/...` 或 `/home/soffy/.veya/work/...`），存在信息泄露风险，且容易发生目录越界（`../` escape）隐患。
- **架构设计**：
  - 特征开关：`server/feature_flags.py` 中注册 `VEYA_WORKSPACE_URI`（owner: `runtime`，默认 `False`）。
  - 核心模块：`server/workspace_uri.py`。
  - URI 规范：统一采用 `veya://workspace/<相对路径>` 作为标准虚拟路径表示。
  - 安全与越界防御：`to_real_path()` 强制校验路径规范化，杜绝 `..` 越界序列及绝对路径注入，非法路径直接抛出 `PathTraversalError`。
  - 工具层适配：
    - `server/tool_registry.py`：`_resolve_path` 与 `_resolve_write_path` 原生支持识别 `veya://workspace/...` 并转换为物理工作区路径；同时在工具写文件输出、哈希行等返回文本中通过 `sanitize_text` 隐藏物理路径。
    - `server/routes/fs.py`：`_resolve` 支持解析 `veya://workspace/...`，`fs_read` 返回规范化虚拟路径。
  - 前端可视化与预览：
    - `apps/web/src/lib/components/MarkdownBlock.svelte`：支持自动解析 Markdown 中的 `veya://workspace/<path>` 链接并渲染为精美文件卡片，提供文件名图标、路径展示及「预览」交互按钮，支持弹出模态窗调用 `/api/v1/fs/read` 实时预览文本内容。

### 3. 记忆可视化与用户全局背景 (`VEYA_MEMORY_UI`)

- **背景与痛点**：Bot 团队缺乏用户层面的强制只读背景/偏好设定机制；同时模型自我沉淀的记忆零散存在底层 Markdown 文件中，用户在 UI 无法查看具体条目，更无法撤回失效或错误事实。
- **架构设计**：
  - 特征开关：`server/feature_flags.py` 中注册 `VEYA_MEMORY_UI`（owner: `learning`，默认 `False`）。
  - 用户全局背景存储：
    - 在 `server/bot_team/memory.py` 和 `memory_layers.py` 中定义 `USER_GLOBAL.md`。
    - **Agent 写入硬隔离守卫**：在 `BotMemoryStore.write`、`BotMemoryStore.delete` 及 `LayeredMemoryStore` 增加写拦截，只要检测到存在 `caller_bot_id`（即 Agent 自身发起的操作），立即拦截并抛出 `PermissionError("USER_GLOBAL.md is user-written and read-only to agents")`，确保用户设定的背景绝对只读。
  - 上下文注入：`server/bot_team/turns.py:build_objective` 在构建上下文时，将 `USER_GLOBAL.md` 作为 `<!-- user-global -->` 优先级区块注入，提示词中明确标注 `(Read-Only)`。
  - REST 端点（带 owner 鉴权）：
    - `GET /api/v1/bots/{bot_id}/memory/user-global`：读取用户全局偏好文本。
    - `PUT /api/v1/bots/{bot_id}/memory/user-global`：用户更新全局偏好文本。
    - `GET /api/v1/bots/{bot_id}/memory/entries`：解析并列出核心事实条目与实时记忆条目。
    - `POST /api/v1/bots/{bot_id}/memory/retract`：用户逐条撤回指定事实，自动从 `CORE.md`、`SCRATCH.md` 及每日日志中消除，记录审计并联动底层 `forget()`。
  - 前端界面增强：
    - `apps/web/src/lib/team/api.ts`：增加对应前端 API 请求客户端。
    - `apps/web/src/routes/team/bot/[bot_id]/MemoryTab.svelte`：
      - 新增「用户全局偏好设置」只读背景编辑卡片。
      - 新增「记忆条目列表」逐条可视化表格，区分 `[核心]` / `[临时]` 来源，并配备两步确认「撤回」交互。
      - 原有底层 Markdown 原始文件查看折叠保留，不破坏原有开发者体验。

---

## 三、新增模型可调用工具清单

| 工具名称 | 注册模块 | 声明原型 | 权限与副作用 | 说明 |
|---|---|---|---|---|
| `read_skill` | `server/tool_registry.py` (MasterToolRegistry) | `read_skill(skill_id: str) -> dict[str, Any]` | `SideEffect.PURE_READ`，`parallel_safe=True` | 根据技能 ID 按需加载并返回该技能的完整说明与 `SKILL.md` 正文内容。仅在模型需要使用指定技能时调用，零系统副作用。 |

---

## 四、测试与门禁验证结果

### 1. 专项单元测试
- **执行命令**：`.venv/bin/pytest -v tests/test_skill_progressive.py tests/test_workspace_uri.py tests/test_memory_ui.py`
- **结果**：**24 passed, 0 failures**（耗时 17.48s）
  - `test_skill_progressive.py`：8 项测试全部通过（开关验证、预设读取、错误处理、工具注册、上下文注入前后对比、Token 节约实测）。
  - `test_workspace_uri.py`：8 项测试全部通过（开关验证、URI 互转、防跨目录越界、文本路径脱敏、工具读写集成、FS API 路径映射）。
  - `test_memory_ui.py`：8 项测试全部通过（开关验证、Agent 写入守卫拒绝、上下文注入、REST user-global 读写、REST 条目列举与撤回）。

### 2. 全量回归测试
- **执行命令**：
  ```bash
  .venv/bin/pytest -q \
    tests/test_w1*.py \
    tests/test_nano_*.py \
    tests/test_lorca_*.py \
    tests/test_orca_*.py \
    tests/test_p3a_bot_isolation.py \
    tests/test_product_shell.py \
    tests/test_llm_router.py \
    tests/test_gateway_auth.py \
    tests/remote/test_policy_resolver.py \
    tests/remote/test_effect_registry_authority.py
  ```
- **结果**：**1082 passed, 6 skipped, 0 failures**（耗时 133.80s）

### 3. Lorca 架构门禁对比 (`gates.sh`)
- **执行命令**：`bash ~/lorca-jobs/gates.sh /data/soffy/projects/veya /tmp/canonical_gates && bash ~/lorca-jobs/gates.sh /data/soffy/projects/veya-omb /tmp/omb_gates`
- **对比结果**：**零新增 FAIL**
  - `check_3o_gates.txt`：canonical 61 项，veya-omb 61 项（`scanned=812` -> `814`，违规数 0）。
  - `check_no_direct_io.txt`：canonical 274 项，veya-omb 274 项（完全一致，零新增）。
  - `check_no_reverse_dep.txt`：canonical 4 项，veya-omb 4 项（完全一致）。
  - `check_oskill_pure.txt`：canonical 0 项，veya-omb 0 项（完全一致）。
  - **总计数**：339 对比 339，**差值为 0**。

### 4. 代码质量与类型检查
- **Ruff Lint & Format**：
  - 对所有新增与修改的 12 个文件执行 `.venv/bin/ruff check` 与 `.venv/bin/ruff format --check`：**全部通过，0 错误**。
- **前端检查与构建**：
  - `pnpm --dir apps/web check`：**0 错误**（32 个既有无障碍告警）。
  - `pnpm --dir apps/web build`：**构建成功**（SSR 与客户端资源打包完成，无编译错误）。

---

## 五、受保护文件与安全规则自检

- [x] 未修改 `scripts/veya_llm_gateway.py`
- [x] 未修改 `server/project_brain/*`
- [x] 未将 `.venv` 或 `platform/3O` 纳入 git staging
- [x] 未新增第二套 scheduler / approval / policy / registry / authority
- [x] 所有新特性 feature flag 默认值为 `False`
- [x] 未执行任何远程 side effects 或服务重启

---

## 六、交付文件清单

1. `server/feature_flags.py`：注册 3 个特性开关。
2. `server/skill_progressive.py`：技能渐进式加载核心实现。
3. `server/workspace_uri.py`：虚拟工作区 URI 互转与防越界过滤。
4. `server/tool_registry.py`：注册 `read_skill` 工具，集成 virtual URI 与路径脱敏。
5. `server/routes/fs.py`：文件服务路由支持 virtual URI。
6. `server/bot_team/memory.py`：用户全局背景定义与 Agent 写入硬隔离。
7. `server/bot_team/memory_layers.py`：分层存储支持 user-global。
8. `server/bot_team/turns.py`：上下文组装中集成渐进式技能摘要与全局背景。
9. `server/routes/bot_memory.py`：增加记忆管理 REST 端点（user-global 读写、条目列举、撤回）。
10. `apps/web/src/lib/team/api.ts`：前端记忆相关接口方法与类型定义。
11. `apps/web/src/routes/team/bot/[bot_id]/MemoryTab.svelte`：前端记忆可视化与偏好编辑界面。
12. `apps/web/src/lib/components/MarkdownBlock.svelte`：前端 virtual URI 文件卡片解析与预览模态框。
13. `tests/test_skill_progressive.py`：技能渐进式加载测试。
14. `tests/test_workspace_uri.py`：虚拟 URI 互转与越界测试。
15. `tests/test_memory_ui.py`：记忆管理与守卫测试。
16. `OMB_REPORT.md`：本报告。
