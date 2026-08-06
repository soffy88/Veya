/**
 * Client API helper — same-origin calls through Caddy / SvelteKit.
 *
 *   api("gateway", "api/v1/agent/verify", { body: { statement } })   → POST /api/v1/agent/verify
 *   api("legacy", "tools/sandbox/execute", { body: { command } })    → POST /legacy/tools/sandbox/execute
 *
 * Production (Caddy): /api/* is reverse-proxied to the L4 gateway (8765);
 * /legacy/* is forwarded by the SvelteKit Node server to the legacy backend.
 * Dev (no Caddy): both are forwarded by SvelteKit server routes using the
 * VEYA_GATEWAY / VEYA_LEGACY env vars.
 */

export type Upstream = "gateway" | "legacy";

/**
 * API 基址: 同源模式为空串; 桌面静态版/独立部署用构建时注入的绝对基址
 * (VITE_VEYA_ENDPOINT, 例如 http://127.0.0.1:8767)。
 */
export const API_BASE: string = (import.meta.env.VITE_VEYA_ENDPOINT as string | undefined) ?? "";

export interface ApiResult {
  ok: boolean;
  status: number;
  data: unknown;
}

export interface ApiOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined>;
}

export async function api(
  upstream: Upstream,
  path: string,
  opts: ApiOptions = {},
): Promise<ApiResult> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v !== undefined && v !== "") q.set(k, String(v));
  }
  const qs = q.size ? `?${q.toString()}` : "";
  const prefix = upstream === "legacy" ? `${API_BASE}/legacy/` : `${API_BASE}/`;
  const res = await fetch(`${prefix}${path.replace(/^\/+/, "")}${qs}`, {
    method: opts.method ?? "POST",
    headers: opts.body !== undefined ? { "content-type": "application/json" } : undefined,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  const text = await res.text();
  let data: unknown = text;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      /* keep raw text */
    }
  }
  return { ok: res.ok, status: res.status, data };
}

/** Format an API result for display (pretty JSON, or raw text fallback). */
export function formatResult(data: unknown): string {
  if (typeof data === "string") return data;
  try {
    return JSON.stringify(data, null, 2);
  } catch {
    return String(data);
  }
}
