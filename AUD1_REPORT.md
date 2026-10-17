# AUD1 — Team / Projects 登录态 + 返回主页

隔离环境：临时后端容器 `veya-aud1`（127.0.0.1:8797，独立库 `veya_aud1`，数据目录 `.aud1data/`），
前端用本仓库 `apps/web` 的 production build（`PORT=3199 VEYA_GATEWAY=http://127.0.0.1:8797 node build/index.js`）。
未占用 8793/8794，未触碰 veya-backend / veya-stage / 生产库，未 push / merge / deploy。

## 1. 根因（三处独立缺陷，共同表现为“已登录却提示需要登录”）

### 根因 A — Projects 后端在未认证时 500（`server/routes/projects.py`）

`get_current_user` 在 SEC H1 下对无效/缺失 token 返回 **`None`**（不是 `anonymous` 字典，见
`server/auth.py:get_current_user`）。但 `server/routes/projects.py` 里 7 个 handler 直接写
`str(user.get("user_id") or "anonymous")`，`user is None` → `AttributeError: 'NoneType' object has no
attribute 'get'` → FastAPI 返回 **500**。前端 `listProjects()` 只把非 2xx 渲染成错误文本，
页面就表现为“取不到数据 / 需要登录”的观感。

证据（`evidence/05_root_cause_projects_500.txt`，用 `git show 0e921313:server/routes/projects.py`
加载修复前模块，把 `get_current_user` override 成返回 `None` 复现）：

```
GET  /api/v1/projects           -> 500 Internal Server Error
POST /api/v1/projects           -> 500 Internal Server Error
GET  /api/v1/projects/x/brief   -> 500 Internal Server Error
```

修复后：

```
GET  /api/v1/projects            -> 200 {"projects": [...]}
POST /api/v1/projects            -> 401 {"detail":"需要登录才能创建项目"}
GET  /api/v1/projects/{id}       -> 404（owner 隔离，不再是崩溃）
GET  /api/v1/projects/{id}/brief -> 404
GET  /api/v1/projects/{id}/tasks -> 404
```

### 根因 B — Projects 前端把 GET 发成了 POST（`apps/web/src/lib/projects/api.ts`）

`$lib/api.ts` 的 `api()` 默认 `method: opts.method ?? "POST"`。`lib/projects/api.ts` 的 5 个读接口
（`listProjects` / `getProject` / `getProjectBrief` / `getProjectMessages` / `getProjectTasks`）
**都没传 `method`**，于是全部以 POST + 空 body 打到后端，被 Pydantic 判 422：

证据（`evidence/06_root_cause_client_method.txt`，对真实 8797 后端）：

```
OLD listProjects() (api() default POST, no body): {"status":422,"body":"{\"detail\":[{\"type\":\"missing\",\"loc\":[\"body\"],\"msg\":\"Field required\"...
NEW listProjects() (explicit GET):                {"status":200,"body":"{\"projects\":[{\"id\":\"default\",...
```

即：Team 页（`lib/team/api.ts` 全部显式写了 `method`）能用，Projects 页不能用的直接原因。

### 根因 C — SvelteKit `/api/v1` 代理没有 PATCH（`apps/web/src/routes/api/v1/[...path]/+server.ts`）

代理只导出 `GET/POST/PUT/DELETE`。浏览器里所有 PATCH（`patchBot`、`patchGroupRoles` 等）
在代理层就被判 **405**，到不了网关。生产 :3105（未修改的旧 build）实测：

```
PATCH /api/v1/bots/aud1-nonexistent-probe via :3105 -> 405
GET   /api/v1/bots/aud1-nonexistent-probe via :3105 -> 401
```

修复后经新 build 的 :3199 代理：`PATCH /api/v1/bots/chef -> 200 {"status":"updated",...}`。
证据：`evidence/07_root_cause_patch_proxy.txt`。

### 根因 D（返回主页）— Team 顶层页没有回主页入口

`/team` 与 `/remote`、`/approvals`、`/missions`、`/settings/*`、`/mab` 都没有 `href="/"`；
只有 `/projects` 和 `/workbench/[id]` 有。Team 因此“进得去、回不来”。

### 顺带发现 — 登录态只写 localStorage，不写 cookie

