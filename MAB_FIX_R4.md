# MAB_FIX R4（2026-10-16 13:30 CST，opencode 工人）
规格: MAB_BRIEF.md；未完成项: MAB_FIX.md、MAB_FIX_R3.md。分支已合并最新 main 24ffca9d（e3333290，含 SEC 安全改动）。
硬性规则: 只改本 worktree；**不得修改/删除 main 已有代码**（git diff 24ffca9d -- server/ 除 mab 文件外只能有新增行）；不 push、不重启服务、不访问 muse.ai、不登录真实账号、不花钱；临时文件只用 TMPDIR=/data/soffy/tmp/mab；测试 `.venv/bin/python -m pytest tests/test_mab_*.py -q -p no:cacheprovider --basetemp=/data/soffy/tmp/mab/pt`。
## 必修（按序，每条完成即 git commit）
1. server/mab_router.py: 删除重复定义的 `_dispatch_cdp_input`/`_forward_cdp_frame`（只留一份）。所有 REST 路由改用 `Depends(require_owner)`（server/auth.py:411；未登录 401、非 owner 403），**鉴权先于 flag 检查**；flag VEYA_MAB_BROWSER 默认 OFF，OFF 时已鉴权的 owner 得 404。WebSocket: ticket 无效/缺失关闭 4401，非 owner 4403。
2. server/tool_registry.py 末尾有两段重复的 MAB 注册块，只保留一段（flag OFF 不注册）；`git diff 24ffca9d -- server/tool_registry.py` 只能有新增行。
3. 审计: `append_canonical_event` 用仓库真实签名（grep 其定义）；事件里不得含 cookie/storage/token。
4. 测试 tests/test_mab_*.py 全绿，并覆盖: 每个 REST 路由 未登录401/非owner403/owner+flag OFF 404；WS 4401/4403；flag OFF 工具不注册；fake CDP 帧转发+输入派发；profile 目录 0700 且 `git check-ignore .veya/mab/profiles/x` 命中；删除需 confirm=true。
5. 跑 `.venv/bin/python -m pytest tests/test_sec_*.py tests/test_mab_*.py -q`，SEC 测试不得新增失败。
## 交付
全部完成后写 MAB_REPORT.md（每条附 commit sha/文件/测试名/通过数），写完不再改代码。末行写 `MAB DONE` 或 `MAB BLOCKED: 原因`。

## 提示（13:05 UTC 追加，监控者）
测试里 owner 得 401 是因为没用 SEC 的测试夹具。照 tests/test_tdx_needs_you.py 写法：用 conftest 里的 `owner_token`/`owner_headers` 夹具（它们把 auth_mod._DB_PATH 指向临时库）；非 owner 用 `auth_mod.create_user("x", "another-long-password", is_owner=False)` + `auth_mod.issue_token(user["user_id"])`，请求带 `Authorization: Bearer <token>`。不要改 server/auth.py 或 conftest 已有代码。
