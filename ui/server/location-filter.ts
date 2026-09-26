import { existsSync, readFileSync } from "node:fs";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { DatabaseSync } from "node:sqlite";

import { applicationDataDirectory, systemContext, type LocationContext } from "./locations.ts";
import { asList, asText, parseJson } from "./onboarding/json.ts";
import { optionalTextColumn, textColumn } from "./rows.ts";

const STATES = {
	AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming", DC: "District of Columbia",
} satisfies Record<string, string>;
const STATE_NAMES = new Map(Object.entries(STATES));
const STATE_CODE = new Map(Object.entries(STATES).flatMap(([code, name]) => [[code.toLowerCase(), code], [name.toLowerCase(), code]]));

export type LocationChoice = { readonly key: string; readonly label: string; readonly count: number };
export type LocationSelection = { readonly selected: readonly string[]; readonly choices: readonly LocationChoice[] };

/** Discover joins multiple ATS locations with semicolons. A Posting belongs to every one. */
export function locationChoices(raw: string | null): readonly { key: string; label: string }[] {
	const locations = (raw ?? "").split(";").map((value) => value.trim().replaceAll(/\s+/gu, " ")).filter(Boolean);
	if (!locations.length) return [{ key: "none", label: "No location" }];
	const choices = new Map<string, { key: string; label: string }>();
	for (const location of locations) {
		if (/\bremote\b/iu.test(location)) choices.set("remote", { key: "remote", label: "Remote" });
		const local = location.replace(/\s*\(remote\)/giu, "").replace(/\bremote\b\s*[-–,/(]?\s*(?:US|USA|United States)?\)?/giu, "").replace(/\s*[-–/,]\s*$/u, "").trim();
		const match = /^([^,]+),\s*([^,]+?)(?:\s*,\s*(?:US|USA|United States))?$/iu.exec(local);
		const place = match?.[1]?.trim() ?? local;
		const region = match?.[2]?.trim() ?? "";
		const state = STATE_CODE.get(region.toLowerCase()) ?? STATE_CODE.get(place.toLowerCase());
		if (state) {
			choices.set(`state:${state}`, { key: `state:${state}`, label: STATE_NAMES.get(state)! });
			if (match && place) {
				const key = `city:${place.toLocaleLowerCase("en-US")},${state}`;
				choices.set(key, { key, label: `${place}, ${state}` });
			}
		} else if (local && !/^(?:US|USA|United States)$/iu.test(local)) {
			const key = `place:${local.toLocaleLowerCase("en-US")}`;
			choices.set(key, { key, label: local });
		}
	}
	return choices.size ? [...choices.values()] : [{ key: "none", label: "No location" }];
}

function selectionPath(context: LocationContext): string {
	return join(applicationDataDirectory(context), "location-filter.json");
}

export function readLocationSelection(context: LocationContext = systemContext()): readonly string[] {
	const path = selectionPath(context);
	if (!existsSync(path)) return [];
	try {
		const values = asList(parseJson(readFileSync(path, "utf8")));
		if (values === null) return [];
		const selected = values.map(asText);
		return selected.every((item) => item !== null) ? selected : [];
	} catch { return []; }
}

export async function saveLocationSelection(selected: readonly string[], context: LocationContext = systemContext()): Promise<void> {
	const directory = applicationDataDirectory(context);
	await mkdir(directory, { recursive: true, mode: 0o700 });
	const temporary = join(directory, `location-filter-${process.pid}-${Date.now()}.tmp`);
	try {
		await writeFile(temporary, JSON.stringify(selected), { flag: "wx", mode: 0o600 });
		await rename(temporary, selectionPath(context));
	} finally { await rm(temporary, { force: true }); }
}

export function availableLocations(database: DatabaseSync): readonly LocationChoice[] {
	const counts = new Map<string, LocationChoice>();
	for (const row of database.prepare("SELECT location FROM postings").all()) {
		for (const choice of locationChoices(optionalTextColumn(row, "location"))) {
			const previous = counts.get(choice.key);
			counts.set(choice.key, { ...choice, count: (previous?.count ?? 0) + 1 });
		}
	}
	return [...counts.values()].sort((a, b) => a.label.localeCompare(b.label, "en-US"));
}

/** Keys are only a read-side predicate. No Posting or Filter Decision is changed. */
export function matchingLocationKeys(database: DatabaseSync, selected: readonly string[]): string | null {
	if (!selected.length) return null;
	const wanted = new Set(selected);
	const keys = database.prepare("SELECT key, location FROM postings").all()
		.filter((row) => locationChoices(optionalTextColumn(row, "location")).some((choice) => wanted.has(choice.key)))
		.map((row) => textColumn(row, "key"));
	return JSON.stringify(keys);
}
