<script lang="ts">
  /**
   * DecisionTrailView — Svelte 5 (Runes) live viewer for the Veya Layer 4
   * decision trail, consumed over SSE from the gateway:
   *
   *   POST http://127.0.0.1:8765/api/v1/agent/stream
   *
   * Reactive state is expressed exclusively with runes:
   *   - `$state`   — frames, session id, connection status
   *   - `$derived` — cost totals / per-event counts
   *   - `$effect`  — stream lifecycle + auto-scroll
   *
   * Parent drives it declaratively: flip `running` to `true` with a `task`
   * and the trail streams in; flipping it back aborts the connection.
   */
  import {
    Brain,
    CheckCircle2,
    CircleAlert,
    Cpu,
    Loader2,
    Radio,
    Terminal,
    Wrench,
  } from "lucide-svelte";
  import { streamAgentRun, effectiveEndpoint, type SseEvent } from "./stream.js";

  interface Props {
    task: string;
    /** Bindable — set true to start, false to abort. */
    running?: boolean;
    mode?: "run" | "dry_run";
    endpoint?: string;
    /** Fired whenever the connection status changes (drives status badges). */
    onStatusChange?: (status: ConnectionStatus) => void;
    /** Fired when the stream terminates (session_done or error). */
    onSessionDone?: (info: { status: string; cost: number; sessionId?: string }) => void;
  }

  let {
    task,
    running = $bindable(false),
    mode = "run",
    endpoint = effectiveEndpoint(),
    onStatusChange,
    onSessionDone,
  }: Props = $props();

  type ConnectionStatus = "idle" | "connecting" | "streaming" | "done" | "error";

  // --- runes: reactive state ------------------------------------------------
  let steps = $state<SseEvent[]>([]);
  let status = $state<ConnectionStatus>("idle");
  let sessionId = $state<string>("");
  let listEl = $state<HTMLDivElement | undefined>();
  let _seq = 0;

  // --- runes: derived views -------------------------------------------------
  const totalCost = $derived(
    steps.reduce((acc, s) => acc + (typeof s.cost === "number" ? s.cost : 0), 0),
  );
  const stepCount = $derived(steps.filter((s) => s.event === "step").length);
  const toolCount = $derived(
    steps.filter((s) => s.event === "tool_call" || s.event === "tool_result").length,
  );

  // --- runes: stream lifecycle ----------------------------------------------
  $effect(() => {
    if (!running || !task.trim()) return;
    setStatus("connecting");
    const ac = new AbortController();

    streamAgentRun(task.trim(), {
      endpoint,
      mode,
      signal: ac.signal,
      onEvent: (ev) => {
        if (ev.event === "session") {
          sessionId = String(ev.session_id ?? "");
          setStatus("streaming");
          steps = [...steps, { ...ev, _id: ++_seq }];
        } else if (ev.event === "done") {
          setStatus("done");
          // control frame — not part of the decision trail
        } else if (ev.event === "error") {
          setStatus("error");
          steps = [...steps, { ...ev, _id: ++_seq }];
        } else {
          steps = [...steps, { ...ev, _id: ++_seq }];
        }
      },
    })
      .catch((err: unknown) => {
        if (ac.signal.aborted) return;
        setStatus("error");
        steps = [...steps, { event: "error", error: String(err) }];
      })
      .finally(() => {
        if (status === "connecting" || status === "streaming") setStatus("done");
        const last = steps.at(-1);
        const doneEvent = last && (last.event === "session_done" || last.event === "error");
        if (doneEvent) {
          onSessionDone?.({
            status: last.event === "error" ? "error" : String(last.status ?? "completed"),
            cost: typeof last.cost === "number" ? last.cost : 0,
            sessionId: sessionId || undefined,
          });
        }
        running = false; // one-shot run → release the parent's run flag
      });

    return () => ac.abort(); // cleanup on re-run / unmount / running=false
  });

  // Auto-scroll the trail as frames arrive (reads steps.length to re-arm).
  $effect(() => {
    void steps.length;
    if (listEl) {
      requestAnimationFrame(() => {
        if (listEl) listEl.scrollTop = listEl.scrollHeight;
      });
    }
  });

  // --- imperative helpers (parent may call via bind:this) --------------------
  export function reset(): void {
    steps = [];
    sessionId = "";
    setStatus("idle");
  }

  function setStatus(next: ConnectionStatus): void {
    status = next;
    onStatusChange?.(next);
  }

  // --- runes: rendering helpers ----------------------------------------------
  function stepLabel(s: SseEvent): string {
    const step = s.step;
    const no = typeof step?.step_no === "number" ? ` ${step.step_no}` : "";
    const action = typeof step?.action === "string" ? ` · ${step.action}` : "";
    return `${s.event}${no}${action}`;
  }

  function stepDetail(s: SseEvent): string {
    if (typeof s.error === "string") return s.error;
    const step = s.step;
    if (!step) return "";
    const d = step.detail ?? step.data ?? step.tool ?? step.action ?? step.content;
    if (typeof d === "string") return d;
    if (d != null) return JSON.stringify(d);
    return "";
  }

  function badgeFor(s: SseEvent): { icon: typeof Brain; cls: string } {
    switch (s.event) {
      case "session":
        return { icon: Radio, cls: "text-sky-400 bg-sky-400/10 border-sky-400/30" };
      case "thinking":
        return { icon: Brain, cls: "text-violet-400 bg-violet-400/10 border-violet-400/30" };
      case "llm_call":
        return { icon: Cpu, cls: "text-fuchsia-400 bg-fuchsia-400/10 border-fuchsia-400/30" };
      case "tool_call":
      case "tool_result":
        return { icon: Wrench, cls: "text-amber-400 bg-amber-400/10 border-amber-400/30" };
      case "session_done":
        return { icon: CheckCircle2, cls: "text-emerald-400 bg-emerald-400/10 border-emerald-400/30" };
      case "error":
        return { icon: CircleAlert, cls: "text-rose-400 bg-rose-400/10 border-rose-400/30" };
      default:
        return { icon: Terminal, cls: "text-zinc-400 bg-zinc-400/10 border-zinc-400/30" };
    }
  }
