/**
 * apiKeyStore — the user's own LLM credentials, used for Phase 1 (research/propose) and
 * the Phase 3 assembly step. Never sent for Genesis's own element-forging calls — those
 * use the server-side GENESIS_API_KEY, kept physically isolated per
 * server/agents/genesis_agent.py.
 *
 * Providers: a handful of built-ins (backend already knows their default endpoint —
 * see veya/llm.py's _ENDPOINTS) plus any number of user-registered custom providers.
 * veya/llm.py's llm_call() honors config.providers.<id>.api_key and
 * config.endpoints.<id> for ANY provider id, so a custom provider just needs a name +
 * OpenAI-compatible base URL — no backend change needed.
 *
 * Credentials (api_key/model) are stored per-provider so switching providers doesn't
 * clobber the others. Persisted to localStorage.
 */

export interface ProviderDef {
	id: string;
	label: string;
	/** omitted for the original 3 built-ins — backend already has a default endpoint for them */
	endpoint?: string;
	/** what veya/llm.py's _DEFAULT_MODELS falls back to when the model field is left blank */
	defaultModel?: string;
	custom?: boolean;
}

export const BUILTIN_PROVIDERS: ProviderDef[] = [
	{ id: "dashscope", label: "DashScope · Qwen", defaultModel: "qwen-plus" },
	{ id: "anthropic", label: "Anthropic · Claude", defaultModel: "claude-haiku-4-5-20251001" },
	{ id: "openai", label: "OpenAI · GPT", defaultModel: "gpt-4o-mini" },
	{ id: "deepseek", label: "DeepSeek", endpoint: "https://api.deepseek.com/v1/chat/completions", defaultModel: "deepseek-chat" },
	{ id: "openrouter", label: "OpenRouter", endpoint: "https://openrouter.ai/api/v1/chat/completions", defaultModel: "openai/gpt-4o-mini" },
	{ id: "moonshot", label: "Moonshot · Kimi", endpoint: "https://api.moonshot.cn/v1/chat/completions", defaultModel: "moonshot-v1-8k" },
	{ id: "zhipu", label: "智谱 · GLM", endpoint: "https://open.bigmodel.cn/api/paas/v4/chat/completions", defaultModel: "glm-4-flash" },
];

interface Credential {
	api_key: string;
	model: string;
}

interface StoredState {
	provider: string;
	creds: Record<string, Credential>;
	customProviders: ProviderDef[];
}

const STORAGE_KEY = "veya.flow.apiKey.v2";
const LEGACY_KEY = "veya.flow.apiKey";

function emptyCred(): Credential {
	return { api_key: "", model: "" };
}

function load(): StoredState {
	if (typeof localStorage === "undefined") return { provider: "dashscope", creds: {}, customProviders: [] };
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		if (raw) {
			const parsed = JSON.parse(raw);
			if (parsed && typeof parsed.provider === "string") {
				return { provider: parsed.provider, creds: parsed.creds ?? {}, customProviders: parsed.customProviders ?? [] };
			}
		}
		// migrate the old single-credential shape if present
		const legacy = localStorage.getItem(LEGACY_KEY);
		if (legacy) {
			const p = JSON.parse(legacy);
			if (p && typeof p.api_key === "string") {
				return {
					provider: p.provider ?? "dashscope",
					creds: { [p.provider ?? "dashscope"]: { api_key: p.api_key, model: p.model ?? "" } },
					customProviders: [],
				};
			}
		}
	} catch {
		/* ignore malformed storage */
	}
	return { provider: "dashscope", creds: {}, customProviders: [] };
}

class ApiKeyStore {
	#initial = load();
	provider = $state(this.#initial.provider);
	customProviders = $state<ProviderDef[]>(this.#initial.customProviders);
	#creds = $state<Record<string, Credential>>(this.#initial.creds);

	get all(): ProviderDef[] {
		return [...BUILTIN_PROVIDERS, ...this.customProviders];
	}

	get current(): ProviderDef {
		return this.all.find((p) => p.id === this.provider) ?? BUILTIN_PROVIDERS[0];
	}

	get api_key(): string {
		return this.#creds[this.provider]?.api_key ?? "";
	}
	set api_key(v: string) {
		this.#creds[this.provider] = { ...(this.#creds[this.provider] ?? emptyCred()), api_key: v };
	}

	get model(): string {
		return this.#creds[this.provider]?.model ?? "";
	}
	set model(v: string) {
		this.#creds[this.provider] = { ...(this.#creds[this.provider] ?? emptyCred()), model: v };
	}

	save() {
		if (typeof localStorage === "undefined") return;
		localStorage.setItem(
			STORAGE_KEY,
			JSON.stringify({ provider: this.provider, creds: this.#creds, customProviders: this.customProviders }),
		);
	}

	/** Register a new OpenAI-compatible provider and switch to it. */
	addCustomProvider(label: string, endpoint: string): void {
		const id = "custom-" + label.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
		if (!id || id === "custom-") return;
		this.customProviders = [...this.customProviders.filter((p) => p.id !== id), { id, label: label.trim(), endpoint: endpoint.trim(), custom: true }];
		this.provider = id;
		this.save();
	}

	removeCustomProvider(id: string): void {
		this.customProviders = this.customProviders.filter((p) => p.id !== id);
		delete this.#creds[id];
		if (this.provider === id) this.provider = BUILTIN_PROVIDERS[0].id;
		this.save();
	}

	/** shape expected by server/routes/flow.py's Phase1/2/3 request bodies. */
	asConfig(): Record<string, unknown> {
		if (!this.api_key.trim()) return {};
		const cfg: Record<string, unknown> = { providers: { [this.provider]: { api_key: this.api_key } } };
		const endpoint = this.current.endpoint;
		if (endpoint) cfg.endpoints = { [this.provider]: endpoint };
		return cfg;
	}
}

export const apiKeyStore = new ApiKeyStore();
