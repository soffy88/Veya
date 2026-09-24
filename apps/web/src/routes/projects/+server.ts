import type { RequestHandler } from "./$types";

const BASE = (process.env.VEYA_GATEWAY ?? "http://127.0.0.1:8765").replace(/\/+$/, "");

async function forward(event: Parameters<RequestHandler>[0]): Promise<Response> {
  const target = `${BASE}/projects${event.url.search}`;
  const headers: Record<string, string> = {};
  const authz = event.request.headers.get("authorization");
  if (authz) headers.authorization = authz;
  const cookie = event.request.headers.get("cookie");
  if (cookie) headers.cookie = cookie;

  const init: RequestInit = { method: event.request.method, headers };
  if (event.request.method !== "GET" && event.request.method !== "HEAD") {
    const contentType = event.request.headers.get("content-type") ?? "";
    if (contentType) headers["content-type"] = contentType;
    init.body = await event.request.text();
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return new Response(JSON.stringify({ detail: "Veya project service unavailable", upstream: BASE, error: detail }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }

  const contentType = upstream.headers.get("content-type") ?? "application/json";
  return new Response(await upstream.text(), {
    status: upstream.status,
    headers: { "content-type": contentType },
  });
}

export const GET: RequestHandler = forward;
export const POST: RequestHandler = forward;
