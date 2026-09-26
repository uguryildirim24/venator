import { existsSync, readFileSync } from "node:fs";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { DatabaseSync } from "node:sqlite";

import { applicationDataDirectory, systemContext, type LocationContext } from "./locations.ts";
import { asList, asText, parseJson } from "./onboarding/json.ts";
import { optionalTextColumn, textColumn, type SqlRow } from "./rows.ts";
import { US_CITIES } from "./us-city-table.ts";

const STATES = {
	AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado", CT: "Connecticut", DE: "Delaware", FL: "Florida", GA: "Georgia", HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas", KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts", MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana", NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico", NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma", OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota", TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington", WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming", DC: "District of Columbia",
} satisfies Record<string, string>;
const STATE_NAMES = new Map(Object.entries(STATES));
const usCities: ReadonlyMap<string, readonly string[]> = new Map(Object.entries(US_CITIES));
const STATE_CODE = new Map(Object.entries(STATES).flatMap(([code, name]) => [[code.toLowerCase(), code], [name.toLowerCase(), code]]));

export type LocationChoice = { readonly key: string; readonly label: string; readonly count: number; readonly parent?: string };
export type LocationSelection = { readonly selected: readonly string[]; readonly choices: readonly LocationChoice[] };

type Place = Omit<LocationChoice, "count">;
const COUNTRY_NAMES = new Map(Object.entries({ US: "United States", USA: "United States", INDIA: "India", IN: "India", CANADA: "Canada", CA: "Canada", UK: "United Kingdom", GB: "United Kingdom", GERMANY: "Germany", DE: "Germany", FRANCE: "France", FR: "France", AUSTRALIA: "Australia", AU: "Australia", IRELAND: "Ireland", IE: "Ireland", MALAYSIA: "Malaysia", MY: "Malaysia", SINGAPORE: "Singapore", SG: "Singapore", SWITZERLAND: "Switzerland", CH: "Switzerland", NETHERLANDS: "Netherlands", NL: "Netherlands", UNITEDKINGDOM: "United Kingdom", UNITEDSTATES: "United States" }));
// ISO-3166 alpha-2 identities, generated from the same pycountry registry used by Discover.
const ISO_CODES = "AW AF AO AI AX AL AD AE AR AM AS AQ TF AG AU AT AZ BI BE BJ BQ BF BD BG BH BS BA BL BY BZ BM BO BR BB BN BT BV BW CF CA CC CH CL CN CI CM CD CG CK CO KM CV CR CU CW CX KY CY CZ DE DJ DM DK DO DZ EC EG ER EH ES EE ET FI FJ FK FR FO FM GA GB GE GG GH GI GN GP GM GW GQ GR GD GL GT GF GU GY HK HM HN HR HT HU ID IM IN IO IE IR IQ IS IL IT JM JE JO JP KZ KE KG KH KI KN KR KW LA LB LR LY LC LI LK LS LT LU LV MO MF MA MC MD MG MV MX MH MK ML MT MM ME MN MP MZ MR MS MQ MU MW MY YT NA NC NE NF NG NI NU NL NO NP NR NZ OM PK PA PN PE PH PW PG PL PR KP PT PY PS PF QA RE RO RU RW SA SD SN SG GS SH SJ SB SL SV SM SO PM RS SS ST SR SK SI SE SZ SX SC SY TC TD TG TH TJ TK TM TL TO TT TN TR TV TW TZ UG UA UM UY US UZ VA VC VE VG VI VN VU WF WS YE ZA ZM ZW";
const regionNames = new Intl.DisplayNames(["en"], { type: "region" });
const isoCountries = new Map(ISO_CODES.split(" ").flatMap((code) => {
	const name = regionNames.of(code);
	return name ? [[code, name], [name.replaceAll(/[^a-z]/giu, "").toUpperCase(), name]] : [];
}));
const country = (text: string): string | undefined => {
	const key = text.replaceAll(/[^a-z]/giu, "").toUpperCase();
	return COUNTRY_NAMES.get(key) ?? isoCountries.get(key);
};
const title = (text: string): string => text.toLocaleLowerCase("en-US").replace(/(^|[\s-])\p{L}/gu, (part) => part.toLocaleUpperCase("en-US"));
const several = (text: string): boolean => /^\d+\s+locations?$/iu.test(text);
const remoteOnly = (text: string): boolean => /^\(?remote\b/iu.test(text) || /^remote_/iu.test(text) ||
	/^option to work remote\b/iu.test(text) || /^(?:US|USA)\s*:\s*USA\s+remote$/iu.test(text) ||
	/^(?:US|USA|United States|[A-Z]{2,3})(?:\s*[-–,: ]\s*[A-Z]{2})?\s*[-–,: ]\s*remote\b/iu.test(text) ||
	/^(?:United States|United Kingdom|India|China|Japan|Germany|Canada)\s*[-–]\s*remote\b/iu.test(text);
const cityName = (text: string): string => text.replace(/\s+(?:job posting location|posting location|job location)$/iu, "")
	.replace(/\s*\([^)]*(?:campus|site|office|location)[^)]*\)$/iu, "")
	.replace(/\s+(?:university|memorial|main|medical|north|south|west|east)\s+campus$/iu, "")
	.replace(/\s+(?:campus|site)$/iu, "").trim();

