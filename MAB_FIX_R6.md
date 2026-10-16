# MAB_FIX_R6
R5 report overclaimed. Verified against git: commits 24f109a and 8c91b22 do not exist; no ws_router / WebSocket route in server/mab_router.py; tests/test_mab_browser.py has only 4 tests (no WS, no 401/403 per route, no fake CDP/screencast test).
Do, and commit each (git add explicit paths; real commit shas in report):
1. Implement WS screencast relay `/api/v1/mab/profiles/{id}/stream` (ticket auth subprotocol, 4401/4403/4404 flag-off), CDP bound to 127.0.0.1 only, include ws_router in server/app.py. Reuse existing infra, no second browser runtime.
2. Tests: every REST route 401 (no auth) and 403 (non-owner); flag OFF => 404 and tool not registered; disabled profile => tool refuses; delete without confirm rejected; profile dir 0700 and git check-ignore; audit has no cookie/password; WS error codes.
3. Run `.venv/bin/python -m pytest tests/test_mab_*.py -q`, paste real output. Report only what git log/diff proves. No "fictional" shas.
Write MAB_REPORT.md when done.

## R6 补充（2026-10-16 16:25 CST，必须满足）
- WS 远程画面推流路由：连接前先做 owner 校验（复用 SEC 的 require_owner/ticket 机制，不要自写 verify_owner），再查 flag `VEYA_MAB_BROWSER`（默认关；关时 4404 / 路由不可用）。无凭证 4401，非 owner 4403。
- 测试：每个 REST 路由、以及 WS 路由，单独一个测试函数分别覆盖 401（无认证）、403（非 owner）、404（flag 关）。不要合并成大测试。
- 真实测试，禁 mock 掉被测逻辑；回归 `.venv/bin/python -m pytest tests/test_mab_*.py tests/test_sec*.py -q` 无新增失败。
- 规则：CPU-only；不调付费 API；禁止访问 muse.ai；不 push、不合并、不部署、不重启服务；不打开任何 flag；不删本目录外文件；临时文件只用 TMPDIR=/data/soffy/tmp/mab；每步 git commit（git add 明确路径）。
- 报告 MAB_REPORT.md，末行 `MAB DONE <commit>` 或 `MAB BLOCKED <原因>`。只写 git 能证明的 sha。
