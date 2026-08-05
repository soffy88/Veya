/**
 * @veya/ui — Veya Layer 4 shared UI kit.
 *
 * 100% code reuse across desktop (Tauri) and web (SvelteKit):
 *   - `DecisionTrailView` — Svelte 5 Runes live viewer of the SSE decision trail
 *   - `streamAgentRun`    — POST-SSE client (fetch + ReadableStream)
 *   - `effectiveEndpoint` / `DEFAULT_ENDPOINT` — gateway URL resolution
 *   - `theme.css`         — Tailwind v4 dark terminal theme (import via "@veya/ui/theme.css")
 */

export { default as DecisionTrailView } from "./DecisionTrailView.svelte";
export { default } from "./DecisionTrailView.svelte";
export {
  DEFAULT_ENDPOINT,
  effectiveEndpoint,
  streamAgentRun,
} from "./stream.js";
export type { SseEvent, StreamOptions } from "./stream.js";
export { gatewayBase } from "./stream.js";
