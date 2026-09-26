/**
 * Live mission events: SSE primary, JSON polling fallback.
 *
 * The backend endpoint is read-only and authenticated with the user's Veya
 * token, so we stream with `fetch` (EventSource cannot send an Authorization
 * header). Reconnects resume from the last seen index, so reconnecting never
 * duplicates events, and simply subscribing can never start an execution.
 */

import { API_BASE } from "../api";
import { eventsStreamPath, listEvents } from "./api";
import type { MissionEvent } from "./types";

export interface EventStreamHandlers {
  onEvent: (event: MissionEvent, index: number) => void;
  onError?: (message: string) => void;
  onOpen?: () => void;
}

export interface EventStreamOptions {
  /** Poll interval for the fallback path (ms). */
  pollMs?: number;
  /** Force the polling path (used by tests / restricted environments). */
  preferPolling?: boolean;
  signal?: AbortSignal;
}

function token(): string | null {
  return typeof localStorage !== "undefined" ? localStorage.getItem("veya.auth.token") : null;
}

/** Parse one SSE chunk buffer into complete frames, returning the leftover. */
export function parseSseBuffer(buffer: string): { frames: string[]; rest: string } {
  const frames: string[] = [];
  let rest = buffer;
  let idx = rest.indexOf("\n\n");
  while (idx !== -1) {
    frames.push(rest.slice(0, idx));
    rest = rest.slice(idx + 2);
    idx = rest.indexOf("\n\n");
  }
  return { frames, rest };
}

/** Extract the JSON payload of an SSE frame (ignores comments/keep-alives). */
export function frameData(frame: string): { id: number | null; payload: MissionEvent | null } {
  let id: number | null = null;
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("id:")) {
      const parsed = Number.parseInt(line.slice(3).trim(), 10);
      id = Number.isNaN(parsed) ? null : parsed;
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim());
    }
  }
  if (!dataLines.length) return { id, payload: null };
  try {
    return { id, payload: JSON.parse(dataLines.join("\n")) as MissionEvent };
  } catch {
    return { id, payload: null };
  }
}

/**
 * Subscribe to a mission's canonical event log.
 *
 * Returns a disposer. `startIndex` is the number of events already applied by
 * the caller, so reconnect/reload never replays or duplicates them.
 */
export function subscribeMissionEvents(
  missionId: string,
  startIndex: number,
  handlers: EventStreamHandlers,
  options: EventStreamOptions = {},
): () => void {
  const controller = new AbortController();
  const signal = options.signal ?? controller.signal;
  let seen = Math.max(0, Math.floor(startIndex));
  let closed = false;

  const emit = (payload: MissionEvent | null, index: number | null) => {
    if (!payload) return;
    const at = index ?? seen;
    if (index !== null) {
      if (index < seen) return; // duplicate / replay guard
      seen = index + 1;
    } else {
      seen += 1;
    }
    handlers.onEvent(payload, at);
  };

  const poll = async () => {
    while (!closed && !signal.aborted) {
      const res = await listEvents(missionId);
      if (!res.ok) {
        handlers.onError?.(res.error ?? `HTTP ${res.status}`);
      } else {
        const events = res.data?.events ?? [];
        for (let i = seen; i < events.length; i += 1) emit(events[i], i);
      }
      await new Promise((resolve) => setTimeout(resolve, options.pollMs ?? 2000));
    }
  };

  const stream = async () => {
    const headers: Record<string, string> = { accept: "text/event-stream" };
    const t = token();
    if (t) headers.authorization = `Bearer ${t}`;
    const res = await fetch(`${API_BASE}/${eventsStreamPath(missionId, seen)}`, {
      method: "GET",
      headers,
      signal,
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    handlers.onOpen?.();
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const { frames, rest } = parseSseBuffer(buffer);
      buffer = rest;
      for (const frame of frames) {
        const { id, payload } = frameData(frame);
        emit(payload, id);
      }
    }
  };

  const run = async () => {
    if (options.preferPolling) {
      await poll();
      return;
    }
    try {
      await stream();
      if (!closed && !signal.aborted) await poll(); // stream ended: keep watching
    } catch (exc) {
      if (closed || signal.aborted) return;
      handlers.onError?.(exc instanceof Error ? exc.message : String(exc));
      await poll();
    }
  };

  void run();

  return () => {
    closed = true;
    controller.abort();
  };
}
