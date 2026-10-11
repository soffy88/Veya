# NANO-W10 Group D: Web Observation & Reading Implementation Report

> Spec: `W10_GROUP_D.md`, `NANO_W10_SPEC.md` (branch `feat/nano-w10-web`).
> Design doc: `docs/NANO_W10_D_DESIGN.md`.
> Documentation update: `docs/NANO_W8_DESIGN.md`.
> Attribution notices: `THIRD_PARTY_NOTICES`.

---

## 1. Item Status

All 5 items in Group D have been fully implemented, verified, and committed:

| Item | Scope | Status | Details |
|---|---|---|---|
| **主体 6** | a11y-first 观察阶梯 (`VEYA_A11Y_FIRST`) | **COMPLETED** | Pure ranking in `veya/oskill/pure/observation_ladder.py`; adapters in `veya/obase/a11y.py`; integrated into `veya/oprim/browser.py`, `veya/oskill/browser.py`, `veya/oskill/hands.py`. Flag defaults to OFF (falls back to legacy screenshot-first). |
| **ADDENDUM R** | DOM Reader `read_browser` (`VEYA_DOM_READER`) | **COMPLETED** | Pure HTML-to-Markdown converter in `veya/oskill/pure/html_to_markdown.py` with `[#n]` reference indexing, sensitive field redaction, 400k/150k/5k/3k safety limits, SSRF protection against cloud metadata and local Veya ports, and firewall provenance. Model tool in `server/dom_reader_tools.py` registered in `server/tool_registry.py`. |
| **ADDENDUM T** | 搜索回退阶梯 | **COMPLETED** | Pure search ladder in `veya/oskill/pure/search_ladder.py` ordering `configured_provider -> model_native -> duckduckgo -> bing -> failure notice`; URL deduplication; prepends `Search backend: <name>`; adapters in `veya/obase/search_adapters.py`. |
| **ADDENDUM V** | 频道阶梯 + 探测 + CLI + REST + UI (`VEYA_CHANNEL_LADDER`) | **COMPLETED** | Pure channel ladder in `veya/oskill/pure/channel_ladder.py` for 9 web channels; real 5s timeout probing with agent-friendly fix hints in `veya/obase/channel_probes.py`; model tool `read_channel` in `server/channel_tools.py`; CLI `veya doctor channels [--json]` in `cli/product.py`; REST endpoint `GET /api/v1/channels/status` in `server/routes/channels.py` with authentication and caller attribution; UI section in `apps/web/src/routes/settings/channels/+page.svelte`. |
| **主体 7c** | W8 阶梯文档扩到 6 步 | **COMPLETED** | Updated `docs/NANO_W8_DESIGN.md` with full 6 steps (`context -> connector/MCP/API -> web search -> browser -> desktop -> ask human`). Implemented `full_6step_tool_ladder` and `FULL_TOOL_LADDER_ORDER` in `veya/oskill/cli_hub.py` and re-exported in `veya/oskill/__init__.py`. |

---

## 2. Actual Test Runs & Results

All tests run cleanly with zero failures.

### A. Group D Test Suite
- **Command**: `.venv/bin/python -m pytest tests/test_nano_w10_d.py`
- **Output**:
  ```
  ============================= test session starts ==============================
  platform linux -- Python 3.12.13, pytest-9.1.1, pluggy-1.6.0 -- /data/soffy/projects/veya-nano-w10d/.venv/bin/python
  cachedir: .pytest_cache
  rootdir: /data/soffy/projects/veya-nano-w10d
  configfile: pyproject.toml
  plugins: cov-7.0.0, asyncio-1.4.0, anyio-4.13.0
  collected 44 items

  tests/test_nano_w10_d.py ............................................   [100%]
  ======================== 44 passed, 1 warning in 31.19s ========================
  ```

