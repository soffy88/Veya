/**
 * BFF stream proxy — POST /api/stream
 *
 * Proxies the web client's SSE request to the Veya Layer 4 FastAPI gateway
 * (`/api/v1/agent/stream`) and pipes the `text/event-stream` response back
 * verbatim.  Same-origin on the client side → no CORS, stable SSE under
 * production hosting, and the gateway address stays server-side only.
 *
 * Upstream override: `VEYA_GATEWAY` (default http://127.0.0.1:8765).
 */
import { error } from "@sveltejs/kit";
import type { RequestHandler } from "./$types";

const UPSTREAM = (process.env.VEYA_GATEWAY ?? "http://127.0.0.1:8765").replace(/\/+$/, "");

export const POST: RequestHandler = async ({ request }) => {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    error(400, "request body must be JSON");
  }

  const upstream = await fetch(`${UPSTREAM}/api/v1/agent/stream`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "text/event-stream",
    },
    body: JSON.stringify(body),
  });

  if (!upstream.ok || !upstream.body) {
    error(502, `veya gateway upstream error: HTTP ${upstream.status}`);
  }

  // Pipe the upstream SSE stream through unchanged.
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-cache",
      "x-accel-buffering": "no",
    },
  });
};
