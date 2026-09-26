<script lang="ts">
	import {
		Brain,
		CheckCircle2,
		ChevronDown,
		CircleAlert,
		Code2,
		History,
		ListTodo,
		Loader2,
		Sparkles,
		Square,
		Wrench,
	} from "lucide-svelte";
	import type { ToolStep } from "$lib/chatTypes";

	interface Props {
		steps: ToolStep[];
		streaming?: boolean;
	}

	let { steps, streaming = false }: Props = $props();

	function stepMeta(ev: ToolStep) {
		switch (ev.type) {
			case "tool_call": {
				const tool = String(ev.tool_name ?? "tool");
				if (tool === "memory_search") return { Icon: Brain, tone: "text-violet-300", label: "读取记忆" };
				if (tool === "skill_run") return { Icon: Sparkles, tone: "text-amber-300", label: "运行 Skill" };
				if (tool.includes("file_read")) return { Icon: Code2, tone: "text-cyan-300", label: "读取文件" };
				if (tool.includes("file_search")) return { Icon: Code2, tone: "text-cyan-300", label: "搜索代码" };
				if (tool.includes("file_write") || tool.includes("file_patch")) return { Icon: Code2, tone: "text-cyan-300", label: "更新文件" };
				if (tool.includes("shell_exec")) return { Icon: Wrench, tone: "text-amber-300", label: "执行命令" };
				if (tool.includes("git_")) return { Icon: History, tone: "text-sky-300", label: "检查 Git" };
				if (tool.includes("test_run")) return { Icon: CheckCircle2, tone: "text-emerald-300", label: "运行测试" };
				if (tool.includes("build_run")) return { Icon: CheckCircle2, tone: "text-emerald-300", label: "构建项目" };
				if (tool.includes("hicode")) return { Icon: Code2, tone: "text-emerald-300", label: "执行代码任务" };
				if (tool.includes("workspace")) return { Icon: Wrench, tone: "text-amber-300", label: "检查工作区" };
				return { Icon: Wrench, tone: "text-amber-300", label: "使用工具" };
			}
			case "tool_error":
				return { Icon: CircleAlert, tone: "text-rose-300", label: `${String(ev.tool_name ?? "工具")} 失败` };
			case "hicode_progress": {
				const stage = String(ev.stage ?? "");
				if (stage === "planning") return { Icon: Brain, tone: "text-emerald-300", label: "规划实现" };
				if (stage === "executing") return { Icon: Code2, tone: "text-emerald-300", label: String(ev.tool ?? "执行代码任务") };
				if (stage === "done") return { Icon: CheckCircle2, tone: "text-emerald-300", label: "代码任务完成" };
				return { Icon: Code2, tone: "text-emerald-300", label: "代码任务" };
			}
			case "plan_update":
				return { Icon: ListTodo, tone: "text-sky-300", label: "更新计划" };
			case "finalization.started":
				return { Icon: Loader2, tone: "text-sky-300", label: "整理结果" };
			case "finalization.completed":
				return { Icon: CheckCircle2, tone: "text-emerald-300", label: "结果整理完成" };
			case "memory.committed":
				return { Icon: Brain, tone: "text-violet-300", label: "保存记忆" };
			case "memory.corrected":
				return { Icon: History, tone: "text-sky-300", label: "更新记忆" };
			case "skill.created":
				return { Icon: Sparkles, tone: "text-amber-300", label: "Skill 已确认" };
			case "continuity.resumed":
				return { Icon: History, tone: "text-sky-300", label: "恢复之前的工作" };
			case "fanin.completed":
				return { Icon: ListTodo, tone: "text-violet-300", label: "汇总并行结果" };
			case "delegate.started":
				return { Icon: Loader2, tone: "text-amber-300", label: "启动并行工作" };
			case "delegate.completed":
				return { Icon: CheckCircle2, tone: "text-emerald-300", label: "并行工作完成" };
			case "delegate.partial":
				return { Icon: CircleAlert, tone: "text-yellow-300", label: "并行工作部分完成" };
			case "delegate.failed":
				return { Icon: CircleAlert, tone: "text-rose-300", label: "并行工作失败" };
			case "delegate.cancelled":
				return { Icon: Square, tone: "text-white/50", label: "并行工作已取消" };
			case "artifact.created":
			case "artifact.verified":
			case "artifact.partial":
				return { Icon: Code2, tone: "text-cyan-300", label: "更新产物" };
			default:
				return null;
		}
	}

	function stepDetail(ev: ToolStep): string {
		if (ev.type === "hicode_progress" && typeof ev.detail === "string") return ev.detail.slice(0, 240);
		if (ev.type === "plan_update" && Array.isArray(ev.todos)) {
			const marks: Record<string, string> = { done: "✓", in_progress: "→", blocked: "!", open: "·" };
			return (ev.todos as { id?: string; title?: string; status?: string }[])
				.map((todo) => `${marks[todo.status ?? "open"] ?? "·"} ${String(todo.title ?? todo.id ?? "")}`)
				.join("\n")
				.slice(0, 600);
		}
		if (ev.type === "tool_call" && ev.tool_args && typeof ev.tool_args === "object") {
			const args = ev.tool_args as Record<string, unknown>;
			const fields: Array<[string, string]> = [
				["path", "文件"],
				["query", "查询"],
				["pattern", "搜索"],
				["url", "页面"],
				["task", "任务"],
				["target", "目标"],
			];
			return fields
				.filter(([key]) => args[key] != null && String(args[key]).trim())
				.slice(0, 2)
				.map(([key, label]) => `${label}：${String(args[key]).slice(0, 180)}`)
				.join("\n");
		}
		if (ev.type === "finalization.started") {
			return `保留 ${String(ev.reserve_s ?? "-")}s 用于收尾`;
		}
		if (ev.type === "fanin.completed") {
			return `${String(ev.complete_count ?? 0)} 完成 · ${String(ev.partial_count ?? 0)} 部分完成 · ${String(ev.failed_count ?? 0)} 失败`;
		}
		if (ev.type.startsWith("delegate.")) return String(ev.stop_reason ?? ev.error ?? "").slice(0, 240);
		if (typeof ev.error === "string") return ev.error.slice(0, 240);
		return "";
	}

	function isDedicated(step: ToolStep): boolean {
		return ["permission_request", "agent_question", "project_understand_ask"].includes(step.type);
	}

	const processSteps = $derived(steps.filter((step) => !isDedicated(step) && stepMeta(step) !== null));
	const errors = $derived(
		processSteps.filter(
			(step) =>
				step.type === "tool_error" ||
				step.type === "delegate.failed" ||
				typeof step.error === "string",
		).length,
	);
