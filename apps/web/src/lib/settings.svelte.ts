/**
 * apiKeyStore — the user's own LLM key, used for Phase 1 (research/propose) and the Phase 3
 * assembly step. Never sent for Genesis's own element-forging calls — those use the
 * server-side GENESIS_API_KEY, kept physically isolated per server/agents/genesis_agent.py.
 * Persisted to localStorage so it survives a reload; never sent anywhere but our own
 * /legacy/flow/* endpoints (see FlowConsole.svelte).
 */

export type Provider = "dashscope" | "anthropic" | "openai";

const STORAGE_KEY = "veya.flow.apiKey";

interface StoredApiKey {
	provider: Provider;
	api_key: string;
}

function load(): StoredApiKey {
	if (typeof localStorage === "undefined") return { provider: "dashscope", api_key: "" };
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (raw) {
			const parsed = JSON.parse(raw);
			if (parsed && typeof parsed.api_key === "string") return parsed;
		}
	} catch {
		/* ignore malformed storage */
	}
	return { provider: "dashscope", api_key: "" };
}

class ApiKeyStore {
	#initial = load();
	provider = $state<Provider>(this.#initial.provider);
	api_key = $state(this.#initial.api_key);

	save() {
		if (typeof localStorage === "undefined") return;
		localStorage.setItem(STORAGE_KEY, JSON.stringify({ provider: this.provider, api_key: this.api_key }));
	}

	/** shape expected by server/routes/flow.py's Phase1/2/3 request bodies (`config.providers.<provider>.api_key`) */
	asConfig(): Record<string, unknown> {
		if (!this.api_key.trim()) return {};
		return { providers: { [this.provider]: { api_key: this.api_key } } };
	}
}

export const apiKeyStore = new ApiKeyStore();
