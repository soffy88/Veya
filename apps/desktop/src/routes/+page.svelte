<script lang="ts">
  /**
   * Veya Desktop — Layer 4 agent console.
   *
   * The UI shell is plain Svelte 5 runes; the heavy lifting (decision-trail
   * rendering, SSE consumption) lives in DecisionTrailView.svelte.
   */
  import { Send, Square, Eraser, FlaskConical } from "lucide-svelte";
  import DecisionTrailView, { effectiveEndpoint } from "@veya/ui";

  let task = $state("做一次简单的代码测试");
  let running = $state(false);
  let dryRun = $state(false);
  let lastResult = $state<{ status: string; cost: number; sessionId?: string } | null>(null);
  let view = $state<DecisionTrailView>();

  function start() {
    if (!task.trim() || running) return;
    lastResult = null;
    running = true;
  }

  function stop() {
    running = false;
  }

  function clear() {
    lastResult = null;
    view?.reset();
  }
</script>

<main class="flex h-screen flex-col p-4 gap-4">
  <!-- top bar -->
  <header class="flex shrink-0 items-center justify-between rounded-xl border border-terminal-edge bg-terminal-panel px-4 py-2.5">
    <div class="flex items-center gap-2.5">
      <span class="flex size-7 items-center justify-center rounded-lg bg-gradient-to-br from-sky-500 to-violet-600 font-mono text-sm font-bold text-white">
        V
      </span>
      <h1 class="font-semibold tracking-tight">Veya Desktop</h1>
      <span class="rounded-full border border-terminal-edge px-2 py-0.5 font-mono text-[10px] text-terminal-dim">
        Layer 4 · 3O
      </span>
    </div>
    <div class="flex items-center gap-2 font-mono text-[11px] text-terminal-dim">
      <span class="inline-block size-1.5 rounded-full bg-emerald-500"></span>
      <span>gateway {effectiveEndpoint()}</span>
    </div>
  </header>

  <!-- task input -->
  <section class="flex shrink-0 items-end gap-3 rounded-xl border border-terminal-edge bg-terminal-panel p-3">
    <div class="min-w-0 flex-1">
      <label for="task" class="mb-1 block font-mono text-[11px] text-terminal-dim">
        task
      </label>
      <textarea
        id="task"
        bind:value={task}
        rows="2"
        placeholder="描述一个任务，例如：做一次简单的代码测试"
        class="w-full resize-none rounded-lg border border-terminal-edge bg-terminal-bg px-3 py-2 font-mono text-sm text-terminal-fg outline-none transition placeholder:text-terminal-dim/60 focus:border-sky-500/60"
      ></textarea>
    </div>
    <div class="flex shrink-0 items-center gap-2 pb-0.5">
      <button
        type="button"
        class:opacity-60={!dryRun}
        onclick={() => (dryRun = !dryRun)}
        class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-3 py-2 font-mono text-xs text-terminal-dim transition hover:border-amber-400/40 hover:text-amber-300"
        title="仅装配 manifest，不真正执行"
      >
        <FlaskConical class="size-3.5" />
        dry-run
      </button>
      {#if running}
        <button
          type="button"
          onclick={stop}
          class="flex items-center gap-1.5 rounded-lg border border-rose-500/40 bg-rose-500/10 px-4 py-2 font-mono text-xs text-rose-300 transition hover:bg-rose-500/20"
        >
          <Square class="size-3.5" />
          stop
        </button>
      {:else}
        <button
          type="button"
          onclick={start}
          disabled={!task.trim()}
          class="flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-sky-500 to-violet-600 px-4 py-2 font-mono text-xs font-semibold text-white transition hover:brightness-110 disabled:opacity-40"
        >
          <Send class="size-3.5" />
          run
        </button>
      {/if}
    </div>
  </section>

  <!-- decision trail -->
  <section class="flex min-h-0 flex-1 flex-col">
    <DecisionTrailView
      bind:this={view}
      {task}
      bind:running
      mode={dryRun ? "dry_run" : "run"}
      onSessionDone={(info) => (lastResult = info)}
    />
  </section>

  <!-- status bar -->
  <footer class="flex h-8 shrink-0 items-center justify-between rounded-xl border border-terminal-edge bg-terminal-panel px-4 font-mono text-[11px] text-terminal-dim">
    <div class="flex items-center gap-4">
      {#if lastResult}
        <span class:text-emerald-400={lastResult.status === "completed"} class:text-rose-400={lastResult.status === "error"}>
          status: {lastResult.status}
        </span>
        <span>cost: ${lastResult.cost.toFixed(6)}</span>
        {#if lastResult.sessionId}
          <span>session: {lastResult.sessionId.slice(0, 12)}</span>
        {/if}
      {:else}
        <span>idle — 等待任务</span>
      {/if}
    </div>
    <button
      type="button"
      onclick={clear}
      class="flex items-center gap-1 rounded-md px-2 py-1 transition hover:text-terminal-fg"
    >
      <Eraser class="size-3" />
      clear
    </button>
  </footer>
</main>
