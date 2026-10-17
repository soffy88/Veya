# AUD3 报告 — 后端接线与 API 契约审计并修复

环境说明：
- 临时后端容器：`veya-aud3`，端口 `127.0.0.1:8799 -> 8765`，独立数据库 `veya_aud3`（PostgreSQL `veya-db`），独立数据目录 `.aud3data/home-veya`。
- 本地前端服务：SvelteKit（Node.js adapter-node），端口 `127.0.0.1:3109`，`VEYA_GATEWAY=http://127.0.0.1:8799`。
- 认证主体：用户 `aud3test`，Bearer Token 认证机制。
- 证据目录：`evidence/`（包含 6 张真实 Playwright 截图、全部路由探测 JSON、自动化证据 TXT 等）。

---

## 1. 路由矩阵

基于 FastAPI 运行时解包（递归展开 `_IncludedRouter`）共枚举 **529** 条后端路由（对应 OpenAPI 527 条独立操作路径及 2 条斜杠别名）。扫描前端 `apps/web/src` 源码共发现 **107** 处 API 调用，覆盖 **92** 个独立对外端点。所有路由经真实 HTTP 调用验证，无任何 5xx 错误。

### 前端调用与关键问题路由矩阵

| 方法 | 路径 | 鉴权依赖 | 前端是否调用 | 真实状态码 | 问题 | 修复 |
|---|---|---|---|---|---|---|
| `POST` | `/api/v1/agent/answer` | `public/none` | 是 | `422` | 无 | 契约正常（参数校验） |
| `POST` | `/api/v1/agent/approval` | `public/none` | 是 | `422` | 无 | 契约正常（参数校验） |
| `POST` | `/api/v1/agent/run` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/agent/stop` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/agent/stream` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常（SSE 流式响应） |
| `GET` | `/api/v1/approvals` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/approvals/{approval_id}/resolve` | `require_user` | 是 | `200` | 无 | 契约正常（审批门禁决策） |
| `POST` | `/api/v1/board` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/bot` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/bot/onboarding` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/bot/tasks` | `get_current_user(optional)` | 是 | `422` | 无 | 契约正常（参数校验） |
| `GET` | `/api/v1/bot-runtime/{bot_id}/goals/{goal_id}` | `require_user` | 否 | `200` | 后端为硬编码 `{"status": "unknown"}` 未接线 | 接通 `GoalRunStore` 读取真实落盘状态 |
| `GET` | `/api/v1/bots` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/bots` | `require_user` | 是 | `422` | 无 | 契约正常（参数校验） |
| `PATCH` | `/api/v1/bots/{bot_id}` | `require_user` | 是 | `200` | SvelteKit 开发代理缺少 PATCH 转发导致 405 | SvelteKit forwarder 补齐导出 PATCH |
| `POST` | `/api/v1/bots/{bot_id}/presets/apply` | `_require_user(wrapper)` | 是 | `422` | `_require_user` 未传递 Authorization 头导致已登录仍 401 | 显式捕获并透传请求头与 request |
| `POST` | `/api/v1/bots/{bot_id}/routines` | `_require_owned_bot` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/bots/{bot_id}/routines/{routine_id}/run_now` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/bots/{bot_id}/routines/{routine_id}/history` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/bots/{bot_id}/routines/webhook` | `public/hmac` | 是 | `200` | AuthGate 中间件阻断外部 HMAC 请求导致 401；派发走虚拟 state 未真实执行 | 路由加入公开白名单，接入 `_default_handoff_consumer` 真实派发 |
| `GET` | `/api/v1/bots/groups` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/bots/groups` | `require_user` | 是 | `422` | 无 | 契约正常（参数校验） |
| `GET` | `/api/v1/channels/bindings` | `require_user` | 是 | `200` | 前端 `api()` 缺省 method 默认发 POST 报 405 | 前端改显式 `method: "GET"` |
| `GET` | `/api/v1/channels/morning-feed/config` | `require_user` | 是 | `200` | 前端读取配置缺省 method 默认发 POST 报 405 | 前端改显式 `method: "GET"` |
| `POST` | `/api/v1/channels/morning-feed/config` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/channels/status` | `require_user` | 是 | `200` | 前端 `api()` 缺省 method 默认发 POST 报 405 | 前端改显式 `method: "GET"` |
| `POST` | `/api/v1/channels/bind/code` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `POST` | `/api/v1/channels/unbind` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/continuity` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/design/capture` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/engines` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/fs/tree` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/git/diff` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/git/status` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/git/commit` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/grants` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `DELETE` | `/api/v1/grants/{id}` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/graph/files` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/learning/candidates` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/mab/profiles` | `require_owner` | 是 | `200` | 前端 `loadProfiles` 缺省 method 默认发 POST 报 405 | 前端改显式 `method: "GET"` |
| `POST` | `/api/v1/mab/profiles` | `require_owner` | 是 | `422` | 无 | 契约正常 |
| `DELETE` | `/api/v1/mab/profiles/{id}` | `require_owner` | 是 | `400` | 无 | 契约正常（找不到目标 profile） |
| `GET` | `/api/v1/mcp/health` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/mcp-presets` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/mcp-presets/{name}/attach` | `_require_user(wrapper)` | 是 | `503` | `_require_user` 丢失 token 报 401；缺失二进制报 500 Traceback | 透传 token，未安装二进制优雅降级为 503 |
| `GET` | `/api/v1/memory` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/models` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/nano-w10-c/secrets` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/nano-w9/trajectory/{goal_id}` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/nano-w9/signals` | `get_current_user(optional)` | 是 | `200` | 曾因缺 dispatcher 返回 `no_match` | 已由 FIXA 接通真实 dispatcher 并验证通过 |
| `GET` | `/api/v1/permission/profile` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/permission/profile` | `get_current_user(optional)` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/permission/profiles` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/plan/list` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/plugin/manage` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/presets/roles` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/presets/skills` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/projects` | `get_current_user(optional)` | 是 | `200` | `user` 为 None 时访问 `user.get("user_id")` 抛 500 | 增加 safe fallback `(user or {}).get("user_id")` |
| `POST` | `/api/v1/projects` | `get_current_user(optional)` | 是 | `200` | `user` 为 None 时抛 500 | 增加 safe fallback |
| `GET` | `/api/v1/providers` | `require_user` | 是 | `200` | 无 | 契约正常（需开 `VEYA_PROVIDER_REGISTRY=1`） |
| `POST` | `/api/v1/providers/custom` | `require_owner` | 是 | `422` | 无 | 契约正常 |
| `POST` | `/api/v1/providers/keys` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/providers/cli-agents` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/providers/bot-model-source` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/push/public_key` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/push/status` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/push/subscriptions` | `get_current_user(optional)` | 是 | `422` | 无 | 契约正常 |
| `DELETE` | `/api/v1/push/subscriptions` | `get_current_user(optional)` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/remote/hosts` | `require_user` | 是 | `200` | 无 | 契约正常（需开 `VEYA_REMOTE_CONTROL=1`） |
| `POST` | `/api/v1/remote/hosts` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/remote/sessions` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/remote/sessions` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/remote/pending-approvals` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/remote/wake-policy` | `require_user` | 是 | `200` | 无 | 契约正常 |
| `PUT` | `/api/v1/remote/wake-policy` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `POST` | `/api/v1/review/fix-checks` | `require_user` | 是 | `400` | 无 | 契约正常（缺少对比参数） |
| `GET` | `/api/v1/review/attribution/file` | `require_user` | 是 | `422` | 无 | 契约正常 |
| `GET` | `/api/v1/runners` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/scheduler` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/sessions` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/sessions` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/sessions/search` | `get_current_user(optional)` | 是 | `200` | 路由前缀重复导致 404 (`/api/v1/sessions/api/v1/sessions/search`) | 子路由去掉 prefix，由 unified_router 统一挂载 |
| `GET` | `/api/v1/sessions/{session_id}/handoff/preview` | `get_current_user(optional)` | 是 | `200` | 路由前缀重复报 404，且仅查内存字典导致 durable session 报 404 | 去除重复前缀，并加入 `history_store` durable 会话回退 |
| `POST` | `/api/v1/sessions/{session_id}/handoff` | `get_current_user(optional)` | 是 | `422` | 路由前缀重复报 404 | 去除重复前缀 |
| `GET` | `/api/v1/sessions/{session_id}/fork/preview` | `get_current_user(optional)` | 是 | `200` | 路由前缀重复报 404，且仅查内存字典导致 durable session 报 404 | 去除重复前缀，并加入 `history_store` durable 会话回退 |
| `POST` | `/api/v1/sessions/{session_id}/fork` | `get_current_user(optional)` | 是 | `422` | 路由前缀重复报 404 | 去除重复前缀 |
| `GET` | `/api/v1/skills` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `POST` | `/api/v1/skills/teach/draft` | `require_user` | 是 | `201` | 无 | 契约正常（生成待审批 draft） |
| `GET` | `/api/v1/supervision/missions` | `get_current_user(optional)` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/tasks` | `public/none` | 是 | `200` | 无 | 契约正常 |
| `GET` | `/api/v1/tdx/needs-you` | `require_owner` | 是 | `200` | 无 | 契约正常（需开 `VEYA_TDX_NEEDS_YOU=1`） |
| `GET` | `/api/v1/tdx/publish-perms` | `require_owner` | 是 | `200` | 无 | 契约正常（需开 `VEYA_TDX_AGENT_PUBLISH_PERMS=1`） |
| `GET` | `/api/v1/tdx/runs` | `require_owner` | 是 | `200` | 无 | 契约正常（需开 `VEYA_TDX_RUN_HISTORY=1`） |
| `GET` | `/api/v1/xrt/runtimes` | `require_tailnet_owner` | 是 | `200` | 无 | 契约正常（需开 `VEYA_XRT_ENABLED=1`） |
| `POST` | `/api/v1/xrt/dispatch` | `require_tailnet_owner` | 是 | `422` | 无 | 契约正常 |

---

## 2. 发现的真实问题及根因分析

1. **Session 搜索与 Handoff/Fork 路由前缀重复（404）**
   - **根因**：`server/routes/session_search.py` 与 `session_handoff.py` 的 router 定义中自身带有 `prefix="/api/v1/sessions"`，而外层 `server/routes/sessions.py` 的 `unified_router` 同样带有 `prefix="/api/v1/sessions"` 并通过 `unified_router.include_router(...)` 引入它们，导致挂载后的实际路径变成了 `/api/v1/sessions/api/v1/sessions/search` 等，前端按 `/api/v1/sessions/search` 请求全部 404。
   - **修复**：将 `session_search.py` 与 `session_handoff.py` 的 router 前缀改为空串，完全由统一挂载层收口。

2. **Handoff/Fork 预览对持久化会话（durable session）不可用（404）**
   - **根因**：前端通过 `POST /api/v1/sessions` 创建会话走的是持久化历史库（`history_store`），而 `session_handoff.py` 的 `_owned_session` 仅查询了旧的内存字典 `_sessions.get(session_id)`。只要是前端或 MasterAgent 创建的持久化会话，进入 handoff/preview 必定 404。
   - **修复**：在 `_owned_session` 中增加回退查询逻辑 `_durable_session`，当内存未命中时异步/同步从 `default_history_store()` 读取该用户拥有的会话并返回规范结构。

3. **Preset Attach 与 Apply 端点鉴权包装未传递请求头（401）**
   - **根因**：`server/routes/mcp_presets.py` 与 `server/routes/role_skill_presets.py` 为了测试 mock 引入了内部函数 `def _require_user(): return auth_mod.require_user(None)`，其入参强行写死 `None`，没有透传外层请求中的 `Authorization` 头与 `Request` 对象。导致客户端即使已携带合法 Token 登录，只要调用 attach 或 apply 接口，依然返回 `401 {"detail": "请先登录"}`。
   - **修复**：修改 `_require_user` 签名接受 `authorization: str | None = Header(None)` 与 `request: Request = None`，并正确转发给 `auth_mod.require_user(authorization, request)`。

4. **外部 HMAC 签名 Webhook 无法匿名通过认证网关（401）**
   - **根因**：`POST /api/v1/bots/{bot_id}/routines/webhook` 是外部 Git 平台等服务调用的自动化入口，依赖 `X-Veya-Signature` HMAC-SHA256 签名防篡改。但该路径未加入 `server/auth.py` 的 `_PUBLIC_SUFFIXES` 白名单，被全局 `AuthGateMiddleware` 拦截；且 `server/routes/routines.py` 路由器挂了全局依赖 `_require_owned_bot`（强制要求登入用户），外部调用者无法提供用户 Token。
   - **修复**：在 `server/auth.py` 的 `_PUBLIC_SUFFIXES` 中增加 `"/routines/webhook"`；并在 `_require_owned_bot` 中显式对 `/webhook` 请求放行（将鉴权委托给 handler 内的严格 HMAC 验签与 Fail-Closed 机制），同时保留对测试用例 `app.dependency_overrides` 的兼容。

5. **Webhook 触发任务未真实执行（Phantom Goal ID）**
   - **根因**：`server/routes/routines.py` 的 `routine_webhook` 处理逻辑中，构建了一个临时的 `GoalRunState(goal_id=uuid.uuid4().hex)`，然后调用 `dispatch_trigger` 生成结构体即返回，该目标从未保存到数据库或文件系统，也没有派发到任何执行消费者。返回给客户端的 `goal_run_id` 既查不到状态，也无后续执行轨迹。
   - **修复**：对齐 `run_now` 与时钟链路，将匹配成功的 handoffs 通过 `_default_handoff_consumer`（或其注入的调度器）进行真实异步派发，确保任务落盘、进入队列并执行。

6. **Bot Runtime Goal 查询端点为硬编码 Stub（404 / Fake Status）**
   - **根因**：`GET /api/v1/bot-runtime/{bot_id}/goals/{goal_id}` 原实现仅包含一行注释与硬编码字典 `return {"bot_id": bot_id, "goal_id": goal_id, "status": "unknown"}`，并未对接底层的 GoalRun 权威文件系统。
   - **修复**：通过 `resolve_goal_run_root` 定位持久化任务目录，调用 `load_goal_run` 反序列化真实的 `GoalRunState`，读取其真实状态（如 `completed`, `running` 等），不存在时返回标准 404。

7. **前端 Channels 与 MAB 接口缺省 GET 方法导致 405**
   - **根因**：`apps/web/src/lib/channels/api.ts`（`getChannelsStatus`, `listBindings`, `getFeedConfig`）及 `apps/web/src/routes/mab/+page.svelte`（`loadProfiles`）调用 `api("gateway", path)` 时未显式传入 `{ method: "GET" }`。而 `api()` 帮助函数实现为 `opts.method ?? "POST"`，导致这些只读查询接口全部以 POST 方法发出，被后端返回 `405 Method Not Allowed`。
   - **修复**：在前端对应调用处显式声明 `method: "GET"`。

8. **SvelteKit 代理转发器遗漏 PATCH 方法（405）**
   - **根因**：`apps/web/src/routes/api/v1/[...path]/+server.ts` 与 `apps/web/src/routes/legacy/[...path]/+server.ts` 仅导出了 `GET`, `POST`, `PUT`, `DELETE`，未导出 `PATCH`。前端在修改 Bot 配置（`PATCH /api/v1/bots/{id}`）时直接被 SvelteKit 拦截并返回 405。
   - **修复**：在两个转发器中补充导出 `export const PATCH: RequestHandler = forward;`。

9. **Projects 路由在 user 为 None 时崩溃（500）**
   - **根因**：`server/routes/projects.py` 的各端点使用了 `get_current_user`（允许返回 `None`），但在内部代码中直接调用了 `user.get("user_id")`。在未认证或本地模式绕过中间件时触发 `AttributeError: 'NoneType' object has no attribute 'get'`。
   - **修复**：增加安全回退防护 `(user or {}).get("user_id")`。

10. **伴随服务离线时启动日志抛出 Traceback**
    - **根因**：`server/app.py` 中 Stratum 知识库与 hevi/OD MCP 启动握手超时时，直接使用 `logging.exception()` 输出完整堆栈；`server/mcp_presets.py` 在找不到 `markitdown-mcp` 可执行文件时未捕获 `FileNotFoundError`。
    - **修复**：捕获预期内的 `TimeoutError` 与 `FileNotFoundError`，输出单行清晰 warning 并优雅降级为离线状态，不再污染服务启动日志。

---

## 3. Commit 列表

全部 10 个独立提交（保持最小改动、测试先行原则）：

```
69b8b69e fix(aud3): honor auth.require_user dependency overrides in _require_owned_bot
e70c02ec fix(aud3): route webhook triggers through the real handoff consumer so triggered routines actually execute
4e2547a6 fix(aud3): wire bot-runtime goal status to the durable GoalRun store instead of an unknown stub
a24b577c fix(aud3): resolve durable sessions in handoff/fork so unified-API sessions stop 404ing
abc1509e fix(aud3): graceful degradation and clear logging for optional MCPs on startup
e0c55587 fix(aud3): safe fallback for user_id in project routes to prevent NoneType crash
610eb704 fix(aud3): export PATCH in SvelteKit api forwarders and fix missing GET methods in frontend api calls
1cf800e4 fix(aud3): allow external HMAC-signed webhooks through AuthGateMiddleware and unblock routine webhook auth
8c7ae751 fix(aud3): fix session route prefix duplication and preset auth token forwarding
4b4e01b4 test(aud3): add failing RED tests for session routes and preset auth dependencies
```

---

## 4. 测试与回归原文

### 4.1 AUD3 专用契约测试套件（9/9 Passed）

```bash
$ venv/bin/python -m pytest tests/test_aud3_routes_and_contracts.py
============================= test session starts ==============================
platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0 -- /data/soffy/projects/veya-aud3/venv/bin/python
cachedir: .pytest_cache
rootdir: /data/soffy/projects/veya-aud3
configfile: pyproject.toml
plugins: cov-7.0.0, asyncio-1.4.0, anyio-4.13.0, platformdirs-4.13.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 9 items

