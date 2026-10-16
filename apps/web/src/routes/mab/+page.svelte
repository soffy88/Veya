<script lang="ts">
	import { onMount } from "svelte";
	import { api } from "$lib/api";
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

	async function loadProfiles() {
		loading = true;
		error = "";
		try {
			const res = await api("gateway", "api/v1/mab/profiles");
			if (res.status === 404) { window.location.replace("/"); return; }
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
		try {
			const res = await api("gateway", `api/v1/mab/profiles/${profile.id}/stream-ticket`, {
				method: "POST"
			});
			if (res.ok && res.data) {
				const ticket = (res.data as any).ticket;
				const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
				const host = window.location.host;
				const wsUrl = `${proto}//${host}/api/v1/mab/profiles/${profile.id}/stream`;
				const ws = new WebSocket(wsUrl, [`ticket.${ticket}`]);
				ws.onopen = () => console.log("MAB WS connected with ticket subprotocol");
				ws.onmessage = (event) => {
					try {
						const msg = JSON.parse(event.data);
						if (msg.type === "frame") {
							// screencast frame data
						}
					} catch {}
				};
			}
		} catch (err: any) {
			error = err.message || "Failed to obtain stream ticket";
		}
	}
</script>

<div class="flex h-full w-full flex-col bg-slate-950 text-slate-100 p-6">
	<div class="flex items-center justify-between border-b border-slate-800 pb-4 mb-6">
		<div>
			<h1 class="text-2xl font-bold flex items-center gap-2">
				<Shield class="h-6 w-6 text-indigo-400" />
				Multi-Account Browser (MAB)
			</h1>
			<p class="text-sm text-slate-400">Isolated session profiles with 0700 permissions and automatic tool authorization.</p>
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
							onclick={() => (selectedProfile = p)}
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
						<span class="text-xs flex items-center gap-1 text-emerald-400 bg-emerald-950/60 border border-emerald-800/60 px-2.5 py-1 rounded-lg">
							<Lock class="h-3 w-3" />
							Tools Authorized
						</span>
					</div>
				</div>

				<div class="flex-1 bg-slate-950 flex flex-col items-center justify-center p-8 text-center relative">
					<div class="absolute inset-0 bg-[radial-gradient(ellipse_at_center,_var(--tw-gradient-stops))] from-indigo-950/20 via-slate-950 to-slate-950 pointer-events-none"></div>
					<div class="max-w-md flex flex-col items-center gap-4 z-10">
						<div class="h-16 w-16 rounded-2xl bg-indigo-950/60 border border-indigo-800/60 flex items-center justify-center text-indigo-400 shadow-xl">
							<Play class="h-8 w-8 ml-1" />
						</div>
						<h3 class="text-lg font-bold text-slate-200">Remote Profile Session Ready</h3>
						<p class="text-sm text-slate-400">
							This profile has an isolated user data directory. All tool requests targeting profile <code class="text-indigo-300 font-mono">{selectedProfile.name}</code> run within this encrypted boundary without leaking raw credentials.
						</p>
						<div class="flex items-center gap-3 pt-2">
							<button
								onclick={() => requestStreamTicket(selectedProfile!)}
								class="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-sm font-medium transition shadow-lg shadow-indigo-600/20"
							>
								Connect Live Stream
							</button>
							<a
								href={selectedProfile.site}
								target="_blank"
								rel="noreferrer"
								class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg text-sm font-medium transition"
							>
								Open External Target
							</a>
						</div>
					</div>
				</div>
			{:else}
				<div class="flex-1 flex items-center justify-center text-slate-500 text-sm">
					Select or create a profile to inspect session status.
				</div>
			{/if}
		</div>
	</div>
</div>