</script>

<!-- ================================ template ================================ -->
<div class="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-terminal-edge bg-terminal-panel">
  <!-- header -->
  <header class="flex shrink-0 items-center justify-between border-b border-terminal-edge px-4 py-2.5">
    <div class="flex items-center gap-2 font-mono text-xs text-terminal-dim">
      <span
        class:animate-pulse={status === "connecting" || status === "streaming"}
        class="inline-block size-2 rounded-full
          {status === 'error' ? 'bg-rose-500' : status === 'done' ? 'bg-emerald-500' : status === 'streaming' ? 'bg-sky-500' : status === 'connecting' ? 'bg-amber-500' : 'bg-zinc-600'}"
      ></span>
      <span>{status}</span>
      {#if sessionId}
        <span class="text-terminal-dim/60">· session {sessionId.slice(0, 8)}</span>
      {/if}
    </div>
    <div class="flex items-center gap-3 font-mono text-xs text-terminal-dim">
      <span>{stepCount} steps</span>
      <span>{toolCount} tool calls</span>
      <span class="text-terminal-fg">cost ${totalCost.toFixed(6)}</span>
    </div>
  </header>

  <!-- trail -->
  <div
    bind:this={listEl}
    class="min-h-0 flex-1 overflow-y-auto p-3 font-mono text-[13px] leading-relaxed"
  >
    {#if steps.length === 0}
      <div class="flex h-full flex-col items-center justify-center gap-2 text-terminal-dim">
        <Terminal class="size-8 opacity-40" />
        <p class="text-sm">决策轨迹将在此实时呈现</p>
        <p class="text-xs opacity-70">POST {endpoint}</p>
      </div>
    {:else}
      <ol class="flex flex-col gap-1.5">
        {#each steps as s (s._id)}
          {@const b = badgeFor(s)}
          {@const Icon = b.icon}
          <li class="flex items-start gap-2.5 rounded-lg border border-transparent px-2 py-1.5 hover:border-terminal-edge hover:bg-white/[0.03]">
            <span class={`mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-md border ${b.cls}`}>
              <Icon class="size-3.5" />
            </span>
            <div class="min-w-0 flex-1">
              <div class="flex items-baseline justify-between gap-3">
                <span class="truncate font-semibold text-terminal-fg">{stepLabel(s)}</span>
                {#if typeof s.cost === "number" && s.cost > 0}
                  <span class="shrink-0 text-[11px] text-terminal-dim">${s.cost.toFixed(6)}</span>
                {/if}
              </div>
              {#if stepDetail(s)}
                <p class="mt-0.5 break-words whitespace-pre-wrap text-terminal-dim">{stepDetail(s)}</p>
              {/if}
              {#if s.event === "session_done"}
                <p class="mt-0.5 text-[11px] text-emerald-400/80">status: {s.status ?? "completed"}</p>
              {/if}
            </div>
          </li>
        {/each}

        {#if status === "connecting" || status === "streaming"}
          <li class="flex items-center gap-2 px-2 py-1 text-terminal-dim">
            <Loader2 class="size-3.5 animate-spin" />
            <span class="text-xs">streaming…</span>
          </li>
        {/if}
      </ol>
    {/if}
  </div>
</div>
