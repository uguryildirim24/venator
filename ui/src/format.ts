/**
 * Dates, in the words a Mac uses for them: "Today", "Yesterday", "Tuesday, September 8",
 * "9:52 AM", "Sep 8". Every list groups and times its cells from the one timestamp it is
 * ordered by, so these take that value and nothing else.
 */

const DAY_NAME = new Intl.DateTimeFormat("en-US", { weekday: "long", month: "long", day: "numeric" });
const DAY_NAME_WITH_YEAR = new Intl.DateTimeFormat("en-US", {
	weekday: "long",
	month: "long",
	day: "numeric",
	year: "numeric",
});
const SHORT_DAY = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });
const SHORT_DAY_WITH_YEAR = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" });
const TIME = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" });

function parseDate(value: string | null): Date | null {
	if (value === null || value === "") return null;
	const parsed = new Date(value);
	return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function startOfDay(date: Date): number {
	return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** Whole days between `date` and today, counted in local calendar days. */
function daysAgo(date: Date, now: Date): number {
	return Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);
}

/** The key two timestamps share when they fall on the same local day. */
export function dayKey(value: string | null): string {
	const parsed = parseDate(value);
	return parsed === null ? "" : String(startOfDay(parsed));
}

/** A day header: "Today", "Yesterday", or "Tuesday, September 8". */
export function formatDayHeader(value: string | null, now = new Date()): string {
	const parsed = parseDate(value);
	if (parsed === null) return "Date unknown";
	const days = daysAgo(parsed, now);
	if (days === 0) return "Today";
	if (days === 1) return "Yesterday";
	return parsed.getFullYear() === now.getFullYear() ? DAY_NAME.format(parsed) : DAY_NAME_WITH_YEAR.format(parsed);
}

/** A time of day: "9:52 AM". */
export function formatTime(value: string | null): string {
	const parsed = parseDate(value);
	return parsed === null ? "" : TIME.format(parsed);
}

/** A short date for running text and dense rows: "Sep 8", with the year only when it is not this one. */
export function formatDay(value: string | null, now = new Date()): string {
	const parsed = parseDate(value);
	if (parsed === null) return "an unknown day";
	return parsed.getFullYear() === now.getFullYear() ? SHORT_DAY.format(parsed) : SHORT_DAY_WITH_YEAR.format(parsed);
}

/** A moment in a sentence: "Today at 9:14 AM", "Yesterday at 6:02 PM", "Sep 8 at 11:26 PM". */
export function formatMoment(value: string | null, now = new Date()): string {
	const parsed = parseDate(value);
	if (parsed === null) return "at an unknown time";
	const days = daysAgo(parsed, now);
	const day = days === 0 ? "Today" : days === 1 ? "Yesterday" : formatDay(value, now);
	return `${day} at ${TIME.format(parsed)}`;
}

/** How long ago a timestamp was, in the coarsest unit that still means something. */
export function formatAgo(value: string | null, now = new Date()): string {
	const parsed = parseDate(value);
	if (parsed === null) return "never";
	const minutes = Math.round((now.getTime() - parsed.getTime()) / 60_000);
	if (minutes < 1) return "just now";
	if (minutes < 60) return `${minutes} min ago`;
	const hours = Math.round(minutes / 60);
	if (hours < 48) return `${hours} h ago`;
	return `${Math.round(hours / 24)} days ago`;
}

/** The host a link goes to, for a meta line: "job-boards.greenhouse.io". Empty when it is not a URL. */
export function hostOf(url: string): string {
	try {
		return new URL(url).host;
	} catch {
		return "";
	}
}
