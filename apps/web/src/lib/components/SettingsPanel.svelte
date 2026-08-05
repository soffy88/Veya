<script lang="ts">
	/**
	 * SettingsPanel — slide-over drawer for model/key config. Plugins and automation
	 * moved to persistent sidebar entries (see +page.svelte) since they're full views,
	 * not one-off settings.
	 */
	import { X } from "lucide-svelte";
	import ApiKeySettings from "./ApiKeySettings.svelte";

	interface Props {
		open: boolean;
		onClose: () => void;
	}
	let { open, onClose }: Props = $props();
</script>

{#if open}
	<div class="fixed inset-0 z-50 flex justify-end">
		<button type="button" class="absolute inset-0 bg-black/50" onclick={onClose} aria-label="关闭设置"></button>

		<aside class="relative flex h-full w-full max-w-md flex-col border-l border-terminal-edge bg-terminal-bg shadow-2xl">
			<div class="flex shrink-0 items-center justify-between border-b border-terminal-edge px-5 py-4">
				<h2 class="text-base font-semibold text-terminal-fg">模型设置</h2>
				<button type="button" onclick={onClose} class="flex size-8 items-center justify-center rounded-lg text-terminal-dim transition hover:bg-white/5 hover:text-terminal-fg" aria-label="关闭">
					<X class="size-4" />
				</button>
			</div>

			<div class="flex-1 overflow-y-auto p-5">
				<ApiKeySettings />
			</div>
		</aside>
	</div>
{/if}
