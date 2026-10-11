/**
 * When does a routine next run? (W11 W-4 — "next run time in user's timezone")
 *
 * The routine clock on the server only answers "is it due *now*"
 * (server/bot_team/routine_clock.py compute_due), and there is no endpoint that
 * forecasts a future fire. The schedule itself is carried on every routine
 * record, so the display is computed here instead of adding a second schedule
 * authority: this module only *reads* the three schedule forms the backend
 * parses (`every Nm|h|d`, `daily HH:MM`, a 5-field cron) and returns the next
 * occurrence, or `null` when the form is not one of those.
 *
 * Nothing here dispatches, persists or gates a run — the server clock stays the
 * only thing that fires routines.
 */

export interface RoutineScheduleInput {
	schedule?: string | null;
	timezone?: string | null;
	/** ISO or epoch seconds of the last fire; `null`/0 means "never ran". */
	last_fired?: string | number | null;
}

const EVERY_RE = /^every\s+(\d+)\s*(m|h|d)$/i;
const DAILY_RE = /^daily\s+(\d{1,2}):(\d{2})$/i;

const MINUTE_MS = 60_000;
/** How far ahead a cron scan looks before giving up (the UI shows "—" then). */
const CRON_SCAN_MINUTES = 3 * 24 * 60;

/** Minutes/hours/days of an `every …` schedule, or null. */
function everyIntervalMs(schedule: string): number | null {
	const match = EVERY_RE.exec(schedule);
	if (!match) return null;
	const value = Number(match[1]);
	if (!Number.isFinite(value) || value <= 0) return null;
	const unit = match[2].toLowerCase();
	return value * (unit === "m" ? MINUTE_MS : unit === "h" ? 60 * MINUTE_MS : 24 * 60 * MINUTE_MS);
}

function timeZone(tz: string | null | undefined): string {
	const value = String(tz ?? "").trim();
	if (!value) return "Asia/Shanghai";
	try {
		new Intl.DateTimeFormat("en-US", { timeZone: value });
		return value;
	} catch {
		return "Asia/Shanghai";
	}
}

/** Wall-clock fields of `date` as seen in `tz`. */
function wallParts(date: Date, tz: string): { y: number; mo: number; d: number; h: number; mi: number } {
	const parts = new Intl.DateTimeFormat("en-US", {
		timeZone: tz,
		hour12: false,
		year: "numeric",
		month: "2-digit",
		day: "2-digit",
		hour: "2-digit",
		minute: "2-digit",
	}).formatToParts(date);
	const value = (type: string): number => Number(parts.find((part) => part.type === type)?.value ?? 0);
	return {
		y: value("year"),
		mo: value("month"),
		d: value("day"),
		h: value("hour") % 24,
		mi: value("minute"),
	};
}

/** Offset of `tz` (ms to add to a UTC instant to reach its wall clock) at `date`. */
function offsetMs(date: Date, tz: string): number {
	const parts = new Intl.DateTimeFormat("en-US", {
		timeZone: tz,
		hour12: false,
		year: "numeric",
		month: "2-digit",
		day: "2-digit",
		hour: "2-digit",
		minute: "2-digit",
		second: "2-digit",
	}).formatToParts(date);
	const value = (type: string): number => Number(parts.find((part) => part.type === type)?.value ?? 0);
	const asUtc = Date.UTC(
		value("year"),
		value("month") - 1,
		value("day"),
		value("hour") % 24,
		value("minute"),
		value("second"),
	);
	return asUtc - date.getTime();
}

/** The UTC instant of a wall-clock time in `tz` (two passes for DST edges). */
function instantFromWall(wall: { y: number; mo: number; d: number; h: number; mi: number }, tz: string): Date {
	const guess = Date.UTC(wall.y, wall.mo - 1, wall.d, wall.h, wall.mi, 0);
	let instant = new Date(guess - offsetMs(new Date(guess), tz));
	instant = new Date(guess - offsetMs(instant, tz));
	return instant;
}

function lastFireMs(input: RoutineScheduleInput): number {
	const raw = input.last_fired;
	if (typeof raw === "number" && raw > 0) return raw > 1e12 ? raw : raw * 1000;
	if (typeof raw === "string" && raw.trim()) {
		const parsed = Date.parse(raw);
		if (!Number.isNaN(parsed)) return parsed;
	}
	return Date.now();
}

