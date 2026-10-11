/**
 * Natural language → routine schedule (W11 W-4).
 *
 * A routine needs a schedule, and a first-time user should be able to type
 * 「每天早上9点汇总今天的新闻」 instead of learning `daily 09:00`. This parses
 * the common Chinese (and the existing English) phrasings into the three forms
 * the backend already understands — `every Nm|h|d`, `daily HH:MM`, 5-field cron.
 *
 * It is a display aid, not a second scheduler: the parsed schedule is always
 * shown back to the user ("识别为：…") and stays editable, and the server clock
 * remains the only thing that fires routines. `null` means "not understood",
 * which the UI says instead of inventing a time.
 */

const EVERY_RE = /^every\s+(\d+)\s*(m|h|d)$/i;
const DAILY_RE = /^daily\s+(\d{1,2}):(\d{2})$/;

const WEEKDAYS: Record<string, number> = {
	"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0,
};

const CN_NUMBERS: Record<string, number> = {
	"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
	"半": 30, "十五": 15, "二十": 20, "三十": 30, "六十": 60,
};

function toInt(value: string | undefined): number | null {
	if (!value) return null;
	if (CN_NUMBERS[value] !== undefined) return CN_NUMBERS[value];
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

/** 早上/上午/下午/晚上/中午 shift an hour into 24h form. */
function to24Hour(hour: number, period: string | undefined): number {
	if (!period) return hour;
	if (period === "中午") return hour < 12 ? hour + 12 : hour;
	if (period === "下午" || period === "晚上") return hour < 12 ? hour + 12 : hour;
	return hour; // 凌晨 / 早上 / 上午
}

/**
 * The schedule `text` describes, or `null` when nothing in it names a time.
 */
export function parseScheduleFromText(text: string): string | null {
	const value = String(text ?? "").trim();
	if (!value) return null;

	// Already in a backend form: pass it through unchanged.
	const every = EVERY_RE.exec(value);
	if (every) return `every ${Number(every[1])}${every[2].toLowerCase()}`;
	const daily = DAILY_RE.exec(value);
	if (daily) return `daily ${daily[1].padStart(2, "0")}:${daily[2]}`;
	if (/^[\d*/,-]+\s+[\d*/,-]+\s+[\d*/,-]+\s+[\d*/,-]+\s+[\d*/,-]+$/.test(value)) return value;

	// 每 N 分钟 / 每半小时
	const minutes = /每\s*([0-9一二两三四五六七八九十半]+)?\s*分钟/.exec(value);
	if (minutes) {
		const amount = toInt(minutes[1]) ?? 1;
		return amount >= 60 && amount % 60 === 0 ? `every ${amount / 60}h` : `every ${amount}m`;
	}

	// 每小时 / 每 N 小时
	const hours = /每\s*([0-9一二两三四五六七八九十]+)?\s*(小时|钟头)/.exec(value);
	if (hours) {
		const amount = toInt(hours[1]) ?? 1;
		return `every ${amount}h`;
	}

	// 每天/每日 (+ 时段 + 时刻)
	const dayTime = /(每天|每日)\s*(早上|上午|中午|下午|晚上|凌晨)?\s*(\d{1,2})\s*(?:点|:|：)\s*(\d{1,2})?/.exec(value);
	if (dayTime) {
		const hour = to24Hour(toInt(dayTime[3]) ?? 9, dayTime[2]);
		const minute = toInt(dayTime[4]) ?? 0;
		return `daily ${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
	}
	if (/(每天|每日)/.test(value)) return "daily 09:00";

	// 每周一 09:00 / 星期三下午3点 → cron (min hour dom month dow)
	const week = /每(?:周|星期)\s*([一二三四五六日天])/.exec(value);
	if (week) {
		const dow = WEEKDAYS[week[1]];
		const time = /(早上|上午|中午|下午|晚上|凌晨)?\s*(\d{1,2})\s*(?:点|:|：)\s*(\d{1,2})?/.exec(value);
		const hour = to24Hour(toInt(time?.[2]) ?? 9, time?.[1]);
		const minute = toInt(time?.[3]) ?? 0;
		return `${minute} ${hour} * * ${dow}`;
	}

	return null;
}

/** What the UI shows under the natural-language box, in the user's words. */
export function describeParsedSchedule(schedule: string): string {
	const every = EVERY_RE.exec(schedule);
	if (every) {
		const amount = Number(every[1]);
		const unit = every[2].toLowerCase();
		return `每 ${amount} ${unit === "m" ? "分钟" : unit === "h" ? "小时" : "天"}`;
	}
	const daily = DAILY_RE.exec(schedule);
	if (daily) return `每天 ${daily[1].padStart(2, "0")}:${daily[2]}`;
	const fields = schedule.split(/\s+/);
	if (fields.length === 5) {
		const names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
		const dow = Number(fields[4]);
		const clock = `${fields[1].padStart(2, "0")}:${fields[0].padStart(2, "0")}`;
		return Number.isFinite(dow) && names[dow] ? `${names[dow]} ${clock}` : `按 cron ${schedule}`;
	}
	return schedule;
}
