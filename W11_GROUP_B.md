# W11 group B — web (read BOT_LANDING_W11_SPEC.md first)
Scope: W-1 … W-7 in apps/web. Use existing API routes (/api/v1/bots*, groups, memory, routines, models/provider
registry). If a needed read-only endpoint is missing (e.g. list selectable models), add a minimal one in
server/routes with auth (require_user) and ownership checks, plus a test in tests/test_bot_landing_web_api.py.
Do not change bot turn/verification code (group A1) or routines/memory backend (A2).
Mobile first: the user uses an iPhone. Chinese-first strings.
Gates: pnpm run check 0 errors, pnpm run build OK, Playwright smoke apps/web/tests/bot_landing.spec.ts with mocked API
(if Playwright browsers are unavailable, make it runnable and note it).
Report: BOT_LANDING_B_REPORT.md