### B. Regression Test Set (Hard Rules)
- **Command 1**: `.venv/bin/python -m pytest tests/test_lorca_p1_bots.py tests/test_lorca_p2_groups.py`
- **Output**:
  ```
  ======================= 28 passed, 6 warnings in 33.28s ========================
  ```
- **Command 2**: `.venv/bin/python -m pytest tests/test_p3a_bot_isolation.py tests/test_product_shell.py`
- **Output**:
  ```
  ======================= 32 passed, 6 warnings in 35.90s ========================
  ```

### C. 3O Architecture & Linter Checks
- **Command 1**: `.venv/bin/python scripts/check_oskill_pure.py`
- **Output**:
  ```
  [OK] oskill 纯净度通过 (共扫描 52 个文件)。
  ```
- **Command 2**: `.venv/bin/python -m ruff check veya/oskill/pure/observation_ladder.py veya/oskill/pure/html_to_markdown.py veya/oskill/pure/search_ladder.py veya/oskill/pure/channel_ladder.py veya/oskill/cli_hub.py veya/obase/a11y.py veya/obase/search_adapters.py veya/obase/channel_probes.py server/dom_reader_tools.py server/channel_tools.py server/routes/channels.py server/feature_flags.py tests/test_nano_w10_d.py
- **Output**:
  ```
  All checks passed!
  ```

### D. Frontend Verification
- **Command 1**: `NODE_OPTIONS=--max-old-space-size=2048 pnpm --dir apps/web run check`
- **Output**:
  ```
  svelte-check found 0 errors and 32 warnings in 3 files
  ```
- **Command 2**: `NODE_OPTIONS=--max-old-space-size=2048 pnpm --dir apps/web run build`
- **Output**:
  ```
  ✓ built in 7.48s
  ✔ done
  ```

---

## 3. New Model-Callable Tools

Two new tools were registered into `server/tool_registry.py` under `master_tools`:

1. **`read_browser`**
   - **Gated by**: Feature flag `VEYA_DOM_READER` (default `False`).
   - **Signature**: `tool_read_browser(url: str | None = None, html: str | None = None, max_chars: int | None = None) -> str`
   - **SideEffect**: `SideEffect.PURE_READ`
   - **Description**: Converts visible web DOM to structured Markdown with `[#n]` indexed references for interactive controls and links. Automatically skips hidden elements, redacts sensitive inputs (`password`, `autocomplete=cc-*`, `autocomplete=one-time-code`), enforces strict safety limits (400k chars, 150k nodes, 5k table rows, 3k controls), protects against SSRF (blocking cloud metadata and local Veya ports 8765, 8767, 9120, 3105), and tags output with firewall provenance.

2. **`read_channel`**
   - **Gated by**: Feature flag `VEYA_CHANNEL_LADDER` (default `False`).
   - **Signature**: `tool_read_channel(channel: str, target: str) -> str`
   - **SideEffect**: `SideEffect.PURE_READ`
   - **Description**: Reads content from an external web channel (`web`, `youtube`, `rss`, `github`, `bilibili`, `reddit`, `twitter`, `xiaohongshu`, `linkedin`) using the channel's ordered backend ladder. Probes active backend availability before dispatch, surfaces agent-friendly fix hints on broken backends, and prefixes output with firewall provenance.

---

## 4. Element → 3O Layer → Rationale (MANDATORY Table)

| Element | File / Path | Layer | Rationale (Why this layer and not another) |
|---|---|---|---|
| `plan_observation` / `ObservationPlan` | `veya/oskill/pure/observation_ladder.py` | **oskill (pure)** | Pure decision table selecting between observation tiers (`a11y -> trimmed_html -> dom_text -> dom_html -> screenshot`) based on capability set and feature flag. Stateless, zero IO. |
| `html_to_markdown` / `read_html` / `is_ssrf_safe_url` | `veya/oskill/pure/html_to_markdown.py` | **oskill (pure)** | Deterministic AST-free HTML tokenizer transforming DOM to structured Markdown with `[#n]` control indexing, sensitive data masking, SSRF validation, and safety limits. Stateless, zero IO. |
| `plan_search` / `merge_search_results` / `format_search_results` | `veya/oskill/pure/search_ladder.py` | **oskill (pure)** | Deterministic search fallback ordering and result deduplication logic. Stateless, no network requests, zero IO. |
| `plan_web_channel_backend` / `reorder_channel_backends` | `veya/oskill/pure/channel_ladder.py` | **oskill (pure)** | Pure ladder configuration table and reordering functions defining backend ladders per web channel. Stateless, zero IO. |
| `full_6step_tool_ladder` / `FULL_TOOL_LADDER_ORDER` | `veya/oskill/cli_hub.py` | **oskill** | Pure algorithmic ranking of the 6-step tool ladder (`context -> connector/MCP/API -> web search -> browser -> desktop -> ask human`) given available tool sources. |
| `capture_browser_a11y` / `capture_desktop_a11y` / `format_a11y_tree` | `veya/obase/a11y.py` | **obase** | External adapter extracting accessibility trees from browser Playwright instances and Linux AT-SPI desktop subsystems. |
| `run_search_ladder_adapter` | `veya/obase/search_adapters.py` | **obase** | External adapter executing search queries against configured providers, model native endpoints, and public web backends. |
| `probe_channel` / `probe_backend` / `probe_all_channels` / `fetch_url_text` | `veya/obase/channel_probes.py` | **obase** | External adapter executing 5s timeout probes and network calls for web channels, returning agent-friendly fix hints. |
| `tool_read_browser` | `server/dom_reader_tools.py` | **server/tools** | Service-level tool wrapper exposing DOM reading to `MasterToolRegistry` under `VEYA_DOM_READER` flag. |
| `tool_read_channel` | `server/channel_tools.py` | **server/tools** | Service-level tool wrapper exposing channel reading to `MasterToolRegistry` under `VEYA_CHANNEL_LADDER` flag. |
| `GET /api/v1/channels/status` | `server/routes/channels.py` | **server/routes** | REST endpoint verifying caller authentication and attribution, returning channel probe statuses. |
| `veya doctor channels` | `cli/product.py` | **cli** | CLI diagnostic command running channel probes and printing formatted tables or JSON output. |
| Web Reading Channels Section | `apps/web/src/routes/settings/channels/+page.svelte` | **apps/web (UI)** | SvelteKit UI presenting web reading channels status, active backends, and fix hints. |

---

## 5. Third-Party Notices & Clean-Room Verification

All borrowed designs are cleanly attributed in `THIRD_PARTY_NOTICES`:
- **Panniantong/Agent-Reach** (MIT): Channel ladder design for multi-platform web reading, backend probing, and ordered fallback chains.
- **FeiZhuLulu/DeepSeek-Bot** (Apache-2.0): DOM reader design with `[#n]` interactive element indexing and sensitive field redaction.

No upstream code was vendored. All implementations were authored clean-room adhering strictly to Python standard libraries and Veya 3O contracts.

---

## 6. Git Commits in Group D (`feat/nano-w10-web`)

- `c310563b`: `feat(nano-w10d): a11y-first observation ladder (item 6)`
- `40ea39a8`: `feat(nano-w10d): DOM reader read_browser (ADDENDUM R)`
- `294b5611`: `fix(nano-w10d): pure oskill and reverse dep compliance for DOM reader`
- `f138b2f7`: `feat(nano-w10d): search fallback ladder (ADDENDUM T)`
- `a188c4bd`: `feat(nano-w10d): web reading channel ladder and probes (ADDENDUM V)`
- `4f77cc66`: `feat(nano-w10d): 6-step tool ladder expansion (主体 7c)`
- `c9f237fd`: `fix(nano-w10d): delegate tool network IO to obase channel_probes`
- `78f85343`: `style(nano-w10d): ruff cleanups and import consolidation`
