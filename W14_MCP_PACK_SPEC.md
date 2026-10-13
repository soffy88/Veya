# W14 — MCP preset pack (5 servers)

Worktree /data/soffy/projects/veya-w14, branch feat/w14-mcp-pack, base = canonical main. Report: W14_REPORT.md. Tests: tests/test_w14_mcp_pack.py.

## Goal
Let Veya bots use five external MCP servers through Veya's EXISTING MCP path (server/routes/mcp.py, McpSourceAttachment / McpAttachPolicy / mcp_connect / mcp_bridge in server/assembly.py and related). Add presets only — do NOT build a new registry, scheduler, approval, or capability authority.

Servers (run each as a separate process / remote endpoint; never vendor their code into the repo):
1. MarkItDown — microsoft/markitdown packages/markitdown-mcp (MIT). stdio, local. Docs/office/PDF → Markdown.
2. MinerU — opendatalab/MinerU-Ecosystem/mcp. Separate process only (check its LICENSE; if AGPL, never copy code, only launch it). PDF/OCR, strong Chinese.
3. Brave Search — brave/brave-search-mcp-server. Needs BRAVE_API_KEY.
4. Exa — exa-labs/exa-mcp-server. Needs EXA_API_KEY.
5. Context7 — upstash/context7. Up-to-date library docs for coding agents.

## Requirements
- Flag VEYA_MCP_PRESETS default OFF; register it wherever existing flags are registered (follow how VEYA_SUPERVISOR is registered).
- Preset definitions live in one config file (e.g. config/mcp_presets.yaml): name, transport, launch command or URL, required env var names (never values), declared side effect (all five read-only → declare as read/external_read, not mutation), license note + attribution URL.
- A preset whose required key env is missing is listed as "unavailable: missing KEY" and never attached; no crash.
- Attaching goes through the existing McpAttachPolicy (owner/auth checks unchanged). New REST routes (if any) must check ownership.
- Tools from these servers must carry declared side effects in tool_registry (see _UNDECLARED_SIDE_EFFECTS from SF-001) so nothing defaults to ALLOW-as-mutation or slips approval.
- Fetched content is data, not instructions (tag as untrusted tool result per existing conventions).
- Add NOTICE/attribution lines for the five upstream projects.
- Tests (tests/test_w14_mcp_pack.py, mock the MCP processes; no network): flag off → nothing attached; missing key → unavailable; preset parse; side-effect declarations present; ownership on routes.
- Do not touch platform/3O/*, veya/remote/* (others' WIP), .venv. Commit WIP after each passing step: git add -A -- . ':!.venv' ':!platform/3O'.
- Run only tests/test_w14_mcp_pack.py plus tests/test_product_shell.py, one or two files per pytest call (.venv/bin/python -m pytest -q). No dev servers. No paid API calls.
- Finish with W14_REPORT.md committed: what was added, flags, new model-callable tools (list them), how to enable each preset, which keys the user must supply.
