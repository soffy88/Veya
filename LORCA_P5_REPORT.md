# LORCA P5 Report — mobile /team UI, PWA, Web Push, async turns

Finished 2026-10-10 by Hark after the OpenCode unit (lorca-chainA) stalled at the commit step; the agent's uncommitted work was verified and committed as-is.

## Built
- Web Push backend: `/api/v1/push` (VAPID public key, subscribe/unsubscribe); VAPID keys generated under `~/.veya/push/` (never committed).
- Push on bot / group / routine completion via `notification_center`.
- Opt-in async turns: `?async=1` on bot message, group turn and Chef returns 202; Chef proposal stays confirmable in the async path (`record_team_proposal()`).
- PWA: manifest, icons, service worker (offline shell + push + notification click).
- SvelteKit `/team` (mobile-first): bot list/edit, Chef, 1:1 chat (`team/bot`), group chat (`team/group`), Markdown memory, routines, push toggle (`apps/web/src/lib/team/`).

## Tests
- `./.venv/bin/python -m pytest -q tests/test_lorca_p5_push.py` → 22 passed.
- `apps/web`: `pnpm run check` → 0 errors, 0 warnings; `pnpm run build` → success.

## New model tools
- None.

## Known gaps
- Push delivery tested with a fake sender only; real browser subscription not exercised on device.
- Async turns run in-process (no durable queue); a server restart drops in-flight async turns.