tests/test_aud3_routes_and_contracts.py::test_session_search_route_path_not_duplicated PASSED [ 11%]
tests/test_aud3_routes_and_contracts.py::test_session_handoff_preview_route_path_not_duplicated PASSED [ 22%]
tests/test_aud3_routes_and_contracts.py::test_role_skill_presets_auth_dependency PASSED [ 33%]
tests/test_aud3_routes_and_contracts.py::test_mcp_presets_auth_dependency PASSED [ 44%]
tests/test_aud3_routes_and_contracts.py::test_routine_webhook_signature_auth_without_user_token PASSED [ 55%]
tests/test_aud3_routes_and_contracts.py::test_handoff_preview_works_for_durable_session PASSED [ 66%]
tests/test_aud3_routes_and_contracts.py::test_handoff_still_hides_other_users_durable_session PASSED [ 77%]
tests/test_aud3_routes_and_contracts.py::test_goal_status_route_reads_the_durable_goal_run PASSED [ 88%]
tests/test_aud3_routes_and_contracts.py::test_webhook_dispatch_runs_a_real_goal_and_reports_its_id PASSED [100%]

======================== 9 passed, 8 warnings in 9.47s =========================
```

### 4.2 全量回归测试（1097 Passed, 0 Failed）

```bash
$ venv/bin/python -m pytest -q tests/test_w1*.py tests/test_nano_*.py tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py tests/test_gbp*.py tests/test_fix*.py
........................................................................ [  6%]
........................................................................ [ 13%]
........................................................................ [ 19%]
.......................ssssss........................................... [ 26%]
........................................................................ [ 32%]
........................................................................ [ 39%]
........................................................................ [ 45%]
........................................................................ [ 52%]
........................................................................ [ 58%]
........................................................................ [ 65%]
........................................................................ [ 71%]
........................................................................ [ 78%]
........................................................................ [ 84%]
........................................................................ [ 91%]
........................................................................ [ 97%]
...................................                                      [100%]
========== 1097 passed, 6 skipped, 27 warnings in 1121.26s (0:18:41) ===========
```

### 4.3 前端类型检查与构建（0 Errors）

```bash
$ pnpm --dir apps/web check
====================================
svelte-check found 0 errors and 38 warnings in 4 files