/** The next occurrence of `daily HH:MM` in `tz`, strictly after `fromMs`. */
function nextDailyMs(schedule: string, tz: string, fromMs: number): number | null {
	const match = DAILY_RE.exec(schedule);
	if (!match) return null;
	const hour = Number(match[1]);
	const minute = Number(match[2]);
	if (hour > 23 || minute > 59) return null;

	const from = new Date(fromMs);
	for (let dayOffset = 0; dayOffset <= 2; dayOffset += 1) {
		const wall = wallParts(from, tz);
		const candidateWall = { ...wall, d: wall.d + dayOffset, h: hour, mi: minute };
		const instant = instantFromWall(candidateWall, tz).getTime();
		if (instant > fromMs) return instant;
	}
	return null;
}

/** The minimal cron matcher — the same field syntax the backend accepts. */
function cronFieldMatches(value: number, expr: string): boolean {
	const field = expr.trim();
	if (field === "*") return true;
	for (const part of field.split(",")) {
		const piece = part.trim();
		if (piece.startsWith("*/")) {
			const step = Number(piece.slice(2));
			if (Number.isFinite(step) && step > 0 && value % step === 0) return true;
			continue;
		}
		if (piece.includes("-")) {
			const [start, end] = piece.split("-").map((raw) => Number(raw));
			if (Number.isFinite(start) && Number.isFinite(end) && start <= value && value <= end) return true;
			continue;
		}
		if (Number(piece) === value) return true;
	}
	return false;
}

/** The next cron occurrence after `fromMs`, scanned minute by minute in `tz`. */
function nextCronMs(schedule: string, tz: string, fromMs: number): number | null {
	const fields = schedule.trim().split(/\s+/);
	if (fields.length !== 5) return null;
	const startMs = Math.floor(fromMs / MINUTE_MS) * MINUTE_MS;
	for (let step = 1; step <= CRON_SCAN_MINUTES; step += 1) {
		const instant = new Date(startMs + step * MINUTE_MS);
		const wall = wallParts(instant, tz);
		const dow = instant.getUTCDay(); // weekday of the *instant* in tz wall terms
		const matches =
			cronFieldMatches(wall.mi, fields[0]) &&
			cronFieldMatches(wall.h, fields[1]) &&
			cronFieldMatches(wall.d, fields[2]) &&
			cronFieldMatches(wall.mo, fields[3]) &&
			cronFieldMatches(dow, fields[4]);
		if (matches) return instant.getTime();
	}
	return null;
}

/**
 * The next run as an absolute instant, or `null` when the schedule is empty or
 * in a form this display helper does not parse.
 */
export function nextRunAt(input: RoutineScheduleInput): Date | null {
	const schedule = String(input.schedule ?? "").trim();
	if (!schedule) return null;
	const tz = timeZone(input.timezone);
	const fromMs = lastFireMs(input);

	const interval = everyIntervalMs(schedule);
	if (interval !== null) {
		let next = Math.floor(fromMs / interval) * interval + interval;
		while (next <= fromMs) next += interval;
		return new Date(next);
	}
	const daily = nextDailyMs(schedule, tz, fromMs);
	if (daily !== null) return new Date(daily);
	return nextCronMs(schedule, tz, fromMs) !== null ? new Date(nextCronMs(schedule, tz, fromMs) as number) : null;
}

/**
 * A human (zh-CN) label for the schedule itself, for the routine card.
 */
export function describeSchedule(schedule: string): string {
	const value = String(schedule ?? "").trim();
	const every = EVERY_RE.exec(value);
	if (every) {
		const amount = Number(every[1]);
		const unit = every[2].toLowerCase();
		const noun = unit === "m" ? "分钟" : unit === "h" ? "小时" : "天";
		return `每 ${amount} ${noun}`;
	}
	const daily = DAILY_RE.exec(value);
	if (daily) return `每天 ${daily[1].padStart(2, "0")}:${daily[2]}`;
	return value;
}

/**
 * "下次运行" in the user's own words and clock: 今天/明天 HH:MM, else M月D日 HH:MM.
 * Renders the *routine's* timezone, which is what the schedule means.
 */
export function formatNextRun(next: Date | null, timezone?: string | null, now: Date = new Date()): string {
	if (!next) return "";
	const tz = timeZone(timezone);
	const dayMs = 24 * 60 * MINUTE_MS;
	const startOfDay = (date: Date): number => {
		const wall = wallParts(date, tz);
		return instantFromWall({ ...wall, h: 0, mi: 0 }, tz).getTime();
	};
	const days = Math.round((startOfDay(next) - startOfDay(now)) / dayMs);
	const clock = new Intl.DateTimeFormat("zh-CN", {
		timeZone: tz,
		hour: "2-digit",
		minute: "2-digit",
		hour12: false,
	}).format(next);
	if (days === 0) return `今天 ${clock}`;
	if (days === 1) return `明天 ${clock}`;
	const date = new Intl.DateTimeFormat("zh-CN", {
		timeZone: tz,
		month: "long",
		day: "numeric",
	}).format(next);
	return `${date} ${clock}`;
}
