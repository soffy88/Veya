/**
 * Veya Layer 4 SSE client.
 *
 * The gateway exposes a POST-based Server-Sent Events endpoint, so we can't
 * use the browser `EventSource` (GET-only).  This client speaks the wire
 * format directly over `fetch` + `ReadableStream`:
 *
 *   data: {"event":"session","session_id":"..."}\n\n
 *   data: {"event":"step","step":{...},"cost":0.0}\n\n
 *   data: {"event":"session_done","status":"completed","cost":0.0}\n\n
 *   data: [DONE]\n\n
 */

export interface SseEvent {
  event: string;
  session_id?: string;
  status?: string;
  cost?: number;
  ts?: number;
  error?: string;
  step?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface StreamOptions {
  /** Base gateway URL (Layer 4 backend, default http://127.0.0.1:8765). */
  endpoint?: string;
  /** Reuse an existing session (memory restore) — optional. */
  sessionId?: string;
  mode?: "run" | "dry_run";
  /** Per-event callback; `{ event: "done" }` is emitted on the [DONE] frame. */
  onEvent?: (ev: SseEvent) => void;
  /** External cancellation handle. */
  signal?: AbortSignal;
}

export const DEFAULT_ENDPOINT = "http://127.0.0.1:8765/api/v1/agent/stream";

/** Extract the gateway base URL from the SSE endpoint for JSON-mode calls. */
export function gatewayBase(): string {
  const ep = effectiveEndpoint();
  const idx = ep.lastIndexOf("/api/v1/agent/stream");
  return idx >= 0 ? ep.slice(0, idx) : "http://127.0.0.1:8765";
}

/**
 * Effective gateway URL.
 *
 * Production contract: ``http://127.0.0.1:8765/api/v1/agent/stream``.
 * Override per-environment via ``VITE_VEYA_ENDPOINT`` (e.g. when another
 * service occupies the default port on a dev box).
 */
export function effectiveEndpoint(): string {
  const override = import.meta.env.VITE_VEYA_ENDPOINT as string | undefined;
  return override?.trim() || DEFAULT_ENDPOINT;
}

/** POST one agent task and parse the SSE stream until [DONE] or abort. */
export async function streamAgentRun(
  task: string,
  opts: StreamOptions = {},
): Promise<void> {
  const endpoint = opts.endpoint ?? effectiveEndpoint();
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  opts.signal?.addEventListener("abort", onAbort, { once: true });

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "text/event-stream",
      },
      body: JSON.stringify({
        task,
        session_id: opts.sessionId,
        mode: opts.mode ?? "run",
      }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      throw new Error(`gateway HTTP ${res.status} ${res.statusText}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let done = false;

    for (;;) {
      const { done: streamEnd, value } = await reader.read();
      if (streamEnd) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        if (frame.trim() === "") continue;
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data:")) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") {
            done = true;
            opts.onEvent?.({ event: "done" });
            break;
          }
          try {
            opts.onEvent?.(JSON.parse(data) as SseEvent);
          } catch {
            // skip malformed frames — never let one bad frame kill the stream
          }
        }
        if (done) return;
      }
    }

    // Flush any trailing partial frame (server closed cleanly without [DONE]).
    if (buffer.trim() !== "") {
      const line = buffer.trim().split("\n").at(-1) ?? "";
      if (line.startsWith("data:")) {
        const data = line.slice(5).trim();
        if (data !== "[DONE]") {
          try {
            opts.onEvent?.(JSON.parse(data) as SseEvent);
          } catch {
            /* ignore */
          }
        }
      }
    }
  } finally {
    opts.signal?.removeEventListener("abort", onAbort);
  }
}
