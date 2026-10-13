<script lang="ts">
	/**
	 * W15: "从模板选择角色" / "添加技能" for one bot.
	 *
	 * Hidden entirely while VEYA_ROLE_SKILL_PRESETS is off (the list endpoints
	 * answer available:false). Applying goes through the server's owner check and
	 * writes the bot's existing instructions / skill fields.
	 */
	import { onMount } from "svelte";
	import type { BotView } from "$lib/team/api";
	import { applyPresets, listRolePresets, listSkillPresets, type PresetItem } from "$lib/team/rolePresets";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";

	let { bot, onApplied }: { bot: BotView; onApplied: () => void } = $props();

	let available = $state(false);
	let roles = $state<PresetItem[]>([]);
	let skills = $state<PresetItem[]>([]);
	let roleId = $state("");
	let skillId = $state("");
	let busy = $state(false);
	let message = $state("");
	let error = $state("");

	const attached = $derived(
		new Set((bot.available_skill_ids ?? []).filter((id) => id.startsWith("preset:")).map((id) => id.slice(7))),
	);

	onMount(async () => {
		const [r, s] = await Promise.all([listRolePresets(), listSkillPresets()]);
		available = r.available && s.available;
		roles = r.items;
		skills = s.items;
	});

	async function apply(body: { role_id?: string; skill_ids?: string[] }): Promise<void> {
		busy = true;
		error = "";
		message = "";
		try {
			const result = await applyPresets(bot.bot_id, body);
			if (result.role) {
				message = result.role_truncated ? "已应用角色（模板较长，已截断到指令上限）" : "已应用角色";
			} else {
				message = result.skills_added.length ? "已添加技能" : "该技能已在列表中";
			}
			onApplied();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}
</script>

{#if available}
	<section class="{cardClass} space-y-2" aria-label="角色与技能模板">
		<label class="block text-xs opacity-60" for="preset-role">从模板选择角色</label>
		<div class="flex gap-2">
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
				onclick={() => void apply({ role_id: roleId })}>应用</button
			>
		</div>
		<label class="block text-xs opacity-60" for="preset-skill">添加技能</label>
		<div class="flex gap-2">
			<select id="preset-skill" class="{inputClass} flex-1" bind:value={skillId}>
				<option value="">选择一个技能…</option>
				{#each skills as skill (skill.id)}
					<option value={skill.id} disabled={attached.has(skill.id)}>{skill.name} — {skill.summary_zh}</option>
				{/each}
			</select>
			<button
				type="button"
				class={actionClass}
				disabled={busy || !skillId}
				onclick={() => void apply({ skill_ids: [skillId] })}>添加</button
			>
		</div>
		{#if attached.size}
			<p class="text-xs opacity-60">已挂技能：{[...attached].join("、")}</p>
		{/if}
		<p class="text-xs opacity-50">模板来自 agency-agents / agent-skills（MIT），角色写入上方指令中的标记块，可随时手动修改。</p>
		{#if message}<p class="text-xs text-green-600">{message}</p>{/if}
		{#if error}<p class="text-xs text-red-500">{error}</p>{/if}
	</section>
{/if}
