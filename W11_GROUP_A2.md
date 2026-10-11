# W11 group A2 — backend always-on behaviours (read BOT_LANDING_W11_SPEC.md first)
Scope: scenarios S5 (memory), S6 (routine fires on its own and posts), S7 (loop goal + checkpoint cards),
S8 (group roles + @mention), S9 (secrets never leak). Fix whatever backend code stops them working with the
production flag set ON (see spec; set those env vars in your tests).
Tests: tests/test_bot_landing_e2e_more.py. Live: scripts/bot_landing_live_more.py exposing functions
s5(h)..s9(h) taking group A1's harness object h (A1 is writing scripts/bot_landing_live.py in parallel; if it
is not there yet, write a minimal compatible harness class in your file with the same method names:
register_login, create_bot, send, messages, cleanup).
D1 (plain chat blocked by verification) is group A1's fix; if you are blocked by it, make the smallest fix
compatible with the spec in the same file and note it in your report (A1 will reconcile at merge).
Not yours: S1–S4, S10–S12, D2, apps/web.
Report: BOT_LANDING_A2_REPORT.md