/** A read-side identity; no approximate geocoding or network lookups. */
export function locationChoices(raw: string | null, sourcePlaces: readonly string[] = []): readonly Place[] {
	const display = (raw ?? "").split(/[;|/]/u).map((part) => part.trim()).filter(Boolean);
	const locations = (display.some(several) && sourcePlaces.length ? sourcePlaces : display)
		.flatMap((part) => part.split(/[;|/]/u).map((piece) => piece.trim()).filter(Boolean));
	if (!locations.length) return [{ key: "none", label: "No location" }];
	const choices = new Map<string, Place>();
	const add = (choice: Place) => choices.set(choice.key, choice);
	for (const value of locations) {
		if (several(value)) continue;
		const location = value.trim().replaceAll(/\s+/gu, " ");
		const hasRemote = /(?:^|[^\p{L}])remote(?:\b|_)/iu.test(location);
		if (hasRemote) add({ key: "remote", label: "Remote" });
		if (remoteOnly(location)) continue;
		let local = location.replace(/\s*\(remote\)/giu, "").replace(/\s+or\s+remote(?:\s+U\.?S\.?)?$/iu, "")
			.replace(/^remote\s+location\s*[-–]\s*/iu, "").replace(/^remote\s*[-–,/(]?\s*(?:US|USA|United States)?\)?$/iu, "")
			.replace(/\s*[-–,/]\s*remote(?:\s*[-–,(]?\s*(?:US|USA|United States)\)?)?$/iu, "")
			.replace(/\s*[-–/,]\s*$/u, "").trim();
		if (!local) continue;
		// Workday and Peopleclick use country-state-city paths; other boards use city-state or city, state.
		local = local.replace(/^(?:US|USA|United States)\s*[-–—,]\s*/iu, "").replace(/\s+\d{5}(?:-\d{4})?$/u, "")
			.replace(/\s*[-–—]\s*(?:US|USA|United States)$/iu, "");
		const statePath = /^(.+?)\s*[-–—]\s*(.+)$/u.exec(local);
		if (statePath && STATE_CODE.has(statePath[1]!.toLowerCase())) local = `${statePath[2]}, ${STATE_CODE.get(statePath[1]!.toLowerCase())}`;
		local = local
			.replace(/\s*,\s*(?:United States(?: of America)?|USA|US)$/iu, "")
			.replace(/\s+\(?USA\)?$/iu, "");
		const path = /^([A-Z]{2})\s*[-–—]\s*(.+)$/u.exec(local);
		if (path && STATE_NAMES.has(path[1]!)) local = `${path[2]}, ${path[1]}`;
		const hyphen = /^(.+?)\s*[-–—]\s*([A-Z]{2})$/u.exec(local);
		if (hyphen && STATE_NAMES.has(hyphen[2]!)) local = `${hyphen[1]}, ${hyphen[2]}`;
		const parts = local.split(/\s*,\s*/u).filter(Boolean);
		parts[0] = cityName(parts[0] ?? "");
		if (hasRemote && /\bremote\b/iu.test(parts[0]!)) continue;
		if (parts[1]) parts[1] = parts[1].replace(/\s*\([^)]*(?:campus|site|office)[^)]*\)$/iu, "")
			.replace(/\s+(?:university|memorial|main|medical|north|south|west|east)\s+campus$/iu, "").trim();
		let state = STATE_CODE.get((parts[1] ?? "").toLowerCase()) ?? STATE_CODE.get(local.toLowerCase());
		if (!state && parts.length === 1) {
			const suffix = /^(.+?)\s+([A-Z]{2})$/iu.exec(local);
			if (suffix && STATE_NAMES.has(suffix[2]!.toUpperCase())) { state = suffix[2]!.toUpperCase(); parts.splice(0, 1, cityName(suffix[1]!)); }
		}
		if (!state && parts.length === 1) {
			const known = usCities.get(parts[0]!.toLocaleLowerCase("en-US"));
			if (known?.length === 1) state = known[0];
		}
		if (state) {
			const parent = `state:${state}`;
			add({ key: parent, label: STATE_NAMES.get(state)! });
			if (parts[0] && !STATE_CODE.has(parts[0].toLowerCase())) {
				const city = title(parts[0]);
				add({ key: `city:${city.toLocaleLowerCase("en-US")},${state}`, label: `${city}, ${state}`, parent });
			}
			continue;
		}
		const pathCountry = /^(.+?)\s*[-–—]\s*(.+)$/u.exec(local);
		const leading = pathCountry ? country(pathCountry[1]!) : undefined;
		const trailing = country(parts.at(-1) ?? "");
		const nation = leading ?? trailing ?? (parts.length === 1 ? country(local) : undefined);
		if (nation) {
			const parent = `country:${nation.toLowerCase()}`;
			add({ key: parent, label: nation });
			const city = leading ? pathCountry![2] : trailing && parts.length > 1 ? parts.slice(0, -1).join(", ") : "";
			if (city) {
				const name = title(city);
				add({ key: `city:${name.toLocaleLowerCase("en-US")},${nation.toLowerCase()}`, label: name, parent });
			}
		} else if (!hasRemote) {
			const parent = "other";
			add({ key: parent, label: "Other places" });
			add({ key: `place:${local.toLocaleLowerCase("en-US")}`, label: title(local), parent });
		}
	}
	return choices.size ? [...choices.values()] : [{ key: "several", label: "Several locations" }];
}

