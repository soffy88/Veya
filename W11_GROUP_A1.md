# W11 group A1 — backend core (read BOT_LANDING_W11_SPEC.md first)
Scope: D1, D2, D3, production flag file deploy/bot_landing_flags.env, scenarios S1 S2 S3 S4 S10 S11 S12.
Tests: tests/test_bot_landing_e2e.py (your scenarios). Also create scripts/bot_landing_live.py with the
shared harness (register/login, create bot, send, poll messages, cleanup) and implement S1 S2 S3 S4 S10 S11 S12
there; group A2 adds S5–S9 to the same script later via functions in scripts/bot_landing_live_more.py that
your script imports if present (design the harness so that works).
Not yours: S5–S9 (A2), anything in apps/web (B).
Report: BOT_LANDING_A1_REPORT.md
