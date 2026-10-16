# MAB_FIX_R7（R6 报告被驳回，原因如下，全部必须修）
1. server/mab_router.py 工具 browser_profile_session 的 navigate/click/type/snapshot 是**假数据**（"Mock page"、"Safe text content only"），根本没调用 veya.mab.mab_manager.perform_action。必须改为真实调用 mab_manager.perform_action（async，用正确的事件循环桥接），返回真实 url/title/脱敏文本；保留审计与 disabled 拒绝；审计失败则拒绝。
2. WebSocket /stream 只收 ping 回 pong，没有 screencast 转发，没有 CDP 输入分发（_dispatch_cdp_input/_forward_cdp_frame 是死代码）。必须接入：用 session['page'] 的 CDP session（context.new_cdp_session）启动 Page.startScreencast，转发帧、ack，并把鼠标/键盘/文本输入分发回去；断开时 stopScreencast。必须只用 Playwright 管道，不得另开对外 CDP 端口（去掉 --remote-debugging-address 或确保仅 127.0.0.1 且不开放端口）。
3. _WS_TICKETS 从未被写入：没有签发 ticket 的 REST 端点。新增 POST /api/v1/mab/profiles/{id}/stream-ticket（owner+flag 检查，ticket 60s 一次性），测试覆盖。
4. verify_owner 函数含"无 owner 配置时返回 test_owner"和 X-Veya-User 头信任的后门，属 fail-open；删除（router 已用 require_owner 依赖，保持这个），并确保测试里 401/403 是真的（未认证 401、非 owner 403），不要靠后门。
5. 路由重复声明 dependencies 与 verify_mab_flag 重复定义，清理；flag OFF 时所有 REST 404。
6. veya/mab/__init__.py 的 delete_profile 的 rglob 手写删除：保持仅限 profile 目录内，且路径必须 resolve 后在 profiles_dir 之下（已有则保留）。
7. 工具注册在 tool_registry 仅 flag ON 时注册，OK；保持 github_pr_tools 注册不变。前端 /mab 页：flag OFF 隐藏入口；需要对接真实 stream-ticket + WebSocket 子协议 ticket.<token>。
8. 测试：新增测试证明 perform_action 真被工具调用（用 _session_factory 注入假页面）、disabled 后工具拒绝、ticket 一次性、跨 profile ticket 拒绝、非 owner 403。pnpm check 0 错 + build。
9. 报告必须如实：逐条列出每项对应的提交/文件/测试；没做的写明没做。不得写"MAB DONE"除非全部完成并 commit。
禁止：访问 muse.ai、登录真实账号、改 worktree 外文件、push。

## R7 补充（16:52 CST 复核 1f9127ee 追加）
- tests/test_mab_browser.py 只有 5 个大测试；R6 要求未满足：每个 REST 路由（list/create/get/put/delete/revoke/enable/stream-ticket）和 WS 路由，各自单独测试函数覆盖 401（无认证）/403（非 owner）/404（flag 关）。可用 pytest.mark.parametrize，但每个路由×状态码要是独立用例。
- 不得削弱 SEC：不改 server/auth.py、ALLOWED_HOSTS、require_owner。回归 `.venv/bin/python -m pytest tests/test_mab_*.py tests/test_sec*.py -q` 全过。
