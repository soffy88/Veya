# ORCA-W5 Design Mode — Architecture & Implementation Plan

## Overview
Design Mode lets a user click an element in the workspace browser panel, capture its full description (HTML, styles, bounding box, screenshot, source mapping), and send it to the agent as a chat attachment with an optional instruction.

---

## 1. Browser Panel — Design Mode Toggle

**Location**: Workspace browser panel (right sidebar in `/workbench/[task_id]`)

**UI**:
- Toggle button "Design Mode" (cyan accent when active)
- When ON:
  - Hovering any element in the browser viewport shows a highlight overlay (2px cyan outline + semi-transparent fill)
  - Click captures the element → opens a preview drawer with captured data + "Send to agent" button + instruction textarea
  - Clicking outside or pressing Escape cancels capture mode

**State**: Local to the browser panel component; persists across task reloads via `sessionStore`.

---

## 2. Capture Payload (client → server)

```typescript
interface DesignCapture {
  // Element identification
  outerHTML: string;              // trimmed to ~8KB
  selectorPath: string;           // e.g. "html > body > div.container > button#submit"
  // Computed styles (subset)
  computedStyles: {
    boxModel: { margin: string; border: string; padding: string; width: string; height: string };
    display: string;              // block, flex, grid, inline-flex, etc.
    flex?: { direction: string; justify: string; align: string; gap: string };
    grid?: { templateColumns: string; templateRows: string; gap: string };
    font: { family: string; size: string; weight: string; lineHeight: string };
    color: { color: string; backgroundColor: string };
    position: { position: string; top: string; right: string; bottom: string; left: string; zIndex: string };
  };
  // Geometry
  boundingBox: { x: number; y: number; width: number; height: number };
  // Visual
  elementScreenshotBase64: string; // cropped to element bounds, JPEG ~quality 0.7
  // Page context
  pageUrl: string;
  pageTitle: string;
  // Source mapping (dev-only)
  sourceLocation?: SourceLocation;
}

interface SourceLocation {
  type: "veya" | "react" | "generic" | "unknown";
  file: string;       // e.g. "src/lib/components/Button.svelte"
  line: number;       // 1-indexed
  column?: number;
  rawAttribute?: string; // original data-veya-src / data-source value
}
```

---

## 3. Source Location Resolution

**Priority order**:
1. `data-veya-src="file:line"` — injected by Vite plugin (dev builds only)
2. `data-source="file:line"` — generic convention
3. `element.__source` (React DevTools) — `{ fileName, lineNumber }`
4. Fallback: `"unknown"`

**Vite Plugin** (`apps/web/vite.config.ts`):
- Dev only (`!isProduction`)
- Transforms Svelte components: adds `data-veya-src="relative/path:line"` to each element's opening tag
- Uses Svelte's `compileOptions.dev` metadata (filename + line)

**Cross-origin limit**: Iframe capture only works for same-origin pages. Cross-origin pages return `"unknown"` source location and no DOM access (browser security). Documented in capture response.

---

## 4. Server Endpoint: `POST /api/v1/design/capture`

**Request**:
```json
{
  "capture": DesignCapture,
  "instruction": "optional user instruction string",
  "session_id": "current chat session id",
  "task_id": "current workbench task id (optional)"
}
```

**Auth**: Standard `Authorization: Bearer <token>` (same as `/api/v1/agent/stream`).

**Processing**:
1. Validate capture payload (trim outerHTML to 8KB, validate base64 screenshot, ensure required fields)
2. Store capture as an artifact file: `.veya/runs/<task_id>/design_captures/<uuid>.json`
3. Compose a user message:
   ```
   [Design Mode Capture]
   Page: <pageTitle> (<pageUrl>)
   Selector: <selectorPath>
   Source: <sourceLocation.file>:<sourceLocation.line> (or "unknown")
   
   <outerHTML>
   
   User instruction: <instruction or "(none)">
   ```
4. Append message to the session via `history_store.save(session_id, [...messages, new_message])`
5. Fire canonical event `design.capture_sent` with metadata
6. Return `{ status: "sent", artifact_path: "...", message_id: "..." }`