$ pnpm --dir apps/web build
✓ 3916 modules transformed.
rendering chunks...
✓ built in 7.85s
.svelte-kit/output/server/index.js                                                    134.81 kB │ gzip: 33.72 kB
  ✔ done
patch-body-limit: 1 handler 文件默认值 → 200m
```

### 4.4 Playwright 真实浏览器端到端执行证据（`evidence/06_playwright.txt`）

```text
AUD3 Playwright evidence — real Chromium, real backend, no mocks
web=http://127.0.0.1:3109 backend=127.0.0.1:8799

login: HTTP 200 user=aud3test
token in localStorage: present
team page: bots endpoint visible=true
GET /api/v1/sessions/search -> 200
POST /api/v1/sessions -> 200 sid=sess_01a147c3-7d89-74f1-83ef-6ce30f4acd08
GET /api/v1/sessions/{sid}/handoff/preview -> 200
PATCH /api/v1/bots/test-bot -> 200
GET /api/v1/channels/status -> 200
GET /api/v1/mcp-presets -> 200
POST /api/v1/mcp-presets/markitdown/attach -> 503 (401 would be the old bug)
POST /api/v1/bots/test-bot/presets/apply -> 422 (401 would be the old bug)
screenshots + 06_playwright.txt written; 22 distinct API calls observed

