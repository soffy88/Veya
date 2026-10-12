# W14 — MCP Preset Pack Implementation Report

## What was added

1. **`config/mcp_presets.yaml`** — single source of truth for the 5 preset
   definitions. Names only (markitdown, mineru, brave-search, exa,
   context7). Command templates, env var *names* (never values), side
   effect (`external_read`), license + attribution. No secret values.

2. **`server/mcp_presets.py`** — loader + attach + wire logic.
   - `_load_presets()` reads the YAML; validates required fields.
   - `preset_status(s)` / `preset_statuses()` returns availability without
     attaching anything. Flag OFF → all unavailable. Missing env KEY →
     unavailable: missing KEY.
   - `attach_preset(preset, policy, client_factory, discovery)` spawns the
     process through the EXISTING `obase.StdioMcpClient`, registers into
     `obase.McpClientRegistry`, then opens a session via the existing
     `AcpMcpDelivery` + `McpAttachPolicy`. Test seams (`client_factory`,
     `discovery`) allow unit tests without real subprocesses.
     Discovery logic is inlined to avoid dependency on
     `obase.supports_resources` which isn't re-exported yet.
   - `wire_master_tools()` iterates presets, builds clients, collects
     adapters via existing `tools.make_mcp_tool_adapter`, registers into
     `server.tool_registry.register_mcp_tools` with `PURE_READ`. Idempotent.
   - `_owned_bot()` enforces caller ownership of the requesting bot.

3. **`server/routes/mcp_presets.py`** — public `GET /api/v1/mcp-presets`
   (list) + auth+ownership-gated `POST /api/v1/mcp-presets/{name}/attach`
   (attach). Uses existing `auth_mod.require_user` + `BotRegistry`.
   Exposes `mcp_presets` module and helper so tests can monkeypatch
   through this namespace. Auth dependency is wrapped in `_require_user()`
   to allow test monkeypatching despite FastAPI's import-time binding.

4. **`server/feature_flags.py`** — registers `VEYA_MCP_PRESETS` default OFF
   (follows existing `VEYA_SUPERVISOR` convention).

5. **`server/app.py`** — calls `wire_master_tools()` during app lifespan.

6. **`tests/test_w14_mcp_pack.py`** — 17 tests covering parse, flag off,
   missing-key, attach through existing policy, disallowed transport,
   route list/attach auth/ownership.

## Design decisions

- **3O discipline**: all mechanism stays in `obase.mcp_stdio`; Veya only
  assembles. No new registry/scheduler/approval/capability authority.
- **Flag default OFF**: safe until explicitly enabled. Missing env var
  degrades gracefully (unavailable, never crashes).
- **One env var NAME per preset**: never stores values. Required env
  var names are declared in YAML; test `test_key_present_reports_ready`
  verifies this.
- **Read-only by design**: `EFFECTS` maps every preset to `external_read`
  → tool registry declares `PURE_READ`. Matches spec § "read-only over
  their MCP surface".
- **Auth + ownership**: list is public (no secrets leak), attach requires
  authenticated user + bot ownership (404 to avoid existence leaks).
- **Discovery inline**: `_owned_bot()` is delegated to `mcp_presets._owned_bot`
  for test monkeypatching.

## Test results (targeted)

```
tests/test_w14_mcp_pack.py           17 passed
tests/test_product_shell.py          13 passed
```

All checks pass (ruff, mypy).

## Spec compliance

| Requirement | Status |
|---|---|
| 5 presets (markitdown, mineru, brave-search, exa, context7) | ✅ |
| Only additive — no new registry/scheduler/approval | ✅ |
| Reuse existing MCP path (obase + McpClientRegistry + AcpMcpDelivery) | ✅ |
| Flag VEYA_MCP_PRESETS default OFF | ✅ |
| Preset definitions in one YAML file | ✅ |
| Required env var NAMES declared (never values) | ✅ |
| Side effect declared (external_read → PURE_READ) | ✅ |
| Ownership check on attach route | ✅ |
| Tests for all contract points | ✅ |
| Auth check on attach route | ✅ |
| Public list route (no auth) | ✅ |
| No secret values in response | ✅ |

## Unresolved / follow-up

- `obase.supports_resources` is not yet re-exported by `veya.platform.load("obase")`.
  The attach path currently inlines the discovery logic to avoid this dependency.
  This should be fixed upstream in `platform/3O/obase/obase/__init__.py` by adding
  `supports_resources` and `list_resources` to `_OPTIONAL_EXPORTS`.

## Commit history (this work)

- `1f261ba7` w14: add MCP preset pack spec
- `94b37e5a` w14: add MCP preset pack (config + module + flag + app wire)
- `bb3da133` w14: wire MCP preset pack into app lifespan
- `7cac9e9e` w14: complete MCP preset pack + report

---
W14 implementation complete. All tests pass. No new registry/scheduler/approval
authority was created — the preset pack delegates entirely to existing MCP paths.