`lib/auth.svelte.ts` 只维护 `localStorage["veya.auth.token"]`，而 `server/auth.py` 的
`_request_token()` 支持 `Authorization` → `?token=` → `cookie(veya_token | veya.auth.token)`。
任何走 cookie 的路径（同源 SSR/直连后端/新标签页）拿不到身份。已补齐双向同步。

## 2. 改动

| 文件 | 改动 |
|---|---|
| `server/routes/projects.py` | 7 处 `user.get(...)` → `(user or {}).get(...)`；`create_project` 对未认证显式 401（原来 500） |
| `apps/web/src/routes/api/v1/[...path]/+server.ts` | 增加 `export const PATCH` |
| `apps/web/src/lib/projects/api.ts` | 5 个读接口显式 `method: "GET"` |
| `apps/web/src/lib/auth.svelte.ts` | `persist()` 同步写/清 `veya_token` 与 `veya.auth.token` cookie（30 天，`SameSite=Lax`）；`loadToken()` 回退读 cookie；导出 `clearAuth()` |
| `apps/web/src/lib/api.ts` | token 读取加 cookie 回退；收到 401 且原本带 token 时调用 `clearAuth()`，避免虚假登录态 |
| `apps/web/src/routes/team/+page.svelte` | 加 `← 返回控制台`；加 `AuthGate`（登录/退出）；未登录时显示明确“需要登录”卡片 + 登录入口 + 回主页链接（不再只抛裸错误） |
| `apps/web/src/routes/projects/+page.svelte` | 加 `AuthGate`；未登录时明确“需要登录”提示卡片 |
| `remote` / `approvals` / `missions` / `settings/channels` / `settings/agent-publish-perms` / `mab` | 各加 `← 返回控制台` 到 header |
| `tests/test_aud1_auth_navigation.py` | 新增 6 条测试（RED → GREEN） |
| `scripts/verify_aud1_playwright.mjs` | 真实浏览器端到端验证脚本 |
| `evidence/*` | 根因复现、回归原文、4 张 Playwright 截图 |

未改动：`veya/obase/_llm_config.py`、`veya/obase/llm.py`、`tests/test_inferera_free_pool.py`（protected files）。

## 3. 测试

RED（提交 `0b50953e`，修复前）：

```
$ python3 -m pytest -q tests/test_aud1_auth_navigation.py
6 failed in 0.64s
```

GREEN（当前）：

```
$ python3 -m pytest -q tests/test_aud1_auth_navigation.py tests/test_w16_routes.py tests/runtime/test_web_api_proxy_forwarding.py
18 passed in 3.29s
```

全量回归（`evidence/09_full_regression.txt`）：

```
$ python3 -m pytest -q tests/test_aud1_auth_navigation.py tests/test_w1*.py tests/test_nano_*.py \
    tests/test_lorca_*.py tests/test_orca_*.py tests/test_p3a_bot_isolation.py \
    tests/test_product_shell.py tests/test_gbp*.py tests/test_fix*.py
========== 1103 passed, 6 skipped, 27 warnings in 1054.57s (0:17:34) ==========
```

0 failed / 0 errors，无新增失败。中途一次运行中
`tests/test_gbp6_evoontology.py::test_bot_tool_skill_teach_eval_gate` 曾因上游 LLM
（NVIDIA NIM `410 Gone` / read timeout）失败，单独重跑与最终全量运行均通过，与 AUD1 无关。

`ruff check server/routes/projects.py tests/test_aud1_auth_navigation.py` → `All checks passed!`
`ruff format --check` 同上两文件 → clean。

前端构建 / 类型检查：

```
$ pnpm --dir apps/web check
svelte-check found 0 errors and 38 warnings in 4 files
$ pnpm --dir apps/web build
✓ built in 20.50s   (@sveltejs/adapter-node ✔ done)
```

38 条 warning 全部是改动前既有的 a11y/`svelte:component` 提示，非本次引入。

## 4. Playwright 真实登录验证（无 mock）

脚本 `scripts/verify_aud1_playwright.mjs`（headless Chromium 156.0.8078.4），对
`http://127.0.0.1:3199`（真实 production build）→ `VEYA_GATEWAY=http://127.0.0.1:8797`
（真实容器 `veya-aud1` + 真实 Postgres `veya_aud1`）执行：

