# NANO-W10 Group D: Web Observation & Reading Design

This document details the architectural design and implementation plan for Group D:
Web Observation and Reading, conforming to the HARD RULES and 3O paradigm specifications
in `NANO_W10_SPEC.md` and `W10_GROUP_D.md`.

---

## 1. Scope & Capabilities

Group D covers five distinct items:

1. **主体 6: a11y-first 观察阶梯 (`VEYA_A11Y_FIRST`)**
   - Observation order for computer use / browser automation:
     `accessibility tree (a11y snapshot) -> trimmed HTML -> screenshot/pixels`
   - Pure ladder selection logic in `oskill` (`veya/oskill/pure/observation_ladder.py`).
   - Adapters in `obase` for browser (Playwright accessibility snapshot) and desktop runner (AT-SPI on Linux).
   - Preserves legacy screenshot-first order when flag is OFF.

2. **ADDENDUM R: DOM Reader `read_browser` (`VEYA_DOM_READER`)**
   - DOM-to-Markdown structured converter in `oskill`:
     Converts headings, lists, tables, code blocks, blockquotes.
   - Marks links and interactive controls inline as `[#n]` with an indexed reference table below.
   - Skips hidden content (`display:none`, `visibility:hidden`, `hidden`, `aria-hidden="true"`).
   - Redacts sensitive field values (`password`, `autocomplete=cc-*`, `autocomplete=one-time-code`).
   - Enforces strict safety limits: 400k chars, 150k nodes, 5k table rows, 3k controls, 1.5s timeout.
   - SSRF protection: blocks cloud metadata endpoints (169.254.169.254, metadata.google.internal) and Veya local ports (8765, 8767, 9120, 3105).
   - Firewall provenance: marks fetched web content as external data, not instructions.
   - Model-callable tool: `read_browser`.

3. **ADDENDUM T: Search Fallback Ladder**
   - Deterministic search order:
     `configured provider -> current model route native search -> keyless public result pages (DuckDuckGo, Bing) -> fail with instruction not to guess URLs`
   - Every search result prepends a `Search backend: <backend_name>` line.
   - Deduplication by normalized URL.
   - Pure ladder logic in `oskill` (`veya/oskill/pure/search_ladder.py`).
   - Provider/backend adapters in `obase`.

4. **ADDENDUM V: Reading Web Channels Ladder (`VEYA_CHANNEL_LADDER`)**
   - Ordered backend ladders for reading external sources:
     - `web`: `jina -> dom_reader -> http_fetch`
     - `youtube`: `yt_dlp -> browser`
     - `rss`: `feedparser -> http_fetch`
     - `github`: `gh_cli -> api`
     - `bilibili`: `cli_bilibili -> browser`
     - `reddit`: `cli_reddit -> browser`
     - `twitter`: `cli_twitter -> browser`
     - `xiaohongshu`: `cli_xiaohongshu -> browser`
     - `linkedin`: `cli_linkedin -> browser`
   - Real backend probing in `obase` (`channel_probes`):
     Executes small known-good calls (timeout 5s); reports status (`ok`, `broken`, `needs-login`) and agent-friendly fix hints.
   - CLI: `veya doctor channels [--json]`.
   - REST API: `GET /api/v1/channels/status` (authenticated caller validation).
   - Web UI: Channels status section on Settings page.
   - Login-based backends default to OFF until enabled via approval flow.

5. **主体 7c: 6-Step Tool Ladder Expansion**
   - Expands the W8 4-step tool ladder (`api -> mcp -> cli -> hands`) to the full 6 steps:
     1. `context` (in-context memory, previous turn artifacts)
     2. `connector/MCP/API` (structured programmatic interface)
     3. `web search` (T search fallback ladder)
     4. `browser` (read_browser DOM reader / Playwright headless)
     5. `desktop` (hands / GUI automation)
     6. `ask human` (human escalation / clarification question)
   - Reflected in `docs/NANO_W8_DESIGN.md` and `veya/oskill/cli_hub.py`.

---

## 2. Element → 3O Layer → Rationale

| Element | Layer | Rationale |
|---------|-------|-----------|
| `observation_ladder` | `oskill` | Pure ranking logic deciding between a11y, HTML, and screenshot observation tiers based on available capabilities and feature flag. Stateless, zero IO. |
| `html_to_markdown` / `dom_reader` | `oskill` | Pure structural conversion of HTML DOM to Markdown with `[#n]` control indexing, sensitive data redaction, limits enforcement, and provenance annotation. Stateless, zero IO. |
| `search_ladder` | `oskill` | Pure ranking and deduplication logic ordering configured providers, model native search, and public keyless search. Stateless, zero IO. |
| `channel_ladder` | `oskill` | Pure configuration table and reordering functions defining backend ladders per web channel (web, youtube, rss, github, etc.). Stateless, zero IO. |
| `a11y_browser_probe` | `oprim` | Atomic operation extracting accessibility tree snapshot from Playwright page. |
| `a11y_desktop_probe` | `oprim` | Atomic operation extracting AT-SPI accessibility tree on Linux desktop runner. |
| `search_duckduckgo` | `oprim` | Atomic HTTP request to keyless public search backend. |
| `search_bing` | `oprim` | Atomic HTTP request to keyless public search backend. |
| `channel_backend_probe` | `oprim` | Atomic probe executing a 5s probe against a specific channel backend. |
| `a11y_adapter` | `obase` | External adapter managing browser and desktop accessibility tree extraction interfaces. |
| `search_adapters` | `obase` | External adapters encapsulating provider search, native search, and keyless scraping. |
| `channel_probes` | `obase` | Probe orchestrator collecting probe results with timeouts and agent-friendly fix hints. |
| `read_browser` | `server/tools` | Model-callable tool registered in tool registry exposing DOM reading to agents. |
| `read_channel` | `server/tools` | Model-callable tool registered in tool registry exposing channel-based reading to agents. |
| `GET /api/v1/channels/status` | `server/routes` | Thin REST route with authentication and caller validation exposing channel probe status. |
| `veya doctor channels` | `cli` | CLI diagnostic command running channel probes and printing formatted table / JSON. |

---

## 3. Feature Flags

All new capabilities are gated by feature flags in `server/feature_flags.py`:

- `VEYA_A11Y_FIRST` (default `False`): Enables a11y-first observation ladder over screenshot-first.
- `VEYA_DOM_READER` (default `False`): Enables DOM-to-Markdown `read_browser` tool.
- `VEYA_CHANNEL_LADDER` (default `False`): Enables web channel ladder execution (probes and read-only status remain available).

---

## 4. Third-Party Attributions

Attributions appended to `THIRD_PARTY_NOTICES`:
- **Panniantong/Agent-Reach** (MIT): Channel ladder design for reading the web across platforms.
- **FeiZhuLulu/DeepSeek-Bot** (Apache-2.0): DOM reader design with `[#n]` interactive element indexing.
