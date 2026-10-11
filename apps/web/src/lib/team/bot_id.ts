/**
 * Bot ids are derived, never typed (W11 W-1).
 *
 * A bot id becomes a directory name on the server, so it is restricted to
 * ``[a-z0-9][a-z0-9._-]{0,63}`` (the same rule the server enforces). The user
 * only ever types a display name; this module turns that into a valid, unique
 * id so nobody has to learn the alphabet.
 */

/** The server's bot-id alphabet (server/routes/bot_team.py). */
const BOT_ID_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;

/** Latin letters only survive; CJK names fall back to a stable transliteration-free id. */
export function slugifyBotId(name: string): string {
	const ascii = String(name ?? "")
		.normalize("NFKD")
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "")
		.slice(0, 40);
	return ascii;
}

/** FNV-1a: a short, stable suffix derived from the name itself. */
function shortHash(value: string): string {
	let hash = 0x811c9dc5;
	for (let index = 0; index < value.length; index += 1) {
		hash ^= value.codePointAt(index) ?? 0;
		hash = Math.imul(hash, 0x01000193) >>> 0;
	}
	return hash.toString(36).slice(0, 6).padStart(6, "0");
}

/**
 * A valid, unused bot id for `name`. When the slug is empty (a name in another
 * script) or already taken, a suffix is appended so the field never forces the
 * user to invent an id by hand.
 */
export function suggestBotId(name: string, taken: Iterable<string> = []): string {
	const existing = new Set(taken);
	const trimmed = String(name ?? "").trim();
	const base = slugifyBotId(trimmed) || `bot-${shortHash(trimmed)}`;
	if (!existing.has(base) && BOT_ID_RE.test(base)) return base;
	for (let suffix = 2; suffix < 1000; suffix += 1) {
		const candidate = `${base}-${suffix}`;
		if (!existing.has(candidate) && BOT_ID_RE.test(candidate)) return candidate;
	}
	return `${base}-${Date.now().toString(36)}`.slice(0, 64);
}

/** Whether `value` is an id the server would accept, for inline validation. */
export function isValidBotId(value: string): boolean {
	return BOT_ID_RE.test(String(value ?? "").trim());
}
