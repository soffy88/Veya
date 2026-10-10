/**
 * Shared bits for the /team pages: formatting and the class strings both pages
 * use, so the mobile-first look (44px tap targets, no zoom-on-focus inputs)
 * is defined once rather than copied.
 */

/** A push notification body is the first line of a reply — show it the same way. */
export function firstLine(text: string, limit = 140): string {
	for (const line of String(text ?? "").split("\n")) {
		if (line.trim()) return line.trim().slice(0, limit);
	}
	return "";
}

export function timeLabel(ts: unknown): string {
	const value = typeof ts === "number" ? ts * 1000 : Date.parse(String(ts ?? ""));
	if (!value || Number.isNaN(value)) return "";
	const date = new Date(value);
	return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

/** Whether a turn is still running (a 202'd async turn has no result yet). */
export function isPending(value: unknown): boolean {
	const status = (value as { status?: unknown } | null)?.status;
	return String(status ?? "") === "accepted";
}

export const actionClass =
	"inline-flex min-h-11 items-center justify-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.04] px-3 text-sm text-terminal-fg transition hover:bg-white/10 active:scale-[0.98] disabled:opacity-40";

export const primaryClass =
	"inline-flex min-h-11 items-center justify-center gap-1.5 rounded-lg bg-gradient-to-br from-sky-500 to-violet-600 px-3 text-sm font-medium text-white transition hover:opacity-90 active:scale-[0.98] disabled:opacity-40";

export const inputClass =
	"min-h-11 w-full rounded-lg border border-white/10 bg-black/40 px-3 text-base text-terminal-fg placeholder:text-white/25 focus:border-sky-500/60 focus:outline-none";

export const cardClass = "rounded-xl border border-white/[0.07] bg-black/40 p-4";
