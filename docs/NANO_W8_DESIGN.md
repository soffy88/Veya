# NANO-W8: Desktop "hands" on runners + subscription/provider models

## Element → Layer → Reason Table

| Element | Layer | Reason |
|---------|-------|--------|
| `hands_screenshot` | oprim | Atomic: single subprocess call (scrot/mss) to capture screen |
| `hands_list_windows` | oprim | Atomic: single subprocess call (xdotool/wmctrl) to list windows |
| `hands_click` | oprim | Atomic: single subprocess call (xdotool/pynput) to click at coordinates |
| `hands_type` | oprim | Atomic: single subprocess call to type text |
| `hands_key` | oprim | Atomic: single subprocess call to press key combination |
| `hands_scroll` | oprim | Atomic: single subprocess call to scroll |
| `hands_focus_window` | oprim | Atomic: single subprocess call to focus window |
| `hands_approval_gate` | oskill | Combines ≥2 oprims: checks approval + records action log + screenshot |
| `hands_operator_loop_step` | oskill | Combines screenshot oprim + VLM oprim + action selection logic |
| `provider_registry_extended` | obase | External adapter: manages provider configs, API keys, capabilities |
| `bot_model_source_config` | oskill | Pure logic: validates/selects model source per bot (server/key/cli) |
| `cli_hub_search` | oprim | Atomic: HTTP call to CLI-Hub registry |
| `cli_hub_read_skill` | oprim | Atomic: HTTP call to fetch SKILL.md |
| `cli_hub_install` | oprim | Atomic: subprocess to install CLI tool |
| `cli_hub_invoke` | oprim | Atomic: subprocess to invoke CLI with --json |
| `cli_hub_tool_ladder` | oskill | Pure logic: ranks API/MCP/CLI-Hub/hands for a task |
| `cli_hub_verify_output` | oskill | Pure logic: checks file magic bytes, size, duration bounds |
| `runner_self_update_check` | oprim | Atomic: HTTP call to release manifest |
| `runner_self_update_download` | oprim | Atomic: HTTP download with signature verification |
| `runner_self_update_stage` | oprim | Atomic: file operations to stage new version |
| `runner_self_update_restart` | oprim | Atomic: subprocess to restart runner |
| `runner_service_install` | oprim | Atomic: file write to create systemd/launchd unit |
| `runner_service_uninstall` | oprim | Atomic: file remove to delete systemd/launchd unit |
| `runner_service_status` | oprim | Atomic: subprocess to check systemd/launchd status |
| `runner_safe_to_restart` | oskill | Pure logic: checks runner state for active bot turns |
| `devices_page_runner_view` | oskill | Combines runner registry + update check oprims |

## Architecture Decisions

### Hands Capability (VEYA_HANDS)
- **oprims** in `veya/oprim/hands.py`: Each action is a single atomic operation
- **oskill** `hands_operator_loop_step` in `veya/oskill/hands.py`: Assembles screenshot → VLM → action
- **Approval**: Uses existing `ActionGatewayAdapter` + `SideEffectLedger` (W6)
- **Linux X11**: xdotool/scrot or python-mss+pynput behind capability detection
- **macOS/Windows**: Stubs returning `{"supported": false, "error": "platform unsupported"}`
- **Human takeover**: Pause/resume via GoalRun state

### Provider Models (VEYA_PROVIDER_REGISTRY)
- **obase** extends `veya/obase/provider_registry.py` (single source, re-exports platform 3O)
- **BotSpec** adds `model_source: "server" | "user_key" | "cli"` and `provider_config: dict`
- **Capability flags** on providers: `vision`, `image-gen` for UI feature gating
- **CLI subscription**: Uses W1 CLI agent registry if present, else detects binaries

### CLI-Hub (VEYA_CLI_HUB) & Tool Ladder
- **oprims** in `veya/oprim/cli_hub.py`: search, read_skill, install, invoke
- **oskill** `cli_hub_tool_ladder` in `veya/oskill/cli_hub.py`: Pure ranking logic
- **6-Step Tool Ladder Expansion (NANO-W10 主体 7c)**:
  1. `context` — In-context memory, previous turn artifacts, session summary (cheapest, zero IO).
  2. `connector/MCP/API` — Structured programmatic interfaces (`api`, `mcp`, `cli_hub`). The contract the service already publishes.
  3. `web search` — External search fallback ladder (`configured_provider -> model_native -> duckduckgo -> bing`).
  4. `browser` — DOM-to-Markdown reader (`read_browser`) or headless Playwright for unstructured web reading.
  5. `desktop` — Hands / GUI automation on runner display (AT-SPI / xdotool). Fragile, requires explicit escalation justification.
  6. `ask human` — Human escalation / clarification via SideEffectLedger approval when automation cannot proceed safely.
- **Verification**: GoalRun steps verify outputs (magic bytes, size bounds)

### Runner Self-Update (VEYA_RUNNER_SELFUPDATE)
- **oprims** in `veya/oprim/runner_ops.py`: check, download, stage, restart
- **oskill** `runner_safe_to_restart` in `veya/oskill/runner_ops.py`: Pure state check
- **Service**: `veya-runner service install|uninstall|status` CLI command
- **Devices page**: Extends P6 `RunnerRegistry` (no new device store)

## Flag Defaults (All OFF)
- `VEYA_HANDS=false`
- `VEYA_PROVIDER_REGISTRY=false`
- `VEYA_CLI_HUB=false`
- `VEYA_RUNNER_SELFUPDATE=false`

## Test Files
- `tests/test_nano_w8_hands_models.py` — Hands + Provider models
- `tests/test_nano_w8_cli_hub.py` — CLI-Hub integration
- `tests/test_nano_w8_runner_ops.py` — Runner self-update/service

## Regression Test Set (Per Spec)
- `tests/test_lorca_p1_bots.py`
- `tests/test_lorca_p2_groups.py`
- `tests/test_p3a_bot_isolation.py`
- `tests/test_product_shell.py` (2 resume failures pre-existing)

## Frontend Build
```bash
cd apps/web && NODE_OPTIONS=--max-old-space-size=2048 pnpm run check && pnpm run build
```

## 3O Gates
```bash
python scripts/check_3o_gates.py --strict
python scripts/check_no_reverse_dep.py
python scripts/check_oskill_pure.py
python scripts/check_no_direct_io.py  # baseline mode
pytest tests/guardians/test_3o_migration_guards.py
```
Zero NEW violations required.