/**
 * Same-origin /legacy/* forwarder — legacy FastAPI server capabilities
 * (sandbox, AST analysis, semantic search, multimodal, integrations, …).
 *
 * Works in dev AND production: Caddy sends non-/api/* traffic to the
 * SvelteKit Node server, which forwards /legacy/* to VEYA_LEGACY
 * (default http://127.0.0.1:8010).
 */
import { error } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";

const BASE = (process.env.VEYA_LEGACY ?? "http://127.0.0.1:8010").replace(/\/+$/, "");

async function forward(event: Parameters<RequestHandler>[0]): Promise<Response> {
  const path = event.params.path ?? "";
  const target = `${BASE}/${path}${event.url.search}`;
  const init: RequestInit = { method: event.request.method, headers: {} };
  if (event.request.method !== "GET" && event.request.method !== "HEAD") {
    init.body = await event.request.text();
    const ct = event.request.headers.get("content-type");
    if (ct) (init.headers as Record<string, string>)["content-type"] = ct;
  }
  const upstream = await fetch(target, init);
  const contentType = upstream.headers.get("content-type") ?? "application/json";

  // SSE (e.g. GET /stream/{session_id}) must be piped live, not buffered —
  // coordinator.handle() fires steps into the queue as it runs.
  if (contentType.startsWith("text/event-stream") && upstream.body) {
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache",
        "x-accel-buffering": "no",
      },
    });
  }

  const text = await upstream.text();
  return new Response(text, { status: upstream.status, headers: { "content-type": contentType } });
}

export const GET: RequestHandler = forward;
export const POST: RequestHandler = forward;
export const PUT: RequestHandler = forward;
export const DELETE: RequestHandler = forward;
