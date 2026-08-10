/** Shared chat message types (ChatConsole + sessionStore). */

export type ChatRole = "user" | "assistant";

export interface ToolStep {
	type: string;
	tool_name?: string;
	role?: string;
	status?: string;
	tool_args?: unknown;
	[key: string]: unknown;
}

export interface ChatMessage {
	role: ChatRole;
	text: string;
	status: "streaming" | "done" | "error" | "stopped";
	steps: ToolStep[];
	cost?: number;
	error?: string;
	images?: string[];
}
