/**
 * Unread bookkeeping for the bot list (W11 W-3).
 *
 * "Unread" is a fact about *this browser*, not about the bot: the server has no
 * read marker for a conversation, so the last-read timestamp per bot is kept in
 * localStorage and compared against the newest bot message. Opening a bot's
 * chat marks it read.
 */

const KEY = "veya.team.lastRead.v1";

type ReadMap = Record<string, number>;

function load(): ReadMap {
	if (typeof localStorage === "undefined") return {};
	try {
		const raw = localStorage.getItem(KEY);
		const parsed = raw ? (JSON.parse(raw) as ReadMap) : {};
		return parsed && typeof parsed === "object" ? parsed : {};
	} catch {
		return {};
	}
}

function save(map: ReadMap): void {
	if (typeof localStorage === "undefined") return;
	try {
		localStorage.setItem(KEY, JSON.stringify(map));
	} catch {
		/* a full/blocked storage must not break the list */
	}
}

/** When this browser last read `botId`'s chat (epoch seconds; 0 = never). */
export function lastReadAt(botId: string): number {
	return load()[botId] ?? 0;
}

/** Mark `botId`'s chat as read now. */
export function markRead(botId: string): void {
	const map = load();
	map[botId] = Date.now() / 1000;
	save(map);
}

/**
 * Whether any bot message is newer than the last read. A message the user just
 * sent is never unread — only the bot's replies count.
 */
export function hasUnread(
	botId: string,
	messages: Array<{ role?: string; ts?: number }>,
	readAt: number = lastReadAt(botId),
): boolean {
	return messages.some((message) => message.role === "bot" && Number(message.ts ?? 0) > readAt);
}

/** The newest bot message in a transcript, or `null` when the bot never spoke. */
export function latestBotMessage<T extends { role?: string; ts?: number }>(messages: T[]): T | null {
	for (let index = messages.length - 1; index >= 0; index -= 1) {
		if (messages[index]?.role === "bot") return messages[index];
	}
	return null;
}