**No new model tool** — uses existing `history_store` + event system. MasterAgent sees the message on next turn.

---

## 5. Browser Integration — Two Paths

### A. 3O Server-Side Browser (Playwright)
- Existing `browser_computer_adapter.py` → `oprim.browser_screenshot` + `oprim.browser_snapshot`
- New action: `browser_design_capture` (selector → returns outerHTML, computed styles, bounding box, cropped screenshot)
- Implemented in `obase.PlaywrightBrowserAdapter` + exposed via `oprim`

### B. Same-Origin Iframe (Dev Server)
- Lightweight DOM capture script injected into the iframe
- Communicates via `postMessage` to parent panel
- No server round-trip for capture; only for "Send to agent"
- Script: `apps/web/src/lib/designModeCapture.ts` (loaded dynamically when Design Mode enabled)

---

## 6. Files to Create / Modify

| File | Purpose |
|------|---------|
| `server/routes/design.py` | New route: `POST /api/v1/design/capture` |
| `server/app.py` | Include `design_router` |
| `platform/3O/oprim/oprim/browser_design_capture.py` | New 3O atomic: server-side element capture |
| `platform/3O/obase/obase/browser.py` | Add `design_capture` to `BrowserAdapter` protocol + `PlaywrightBrowserAdapter` |
| `server/browser_computer_adapter.py` | Wire new action into `BrowserComputerAdapter._browser_actions` |
| `apps/web/vite.config.ts` | Add Vite plugin for `data-veya-src` (dev only) |
| `apps/web/src/lib/designModeCapture.ts` | Iframe DOM capture script (same-origin) |
| `apps/web/src/lib/components/BrowserPanel.svelte` | New component: browser viewport + Design Mode UI |
| `apps/web/src/routes/workbench/[task_id]/+page.svelte` | Integrate `BrowserPanel` |
| `tests/test_orca_w5_design_mode.py` | Unit/integration tests |

---

## 7. Testing Checklist

| Test | Description |
|------|-------------|
| `test_capture_payload_normalization` | outerHTML trimmed to 8KB, base64 valid, required fields present |
| `test_source_resolution_veya_attr` | `data-veya-src` parsed correctly |
| `test_source_resolution_react_source` | `element.__source` fallback works |
| `test_source_resolution_unknown` | Cross-origin / missing attrs → `"unknown"` |
| `test_message_composition` | User message formatted correctly with all capture fields |
| `test_endpoint_auth` | 401 without token, 200 with valid token |
| `test_vite_plugin_stamps_elements` | Dev build HTML contains `data-veya-src` |
| `test_svelte_check` | `pnpm run check` passes |
| `test_build` | `pnpm run build` passes |

---

## 8. Security & Limits

- outerHTML trimmed to 8KB (prevents huge payloads)
- Screenshot cropped to element bounds, JPEG quality 0.7, max ~200KB base64
- Only same-origin iframe capture allowed (browser-enforced)
- Artifact stored in task-scoped directory (`.veya/runs/<task_id>/design_captures/`)
- No new model-visible tool; capture enters as a regular user message

---

## 9. Known Gaps / Future Work

- Cross-origin pages: capture limited to screenshot + URL (no DOM/styles/source)
- No persistent Design Mode across browser sessions (localStorage only)
- Element picker could support keyboard navigation (Tab/Enter)
- Large pages: capture script should debounce highlight renders

---

## 10. Implementation Order

1. **Design doc** ✓ (this file)
2. Server route + artifact storage + message composition
3. 3O browser atomic (`browser_design_capture`) + Playwright adapter
4. BrowserComputerAdapter wiring
5. Vite plugin for `data-veya-src`
6. Iframe capture script (`designModeCapture.ts`)
7. `BrowserPanel.svelte` with Design Mode toggle, highlight, click capture
8. Workbench page integration
9. Tests
10. `svelte-check` + `build`
11. Regression suite
12. `ORCA_W5_REPORT.md`