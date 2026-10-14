<script lang="ts">
	/**
	 * /projects/[id] — Project Brain detail & control center (W16 Project Brain)
	 *
	 * Left pane: Dedicated project chat (GoalRun dispatch, steering, status queries)
	 * Right pane:
	 *   - Brief (PROJECT_STATE.md sections: Goal, Files, Rules, Tasks, Decisions)
	 *   - Active Task cards (Verifier evidence, repairs made, patrol actions)
	 */
	import { onDestroy, onMount, untrack } from "svelte";
	import {
		AlertCircle,
		ArrowLeft,
		Bot,
		CheckCircle2,
		Clock,
		Code,
		Edit3,
		Eye,
		FileText,
		ListTodo,
		MessageSquare,
		Pause,
		Play,
		RefreshCw,
		Save,
		Send,
		ShieldCheck,
		Terminal,
		User,
		XCircle,
	} from "lucide-svelte";
	import MarkdownBlock from "$lib/components/MarkdownBlock.svelte";
	import {
		getProject,
		getProjectBrief,
		getProjectMessages,
		getProjectTasks,
		postProjectMessage,
		taskAction,
		updateProjectBrief,
		type BriefView,
		type ProjectTaskView,
		type ProjectView,
	} from "$lib/projects/api";
	import type { BotMessage } from "$lib/team/api";

	let { data }: { data: { projectId: string } } = $props();
	const projectId = untrack(() => data.projectId);

	let project = $state<ProjectView | null>(null);
	let brief = $state<BriefView | null>(null);
	let briefMarkdown = $state("");
	let editedMarkdown = $state("");
	let tasks = $state<ProjectTaskView[]>([]);
	let messages = $state<BotMessage[]>([]);

	let loading = $state(true);
	let error = $state("");
	let activeTab = $state<"brief" | "tasks">("brief");
	let editMode = $state(false);

	let inputText = $state("");
	let sending = $state(false);
	let savingBrief = $state(false);
	let saveBriefMsg = $state("");
	let actionInProgress = $state<Record<string, boolean>>({});

	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let chatContainerEl: HTMLDivElement | null = null;

	function scrollToBottom(): void {
		if (chatContainerEl) {
			chatContainerEl.scrollTop = chatContainerEl.scrollHeight;
		}
	}

	async function loadAll(): Promise<void> {
		error = "";
		try {
			const [p, bData, mList, tList] = await Promise.all([
				getProject(projectId),
				getProjectBrief(projectId),
				getProjectMessages(projectId),
				getProjectTasks(projectId),
			]);
			project = p;
			brief = bData.brief;
			briefMarkdown = bData.markdown;
			if (!editMode) {
				editedMarkdown = bData.markdown;
			}
			messages = mList;
			tasks = tList;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loading = false;
		}
	}

	async function pollUpdates(): Promise<void> {
		if (loading || editMode) return;
		try {
			const [mList, tList, bData] = await Promise.all([
				getProjectMessages(projectId),
				getProjectTasks(projectId),
				getProjectBrief(projectId),
			]);
			const hadNewMsg = mList.length > messages.length;
			messages = mList;
			tasks = tList;
			brief = bData.brief;
			briefMarkdown = bData.markdown;
			if (hadNewMsg) {
				setTimeout(scrollToBottom, 50);
			}
		} catch {
			// Ignore transient poll failures
		}
	}

	async function handleSendMessage(e?: Event): Promise<void> {
		if (e) e.preventDefault();
		const text = inputText.trim();
		if (!text || sending) return;

		sending = true;
		inputText = "";
		// Optimistically append user message
		const tempMsg: BotMessage = {
			id: `temp-${Date.now()}`,
			role: "user",
			text,
			ts: Math.floor(Date.now() / 1000),
		};
		messages = [...messages, tempMsg];
		setTimeout(scrollToBottom, 20);

		try {
			await postProjectMessage(projectId, text, { async: true });
			// Immediate poll for response
			setTimeout(async () => {
				const mList = await getProjectMessages(projectId);
				messages = mList;
				scrollToBottom();
			}, 1000);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			sending = false;
		}
	}

	async function handleSaveBrief(): Promise<void> {
		savingBrief = true;
		saveBriefMsg = "";
		try {
			const res = await updateProjectBrief(projectId, {
				markdown: editedMarkdown,
			});
			brief = res.brief;
			briefMarkdown = res.markdown;
			editMode = false;
			saveBriefMsg = "简报已更新并落盘 PROJECT_STATE.md";
			setTimeout(() => {
				saveBriefMsg = "";
			}, 3000);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			savingBrief = false;
		}
	}

	async function handleTaskAction(taskId: string, action: "cancel" | "pause" | "resume"): Promise<void> {
		actionInProgress[taskId] = true;
		try {
			await taskAction(projectId, taskId, action);
			tasks = await getProjectTasks(projectId);
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			actionInProgress[taskId] = false;
		}
	}

	function handleKeydown(e: KeyboardEvent): void {
		if (e.key === "Enter" && !e.shiftKey) {
			e.preventDefault();
			void handleSendMessage();
		}
	}

	onMount(() => {
		void loadAll().then(() => {
			setTimeout(scrollToBottom, 100);
		});
		pollTimer = setInterval(() => {
			void pollUpdates();
		}, 4000);
	});

	onDestroy(() => {
		if (pollTimer) clearInterval(pollTimer);
	});
