/**
 * Which bots are mid-turn, and what they are waiting on (W11 W-2/W-3).
 *
 * The bot list shows "运行中" / "待批准" on a card. The server has no per-bot
 * status field to read (a turn's task id lives on the turn, not the bot), so
 * this store holds what *this browser* knows: a turn it started itself, the
 * task it was handed, and the pending approvals the projection reported for
 * that task.
 *
 * That is deliberately per-browser: a turn started on the phone does not mark
 * the bot running on the laptop, and the UI never claims otherwise.
 */

export type BotPhase = "idle" | "running" | "awaiting_approval";

export interface TurnState {
	taskId: string;
	sessionId: string;
	startedAt: number;
	approvals: Array<{ request_id: string; tool_name: string; reason: string }>;
}

class TurnStore {
	/** bot_id → the turn this browser started and has not seen land yet. */
	#running = $state<Record<string, TurnState>>({});

	get running(): Record<string, TurnState> {
		return this.#running;
	}

	phase(botId: string): BotPhase {
		const turn = this.#running[botId];
		if (!turn) return "idle";
		return turn.approvals.length > 0 ? "awaiting_approval" : "running";
	}

	start(botId: string, turn: Omit<TurnState, "approvals" | "startedAt">): void {
		this.#running[botId] = { ...turn, startedAt: Date.now(), approvals: [] };
	}

	setApprovals(botId: string, approvals: TurnState["approvals"]): void {
		const turn = this.#running[botId];
		if (!turn) return;
		turn.approvals = approvals;
	}

	clear(botId: string): void {
		if (this.#running[botId]) delete this.#running[botId];
	}
}

export const turnStore = new TurnStore();
