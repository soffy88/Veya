<script lang="ts">
	import { Brain, Check, Eye, History, RefreshCw, RotateCcw, Sparkles, Trash2 } from "lucide-svelte";
	import { onMount } from "svelte";
	import { api, type ApiResult } from "$lib/api";

	type Memory = {
		id: string;
		content: string;
		memory_type: string;
		confidence: number;
		status: string;
		provenance?: Record<string, unknown>;
		updated_at?: number;
	};
	type Skill = {
		id: string;
		skill_id?: string;
		name?: string;
		version?: number;
		description?: string;
		success_rate?: number;
		trust_status?: string;
		status?: string;
	};
	type Continuity = {
		id?: string;
		active_tasks?: Array<Record<string, unknown>>;
		paused_tasks?: Array<Record<string, unknown>>;
		unfinished_work?: Array<Record<string, unknown>>;
	};
	type Learning = { id: string; observation: string; status: string; confidence: number };

	let memories = $state<Memory[]>([]);
	let skills = $state<Skill[]>([]);
	let continuity = $state<Continuity | null>(null);
	let learning = $state<Learning[]>([]);
	let memorySources = $state<Record<string, { provenance?: Record<string, unknown>; events?: unknown[] }>>({});
	let error = $state("");
	let busy = $state(false);

	async function load(): Promise<void> {
		busy = true;
		error = "";
		const [m, s, c, l] = await Promise.all([
			api("gateway", "api/v1/memory", { method: "GET", query: { limit: 30 } }),
			api("gateway", "api/v1/skills", { method: "GET", query: { limit: 30, include_candidates: true } }),
			api("gateway", "api/v1/continuity", { method: "GET" }),
			api("gateway", "api/v1/learning/candidates", { method: "GET", query: { limit: 20 } }),
		]);
		busy = false;
		if (m.ok && typeof m.data === "object" && m.data) memories = ((m.data as { records?: Memory[] }).records ?? []);
		if (s.ok && typeof s.data === "object" && s.data) skills = ((s.data as { skills?: Skill[] }).skills ?? []);
		if (c.ok && typeof c.data === "object" && c.data) continuity = c.data as Continuity;
		if (l.ok && typeof l.data === "object" && l.data) learning = ((l.data as { candidates?: Learning[] }).candidates ?? []);
		if (![m, s, c, l].every((item) => item.ok)) error = "个人上下文部分加载失败，请重试";
	}

	async function forgetMemory(id: string): Promise<void> {
		const r = await api("gateway", `api/v1/memory/${encodeURIComponent(id)}/forget`, { method: "POST" });
		if (r.ok) memories = memories.filter((item) => item.id !== id);
		else error = `忘记失败 (${r.status})`;
	}

	async function correctMemory(memory: Memory): Promise<void> {
		const content = window.prompt("修正 Veya 记住的内容", memory.content);
		if (!content || content === memory.content) return;
		const r = await api("gateway", `api/v1/memory/${encodeURIComponent(memory.id)}/correct`, {
			method: "POST",
			body: { content, source_session_ids: [] },
		});
		if (r.ok) await load();
		else error = `修正失败 (${r.status})`;
	}

	async function inspectMemory(memory: Memory): Promise<void> {
		const r = await api("gateway", `api/v1/memory/${encodeURIComponent(memory.id)}`, { method: "GET" });
		if (r.ok && typeof r.data === "object" && r.data) {
			const record = (r.data as { record?: { provenance?: Record<string, unknown>; sources?: { events?: unknown[] } } }).record;
			memorySources = { ...memorySources, [memory.id]: { provenance: record?.provenance, events: record?.sources?.events } };
		}
	}

	async function confirmSkill(skill: Skill): Promise<void> {
		const r = await api("gateway", `api/v1/skills/${encodeURIComponent(skill.id)}/confirm`, { method: "POST" });
		if (r.ok) await load();
		else error = `Skill 确认失败 (${r.status})`;
	}

	async function rollbackSkill(skill: Skill): Promise<void> {
		if (!skill.skill_id || Number(skill.version ?? 1) <= 1) return;
		const r = await api("gateway", `api/v1/skills/${encodeURIComponent(skill.skill_id)}/rollback`, { method: "POST", body: { version: 1 } });
		if (r.ok) await load();
		else error = `Skill 回滚失败 (${r.status})`;
	}

	async function disableSkill(skill: Skill): Promise<void> {
		const id = skill.skill_id ?? skill.id;
		const r = await api("gateway", `api/v1/skills/${encodeURIComponent(id)}/deprecate`, { method: "POST" });
		if (r.ok) await load();
		else error = `Skill 停用失败 (${r.status})`;
	}

	async function continueTask(task: Record<string, unknown>): Promise<void> {
		const id = String(task.id ?? "");
		if (!id) return;
		const r: ApiResult = await api("gateway", `api/v1/tasks/${encodeURIComponent(id)}/continue`, { method: "POST" });
		if (!r.ok) error = `恢复失败 (${r.status})`;
	}

	onMount(() => {
		void load();
	});
</script>