function postingPlaces(row: SqlRow): readonly Place[] {
	const raw = optionalTextColumn(row, "location");
	const source = optionalTextColumn(row, "location_places");
	let places: string[] = [];
	if (source) {
		try {
			const parsed = asList(parseJson(source));
			if (parsed !== null) places = parsed.map(asText).filter((item): item is string => item !== null && !several(item));
		} catch { /* The display label still works without source places. */ }
	}
	return locationChoices(raw, places);
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
	for (const row of database.prepare("SELECT location, location_places FROM postings").all()) {
		for (const choice of postingPlaces(row)) {
			const previous = counts.get(choice.key);
			counts.set(choice.key, { ...choice, count: (previous?.count ?? 0) + 1 });
		}
	}
	return [...counts.values()].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, "en-US"));
}

/** Keys are only a read-side predicate. No Posting or Filter Decision is changed. */
export function matchingLocationKeys(database: DatabaseSync, selected: readonly string[]): string | null {
	if (!selected.length) return null;
	const wanted = new Set(selected);
	const keys = database.prepare("SELECT key, location, location_places FROM postings").all()
		.filter((row) => postingPlaces(row).some((choice) => wanted.has(choice.key)))
		.map((row) => textColumn(row, "key"));
	return JSON.stringify(keys);
}