</script>

<svelte:head>
	<title>{project?.name ? `${project.name} | 项目主脑` : "项目主脑"}</title>
</svelte:head>

<div class="flex h-screen flex-col bg-terminal-bg text-terminal-fg">
	<!-- Top Bar -->
	<header class="flex h-14 shrink-0 items-center justify-between border-b border-white/[0.08] bg-white/[0.02] px-4 backdrop-blur md:px-6">
		<div class="flex items-center gap-3 min-w-0">
			<a
				href="/projects"
				class="flex items-center gap-1.5 rounded-lg border border-white/[0.08] px-2.5 py-1 text-xs text-terminal-dim transition hover:bg-white/[0.05] hover:text-terminal-fg"
			>
				<ArrowLeft class="size-3.5" />
				<span class="hidden sm:inline">项目列表</span>
			</a>
			<span class="text-white/20">/</span>
			{#if project}
				<div class="flex items-center gap-2.5 min-w-0">
					<div
						class="flex size-7 shrink-0 items-center justify-center rounded-lg text-xs font-semibold text-white shadow"
						style="background-color: {project.color || '#4A90D9'}"
					>
						{project.icon || project.name.slice(0, 1).toUpperCase()}
					</div>
					<div class="min-w-0">
						<h1 class="truncate text-sm font-semibold text-terminal-fg">{project.name}</h1>
						<p class="truncate text-[10px] text-terminal-dim">{project.workspace_path}</p>
					</div>
				</div>
			{:else}
				<div class="h-5 w-32 animate-pulse rounded bg-white/[0.05]"></div>
			{/if}
		</div>

		<div class="flex items-center gap-2">
			<button
				type="button"
				onclick={() => void loadAll()}
				class="flex items-center gap-1.5 rounded-lg border border-white/[0.08] bg-white/[0.04] px-2.5 py-1 text-xs text-terminal-dim transition hover:bg-white/[0.08] hover:text-terminal-fg"
			>
				<RefreshCw class="size-3.5 {loading ? 'animate-spin' : ''}" />
				<span class="hidden sm:inline">刷新</span>
			</button>
		</div>
	</header>

	<!-- Error Bar -->
	{#if error}
		<div class="flex items-center justify-between gap-3 border-b border-amber-500/20 bg-amber-500/10 px-4 py-2 text-xs text-amber-200">
			<div class="flex items-center gap-2 truncate">
				<AlertCircle class="size-4 shrink-0 text-amber-400" />
				<span class="truncate">{error}</span>
			</div>
			<button type="button" onclick={() => (error = "")} class="text-amber-300 hover:text-white">
				✕
			</button>
		</div>
	{/if}

	<!-- Dual-Pane Body -->
	<div class="flex min-h-0 flex-1 flex-col lg:flex-row">
		<!-- Left Pane: Project Chat -->
		<section class="flex flex-1 flex-col border-b border-white/[0.08] lg:border-b-0 lg:border-r">
			<!-- Chat Header -->
			<div class="flex h-11 shrink-0 items-center justify-between border-b border-white/[0.06] bg-white/[0.01] px-4">
				<div class="flex items-center gap-2 text-xs font-medium text-white/80">
					<Bot class="size-4 text-sky-400" />
					<span>主脑对话 ({project?.bot_id || `proj-${projectId}`})</span>
				</div>
				<span class="text-[11px] text-terminal-dim">长任务自动派工与持续监工</span>
			</div>

			<!-- Messages Scroll Container -->
			<div
				bind:this={chatContainerEl}
				class="min-h-0 flex-1 space-y-4 overflow-y-auto p-4"
			>
				{#if messages.length === 0}
					<div class="flex h-full flex-col items-center justify-center text-center">
						<div class="flex size-11 items-center justify-center rounded-xl bg-sky-500/10 text-sky-400">
							<MessageSquare class="size-5" />
						</div>
						<h3 class="mt-3 text-xs font-medium text-white/90">向主脑描述您的目标</h3>
						<p class="mt-1 max-w-sm text-[11px] leading-relaxed text-terminal-dim">
							例如: 「帮我把单元测试补齐并修复覆盖率，每 5 分钟汇报一次直到完成」<br />
							主脑将拆解为 GoalRun 任务，在右侧生成任务卡片并按规则巡检。
						</p>
					</div>
				{:else}
					{#each messages as msg (msg.id)}
						<div class="flex gap-3 {msg.role === 'user' ? 'justify-end' : 'justify-start'}">
							{#if msg.role !== 'user'}
								<div class="flex size-7 shrink-0 items-center justify-center rounded-lg bg-sky-500/20 text-sky-400">
									<Bot class="size-4" />
								</div>
							{/if}

							<div
								class="max-w-[85%] rounded-2xl px-4 py-2.5 text-xs {msg.role === 'user'
									? 'bg-sky-500 text-black font-medium'
									: 'border border-white/[0.08] bg-white/[0.03] text-terminal-fg'}"
							>
								{#if msg.role === 'user'}
									<p class="whitespace-pre-wrap">{msg.text}</p>
								{:else}
									<div class="prose prose-invert prose-xs max-w-none">
										<MarkdownBlock content={msg.text} />
									</div>
								{/if}
								{#if msg.ts}
									<div class="mt-1 text-right text-[10px] {msg.role === 'user' ? 'text-black/60' : 'text-terminal-dim'}">
										{new Date(msg.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
									</div>
								{/if}
							</div>

							{#if msg.role === 'user'}
								<div class="flex size-7 shrink-0 items-center justify-center rounded-lg bg-white/10 text-white">
									<User class="size-4" />
								</div>
							{/if}
						</div>
					{/each}
				{/if}
			</div>

			<!-- Chat Input Bar -->
			<div class="shrink-0 border-t border-white/[0.08] bg-white/[0.02] p-3">
				<form onsubmit={handleSendMessage} class="flex items-end gap-2">
					<textarea
						bind:value={inputText}
						onkeydown={handleKeydown}
						rows="2"
						placeholder="对项目主脑下达任务、问询进度或中途修正需求 (Enter 发送)..."
						class="w-full resize-none rounded-xl border border-white/[0.1] bg-white/[0.04] p-2.5 text-xs text-terminal-fg placeholder:text-terminal-dim/60 focus:border-sky-500 focus:outline-none"
					></textarea>
					<button
						type="submit"
						disabled={sending || !inputText.trim()}
						class="flex size-9 shrink-0 items-center justify-center rounded-xl bg-sky-500 text-black transition hover:bg-sky-400 disabled:opacity-40"
					>
						{#if sending}
							<RefreshCw class="size-4 animate-spin" />
						{:else}
							<Send class="size-4" />
						{/if}
					</button>
				</form>
				<p class="mt-1.5 text-[10px] text-terminal-dim">
					Shift + Enter 换行。长任务由主脑自动拆解、派发工人并持续核验报告。
				</p>
			</div>
		</section>

		<!-- Right Pane: Brief & Tasks -->
		<section class="flex flex-1 flex-col bg-white/[0.005]">
			<!-- Tabs Header -->
			<div class="flex h-11 shrink-0 items-center justify-between border-b border-white/[0.06] bg-white/[0.01] px-4">
				<div class="flex items-center gap-2">
					<button
						type="button"
						onclick={() => (activeTab = "brief")}
						class="flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition {activeTab === 'brief'
							? 'border-sky-400 text-sky-300'
							: 'border-transparent text-terminal-dim hover:text-terminal-fg'}"
					>
						<FileText class="size-3.5" />
						<span>项目简报 (Brief)</span>
					</button>
					<button
						type="button"
						onclick={() => (activeTab = "tasks")}
						class="flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium transition {activeTab === 'tasks'
							? 'border-sky-400 text-sky-300'
							: 'border-transparent text-terminal-dim hover:text-terminal-fg'}"
					>
						<ListTodo class="size-3.5" />
						<span>任务卡片 ({tasks.length})</span>
					</button>
				</div>

				{#if activeTab === 'brief'}
					<div class="flex items-center gap-2">
						{#if saveBriefMsg}
							<span class="text-[11px] text-emerald-400">{saveBriefMsg}</span>
						{/if}
						<button
							type="button"
							onclick={() => {
								editMode = !editMode;
								if (editMode) editedMarkdown = briefMarkdown;
							}}
							class="flex items-center gap-1 rounded border border-white/[0.08] bg-white/[0.04] px-2 py-1 text-[11px] text-terminal-dim hover:text-terminal-fg"
						>
							{#if editMode}
								<Eye class="size-3" />
								<span>预览</span>
							{:else}
								<Edit3 class="size-3" />
								<span>编辑</span>
							{/if}
						</button>
					</div>
				{/if}
			</div>

			<!-- Tab 1: Brief Content -->
			{#if activeTab === 'brief'}
				<div class="min-h-0 flex-1 overflow-y-auto p-4">
					{#if editMode}
						<div class="flex h-full flex-col gap-3">
							<textarea
								bind:value={editedMarkdown}
								rows="18"
								class="w-full flex-1 rounded-xl border border-white/[0.1] bg-black/40 p-3 font-mono text-xs text-terminal-fg focus:border-sky-500 focus:outline-none"
								placeholder="编辑 PROJECT_STATE.md..."
							></textarea>
							<div class="flex items-center justify-end gap-2">
								<button
									type="button"
									onclick={() => {
										editMode = false;
										editedMarkdown = briefMarkdown;
									}}
									class="rounded-lg border border-white/[0.08] px-3 py-1.5 text-xs text-terminal-dim hover:text-terminal-fg"
								>
									取消
								</button>
								<button
									type="button"
									onclick={handleSaveBrief}
									disabled={savingBrief}
									class="flex items-center gap-1.5 rounded-lg bg-sky-500 px-4 py-1.5 text-xs font-medium text-black hover:bg-sky-400 disabled:opacity-50"
								>
									{#if savingBrief}
										<RefreshCw class="size-3.5 animate-spin" />
									{:else}
										<Save class="size-3.5" />
									{/if}
									<span>保存简报</span>
								</button>
							</div>
						</div>
					{:else}
						<!-- Rendered Brief View -->
						<div class="space-y-5">
							<!-- 1. Goal & Scope -->
							<div class="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
								<h3 class="flex items-center gap-2 text-xs font-semibold text-white/90">
									<Terminal class="size-3.5 text-sky-400" />
									<span>1. 目标与范围 (Goal & Scope)</span>
								</h3>
								<p class="mt-2 text-xs leading-relaxed text-white/80">
									{brief?.goal_scope || "暂无目标描述。可在左侧对话更新，或点击上方编辑简报。"}
								</p>
							</div>

							<!-- 2. Key Files -->
							<div class="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
								<h3 class="flex items-center gap-2 text-xs font-semibold text-white/90">
									<Code class="size-3.5 text-sky-400" />
									<span>2. 重要文件 (Key Files)</span>
								</h3>
								{#if brief?.key_files && brief.key_files.length > 0}
									<ul class="mt-2 space-y-1">
										{#each brief.key_files as file}
											<li class="flex items-center gap-2 font-mono text-xs text-sky-300/90">
												<span class="text-white/40">•</span>
												<span>{file}</span>
											</li>
										{/each}
									</ul>
								{:else}
									<p class="mt-2 text-xs text-terminal-dim">暂无登记的关键文件。</p>
								{/if}
							</div>

							<!-- 3. Rules -->
							<div class="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
								<h3 class="flex items-center gap-2 text-xs font-semibold text-white/90">
									<ShieldCheck class="size-3.5 text-sky-400" />
									<span>3. 巡检规则 (Rules: 当 X 就做 Y)</span>
								</h3>
								{#if brief?.rules && brief.rules.length > 0}
									<div class="mt-2 space-y-2">
										{#each brief.rules as rule}
											<div class="flex items-start justify-between gap-3 rounded-lg border border-white/[0.04] bg-white/[0.02] p-2.5 text-xs">
												<div>
													<span class="font-medium text-amber-300">当</span>
													<span class="text-white/90"> {rule.when} </span>
													<span class="font-medium text-sky-300">→ 则执行</span>
													<span class="text-white/90"> {rule.do}</span>
												</div>
												<span class="shrink-0 rounded px-1.5 py-0.5 text-[10px] {rule.approval === 'auto' ? 'bg-emerald-500/20 text-emerald-300' : 'bg-amber-500/20 text-amber-300'}">
													{rule.approval === 'auto' ? '免审批' : '需审批'}
												</span>
											</div>
										{/each}
									</div>
								{:else}
									<p class="mt-2 text-xs text-terminal-dim">暂未设定巡检规则。规则格式示例: <code>- when: 测试失败 → do: 自动重试 [approval: ask]</code></p>
								{/if}
							</div>

							<!-- 4. Active Tasks -->
							<div class="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
								<h3 class="flex items-center gap-2 text-xs font-semibold text-white/90">
									<ListTodo class="size-3.5 text-sky-400" />
									<span>4. 进行中任务与阻塞</span>
								</h3>
								<p class="mt-2 text-xs leading-relaxed text-white/80">
									{brief?.active_tasks || "暂无进行中任务与阻塞。"}
								</p>
							</div>

							<!-- 5. Conventions & Decisions -->
							<div class="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
								<h3 class="flex items-center gap-2 text-xs font-semibold text-white/90">
									<FileText class="size-3.5 text-sky-400" />
									<span>5. 约定与决策 (Conventions & Decisions)</span>
								</h3>
								<p class="mt-2 text-xs leading-relaxed text-white/80">
									{brief?.conventions || "暂无特别约定。"}
								</p>
							</div>
						</div>
					{/if}
				</div>
			{:else}
				<!-- Tab 2: Task Cards -->
				<div class="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
					{#if tasks.length === 0}
						<div class="flex h-full flex-col items-center justify-center text-center">
							<ListTodo class="size-8 text-terminal-dim/50" />
							<p class="mt-2 text-xs text-terminal-dim">当前项目暂无任务卡片</p>
							<p class="mt-1 text-[11px] text-terminal-dim/70">
								在左侧聊天中派发长任务后，主脑将在此呈现独立核验与进度。
							</p>
						</div>
					{:else}
						{#each tasks as task (task.task_id)}
							<div class="rounded-xl border border-white/[0.08] bg-white/[0.02] p-4 transition hover:border-white/[0.12]">
								<!-- Task Card Header -->
								<div class="flex items-start justify-between gap-3">
									<div>
										<div class="flex items-center gap-2">
											<span class="rounded px-2 py-0.5 text-[10px] font-medium {task.status === 'done' ? 'bg-emerald-500/20 text-emerald-300' : task.status === 'running' ? 'bg-sky-500/20 text-sky-300 animate-pulse' : task.status === 'failed' ? 'bg-red-500/20 text-red-300' : task.status === 'cancelled' ? 'bg-zinc-500/20 text-zinc-400' : 'bg-purple-500/20 text-purple-300'}">
												{task.status.toUpperCase()}
											</span>
											<span class="font-mono text-[11px] text-terminal-dim">{task.task_id}</span>
										</div>
										<h4 class="mt-1.5 text-xs font-semibold text-terminal-fg">{task.objective}</h4>
									</div>

									<!-- Action Controls -->
									<div class="flex items-center gap-1.5">
										{#if task.status === 'running'}
											<button
												type="button"
												onclick={() => handleTaskAction(task.task_id, "pause")}
												disabled={actionInProgress[task.task_id]}
												title="暂停巡检"
												class="rounded p-1 text-terminal-dim hover:bg-white/[0.05] hover:text-terminal-fg"
											>
												<Pause class="size-3.5" />
											</button>
										{:else if task.status === 'accepted'}
											<button
												type="button"
												onclick={() => handleTaskAction(task.task_id, "resume")}
												disabled={actionInProgress[task.task_id]}
												title="开始执行"
												class="rounded p-1 text-terminal-dim hover:bg-white/[0.05] hover:text-terminal-fg"
											>
												<Play class="size-3.5" />
											</button>
										{/if}

										{#if task.status !== 'done' && task.status !== 'cancelled'}
											<button
												type="button"
												onclick={() => handleTaskAction(task.task_id, "cancel")}
												disabled={actionInProgress[task.task_id]}
												title="取消任务"
												class="rounded p-1 text-red-400 hover:bg-red-500/10"
											>
												<XCircle class="size-3.5" />
											</button>
										{/if}
									</div>
								</div>

								<!-- Verifier Evidence -->
								{#if task.verifier}
									<div class="mt-3 rounded-lg border border-white/[0.04] bg-white/[0.01] p-2.5 text-[11px]">
										<div class="flex items-center gap-1.5 font-medium {task.verifier.usable ? 'text-emerald-300' : 'text-amber-300'}">
											{#if task.verifier.usable}
												<CheckCircle2 class="size-3.5" />
												<span>核验证据有效 (Usable)</span>
											{:else}
												<AlertCircle class="size-3.5" />
												<span>核验证据尚不可用</span>
											{/if}
										</div>
										{#if task.verifier.evidence}
											<p class="mt-1 font-mono text-[10px] text-white/70 line-clamp-3">
												{task.verifier.evidence}
											</p>
										{/if}
									</div>
								{/if}

								<!-- Fix notes / Repairs -->
								<div class="mt-3 flex flex-wrap items-center gap-3 border-t border-white/[0.04] pt-2.5 text-[10px] text-terminal-dim">
									<div class="flex items-center gap-1">
										<Clock class="size-3 text-terminal-dim/70" />
										<span>报告轮次: {task.report_seq ?? task.last_report_seq ?? 0}</span>
									</div>
									<div>
										<span>修复尝试: {task.repairs_made ?? 0} / {task.max_repairs ?? 3}</span>
									</div>
									{#if task.fix_notes && task.fix_notes.length > 0}
										<div class="text-amber-300">
											<span>修正记录: {task.fix_notes.length} 条</span>
										</div>
									{/if}
								</div>
							</div>
						{/each}
					{/if}
				</div>
			{/if}
		</section>
	</div>
</div>