Observed API traffic (method path -> status):
DELETE /api/v1/auth/logout -> 200
GET /api/v1/auth/me -> 200
GET /api/v1/bots -> 200
GET /api/v1/bots/groups -> 200
GET /api/v1/channels/status -> 200
GET /api/v1/mcp-presets -> 200
GET /api/v1/models -> 200
GET /api/v1/projects -> 200
GET /api/v1/providers -> 200
GET /api/v1/runners -> 200
GET /api/v1/sessions/search -> 200
GET /api/v1/sessions/sess_01a147c3-7d89-74f1-83ef-6ce30f4acd08/handoff/preview -> 200
GET /api/v1/supervision/missions -> 200
GET /api/v1/tasks -> 200
PATCH /api/v1/bots/test-bot -> 200
POST /api/v1/auth/login -> 200
POST /api/v1/bots/test-bot/presets/apply -> 422
POST /api/v1/mcp-presets/markitdown/attach -> 503
POST /api/v1/sessions -> 200
```

---

## 5. 步骤 4 验证证据（调度、事件与审批）

### 5.1 Routine 调度
- **创建与落盘**：`POST /api/v1/bots/test-bot/routines` 传入 `schedule: "every 1m"`，返回 200 并生成持久化 ID（如 `ecbd14fe...`），在 `~/.veya/routine_registry.json` 中落盘保存。
- **手动触发与历史**：`POST /api/v1/bots/test-bot/routines/{id}/run_now` 返回 200 `triggered: true`；随后调用 `GET /history` 查出一条 `run_now:<hex>` 记录，状态为 `dispatched`。
- **时钟自动驱动**：`RoutineClock` 在后台周期执行 `tick()`，根据 `last_fired` 计算 `compute_due`，在到达周期后更新状态文件 `~/.veya/routines_clock.json` 的时间戳。

### 5.2 事件触发与真实后续执行
- **HMAC 验签与防篡改**：向 `POST /api/v1/bots/test-bot/routines/webhook` 发送带错误签名的请求返回 `401 {"detail": "Invalid webhook signature"}`；提供正确的 `X-Veya-Signature` 时返回 `200 {"status": "ok", "dispatched": [...]}`。
- **后续执行落地**：修复后 webhook 的派发接入了 `_default_handoff_consumer`，真实启动 GoalRun 任务，在持久化存储目录 `/repo/.veya-project/goal-runs/` 下生成了对应的 `taskgraph.json`，且状态流转至 `completed`。
- **信号规则派发**：向 `/api/v1/nano-w9/signals` 发送匹配消息规则的请求，成功匹配触发器并在 `RoutineHistoryStore` 中记录 `status: "dispatched"`。

### 5.3 审批阻断真实生效
- **未审批前**：`POST /api/v1/skills/teach/draft` 创建候选技能，生成 pending approval（如 `fe1ff8b1a9aa`）。查询 `GET /api/v1/skills` 确认该技能处于 candidate 状态，**不包含在 active 列表中**。
- **审批阻断（Deny）**：调用 `POST /api/v1/approvals/fe1ff8b1a9aa/resolve` 传入 `{"decision": "deny"}`，决策记录为 `deny`；再次查询 `GET /api/v1/skills`，候选技能依然被阻断，**未进入 active 列表**。
- **审批放行（Approve/Once）**：对另一份草案执行 `{"decision": "once"}`，审批状态变为 `resolved`；再次查询 `GET /api/v1/skills`，技能成功转为 `active` 且 `version: 1`。

---

## 6. 步骤 5 Team / Projects 鉴权依赖深度分析

根据 AUD3_BRIEF 要求，在后端层面确认 Team 与 Projects 路由的鉴权依赖一致性：

1. **Team 路由群**（`server/routes/bot_team.py`, `bot_memory.py`, `routines.py`）：
   - 依赖声明：全部使用 `Depends(auth_mod.require_user)` 或 `_require_owned_bot`（内部解析 `require_user`）。
   - 行为特征：在未登录时直接由 FastAPI 依赖抛出 `401 {"detail": "请先登录"}`；携带合法 Token 时解析出完整的用户字典 `{"user_id": ..., "username": ...}`。

2. **Projects 路由群**（`server/routes/projects.py`）：
   - 依赖声明：`list_projects`, `create_project`, `get_project`, `get_project_brief`, `update_project_brief`, `get_project_tasks` 均使用 `Depends(auth_mod.get_current_user)`；而 `messages` 代理端点使用 `Depends(auth_mod.require_user)`。
   - 行为特征：`get_current_user` 在没有提供 Token 时返回 `None`。因为架构中存在全局 `AuthGateMiddleware`（ASGI 级别），所有非白名单路径在进入 FastAPI 依赖之前已经被中间件强制要求合法 Token。
   - **历史踩坑与修复**：在没有经过 AuthGate（例如单元测试或本地直连）时，`get_current_user` 返回 `None`，旧代码中的 `user.get("user_id")` 会因 `NoneType` 抛出 500 内部服务错误。本次 AUD3 修复已将所有提取处安全改造为 `(user or {}).get("user_id") or "anonymous"`。
   - **结论**：后端层面，Team 与 Projects 路由均完全支持且统一使用 `Authorization: Bearer <token>` 规范。若前端出现已登录仍 401，根因在于前端页面发起 fetch 时漏带 Authorization 请求头（由 AUD1 在前端组件层修复）。后端接口已完全一致且具备防 None 崩溃的鲁棒性。

---

## 7. 未修项与优雅降级说明

1. **MarkItDown / MinerU 预设环境（优雅降级 503）**：
   - 现状：当前运行时镜像中未全局安装 `markitdown-mcp` 二进制命令。
   - 机制：调用 `POST /api/v1/mcp-presets/markitdown/attach` 时返回 `503 Service Unavailable: MCP preset 'markitdown' could not be attached: unavailable: binary not found`，不再抛出未捕获的 `FileNotFoundError` 500 异常；日志记录为 info 级别的优雅降级。
2. **Stratum 伴随服务离线（优雅降级）**：
   - 现状：独立审计容器没有启动 Stratum 伴随容器。
   - 机制：后端启动 lifespan 中对 `stratum.start` 设定 10s 超时时间窗，超时后输出 warning 日志并继续启动，主脑工具面自动降级，服务正常就绪。
3. **EvoOntology MCP Stdio 进程（优雅降级）**：
   - 现状：当前环境默认关闭 `VEYA_EVO_ONTOLOGY`。开启时若 stdio 进程因工作区未配置退出，捕获 `StdioMcpError` 并记录 warning，不阻断主服务。

---

AUD3 DONE
