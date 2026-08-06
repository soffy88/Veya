/**
 * Same-origin /api/v1/* forwarder (dev mode).
 *
 * In production, Caddy intercepts /api/* and reverse-proxies straight to the
 * L4 gateway, so this route never fires. In dev (no Caddy) it forwards to
 * VEYA_GATEWAY (default http://127.0.0.1:8765) so the frontend works standalone.
 */
import { error } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";
import { gatewayGuideResponse, probeGateway } from "$lib/upstreamProbe";

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
  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch (e) {
    // 连接失败 (服务未启动 / 端口错误) → 直接给引导, 不透明 500
    return gatewayGuideResponse(
      BASE,
      e instanceof Error ? e.message : String(e),
    );
  }
  // 上游 404/502: 探活确认目标是不是 veya —— 不是则替换为引导错误
  if (upstream.status === 404 || upstream.status === 502) {
    const probe = await probeGateway(BASE);
    if (!probe.ok) {
      return gatewayGuideResponse(BASE, probe.detail);
    }
  }
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
