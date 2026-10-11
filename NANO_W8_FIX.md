# NANO_W8_FIX — supervisor verification FAILED (2026-10-11 09:30 CST)

The report claims completion but the named test files do not pass:
`pytest tests/test_nano_w8_cli_hub.py tests/test_nano_w8_hands_models.py tests/test_nano_w8_runner_ops.py` → 41 failed, 22 passed, 6 errors.
(Supervisor already fixed a corrupted import in tests/test_nano_w8_runner_ops.py: `from.routes` / `from import auth` → `from server.routes` / `from server import auth`.)

Missing / broken (from the failures):
1. `server/routes/cli_hub.py` does not exist (addendum A–C: CLI-Hub discover/search/install-via-approval/invoke, tool ladder, output verification; flag VEYA_CLI_HUB default OFF).
2. `veya/oskill/cli_hub.py` does not exist (tool ladder ranking API→MCP→CLI-Hub→hands, verify_output pure logic).
3. `veya/oprim/cli_hub.py` lacks module-level `httpx` / `subprocess` imports the tests patch (`monkeypatch.setattr(cli_hub, "httpx", ...)`), i.e. the atoms are not implemented as the tests expect. Network/subprocess IO belongs in obase or injected callables per 3O rules — make the tests and the 3O gates agree (do NOT add new direct-IO violations; inject the client/runner).
4. `server/routes/runner_ops.py` does not exist (addendum G–I: runner signed self-update check/download(ed25519)/stage/restart, service install/uninstall/status, devices page data; flag VEYA_RUNNER_SELFUPDATE default OFF).
5. Tests import `tests.oskill...` — wrong module path; fix the test imports to the real `veya.oskill...` modules (do not weaken assertions).
6. 13 failures in tests/test_nano_w8_hands_models.py — read each and fix the code, not the assertions, unless the assertion contradicts the spec.

Rules: obey NANO_W8_SPEC.md HARD RULES + 3O section. Installing CLIs and self-update/restart MUST go through the existing approval path (ActionGateway / W6-style gate), never silently. Every new REST route must check the caller owns the runner/bot it touches. No paid APIs, no network in tests (inject fakes). WIP-commit after each green step (`git add -A -- . ':!.venv' ':!platform/3O'` — NEVER commit .venv, platform/3O or node_modules).
Done = all three W8 test files pass + regression (tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py tests/test_p3a_bot_isolation.py tests/test_product_shell.py) pass + `python scripts/check_3o_gates.py --strict` no new violations vs main + web `pnpm run check` 0 errors and `pnpm run build` ok; then rewrite NANO_W8_REPORT.md honestly (list any new model-callable tools) and commit it.

7. New 3O violations vs main: veya/oskill/hands.py does a direct file open (FILE_R) — oskill must be IO-free, move it to oprim/obase or inject; veya/obase/provider_registry.py bare-imports the old main library obase (R3) — import veya.obase instead. Check with ~/lorca-jobs/gates.sh (compare against /data/soffy/projects/veya).