</script>

{#if processSteps.length > 0}
	<details class="group overflow-hidden rounded-xl border border-white/[0.07] bg-white/[0.015]" open={streaming}>
		<summary class="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-sm text-terminal-dim hover:bg-white/[0.025] hover:text-terminal-fg">
			{#if streaming}
				<Loader2 class="size-3.5 animate-spin text-sky-300" />
			{:else}
				<CheckCircle2 class="size-3.5 text-emerald-300" />
			{/if}
			<span class="font-medium text-terminal-fg">工作过程</span>
			<span class="text-xs">{processSteps.length} 步</span>
			{#if errors > 0}
				<span class="rounded-full bg-rose-500/10 px-1.5 py-0.5 text-[11px] text-rose-300">{errors} 个异常</span>
			{/if}
			<span class="flex-1"></span>
			<ChevronDown class="size-4 transition-transform group-open:rotate-180" />
		</summary>

		<div class="space-y-1 border-t border-white/[0.06] px-2 py-2">
			{#each processSteps as step, index (index)}
				{@const meta = stepMeta(step)}
				{#if meta}
					{@const Icon = meta.Icon}
					<details class="group/step rounded-lg px-2 py-1.5 hover:bg-white/[0.025]">
						<summary class="flex cursor-pointer list-none items-center gap-2 text-xs">
							<Icon class="size-3.5 shrink-0 {meta.tone} {step.type === 'delegate.started' || step.type === 'finalization.started' ? 'animate-pulse' : ''}" />
							<span class="min-w-0 flex-1 truncate text-terminal-fg/85">{meta.label}</span>
							{#if stepDetail(step)}
								<ChevronDown class="size-3.5 shrink-0 text-terminal-dim transition-transform group-open/step:rotate-180" />
							{/if}
						</summary>
						{#if stepDetail(step)}
							<pre class="mt-2 max-h-36 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-black/20 p-2 text-[11px] leading-5 text-terminal-dim">{stepDetail(step)}</pre>
						{/if}
					</details>
				{/if}
			{/each}
		</div>
	</details>
{/if}
