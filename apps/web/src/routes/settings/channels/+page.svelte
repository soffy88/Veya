<script lang="ts">
	/**
	 * /settings/channels — bind chat apps to bots/groups and configure the
	 * morning feed.
	 *
	 * Both sections are feature-flagged server-side. When a flag is off the
	 * endpoint answers 404 and the page says so instead of rendering a panel
	 * that cannot work.
	 */
	import { onMount } from "svelte";
	import { Bell, Check, Link2, RefreshCw, Trash2 } from "lucide-svelte";
	import {
		CHANNEL_PLATFORMS,
		FEED_SECTIONS,
		createLinkCode,
		getChannelsStatus,
		getFeedConfig,
		listBindings,
		platformLabel,
		saveFeedConfig,
		unbind,
		type ChannelBindingView,
		type ChannelPlatform,
		type ChannelsStatus,
		type FeedConfigView,
	} from "$lib/channels/api";
	import { actionClass, cardClass, inputClass, primaryClass } from "$lib/team/ui";
	import { api } from "$lib/api";

	let status = $state<ChannelsStatus | null>(null);
	let bindings = $state<ChannelBindingView[]>([]);
	let feed = $state<FeedConfigView | null>(null);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state("");
	let notice = $state("");
	let publishPermsEnabled = $state(false);

	// Bind form
	let showBind = $state(false);
	let platform = $state<ChannelPlatform>("telegram");
	let targetType = $state<"bot" | "group">("bot");
	let targetId = $state("");
	let issuedCode = $state("");
	let formError = $state("");

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			status = await getChannelsStatus();
			bindings = await listBindings();
			feed = status?.morning_feed_enabled ? await getFeedConfig() : null;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	async function submitBind(): Promise<void> {
		busy = true;
		formError = "";
		issuedCode = "";
		try {
			const issued = await createLinkCode({
				platform,
				target_type: targetType,
				target_id: targetId.trim(),
			});
			if (!issued) {
				formError = "无法生成绑定码，请确认服务端已开启 VEYA_CHANNELS。";
				return;
			}
			issuedCode = issued.code;
			await load();
		} catch (err) {
			formError = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function removeBinding(bindingId: string): Promise<void> {
		busy = true;
		error = "";
		try {
			await unbind(bindingId);
			await load();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	async function persistFeed(): Promise<void> {
		if (!feed) return;
		busy = true;
		error = "";
		notice = "";
		try {
			const { owner_ref: _ownerRef, ...rest } = feed;
			if (await saveFeedConfig(rest)) notice = "已保存晨间简报配置";
			else error = "保存失败，请确认服务端已开启 VEYA_MORNING_FEED。";
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			busy = false;
		}
	}

	function toggleSection(name: string): void {
		if (!feed) return;
		feed.sections = feed.sections.includes(name)
			? feed.sections.filter((s) => s !== name)
			: [...feed.sections, name];
	}

	function toggleChannel(name: string): void {
		if (!feed) return;
		feed.push_channels = feed.push_channels.includes(name)
			? feed.push_channels.filter((p) => p !== name)
			: [...feed.push_channels, name];
	}

	async function loadPublishPermsFlag(): Promise<void> {
		try {
			const response = await api("gateway", "api/v1/tdx/publish-perms", { method: "GET" });
			publishPermsEnabled = response.ok && (response.data as { enabled?: boolean }).enabled === true;
		} catch {
			publishPermsEnabled = false;
		}
	}

	onMount(() => {
		void load();
		void loadPublishPermsFlag();
	});
</script>

<div class="mx-auto w-full max-w-3xl p-4 pb-24">
	<header class="mb-5 flex items-center justify-between gap-3">
		<div class="min-w-0">
			<h1 class="text-xl font-semibold tracking-tight">聊天渠道与晨间简报</h1>
			<p class="text-xs opacity-60">把外部群聊接到 bot / 群组，每天早上收到一份简报。</p>
			{#if publishPermsEnabled}
				<a href="/settings/agent-publish-perms" class="mt-2 inline-flex text-xs underline underline-offset-4 opacity-80 hover:opacity-100">Agent 发布权限 / Agent publish permissions</a>
			{/if}
		</div>
		<button class={actionClass} onclick={() => void load()} disabled={loading}>
			<RefreshCw size={16} class={loading ? "animate-spin" : ""} /> 刷新
		</button>
	</header>

	{#if error}
		<div class="{cardClass} mb-4 border-red-500/40 text-sm text-red-300">{error}</div>
	{/if}
	{#if notice}
		<div class="{cardClass} mb-4 border-emerald-500/40 text-sm text-emerald-300">{notice}</div>
	{/if}

	<!-- Channels -->
	<section class="mb-6">
		<div class="mb-2 flex items-center justify-between">
			<h2 class="text-sm font-medium opacity-80">渠道</h2>
			{#if status?.channels_enabled}
				<button class={actionClass} onclick={() => (showBind = !showBind)}>
					<Link2 size={16} /> 绑定
				</button>
			{/if}
		</div>

		{#if !status}
			<p class="{cardClass} text-sm opacity-60">
				渠道功能未启用（服务端 <code>VEYA_CHANNELS</code> 未开启）。
			</p>
		{:else}
			{#if showBind}
				<form
					class="{cardClass} mb-3 space-y-3"
					onsubmit={(event) => {
						event.preventDefault();
						void submitBind();
					}}
				>
					<select class={inputClass} bind:value={platform}>
						{#each CHANNEL_PLATFORMS as p (p)}
							<option value={p}>{platformLabel(p)}</option>
						{/each}
					</select>
					<select class={inputClass} bind:value={targetType}>
						<option value="bot">接到 bot</option>
						<option value="group">接到群组</option>
					</select>
					<input
						class={inputClass}
						placeholder="目标 id（bot id 或 group id）"
						bind:value={targetId}
						autocomplete="off"
					/>
					{#if formError}<p class="text-xs text-red-300">{formError}</p>{/if}
					{#if issuedCode}
						<p class="text-xs text-emerald-300">
							把这段一次性码发到外部聊天里完成绑定：<code class="break-all">{issuedCode}</code>
						</p>
					{/if}
					<div class="flex gap-2">
						<button class={primaryClass} type="submit" disabled={busy || !targetId.trim()}>
							生成绑定码
						</button>
						<button class={actionClass} type="button" onclick={() => (showBind = false)}>取消</button>
					</div>
				</form>
			{/if}

			{#if loading && bindings.length === 0}
				<p class="{cardClass} text-sm opacity-60">加载中…</p>
			{:else if bindings.length === 0}
				<p class="{cardClass} text-sm opacity-60">还没有绑定任何聊天。</p>
			{:else}
				<ul class="space-y-2">
					{#each bindings as binding (binding.binding_id)}
						<li class="{cardClass} flex items-center gap-3">
							<span class="min-w-0 flex-1">
								<span class="block truncate text-sm font-medium">
									{platformLabel(binding.platform)} → {binding.target_type}:
									{binding.target_id}
								</span>
								<span class="block truncate text-xs opacity-60">
									chat {binding.external_chat_id || "—"}
									{#if status.configured[binding.platform]}
										<span class="ml-1 text-emerald-400">已配置</span>
									{:else}
										<span class="ml-1 text-amber-400">缺少服务端密钥</span>
									{/if}
								</span>
							</span>
							<button
								class={actionClass}
								onclick={() => void removeBinding(binding.binding_id)}
								disabled={busy}
								aria-label="解绑"
							>
								<Trash2 size={16} />
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		{/if}
	</section>

	<!-- Morning feed -->
	<section>
		<div class="mb-2 flex items-center justify-between">
			<h2 class="flex items-center gap-1.5 text-sm font-medium opacity-80">
				<Bell size={15} /> 晨间简报
			</h2>
			{#if feed}
				<button class={actionClass} onclick={() => void persistFeed()} disabled={busy}>
					<Check size={16} /> 保存
				</button>
			{/if}
		</div>

		{#if !feed}
			<p class="{cardClass} text-sm opacity-60">
				晨间简报未启用（服务端 <code>VEYA_MORNING_FEED</code> 未开启）。
			</p>
		{:else}
			<div class="{cardClass} space-y-3">
				<label class="flex items-center gap-2 text-sm">
					<input type="checkbox" bind:checked={feed.enabled} />
					启用晨间简报
				</label>

				<div class="grid grid-cols-2 gap-2">
					<label class="space-y-1">
						<span class="block text-xs opacity-60">时间</span>
						<input class={inputClass} type="time" bind:value={feed.time} />
					</label>
					<label class="space-y-1">
						<span class="block text-xs opacity-60">时区</span>
						<input class={inputClass} placeholder="Asia/Shanghai" bind:value={feed.timezone} />
					</label>
				</div>

				<fieldset class="space-y-1">
					<legend class="text-xs opacity-60">包含板块</legend>
					<div class="flex flex-wrap gap-2">
						{#each FEED_SECTIONS as section (section)}
							<button
								type="button"
								class={feed.sections.includes(section) ? primaryClass : actionClass}
								onclick={() => toggleSection(section)}
							>
								{section}
							</button>
						{/each}
					</div>
				</fieldset>

				<label class="flex items-center gap-2 text-sm">
					<input type="checkbox" bind:checked={feed.push_webpush} />
					Web Push 推送
				</label>

				<fieldset class="space-y-1">
					<legend class="text-xs opacity-60">同时发送到聊天渠道</legend>
					<div class="flex flex-wrap gap-2">
						{#each CHANNEL_PLATFORMS as p (p)}
							<button
								type="button"
								class={feed.push_channels.includes(p) ? primaryClass : actionClass}
								onclick={() => toggleChannel(p)}
							>
								{platformLabel(p)}
							</button>
						{/each}
					</div>
				</fieldset>
			</div>
		{/if}
	</section>

	<!-- Web Reading Channels Section (ADDENDUM V) -->
	<section class="mt-6 space-y-3">
		<div class="flex items-center justify-between">
			<h2 class="text-sm font-medium">网页阅读频道阶梯 (ADDENDUM V)</h2>
			<span class="text-xs opacity-60">ordered backends &amp; probes</span>
		</div>

		{#if !status?.web_channels || Object.keys(status.web_channels).length === 0}
			<p class="{cardClass} text-sm opacity-60">
				暂无网页阅读频道探针数据（使用 <code>veya doctor channels</code> 运行自检）。
			</p>
		{:else}
			<div class="{cardClass} space-y-2">
				<div class="overflow-x-auto">
					<table class="w-full text-left text-xs">
						<thead>
							<tr class="border-b border-white/10 opacity-60">
								<th class="py-2 pr-4 font-normal">Channel</th>
								<th class="py-2 pr-4 font-normal">Active Backend</th>
								<th class="py-2 pr-4 font-normal">Status</th>
								<th class="py-2 font-normal">Fix Hint</th>
							</tr>
						</thead>
						<tbody class="divide-y divide-white/5">
							{#each Object.entries(status.web_channels) as [name, ch] (name)}
								<tr>
									<td class="py-2 pr-4 font-mono font-medium">{name}</td>
									<td class="py-2 pr-4 opacity-80">{ch.active_backend}</td>
									<td class="py-2 pr-4">
										{#if ch.status === 'ok'}
											<span class="rounded bg-emerald-500/10 px-1.5 py-0.5 text-emerald-400">ok</span>
										{:else if ch.status === 'needs-login'}
											<span class="rounded bg-amber-500/10 px-1.5 py-0.5 text-amber-400">needs-login</span>
										{:else}
											<span class="rounded bg-rose-500/10 px-1.5 py-0.5 text-rose-400">broken</span>
										{/if}
									</td>
									<td class="py-2 text-xs opacity-60 truncate max-w-[200px]" title={ch.fix_hint}>
										{ch.fix_hint || '-'}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</div>
		{/if}
	</section>
</div>