# MAB_FIX R5（2026-10-16 14:28 CST 监控者核对）
R4 报告被打回（改名 MAB_REPORT_R4_REJECTED.md）。已完成且保留: 去重 helper、tool_registry 单一注册块、REST require_owner 先于 flag。规则同 MAB_FIX_R4.md（含“提示”节），另：不访问 muse.ai。
## 仍未完成（逐条 commit）
1. **没有注册任何 WebSocket 路由**（只有 _take_ws_ticket/_dispatch_cdp_input/_forward_cdp_frame 帮助函数）。新增 `@router.websocket("/profiles/{profile_id}/stream")`：注意 router 级 `Depends(require_owner)` 对 WS 不适用——WS 路由放到单独的 `ws_router`（同前缀，无 router 级依赖）并在 server/app.py 新增 include 行；用 ticket 鉴权（缺失/无效 close 4401，非 owner 4403），flag OFF close 4404；连上后调用 CDP Page.startScreencast 转发帧，客户端 click/key 消息经 _dispatch_cdp_input 转 Input.dispatchMouseEvent/KeyEvent。CDP 只连 127.0.0.1。
2. 测试拆成独立 test 函数（现仅 4 个大函数）：每个 REST 路由各自 未登录401/非owner403/owner+flagOFF 404；ws-ticket 路由同；WS 4401/4403/4404；fake CDP（monkeypatch，不起真浏览器）验证帧转发和输入派发；flag OFF 工具不注册；0700；git check-ignore；delete 需 confirm。
3. `.venv/bin/python -m pytest tests/test_sec_*.py tests/test_mab_*.py -q` 全绿。
## 交付
写 MAB_REPORT.md（每条 commit sha + 测试名 + 通过数），末行 MAB DONE / MAB BLOCKED。