1. 打开 `/`，点“登录”，切到“注册”，用真实账号注册并登录（`/api/v1/auth/register` 落库、签发真实 token）。
2. 跳 `/team`：断言页面不含“需要登录/请先登录”，Chef bot 卡片与“机器人”列表正常渲染 → `evidence/01_team_logged_in.png`。
3. 点 `← 返回控制台`：断言 URL 回到 `/` → `evidence/02_team_back_home.png`。
4. 跳 `/projects`：断言不含“需要登录/无法获取项目列表”，Default 项目卡片正常渲染 → `evidence/03_projects_logged_in.png`。
5. 点 `← 返回控制台`：断言 URL 回到 `/` → `evidence/04_projects_back_home.png`。

原文输出：

```
Starting AUD1 Playwright verification against http://127.0.0.1:3199...
Navigating to home page...
Opening login dialog...
Registering / logging in as u_mvbsxdja...
Successfully logged in, auth badge visible!
Navigating to /team...
Team page loaded successfully with no login errors.
Saved screenshot: evidence/01_team_logged_in.png
Clicking '← 返回控制台' on Team page...
Successfully navigated back to home page from Team!
Saved screenshot: evidence/02_team_back_home.png
Navigating to /projects...
Projects page loaded successfully with no errors.
Saved screenshot: evidence/03_projects_logged_in.png
Clicking '← 返回控制台' on Projects page...
Successfully navigated back to home page from Projects!
Saved screenshot: evidence/04_projects_back_home.png
ALL PLAYWRIGHT VERIFICATIONS PASSED!
```

截图里可见：Team 页右上 `u_mvbsxdja` 已登录徽章 + Chef 卡片 + 机器人列表；Projects 页
右上已登录徽章 + “Default / ID: default”项目卡片 —— 两页都不再提示未登录。

## 5. 同类问题排查（任务 3）

| 顶层页 | 改动前 | 改动后 |
|---|---|---|
| `/` | 有 AuthGate | 不变 |
| `/team` | 无回主页、无登录入口 | 回主页 + AuthGate + 未登录明确提示 |
| `/projects` | 有回主页、无登录入口 | 回主页 + AuthGate + 未登录明确提示 |
| `/projects/[id]` | 有回 `/projects` | 不变 |
| `/remote` | 无回主页 | 已加 |
| `/approvals` | 无回主页 | 已加 |
| `/approvals/[id]` | 依赖 `/approvals` | 不变 |
| `/missions` | 无回主页 | 已加 |
| `/settings/channels` | 无回主页 | 已加 |
| `/settings/agent-publish-perms` | 无回主页 | 已加 |
| `/mab` | 无回主页 | 已加 |
| `/workbench/[id]` | 已有回主页 | 不变 |

鉴权面：`/api/v1/bots*` 用 `require_user`（Team 页），`/api/v1/projects*` 用 `get_current_user`
（Projects 页）。二者现在都经同一套 `$lib/api.ts`（Bearer + cookie 回退 + 401 清理）与同一
`AuthGate` 组件，不再有第二套登录态。

## 6. Commit

```
0b50953e test(aud1): add failing test for Team and Projects auth and navigation (RED)
8971cf20 feat(aud1): fix Team and Projects auth state, cookie sync, and home navigation (GREEN)
34e83ef0 test(aud1): add Playwright verification script and evidence screenshots
325ff12b style(aud1): ruff format and import sort
9f511d75 docs(aud1): capture root-cause reproductions and regression evidence
```

## 7. 已知缺口（未在本轮处理，仅报告）

- `.aud1data/`（临时容器数据目录）未纳入 git，容器 `veya-aud1` 已 stop 保留供复核；库 `veya_aud1` 保留。
- `api()` 在浏览器无 token 时仍会把 GET 写成 POST 的默认值（`opts.method ?? "POST"`）保留原样：
  本轮只在 `lib/projects/api.ts` 显式传 `method`，未改全局默认（改全局默认会波及全部调用点，
  超出本任务范围）。若后续要根治，应把默认值改为 `GET` 并逐个 review 写调用。
- 生产 :3105 仍是修复前的 build，需要重新构建 + 重启 `veya-web` 才生效（本轮按规则不 deploy）。

AUD1 DONE
