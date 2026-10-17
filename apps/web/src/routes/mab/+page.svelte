<script lang="ts">
	import { onMount, onDestroy } from "svelte";
	import { api, API_BASE } from "$lib/api";
	import { Shield, Plus, Trash2, RefreshCw, Power, PowerOff, Globe, Lock, Play } from "lucide-svelte";

	interface Profile {
		id: string;
		name: string;
		site: string;
		account_label: string;
		status: string;
		created_at: number;
		last_used: number;
	}

	let profiles = $state<Profile[]>([]);
	let loading = $state(true);
	let error = $state("");
	let selectedProfile = $state<Profile | null>(null);

	let newName = $state("");
	let newSite = $state("");
	let newLabel = $state("");
	let creating = $state(false);
	let featureEnabled = $state(false);
	let streamStatus = $state("Disconnected");
	let streamError = $state("");
	let frameData = $state("");
	let streamWidth = $state(1280);
	let streamHeight = $state(800);
	let textToSend = $state("");
	let streamSocket: WebSocket | null = null;

	function stopStream() {
		const socket = streamSocket;
		streamSocket = null;
		if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "Stream closed");
		streamStatus = "Disconnected";
		frameData = "";
	}

	function sendStreamMessage(message: Record<string, unknown>) {
		if (streamSocket && streamSocket.readyState === WebSocket.OPEN) streamSocket.send(JSON.stringify(message));
	}

	function chooseProfile(profile: Profile) {
		stopStream();
		selectedProfile = profile;
		streamError = "";
	}

	function sendMouse(event: MouseEvent, eventType: string) {
		const viewport = event.currentTarget as HTMLElement;
		const image = viewport.querySelector("img");
		const rect = (image || viewport).getBoundingClientRect();
		if (!rect.width || !rect.height) return;
		const buttons = ["left", "middle", "right", "back", "forward"];
		const message: Record<string, unknown> = {
			type: "mouse",
			eventType,
			x: Math.round(((event.clientX - rect.left) / rect.width) * streamWidth),
			y: Math.round(((event.clientY - rect.top) / rect.height) * streamHeight),
			button: buttons[event.button] || "none",
			clickCount: Math.max(1, event.detail || 1)
		};
		if (eventType === "mouseWheel") {
			message.deltaX = (event as WheelEvent).deltaX;
			message.deltaY = (event as WheelEvent).deltaY;
		}
		sendStreamMessage(message);
	}

	function sendKey(event: KeyboardEvent, eventType: "keyDown" | "keyUp") {
		event.preventDefault();
		const modifiers = (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
		sendStreamMessage({ type: "keyboard", eventType, key: event.key, code: event.code, windowsVirtualKeyCode: event.keyCode, modifiers });
	}

	function sendText() {
		if (!textToSend || streamSocket?.readyState !== WebSocket.OPEN) return;
		sendStreamMessage({ type: "text", text: textToSend });
		textToSend = "";
	}

	async function loadProfiles() {
		loading = true;
		error = "";
		try {
			const res = await api("gateway", "api/v1/mab/profiles");
			if (res.status === 404) { featureEnabled = false; stopStream(); window.location.replace("/"); return; }
			featureEnabled = true;
			if (res.ok && res.data) {
				profiles = (res.data as any).profiles || [];
				if (profiles.length > 0 && !selectedProfile) {
					selectedProfile = profiles[0];
				}
			} else {
				error = "MAB is disabled or unauthorized";
			}
		} catch (e: any) {
			error = e.message || "Failed to load profiles";
		} finally {
			loading = false;
		}
	}

	async function createProfile(e: Event) {
		e.preventDefault();
		if (!newName || !newSite) return;
		creating = true;
		try {
			const res = await api("gateway", "api/v1/mab/profiles", {
				method: "POST",
				body: {
					name: newName,
					site: newSite,
					account_label: newLabel || "default"
				}
			});
			if (res.ok) {
				newName = "";
				newSite = "";
				newLabel = "";
				await loadProfiles();
			}
		} catch (err: any) {
			error = err.message || "Creation failed";
		} finally {
			creating = false;
		}
	}

	async function toggleStatus(profile: Profile) {
		const action = profile.status === "active" ? "revoke" : "enable";
		try {
			const res = await api("gateway", `api/v1/mab/profiles/${profile.id}/${action}`, {
				method: "POST"
			});
			if (res.ok) {
				await loadProfiles();
			}
		} catch (err: any) {
			error = err.message;
		}
	}

	async function deleteProfile(profile: Profile) {
		if (!confirm(`Delete profile '${profile.name}'? This removes isolated local storage permanently.`)) return;
		try {
			const res = await api("gateway", `api/v1/mab/profiles/${profile.id}`, {
				method: "DELETE",
				query: { confirm: true }
			});
			if (res.ok) {
				if (selectedProfile?.id === profile.id) {
					selectedProfile = null;
				}
				await loadProfiles();
			}
		} catch (err: any) {
			error = err.message;
		}
	}

	async function requestStreamTicket(profile: Profile) {
		stopStream();
		streamError = "";
		streamStatus = "Requesting ticket";
		try {
			const response = await api("gateway", "api/v1/mab/profiles/" + profile.id + "/stream-ticket", { method: "POST" });
			if (response.status === 404) {
				featureEnabled = false;
				window.location.replace("/");
				return;
			}
			const data = response.data as { ticket?: string };
			if (!response.ok || !data?.ticket) throw new Error("Stream ticket request failed (" + response.status + ")");
			const wsUrl = new URL("/api/v1/mab/profiles/" + encodeURIComponent(profile.id) + "/stream", API_BASE || window.location.origin);
			wsUrl.protocol = wsUrl.protocol === "https:" ? "wss:" : "ws:";
			streamStatus = "Connecting";
			const socket = new WebSocket(wsUrl.toString(), ["ticket." + data.ticket]);
			streamSocket = socket;
			socket.onopen = () => { if (streamSocket === socket) streamStatus = "Connected"; };
			socket.onmessage = (event) => {
				if (streamSocket !== socket) return;
				try {
					const message = JSON.parse(String(event.data)) as { type?: string; data?: string; metadata?: { width?: number; height?: number }; error?: string; message?: string };
					if (message.type === "frame" && message.data) {
						frameData = message.data;
						streamWidth = message.metadata?.width || 1280;
						streamHeight = message.metadata?.height || 800;
					} else if (message.type === "error") {
						streamError = message.error || message.message || "Browser input was rejected";
					}
				} catch { streamError = "Invalid stream message"; }
			};
			socket.onerror = () => { if (streamSocket === socket) streamError = "WebSocket stream failed"; };
			socket.onclose = (event) => {
				if (streamSocket !== socket) return;
				streamSocket = null;
				streamStatus = "Disconnected";
				if (event.code !== 1000) streamError = "Stream closed (" + event.code + ")";
			};
		} catch (err: any) {
			streamStatus = "Disconnected";
			streamError = err?.message || "Failed to obtain stream ticket";
		}
	}

	onDestroy(() => stopStream());

</script>

{#if featureEnabled}
<div class="flex h-full w-full flex-col bg-slate-950 text-slate-100 p-6">
	<div class="flex items-center justify-between border-b border-slate-800 pb-4 mb-6">
		<div class="flex items-center gap-3">
			<a href="/" class="text-xs text-slate-400 hover:text-slate-200">← 返回控制台</a>
			<span class="opacity-20 text-xs">/</span>
			<div>
				<h1 class="text-2xl font-bold flex items-center gap-2">
					<Shield class="h-6 w-6 text-indigo-400" />
					Multi-Account Browser (MAB)
				</h1>
				<p class="text-sm text-slate-400">Isolated session profiles with 0700 permissions and automatic tool authorization.</p>
			</div>
		</div>
		<button
			onclick={loadProfiles}
			class="flex items-center gap-1.5 px-3 py-1.5 bg-slate-800 hover:bg-slate-700 rounded-lg text-sm text-slate-200 transition"
		>
			<RefreshCw class="h-4 w-4" />
			Refresh
		</button>
	</div>

	{#if error}
		<div class="mb-4 rounded-lg bg-red-950/50 border border-red-800 p-3 text-sm text-red-300">
			{error} (Note: VEYA_MAB_BROWSER must be enabled on the backend)
		</div>
	{/if}

	<div class="grid grid-cols-1 lg:grid-cols-3 gap-6 flex-1 overflow-hidden">
		<!-- Left: Profiles List & Creator -->
		<div class="flex flex-col gap-6 overflow-y-auto pr-2">
			<!-- Create Form -->
			<form onsubmit={createProfile} class="bg-slate-900 border border-slate-800 rounded-xl p-4 flex flex-col gap-3">
				<h2 class="text-sm font-semibold text-slate-300">New Account Profile</h2>
				<input
					type="text"
					placeholder="Profile Name (e.g. Work Admin)"
					bind:value={newName}
					class="bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
				/>
				<input
					type="text"
					placeholder="Target Site (e.g. https://github.com)"
					bind:value={newSite}
					class="bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
				/>
				<input
					type="text"
					placeholder="Account Tag / Label"
					bind:value={newLabel}
					class="bg-slate-950 border border-slate-800 rounded-lg px-3 py-2 text-sm text-slate-100 focus:outline-none focus:border-indigo-500"
				/>
				<button
					type="submit"
					disabled={creating || !newName || !newSite}
					class="flex items-center justify-center gap-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-lg py-2 text-sm font-medium transition"
				>
					<Plus class="h-4 w-4" />
					Create Profile
				</button>
			</form>

			<!-- Profile List -->
			<div class="flex flex-col gap-3">
				<h2 class="text-sm font-semibold text-slate-300">Isolated Profiles ({profiles.length})</h2>
				{#if loading}
					<div class="text-sm text-slate-500 py-4 text-center">Loading profiles...</div>
				{:else if profiles.length === 0}
					<div class="text-sm text-slate-500 py-4 text-center">No profiles found. Create one above.</div>
				{:else}
					{#each profiles as p}
						<div
							class={`border rounded-xl p-4 flex flex-col gap-3 cursor-pointer transition ${
								selectedProfile?.id === p.id
									? "bg-slate-900 border-indigo-500/50 shadow-lg shadow-indigo-500/10"
									: "bg-slate-900/50 border-slate-800 hover:bg-slate-900"
							}`}
							onclick={() => chooseProfile(p)}
						>
							<div class="flex items-center justify-between">
								<span class="font-semibold text-slate-200">{p.name}</span>
								<span
									class={`text-xs px-2 py-0.5 rounded-full ${
										p.status === "active"
											? "bg-emerald-950 text-emerald-400 border border-emerald-800"
											: "bg-amber-950 text-amber-400 border border-amber-800"
									}`}
								>
									{p.status}
								</span>
							</div>
							<div class="text-xs text-slate-400 flex items-center gap-1.5 truncate">
								<Globe class="h-3.5 w-3.5 text-slate-500 shrink-0" />
								{p.site}
							</div>
							<div class="flex items-center justify-between pt-2 border-t border-slate-800/60 text-xs text-slate-500">
								<span>Tag: {p.account_label}</span>
								<div class="flex items-center gap-2" onclick={(e) => e.stopPropagation()}>
									<button
										title={p.status === "active" ? "Revoke / Disable Auth" : "Enable Profile"}
										onclick={() => toggleStatus(p)}
										class="p-1 hover:text-slate-200 transition"
									>
										{#if p.status === "active"}
											<PowerOff class="h-4 w-4 text-amber-400" />
										{:else}
											<Power class="h-4 w-4 text-emerald-400" />
										{/if}
									</button>
									<button
										title="Delete profile"
										onclick={() => deleteProfile(p)}
										class="p-1 hover:text-red-400 transition"
									>
										<Trash2 class="h-4 w-4" />
									</button>
								</div>
							</div>
						</div>
					{/each}
				{/if}
			</div>
		</div>

		<!-- Right: Active Viewport / Session Inspector -->
		<div class="lg:col-span-2 bg-slate-900 border border-slate-800 rounded-xl flex flex-col overflow-hidden">
			{#if selectedProfile}
				<div class="flex items-center justify-between px-6 py-4 border-b border-slate-800 bg-slate-900/80">
					<div>
						<h2 class="font-semibold text-slate-100 flex items-center gap-2">
							{selectedProfile.name}
							<span class="text-xs font-normal text-slate-400">({selectedProfile.site})</span>
						</h2>
						<p class="text-xs text-slate-500">ID: {selectedProfile.id} • Storage: 0700 Isolated</p>
					</div>
					<div class="flex items-center gap-2">
						<span class="text-xs text-slate-400 px-2 py-1 rounded-lg border border-slate-700">{streamStatus}</span>
						<button type="button" onclick={() => requestStreamTicket(selectedProfile!)} disabled={streamStatus === "Connecting" || streamStatus === "Requesting ticket"} class="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded text-xs">Connect stream</button>
						<button type="button" onclick={stopStream} class="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 rounded text-xs">Disconnect</button>
					</div>
				</div>

				<div class="flex-1 min-h-0 bg-black flex flex-col overflow-hidden">
					<div class="flex-1 min-h-0 flex items-center justify-center overflow-hidden bg-black"
						role="application" tabindex="0"
						aria-label="Remote browser viewport; focus it to forward keyboard input"
						onmousemove={(event) => sendMouse(event, "mouseMoved")}
						onmousedown={(event) => { (event.currentTarget as HTMLElement).focus(); sendMouse(event, "mousePressed"); }}
						onmouseup={(event) => sendMouse(event, "mouseReleased")}
						onwheel={(event) => { event.preventDefault(); sendMouse(event, "mouseWheel"); }}
						onkeydown={(event) => sendKey(event, "keyDown")}
						onkeyup={(event) => sendKey(event, "keyUp")}>
						{#if frameData}
							<img class="max-w-full max-h-full object-contain select-none" width={streamWidth} height={streamHeight} src={"data:image/jpeg;base64," + frameData} alt="Live remote browser frame" draggable="false" />
						{:else}
							<div class="p-8 text-center text-sm text-slate-400"><Play class="h-8 w-8 mx-auto mb-3 text-indigo-400" />Connect to view and control the isolated Playwright browser session.</div>
						{/if}
					</div>
					<div class="flex gap-2 p-3 border-t border-slate-800 bg-slate-900">
						<input class="min-w-0 flex-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm" placeholder="Send text to the focused browser element" bind:value={textToSend} onkeydown={(event) => { if (event.key === "Enter") { event.preventDefault(); sendText(); } }} />
						<button type="button" onclick={sendText} disabled={!textToSend || streamStatus !== "Connected"} class="px-3 py-2 bg-slate-800 hover:bg-slate-700 disabled:opacity-50 rounded-lg text-sm">Send text</button>
					</div>
					{#if streamError}<div class="px-4 pb-3 bg-slate-900 text-sm text-red-300">{streamError}</div>{/if}
				</div>
			{:else}
				<div class="flex-1 flex items-center justify-center text-slate-500 text-sm">
					Select or create a profile to inspect session status.
				</div>
			{/if}
		</div>
	</div>
</div>
{/if}