<div class="h-full overflow-y-auto p-6">
	<div class="mx-auto max-w-6xl space-y-6">
		<div class="flex items-center gap-3">
			<div class="flex size-9 items-center justify-center rounded-xl bg-violet-500/15 text-violet-300"><Brain class="size-5" /></div>
			<div class="flex-1"><h2 class="text-lg font-semibold">Personal Context</h2><p class="text-xs text-terminal-dim">只展示已持久化、可纠正的事实与任务投影</p></div>
			<button type="button" class="flex items-center gap-1.5 rounded-lg border border-terminal-edge px-3 py-2 text-xs text-terminal-dim hover:text-terminal-fg" onclick={load} disabled={busy}><RefreshCw class="size-3.5 {busy ? 'animate-spin' : ''}" />刷新</button>
		</div>
		{#if error}<div class="rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">{error}</div>{/if}

		<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
			<div class="mb-3 flex items-center gap-2"><History class="size-4 text-sky-300" /><h3 class="text-sm font-medium">Continue where you left off</h3></div>
			{#if continuity?.unfinished_work?.length}
				<div class="grid gap-2 md:grid-cols-2">{#each continuity.unfinished_work as task (String(task.id))}<div class="flex items-center gap-3 rounded-lg border border-white/5 bg-black/20 p-3"><div class="min-w-0 flex-1"><p class="truncate text-sm">{String(task.objective ?? task.id)}</p><p class="mt-1 font-mono text-[10px] text-terminal-dim">{String(task.status ?? "unfinished")}</p></div><button type="button" class="rounded-md border border-sky-400/30 px-2 py-1 text-xs text-sky-300 hover:bg-sky-400/10" onclick={() => continueTask(task)}>继续</button></div>{/each}</div>
			{:else}<p class="text-xs text-terminal-dim">暂无未完成任务</p>{/if}
		</section>

		<div class="grid gap-6 lg:grid-cols-2">
			<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
				<div class="mb-3 flex items-center gap-2"><Brain class="size-4 text-violet-300" /><h3 class="text-sm font-medium">Remembered</h3><span class="font-mono text-[10px] text-terminal-dim">{memories.length}</span></div>
				<div class="space-y-2">{#each memories as memory (memory.id)}<div class="rounded-lg border border-white/5 p-3"><div class="flex gap-2"><p class="flex-1 text-sm">{memory.content}</p><span class="rounded bg-violet-400/10 px-1.5 py-0.5 font-mono text-[10px] text-violet-300">{memory.memory_type}</span></div>{#if memorySources[memory.id]}<p class="mt-2 text-[10px] text-terminal-dim">来源事件 {memorySources[memory.id].events?.length ?? 0} · {JSON.stringify(memorySources[memory.id].provenance ?? {})}</p>{/if}<div class="mt-2 flex items-center gap-2 text-[10px] text-terminal-dim"><span>confidence {(memory.confidence * 100).toFixed(0)}%</span><span>·</span><span>{memory.status}</span><span class="flex-1"></span><button type="button" title="查看来源" class="text-violet-300 hover:text-violet-200" onclick={() => inspectMemory(memory)}><Eye class="size-3.5" /></button><button type="button" title="纠正" class="text-sky-300 hover:text-sky-200" onclick={() => correctMemory(memory)}><RotateCcw class="size-3.5" /></button><button type="button" title="忘记" class="text-rose-300 hover:text-rose-200" onclick={() => forgetMemory(memory.id)}><Trash2 class="size-3.5" /></button></div></div>{:else}<p class="text-xs text-terminal-dim">还没有已确认记忆</p>{/each}</div>
			</section>

			<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
				<div class="mb-3 flex items-center gap-2"><Sparkles class="size-4 text-amber-300" /><h3 class="text-sm font-medium">Skills</h3><span class="font-mono text-[10px] text-terminal-dim">{skills.length}</span></div>
				<div class="space-y-2">{#each skills as skill (skill.id)}<div class="rounded-lg border border-white/5 p-3"><div class="flex items-center gap-2"><p class="flex-1 text-sm">{skill.name ?? skill.id}</p><span class="font-mono text-[10px] text-emerald-300">v{skill.version ?? "?"}</span></div><p class="mt-1 line-clamp-2 text-xs text-terminal-dim">{skill.description ?? ""}</p><div class="mt-2 flex items-center gap-2 text-[10px] text-terminal-dim"><span>{skill.trust_status ?? "review_required"}</span><span>·</span><span>success {((skill.success_rate ?? 0) * 100).toFixed(0)}%</span><span class="flex-1"></span>{#if skill.status === "candidate"}<button type="button" class="rounded border border-sky-400/30 px-2 py-1 text-sky-300 hover:bg-sky-400/10" onclick={() => confirmSkill(skill)}>确认候选</button>{:else}<Check class="size-3.5 text-emerald-300" />{#if Number(skill.version ?? 1) > 1}<button type="button" title="回滚到 v1" class="text-amber-300 hover:text-amber-200" onclick={() => rollbackSkill(skill)}><RotateCcw class="size-3.5" /></button>{/if}<button type="button" title="停用" class="text-rose-300 hover:text-rose-200" onclick={() => disableSkill(skill)}><Trash2 class="size-3.5" /></button>{/if}</div></div>{:else}<p class="text-xs text-terminal-dim">还没有已确认 Skill</p>{/each}</div>
			</section>
		</div>

		<section class="rounded-xl border border-terminal-edge bg-white/[0.02] p-4">
			<div class="mb-3 flex items-center gap-2"><Sparkles class="size-4 text-cyan-300" /><h3 class="text-sm font-medium">Learning candidates</h3><span class="font-mono text-[10px] text-terminal-dim">{learning.length}</span></div>
			<div class="grid gap-2 md:grid-cols-2">{#each learning as item (item.id)}<div class="rounded-lg border border-white/5 p-3"><p class="text-sm">{item.observation}</p><p class="mt-1 font-mono text-[10px] text-terminal-dim">{item.status} · confidence {(item.confidence * 100).toFixed(0)}% · 需离线评估/确认后才会应用</p></div>{:else}<p class="text-xs text-terminal-dim">暂无学习候选；单次失败不会自动修改 Skill 或提示词</p>{/each}</div>
		</section>
	</div>
</div>
