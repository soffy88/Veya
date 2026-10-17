<script lang="ts">
	/**
	 * W15 & Role Skills: "从模板选择角色" / "管理角色技能" for one bot.
	 *
	 * Displays attached skills from both presets and runtime (including taught
	 * skills), with one-click attach and detach.
	 */
	import { onMount } from "svelte";
	import { Trash2, Plus, Sparkles, X } from "lucide-svelte";
	import { patchBot, type BotView } from "$lib/team/api";
	import { applyPresets, listRolePresets, listSkillPresets, type PresetItem } from "$lib/team/rolePresets";
	import { api } from "$lib/api";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";

	let { bot, onApplied }: { bot: BotView; onApplied: () => void } = $props();

	let available = $state(true);
	let roles = $state<PresetItem[]>([]);
	let presetSkills = $state<PresetItem[]>([]);
	let runtimeSkills = $state<Array<{ skill_id: string; name: string; description: string }>>([]);
	let roleId = $state("");
	let selectedSkillId = $state("");
	let customSkillId = $state("");
	let busy = $state(false);
	let message = $state("");
	let error = $state("");

	const allAttached = $derived(bot.available_skill_ids ?? []);

	async function load(): Promise<void> {
		try {
			const [r, s] = await Promise.all([listRolePresets(), listSkillPresets()]);
			roles = r.items ?? [];
			presetSkills = s.items ?? [];
		} catch {
			/* presets unavailable */
		}

		try {
			const res = await api("gateway", "api/v1/skills", { method: "GET" });
			if (res.ok && res.data && typeof res.data === "object") {
				const list = (res.data as { skills?: Array<{ skill_id: string; name: string; description: string }> }).skills ?? [];
				runtimeSkills = list;
			}
		} catch {
			/* runtime skills endpoint unavailable */
		}
	}

	onMount(load);

	async function applyRole(): Promise<void> {
		if (!roleId) return;
		busy = true;
		error = "";
		message = "";
		try {
			const result = await applyPresets(bot.bot_id, { role_id: roleId });
			message = result.role_truncated ? "已应用角色（模板较长，已截断到指令上限）" : "已应用角色";
			onApplied();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function addSkill(skillToAdd: string): Promise<void> {
		const clean = skillToAdd.trim();
		if (!clean) return;
		if (allAttached.includes(clean)) {
			message = "该技能已在列表中";
			return;
		}
		busy = true;
		error = "";
		message = "";
		try {
			const nextList = [...allAttached, clean];
			await patchBot(bot.bot_id, { available_skill_ids: nextList });
			message = `已添加技能：${clean}`;
			selectedSkillId = "";
			customSkillId = "";
			onApplied();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function removeSkill(skillToRemove: string): Promise<void> {
		busy = true;
		error = "";
		message = "";
		try {
			const nextList = allAttached.filter((s) => s !== skillToRemove);
			await patchBot(bot.bot_id, { available_skill_ids: nextList });
			message = `已移除技能：${skillToRemove}`;
			onApplied();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

<section class="{cardClass} space-y-3" aria-label="角色与技能管理">
	{#if roles.length > 0}
		<div>
			<label class="block text-xs opacity-60" for="preset-role">从模板选择角色</label>
			<div class="mt-1 flex gap-2">
				<select id="preset-role" class="{inputClass} flex-1" bind:value={roleId}>
					<option value="">选择一个角色…</option>
					{#each roles as role (role.id)}
						<option value={role.id}>{role.name} — {role.summary_zh}</option>
					{/each}
				</select>
				<button
					type="button"
					class={primaryClass}
					disabled={busy || !roleId}
					onclick={() => void applyRole()}>应用角色</button
				>
			</div>
		</div>
	{/if}

	<div>
		<div class="flex items-center justify-between mb-1">
			<label class="block text-xs font-medium opacity-70" for="preset-skill">挂载技能（Role Skills）</label>
			<span class="text-[11px] opacity-40">运行时通过 &lt;available_skills&gt; 注入提示词</span>
		</div>

		<!-- Attached skills list chips -->
		<div class="flex flex-wrap gap-1.5 min-h-8 p-2 rounded-lg bg-white/[0.03] border border-white/5 mb-2">
			{#if allAttached.length === 0}
				<span class="text-xs text-white/40 italic py-0.5">尚未挂载任何技能。请在下方选择或输入技能 ID 添加。</span>
			{:else}
				{#each allAttached as skill (skill)}
					<span class="inline-flex items-center gap-1.5 rounded-full bg-sky-950/50 border border-sky-500/30 px-2.5 py-0.5 text-xs text-sky-200">
						<Sparkles size={11} class="text-sky-400" />
						<span>{skill}</span>
						<button
							type="button"
							class="text-sky-400 hover:text-rose-300 transition"
							title="移除此技能"
							aria-label="移除技能 {skill}"
							onclick={() => void removeSkill(skill)}
							disabled={busy}
						>
							<X size={12} />
						</button>
					</span>
				{/each}
			{/if}
		</div>

		<!-- Selector for adding skill -->
		<div class="flex gap-2">
			<select id="preset-skill" class="{inputClass} flex-1" bind:value={selectedSkillId}>
				<option value="">-- 选择预设或运行时技能 --</option>
				{#if runtimeSkills.length > 0}
					<optgroup label="已习得 / 运行时技能">
						{#each runtimeSkills as rs (rs.skill_id)}
							<option value={rs.name || rs.skill_id} disabled={allAttached.includes(rs.name || rs.skill_id)}>
								{rs.name} ({rs.skill_id})
							</option>
						{/each}
					</optgroup>
				{/if}
				{#if presetSkills.length > 0}
					<optgroup label="预设技能模板">
						{#each presetSkills as ps (ps.id)}
							<option value={"preset:" + ps.id} disabled={allAttached.includes("preset:" + ps.id)}>
								{ps.name} — {ps.summary_zh}
							</option>
						{/each}
					</optgroup>
				{/if}
			</select>
			<button
				type="button"
				class={actionClass}
				disabled={busy || !selectedSkillId}
				onclick={() => void addSkill(selectedSkillId)}>添加</button
			>
		</div>

		<!-- Custom skill ID input -->
		<div class="mt-2 flex gap-2">
			<input
				class="{inputClass} flex-1 text-xs"
				placeholder="或直接输入自定义技能 ID / 函数名…"
				bind:value={customSkillId}
				onkeydown={(e) => {
					if (e.key === "Enter") {
						e.preventDefault();
						void addSkill(customSkillId);
					}
				}}
			/>
			<button
				type="button"
				class={actionClass}
				disabled={busy || !customSkillId.trim()}
				onclick={() => void addSkill(customSkillId)}>
				<Plus size={14} /> 自定义
			</button>
		</div>
	</div>

	{#if message}<p class="text-xs text-emerald-400">{message}</p>{/if}
	{#if error}<p class="text-xs text-rose-400">{error}</p>{/if}
</section>
