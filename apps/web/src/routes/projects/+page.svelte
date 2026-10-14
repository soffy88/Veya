<script lang="ts">
	/**
	 * /projects — Project Brain list & onboarding (W16 Project Brain)
	 *
	 * Lists user's project bots (projected from BotRegistry),
	 * with quick search and a modal to create new projects with
	 * .veya-project workspace layout and dedicated project brain.
	 */
	import { onMount } from "svelte";
	import {
		AlertCircle,
		ArrowRight,
		Calendar,
		Folder,
		FolderKanban,
		Plus,
		RefreshCw,
		Search,
		Sparkles,
		X,
	} from "lucide-svelte";
	import { createProject, listProjects, type ProjectView } from "$lib/projects/api";

	let projects = $state<ProjectView[]>([]);
	let loading = $state(true);
	let error = $state("");
	let searchQuery = $state("");

	// New project modal state
	let showCreateModal = $state(false);
	let newName = $state("");
	let newDesc = $state("");
	let newWorkspace = $state("");
	let creating = $state(false);
	let createError = $state("");

	const filteredProjects = $derived(
		projects.filter((p) => {
			const q = searchQuery.trim().toLowerCase();
			if (!q) return true;
			return (
				p.name.toLowerCase().includes(q) ||
				p.id.toLowerCase().includes(q) ||
				(p.description ?? "").toLowerCase().includes(q) ||
				(p.workspace_path ?? "").toLowerCase().includes(q)
			);
		}),
	);

	async function load(): Promise<void> {
		loading = true;
		error = "";
		try {
			projects = await listProjects();
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	async function handleCreate(e: SubmitEvent): Promise<void> {
		e.preventDefault();
		const name = newName.trim();
		if (!name) {
			createError = "请输入项目名称";
			return;
		}
		creating = true;
		createError = "";
		try {
			const created = await createProject({
				name,
				description: newDesc.trim() || undefined,
				workspace_path: newWorkspace.trim() || undefined,
			});
			showCreateModal = false;
			newName = "";
			newDesc = "";
			newWorkspace = "";
			projects = [created, ...projects];
		} catch (err) {
			createError = err instanceof Error ? err.message : String(err);
		} finally {
			creating = false;
		}
	}

	onMount(() => {
		void load();
	});
</script>

<svelte:head>
	<title>项目主脑 | Veya</title>
</svelte:head>

<div class="min-h-screen bg-terminal-bg text-terminal-fg">
	<!-- Top Navigation -->
	<header class="border-b border-white/[0.08] bg-white/[0.02] px-4 py-3 backdrop-blur md:px-8">
		<div class="mx-auto flex max-w-6xl items-center justify-between gap-4">
			<div class="flex items-center gap-3">
				<a
					href="/"
					class="flex items-center gap-2 text-sm text-terminal-dim transition hover:text-terminal-fg"
				>
					<span>← 返回控制台</span>
				</a>
				<span class="text-white/20">/</span>
				<div class="flex items-center gap-2 font-semibold">
					<FolderKanban class="size-5 text-sky-400" />
					<h1 class="text-base text-terminal-fg md:text-lg">项目主脑</h1>
				</div>
			</div>

			<div class="flex items-center gap-2">
				<button
					type="button"
					onclick={() => void load()}
					disabled={loading}
					class="flex items-center gap-1.5 rounded-lg border border-white/[0.08] bg-white/[0.04] px-3 py-1.5 text-xs text-terminal-dim transition hover:bg-white/[0.08] hover:text-terminal-fg disabled:opacity-50"
				>
					<RefreshCw class="size-3.5 {loading ? 'animate-spin' : ''}" />
					<span class="hidden sm:inline">刷新</span>
				</button>
				<button
					type="button"
					onclick={() => {
						showCreateModal = true;
						createError = "";
					}}
					class="flex items-center gap-1.5 rounded-lg bg-sky-500 px-3 py-1.5 text-xs font-medium text-black transition hover:bg-sky-400"
				>
					<Plus class="size-3.5" />
					<span>新建项目</span>
				</button>
			</div>
		</div>
	</header>

	<!-- Main Content Area -->
	<main class="mx-auto max-w-6xl px-4 py-6 md:px-8">
		<!-- Subtitle & Search -->
		<div class="mb-6 flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
			<div>
				<h2 class="text-sm font-medium text-white/90">自主长任务与持续巡检项目空间</h2>
				<p class="mt-0.5 text-xs text-terminal-dim">
					每个项目配备专属主脑、独立状态简报 (PROJECT_STATE.md) 与后台周期巡检
				</p>
			</div>
			<div class="relative w-full sm:w-64">
				<Search class="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-terminal-dim" />
				<input
					type="text"
					bind:value={searchQuery}
					placeholder="搜索项目..."
					class="w-full rounded-lg border border-white/[0.08] bg-white/[0.03] py-1.5 pl-8 pr-3 text-xs text-terminal-fg placeholder:text-terminal-dim/60 focus:border-sky-500/50 focus:outline-none focus:ring-1 focus:ring-sky-500/50"
				/>
			</div>
		</div>

		<!-- Error Banner -->
		{#if error}
			<div class="mb-6 flex items-center justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-200">
				<div class="flex items-center gap-2">
					<AlertCircle class="size-4 shrink-0 text-amber-400" />
					<span>{error}</span>
				</div>
				<button
					type="button"
					onclick={() => void load()}
					class="shrink-0 underline hover:text-white"
				>
					重试
				</button>
			</div>
		{/if}

		<!-- Loading State -->
		{#if loading && projects.length === 0}
			<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
				{#each [1, 2, 3] as _}
					<div class="h-36 animate-pulse rounded-xl border border-white/[0.06] bg-white/[0.02] p-4"></div>
				{/each}
			</div>
		{:else if filteredProjects.length === 0}
			<!-- Empty State -->
			<div class="flex flex-col items-center justify-center rounded-2xl border border-dashed border-white/[0.1] bg-white/[0.01] px-6 py-16 text-center">
				<div class="flex size-12 items-center justify-center rounded-xl bg-sky-500/10 text-sky-400">
					<Sparkles class="size-6" />
				</div>
				<h3 class="mt-4 text-sm font-medium text-terminal-fg">暂无项目</h3>
				<p class="mt-1 max-w-sm text-xs text-terminal-dim">
					{searchQuery ? "没有找到符合搜索条件的项目" : "创建您的第一个项目主脑，开始拆解目标与自主巡检。"}
				</p>
				{#if !searchQuery}
					<button
						type="button"
						onclick={() => {
							showCreateModal = true;
							createError = "";
						}}
						class="mt-4 flex items-center gap-1.5 rounded-lg bg-sky-500 px-3.5 py-1.5 text-xs font-medium text-black transition hover:bg-sky-400"
					>
						<Plus class="size-3.5" />
						<span>创建项目</span>
					</button>
				{/if}
			</div>
		{:else}
			<!-- Projects Grid -->
			<div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
				{#each filteredProjects as project (project.id)}
					<a
						href="/projects/{project.id}"
						class="group flex flex-col justify-between rounded-xl border border-white/[0.08] bg-white/[0.02] p-4 transition hover:border-sky-500/40 hover:bg-white/[0.04]"
					>
						<div>
							<div class="flex items-start justify-between gap-3">
								<div class="flex items-center gap-3">
									<div
										class="flex size-9 shrink-0 items-center justify-center rounded-lg text-sm font-semibold text-white shadow"
										style="background-color: {project.color || '#4A90D9'}"
									>
										{project.icon || project.name.slice(0, 1).toUpperCase()}
									</div>
									<div class="min-w-0">
										<h3 class="truncate text-sm font-medium text-terminal-fg group-hover:text-sky-300">
											{project.name}
										</h3>
										<p class="text-[11px] text-terminal-dim">ID: {project.id}</p>
									</div>
								</div>
								<ArrowRight class="size-4 shrink-0 text-terminal-dim transition group-hover:translate-x-0.5 group-hover:text-sky-300" />
							</div>

							{#if project.description}
								<p class="mt-3 line-clamp-2 text-xs text-white/70">
									{project.description}
								</p>
							{/if}
						</div>

						<div class="mt-4 border-t border-white/[0.04] pt-3 text-[11px] text-terminal-dim">
							{#if project.workspace_path}
								<div class="flex items-center gap-1.5 truncate">
									<Folder class="size-3 shrink-0 text-terminal-dim/70" />
									<span class="truncate">{project.workspace_path}</span>
								</div>
							{/if}
							{#if project.created_at}
								<div class="mt-1 flex items-center gap-1.5">
									<Calendar class="size-3 shrink-0 text-terminal-dim/70" />
									<span>{project.created_at.replace("T", " ").replace("Z", "")}</span>
								</div>
							{/if}
						</div>
					</a>
				{/each}
			</div>
		{/if}
	</main>

	<!-- Create Project Modal -->
	{#if showCreateModal}
		<div class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm">
			<div class="w-full max-w-md rounded-2xl border border-white/[0.12] bg-neutral-900 p-6 shadow-2xl">
				<div class="flex items-center justify-between border-b border-white/[0.08] pb-3">
					<h3 class="text-sm font-semibold text-terminal-fg">新建项目主脑</h3>
					<button
						type="button"
						onclick={() => (showCreateModal = false)}
						class="rounded p-1 text-terminal-dim transition hover:text-terminal-fg"
					>
						<X class="size-4" />
					</button>
				</div>

				<form onsubmit={handleCreate} class="mt-4 space-y-4">
					{#if createError}
						<div class="rounded-lg border border-red-500/30 bg-red-500/10 p-2.5 text-xs text-red-300">
							{createError}
						</div>
					{/if}

					<div>
						<label for="project-name" class="block text-xs font-medium text-white/80">项目名称 *</label>
						<input
							id="project-name"
							type="text"
							bind:value={newName}
							required
							placeholder="例如: Trading Core Engine"
							class="mt-1 w-full rounded-lg border border-white/[0.1] bg-white/[0.04] px-3 py-2 text-xs text-terminal-fg placeholder:text-terminal-dim/60 focus:border-sky-500 focus:outline-none"
						/>
					</div>

					<div>
						<label for="project-desc" class="block text-xs font-medium text-white/80">项目描述</label>
						<textarea
							id="project-desc"
							bind:value={newDesc}
							rows="2"
							placeholder="简短描述项目目标..."
							class="mt-1 w-full rounded-lg border border-white/[0.1] bg-white/[0.04] px-3 py-2 text-xs text-terminal-fg placeholder:text-terminal-dim/60 focus:border-sky-500 focus:outline-none"
						></textarea>
					</div>

					<div>
						<label for="project-workspace" class="block text-xs font-medium text-white/80">工作区绝对路径 (可选)</label>
						<input
							id="project-workspace"
							type="text"
							bind:value={newWorkspace}
							placeholder="留空自动创建在 ~/.veya/projects/{newName || '<id>'}"
							class="mt-1 w-full rounded-lg border border-white/[0.1] bg-white/[0.04] px-3 py-2 text-xs text-terminal-fg placeholder:text-terminal-dim/60 focus:border-sky-500 focus:outline-none"
						/>
						<p class="mt-1 text-[11px] text-terminal-dim">
							主脑将在该目录下初始化 <code class="text-sky-300">.veya-project/</code> 及简报。
						</p>
					</div>

					<div class="flex items-center justify-end gap-2 pt-2">
						<button
							type="button"
							onclick={() => (showCreateModal = false)}
							class="rounded-lg border border-white/[0.08] px-3.5 py-1.5 text-xs text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg"
						>
							取消
						</button>
						<button
							type="submit"
							disabled={creating || !newName.trim()}
							class="flex items-center gap-1.5 rounded-lg bg-sky-500 px-4 py-1.5 text-xs font-medium text-black transition hover:bg-sky-400 disabled:opacity-50"
						>
							{#if creating}
								<RefreshCw class="size-3.5 animate-spin" />
							{/if}
							<span>创建项目</span>
						</button>
					</div>
				</form>
			</div>
		</div>
	{/if}
</div>
