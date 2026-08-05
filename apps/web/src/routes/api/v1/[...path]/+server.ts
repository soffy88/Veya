/**
 * Same-origin /api/v1/* forwarder (dev mode).
 *
 * In production, Caddy intercepts /api/* and reverse-proxies straight to the
 * L4 gateway, so this route never fires. In dev (no Caddy) it forwards to
 * VEYA_GATEWAY (default http://127.0.0.1:8765) so the frontend works standalone.
 */
import { error } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";

const BASE = (process.env.VEYA_GATEWAY ?? "http://127.0.0.1:8765").replace(/\/+$/, "");

async function forward(event: Parameters<RequestHandler>[0]): Promise<Response> {
  const path = event.params.path ?? "";
  const target = `${BASE}/api/v1/${path}${event.url.search}`;
  const init: RequestInit = { method: event.request.method, headers: {} };
  if (event.request.method !== "GET" && event.request.method !== "HEAD") {
    init.body = await event.request.text();
    const ct = event.request.headers.get("content-type");
    if (ct) (init.headers as Record<string, string>)["content-type"] = ct;
  }
  const upstream = await fetch(target, init);
  const text = await upstream.text();
  const headers: Record<string, string> = {
    "content-type": upstream.headers.get("content-type") ?? "application/json",
  };
  return new Response(text, { status: upstream.status, headers });
}

export const GET: RequestHandler = forward;
export const POST: RequestHandler = forward;
export const PUT: RequestHandler = forward;
export const DELETE: RequestHandler = forward;
