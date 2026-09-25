/**
 * Validating and writing one Profile.
 *
 * Two jobs, and the order matters: nothing reaches the filesystem until the whole proposal
 * has been checked, and the check is aimed at one class of outcome above all others — a
 * Profile that *loads* but filters nothing. `src/venator/profile/loader.py` names four such
 * failures explicitly and refuses them; every one is reproduced here, because a Profile
 * written by onboarding that the loader then rejects is a broken Install, and a Profile the
 * loader accepts with no Hard Filter is a worse one: it passes every Posting and looks like it
 * is working.
 *
 * The Python loader stays the arbiter, and since the load-back it is the arbiter in the
 * literal sense as well as the figurative one. This module refuses what the loader refuses and
 * decides nothing the loader would allow; and what it renders is then handed to the loader
 * itself, in a real interpreter, before either rename (`verify.ts`). The checks below are one
 * person's reading of `loader.py` and the emitter is one person's list of what can go wrong in
 * a YAML scalar — that list was measurably incomplete once — so the read-back is what turns
 * both of them from a claim into a check.
 */

import {
	closeSync,
	existsSync,
	fsyncSync,
	lstatSync,
	mkdirSync,
	mkdtempSync,
	openSync,
	realpathSync,
	renameSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";

import { type LocationContext, systemContext, writableProfilesRoot } from "../locations.ts";
import { invalidProfile, OnboardingError } from "./errors.ts";
import { asBoolean, asList, asMapping, asNumber, asText, at, isPresent, keysOf, RESERVED_KEYS } from "./json.ts";
import type { JsonMapping, JsonValue } from "./json.ts";
import { oneWriterPerProfile } from "./one-writer.ts";
import { captureReference, stageResumeReference, validateReferencePdf, type ReferenceCapture } from "./resume-reference.ts";
import { requireLoadableProfile } from "./verify.ts";
import { LONE_SURROGATE, writeYamlDocument, YamlWriteError } from "./yaml.ts";

/** `loader.py: KNOWN_RULES` — the Hard Filters a Profile may enable. */
export const KNOWN_RULES: readonly string[] = ["work_authorization", "education_fit", "role_target", "eligibility"];

/** `schema.py: REMOTE_PREFERENCES`. */
const REMOTE_PREFERENCES: readonly string[] = ["required", "preferred", "acceptable", "no"];

/** `loader.py: _education_fit_policy` uses this when the Profile names no ceiling. */
const DEFAULT_IMPLAUSIBLE_YEARS = 15;

/** The four fields `resume/render.py:310-318` reads with `[]` and therefore requires. */
const REQUIRED_CONTACT_FIELDS: readonly string[] = ["location", "phone", "email", "linkedin"];

/** Long enough for any name a person would choose; short enough to stay a directory name. */
const MAXIMUM_NAME_LENGTH = 64;

export type ProfileFileName = "resume.yaml" | "constraints.yaml" | "targeting.yaml";

export type ProfileProposal = {
	readonly name: string;
	readonly overwrite: boolean;
	readonly referencePdf?: Uint8Array;
	readonly resume: JsonMapping | null;
	readonly constraints: JsonMapping | null;
	readonly targeting: JsonMapping | null;
};

export type WrittenProfile = {
	readonly name: string;
	readonly directory: string;
	readonly files: readonly ProfileFileName[];
	readonly replaced: boolean;
	/** Things worth saying out loud that are not reasons to refuse the write. */
	readonly warnings: readonly string[];
};

/**
 * The name rules `select.py` enforces, plus a character set.
 *
 * `select.py` refuses an empty value, a path separator and a leading dot, because
 * `--profile ""` once resolved to the working directory and loaded a Profile with no Hard
 * Filter. Both separators are refused on every platform, not just the host's: a name is
 * written on one machine and read on another, and `a\b` is one directory here and two there.
 * The character set on top is this surface's own — it is stricter than the loader, never
 * looser, so nothing it accepts is something the pipeline would reject.
 */
export function validateProfileName(raw: string): string {
	const name = raw.trim();
	if (name === "") {
		throw new OnboardingError(
			"invalid_profile_name",
			"A Profile needs a name.",
			"Choose a short name for this search — it becomes the directory the Profile lives in.",
			"name",
		);
	}
	if (name.length > MAXIMUM_NAME_LENGTH) {
		throw new OnboardingError(
			"invalid_profile_name",
			`A Profile name may be at most ${MAXIMUM_NAME_LENGTH} characters.`,
			"Choose something shorter.",
			"name",
		);
	}
	if (name.includes("/") || name.includes("\\")) {
		throw new OnboardingError(
			"invalid_profile_name",
			"A Profile name is a name, not a path.",
			"Use letters, digits, dots, dashes and underscores — no slashes.",
			"name",
		);
	}
	if (name.startsWith(".")) {
		throw new OnboardingError(
			"invalid_profile_name",
			"A Profile name may not begin with a dot.",
			"A leading dot hides the directory, and the pipeline refuses to resolve one.",
			"name",
		);
	}
	if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/u.test(name)) {
		throw new OnboardingError(
			"invalid_profile_name",
			"A Profile name may hold only letters, digits, dots, dashes and underscores, and must begin with a letter or a digit.",
			"Rename it using those characters.",
			"name",
		);
	}
	return name;
}

/**
 * Where a named Profile would be written, having proved it lands inside the writable root.
 *
 * The name has already been checked, so this is belt and braces — but it is the check that
 * holds if the rules above are ever loosened: the parent of the resolved path must be the
 * root itself. A traversal, an absolute path or a name that resolves elsewhere fails here
 * regardless of how it got through.
 */
export function profileDirectoryFor(name: string, context: LocationContext = systemContext()): string {
	const root = writableProfilesRoot(context);
	const target = resolve(root, name);
	if (dirname(target) !== resolve(root) || target === resolve(root)) {
		throw new OnboardingError(
			"invalid_profile_name",
			"That name does not resolve to a Profile directory.",
			"Use a plain name with no path in it.",
			"name",
		);
	}
	return target;
}

function requireMapping(value: JsonValue | undefined, field: string): JsonMapping {
	const mapping = asMapping(value);
	if (mapping === null) throw invalidProfile(field, `${field} must be a mapping.`);
	return mapping;
}

function optionalMapping(value: JsonValue | undefined, field: string): JsonMapping | null {
	if (!isPresent(value)) return null;
	return requireMapping(value, field);
}

function stringList(value: JsonValue | undefined, field: string): readonly string[] {
	if (!isPresent(value)) return [];
	const list = asList(value);
	if (list === null) throw invalidProfile(field, `${field} must be a list.`);
	return list.map((item, index) => {
		const text = asText(item);
		if (text === null || text.trim() === "") {
			throw invalidProfile(`${field}.${index}`, `${field}.${index} must be a non-empty string.`);
		}
		return text;
	});
}

function wholeNumber(value: JsonValue | undefined, field: string): number | null {
	if (!isPresent(value)) return null;
	const numeric = asNumber(value);
	if (numeric === null || !Number.isInteger(numeric) || numeric < 0) {
		throw invalidProfile(field, `${field} must be a whole number.`);
	}
	return numeric;
}

function flag(value: JsonValue | undefined, field: string): boolean | null {
	if (!isPresent(value)) return null;
	const boolean = asBoolean(value);
	if (boolean === null) throw invalidProfile(field, `${field} must be true or false.`);
	return boolean;
}

/** No key anywhere in a Profile may address an object's prototype rather than its contents. */
function refuseReservedKeys(value: JsonValue, field: string): void {
	const mapping = asMapping(value);
	if (mapping !== null) {
		for (const key of keysOf(mapping)) {
			if (RESERVED_KEYS.includes(key)) {
				throw invalidProfile(`${field}.${key}`, `A Profile may not hold a key named "${key}".`);
			}
			refuseReservedKeys(mapping[key] ?? null, `${field}.${key}`);
		}
		return;
	}
	const list = asList(value);
	if (list !== null) list.forEach((item, index) => refuseReservedKeys(item, `${field}.${index}`));
}

/**
 * No text anywhere in a Profile may be a codepoint with no character behind it.
 *
 * The same bound `/employers` holds on an employer name and a board token, held here on the
 * whole Profile — because it is the same defect and this is the route every person goes
 * through first. A lone surrogate in a search term, a Hard Filter's wording, or any other
 * string is written out as an ASCII escape, read back by `venator.profile` without complaint,
 * and then makes `filters_version()` raise `UnicodeEncodeError` — so every `match.run` for
 * that Profile fails from then on, past a load-back that reported success. `yaml.ts` states
 * the measurement and owns the pattern; this is where the whole document is walked for it.
 *
 * Keys as well as values, and at every depth, on `refuseReservedKeys`'s own reasoning: a
 * Profile holds arbitrary nested mappings, `sources.names` puts an employer's board token in
 * *key* position, and a bound that checked only the keys this module knows by name would miss
 * whatever it does not.
 */
function refuseTextThatIsNotText(value: JsonValue, field: string): void {
	const mapping = asMapping(value);
	if (mapping !== null) {
		for (const key of keysOf(mapping)) {
			const here = `${field}.${key}`;
			if (LONE_SURROGATE.test(key)) throw notACharacter(here);
			refuseTextThatIsNotText(mapping[key] ?? null, here);
		}
		return;
	}
	const list = asList(value);
	if (list !== null) {
		list.forEach((item, index) => refuseTextThatIsNotText(item, `${field}.${index}`));
		return;
	}
	const text = asText(value);
	if (text !== null && LONE_SURROGATE.test(text)) throw notACharacter(field);
}

function notACharacter(field: string): OnboardingError {
	return invalidProfile(
		field,
		"That holds half of a surrogate pair, which is not a character.",
		"Send it as text rather than as a `\\u` escape. A Profile carrying one is read back perfectly well and then stops every stage that comes after, so it is refused here instead.",
	);
}

/** Every `*_patterns` list is a regular expression compiled verbatim by the pipeline. */
function warnAboutPatterns(value: JsonValue, field: string, warnings: string[]): void {
	const mapping = asMapping(value);
	if (mapping !== null) {
		for (const key of keysOf(mapping)) {
			const child = mapping[key] ?? null;
			if (key.endsWith("_patterns") && (asList(child)?.length ?? 0) > 0) {
				warnings.push(
					`${field}.${key} holds regular expressions, which the pipeline compiles exactly as written. ` +
						"They are not checked here, and a pattern that backtracks can stall a filter pass.",
				);
			}
			warnAboutPatterns(child, `${field}.${key}`, warnings);
		}
		return;
	}
	const list = asList(value);
	if (list !== null) list.forEach((item, index) => warnAboutPatterns(item, `${field}.${index}`, warnings));
}

function validateIdentity(targeting: JsonMapping, name: string): void {
	const identity = optionalMapping(at(targeting, "profile"), "targeting.profile");
	if (identity === null) return;
	const declared = at(identity, "name");
	if (isPresent(declared)) {
		const text = asText(declared);
		if (text === null || text.trim() === "") {
			throw invalidProfile("targeting.profile.name", "targeting.profile.name must be a non-empty string.");
		}
		if (text.trim() !== name) {
			throw invalidProfile(
				"targeting.profile.name",
				"The name inside targeting.yaml must match the Profile's directory name.",
				"The pipeline stamps a decisions store with this name and refuses a second Profile against it, so the two disagreeing would be reported as somebody else's search.",
			);
		}
	}
	if (flag(at(identity, "scaffold"), "targeting.profile.scaffold") === true) {
		throw invalidProfile(
			"targeting.profile.scaffold",
			"A Profile written here may not be marked as a scaffold.",
			"A scaffold is a template the pipeline never picks on its own, so this Install would have no Profile to run.",
		);
	}
}

function validateSearch(targeting: JsonMapping, warnings: string[]): void {
	const search = optionalMapping(at(targeting, "search"), "targeting.search");
	if (search === null) return;
	for (const key of ["queries", "locations", "keywords", "seniority", "industries"]) {
		stringList(at(search, key), `targeting.search.${key}`);
	}
	const remote = at(search, "remote");
	if (isPresent(remote)) {
		const text = asText(remote);
		if (text === null || !REMOTE_PREFERENCES.includes(text)) {
			throw invalidProfile(
				"targeting.search.remote",
				`targeting.search.remote must be one of ${REMOTE_PREFERENCES.join(", ")}.`,
			);
		}
	}
	const salary = asMapping(at(search, "salary_floor"));
	if (salary === null) wholeNumber(at(search, "salary_floor"), "targeting.search.salary_floor");
	else {
		wholeNumber(at(salary, "amount"), "targeting.search.salary_floor.amount");
		for (const field of ["currency", "period"]) {
			const value = at(salary, field);
			if (isPresent(value) && asText(value) === null) throw invalidProfile(`targeting.search.salary_floor.${field}`, `${field} must be text.`);
		}
	}
	if (stringList(at(search, "queries"), "targeting.search.queries").length === 0) {
		warnings.push("No aggregator search terms are set, so only the ATS boards below will be polled.");
	}
}

function validateSources(targeting: JsonMapping, warnings: string[]): void {
	const sources = optionalMapping(at(targeting, "sources"), "targeting.sources");
	if (sources === null) return;
	const names = optionalMapping(at(sources, "names"), "targeting.sources.names") ?? {};
	for (const token of keysOf(names)) {
		const display = asText(names[token] ?? null);
		if (display === null || display.trim() === "") {
			throw invalidProfile(
				`targeting.sources.names.${token}`,
				`targeting.sources.names.${token} must be an employer display name.`,
			);
		}
	}
	const boards = optionalMapping(at(sources, "boards"), "targeting.sources.boards") ?? {};
	const registered: string[] = [];
	for (const source of keysOf(boards)) {
		registered.push(...stringList(boards[source] ?? null, `targeting.sources.boards.${source}`));
	}
	const unnamed = registered.filter((token) => !Object.hasOwn(names, token));
	if (unnamed.length > 0) {
		warnings.push(
			`${unnamed.length} board${unnamed.length === 1 ? "" : "s"} have no employer display name. ` +
				"Dedup resolves an employer through that map, so Postings from an unnamed board join no duplicate group and the dashboard has no employer to show.",
		);
	}
}

/** `loader.py:352-358` — a rung named in `accept` must exist on the ladder. */
function validateRoleTarget(filters: JsonMapping): void {
	const roleTarget = optionalMapping(at(filters, "role_target"), "targeting.filters.role_target");
	if (roleTarget === null) return;
	const levels = asList(at(roleTarget, "levels")) ?? [];
	const names: string[] = [];
	levels.forEach((level, index) => {
		const rung = requireMapping(level, `targeting.filters.role_target.levels.${index}`);
		const rungName = asText(at(rung, "name"));
		if (rungName === null || rungName.trim() === "") {
			throw invalidProfile(
				`targeting.filters.role_target.levels.${index}.name`,
				"Every rung of the ladder needs a name.",
			);
		}
		if (names.includes(rungName)) {
			throw invalidProfile(
				`targeting.filters.role_target.levels.${index}.name`,
				`"${rungName}" is already a rung on this ladder.`,
			);
		}
		names.push(rungName);
	});
	const accept = stringList(at(roleTarget, "accept"), "targeting.filters.role_target.accept");
	accept.forEach((rung, index) => {
		if (!names.includes(rung)) {
			throw invalidProfile(
				`targeting.filters.role_target.accept.${index}`,
				`"${rung}" is not a rung of this ladder — expected one of ${names.join(", ")}.`,
				"A rung that does not exist accepts nothing, so the whole band would kill every Posting it reads.",
			);
		}
	});
}

/** `loader.py:315-318` — a kill threshold above the implausibility ceiling never fires. */
function validateEducationFit(filters: JsonMapping): void {
	const educationFit = optionalMapping(at(filters, "education_fit"), "targeting.filters.education_fit");
	if (educationFit === null) return;
	flag(at(educationFit, "kill_completed_degree"), "targeting.filters.education_fit.kill_completed_degree");
	const kill = wholeNumber(at(educationFit, "experience_years_kill"), "targeting.filters.education_fit.experience_years_kill");
	const ceiling =
		wholeNumber(
			at(educationFit, "experience_years_implausible"),
			"targeting.filters.education_fit.experience_years_implausible",
		) ?? DEFAULT_IMPLAUSIBLE_YEARS;
	if (kill !== null && kill > ceiling) {
		throw invalidProfile(
			"targeting.filters.education_fit.experience_years_kill",
			`experience_years_kill must not exceed experience_years_implausible (${ceiling}).`,
			"Above the ceiling a stated requirement reads as company history rather than a requirement, so the kill would never fire.",
		);
	}
}

/**
 * `loader.py:381-389` — the failure that fails open.
 *
 * A Profile that writes a filter policy and enables nothing runs no Hard Filter at all, and
 * `apply_filters` reports every Posting as "no Hard Filter is configured; passed".
 */
function validateFilters(targeting: JsonMapping, warnings: string[]): void {
	const filters = optionalMapping(at(targeting, "filters"), "targeting.filters");
	if (filters === null) {
		warnings.push("No eligibility filter policy is set, so listings will not be excluded by profile filters.");
		return;
	}
	const enabled = stringList(at(filters, "enabled"), "targeting.filters.enabled");
	enabled.forEach((rule, index) => {
		if (!KNOWN_RULES.includes(rule)) {
			throw invalidProfile(
				`targeting.filters.enabled.${index}`,
				`"${rule}" is not a Hard Filter — expected one of ${KNOWN_RULES.join(", ")}.`,
			);
		}
	});
	validateEducationFit(filters);
	validateRoleTarget(filters);

	const configured = KNOWN_RULES.filter((rule) => isPresent(at(filters, rule)));
	if (configured.length > 0 && enabled.length === 0) {
		throw invalidProfile(
			"targeting.filters.enabled",
			`This Profile configures ${configured.join(", ")} but enables no Hard Filter, so every Posting would pass unfiltered.`,
			"List the Hard Filters to run, in the order they should run.",
		);
	}
	if (enabled.length === 0) {
		warnings.push("No eligibility filter is enabled, so listings will not be excluded by profile filters.");
	}
}

/**
 * The screening block, checked for the one thing it must never become: an answer nobody gave.
 *
 * Every declared field stays exactly as it arrives, and an absent one is written as null. A
 * sponsorship answer is refused unless `work_authorization.requires_sponsorship` is literally
 * true, which is the same rule the fill planner holds to — a sponsorship answer is never
 * inferred from an immigration status, and the planner is structurally unable to answer one
 * "No". EEO fields are never defaulted here or anywhere else.
 */
function validateConstraints(constraints: JsonMapping): void {
	const authorization = optionalMapping(at(constraints, "work_authorization"), "constraints.work_authorization");
	const requiresSponsorship = authorization === null
		? null
		: flag(at(authorization, "requires_sponsorship"), "constraints.work_authorization.requires_sponsorship");
	const screening = optionalMapping(at(constraints, "screening"), "constraints.screening");
	if (screening === null) return;
	for (const field of keysOf(screening)) {
		if (!/sponsor/iu.test(field)) continue;
		if (isPresent(at(screening, field)) && requiresSponsorship !== true) {
			throw invalidProfile(
				`constraints.screening.${field}`,
				"A sponsorship question is answered only when the Profile states outright that sponsorship is required.",
				"Set work_authorization.requires_sponsorship to true if that is true, or leave this answer blank so a form field is left unmapped rather than guessed.",
			);
		}
	}
}

/** `resume/render.py:310-318` reads these five with `[]`; without them the PDF cannot render. */
function validateResume(resume: JsonMapping): void {
	const name = asText(at(resume, "name"));
	if (name === null || name.trim() === "") {
		throw invalidProfile("resume.name", "A resume needs a name.", "It is printed at the top of the rendered page.");
	}
	const contact = optionalMapping(at(resume, "contact"), "resume.contact");
	if (contact === null) {
		throw invalidProfile(
			"resume.contact",
			`A resume needs a contact block holding ${REQUIRED_CONTACT_FIELDS.join(", ")}.`,
		);
	}
	for (const field of REQUIRED_CONTACT_FIELDS) {
		const value = asText(at(contact, field));
		if (value === null || value.trim() === "") {
			throw invalidProfile(
				`resume.contact.${field}`,
				`resume.contact.${field} is required.`,
				"All four contact fields are printed on one line of the rendered resume, and the renderer reads each of them directly.",
			);
		}
	}
}

/** Everything that can be decided without touching the filesystem. */
export function validateProposal(proposal: ProfileProposal): readonly string[] {
	const warnings: string[] = [];
	for (const [field, mapping] of [
		["resume", proposal.resume],
		["constraints", proposal.constraints],
		["targeting", proposal.targeting],
	] as const) {
		if (mapping === null) continue;
		refuseReservedKeys(mapping, field);
		refuseTextThatIsNotText(mapping, field);
	}

	if (proposal.resume !== null && keysOf(proposal.resume).length > 0) {
		validateResume(proposal.resume);
	} else {
		warnings.push(
			"resume.yaml is being written empty. The renderer needs a name and all four contact fields before it can produce a PDF.",
		);
	}

	if (proposal.constraints !== null) validateConstraints(proposal.constraints);

	if (proposal.targeting === null || keysOf(proposal.targeting).length === 0) {
		warnings.push("targeting.yaml is being written empty, so no profile filters run. Review each listing’s evidence, conflicts, and unknowns.");
	} else {
		validateIdentity(proposal.targeting, proposal.name);
		validateSearch(proposal.targeting, warnings);
		validateSources(proposal.targeting, warnings);
		validateFilters(proposal.targeting, warnings);
		warnAboutPatterns(proposal.targeting, "targeting", warnings);
	}
	return warnings;
}

const HEADERS: ReadonlyMap<ProfileFileName, readonly string[]> = new Map([
	[
		"resume.yaml",
		[
			"Profile: the canonical facts an Application may draw from.",
			"Written by the onboarding flow. Edit it by hand at any time — the pipeline",
			"reads this file, not a copy of it, and Tailoring never invents a fact.",
		],
	],
	[
		"constraints.yaml",
		[
			"Profile: work authorization, and the answers an application form may use.",
			"Written by the onboarding flow. Every screening answer left null stays null:",
			"a form field with no truthful answer is left unmapped rather than guessed,",
			"and an EEO field is never defaulted to a decline.",
		],
	],
	[
		"targeting.yaml",
		[
			"Profile: what to look for, where to look, and how listings are checked for eligibility.",
			"Written by the onboarding flow.",
			"",
			"Editing this file changes filters_version, so the next match run replays the",
			"whole corpus under the new rules. Fields named *_patterns are regular",
			"expressions compiled exactly as written; terms, places, region and cities are",
			"plain words matched literally.",
		],
	],
]);

// Jev's current release has a closed life-sciences domain vocabulary. Setup does not
// ask the person to choose among those domains, so keep all of them rather than
// guessing a field from a search phrase. Outside those fields Jev will flag a
// domain review, not silently exclude the Posting. Every exclusion policy starts
// at review until the person explicitly chooses otherwise in their Profile.
const SHADOW_JEV_POLICY: JsonMapping = {
	policy_version: "onboarding-shadow-1",
	restricted_roles: "review",
	temporary_student_authorization_exclusion: "review",
	no_sponsorship_student: "review",
	no_sponsorship_nonstudent: "review",
	unmet_completed_degree: "review",
	domains: [
		"biochemistry", "laboratory_research", "pharmacy", "quality_control",
		"biomanufacturing", "clinical_research", "computational_life_sciences", "regulatory_science",
	],
};

function targetingWithShadowJev(targeting: JsonMapping | null): JsonMapping {
	const source = targeting ?? {};
	const filters = asMapping(at(source, "filters")) ?? {};
	// Respect an explicit policy or explicit deterministic opt-out. This default
	// belongs to the new Profile only; it never changes a Profile already on disk.
	if (isPresent(at(filters, "qualification_mode")) || isPresent(at(filters, "jev"))) return source;
	return { ...source, filters: { ...filters, qualification_mode: "jev", jev: SHADOW_JEV_POLICY } };
}

function documentFor(file: ProfileFileName, mapping: JsonMapping | null): string {
	return writeYamlDocument(mapping ?? {}, HEADERS.get(file) ?? []);
}

/**
 * Writes one staged file and flushes it before anything is moved into place.
 *
 * Without the flush the rename can reach the disk before the bytes do, and the crash that
 * follows leaves a Profile directory holding three empty files — which loads, and which
 * filters nothing. That is exactly the outcome the staging directory exists to prevent, so
 * the flush is part of the same guarantee rather than an optimisation of it.
 */
export function writeAndFlush(path: string, contents: string): void {
	const handle = openSync(path, "w", 0o600);
	try {
		writeFileSync(handle, contents, { encoding: "utf8" });
		fsyncSync(handle);
	} finally {
		closeSync(handle);
	}
}

/**
 * What a platform says when it refuses to open a directory as a file at all.
 *
 * Windows is the one that matters: `openSync(dir, "r")` throws there rather than returning a
 * handle. The others are the same refusal from filesystems that phrase it differently.
 */
const DIRECTORY_HANDLE_REFUSED: ReadonlySet<string> = new Set(["EISDIR", "EPERM", "EACCES", "EBADF"]);

/** Flushes a directory entry, so the files inside it are durable before it is renamed. */
function flushDirectory(path: string): void {
	// The open is inside the guard, not outside it. Windows does not hand out a handle on a
	// directory this way at all — `openSync(dir, "r")` throws rather than returning one — so
	// with the open outside, the throw escaped this function, landed in writeProfile's
	// rollback, and every onboarding Profile write reported "The Profile could not be
	// written". The tolerance the catch was written for is exactly the same tolerance the
	// open needs.
	let handle: number;
	try {
		handle = openSync(path, "r");
	} catch (cause) {
		// Narrowed to the errnos that mean "this platform will not hand out a handle on a
		// directory", which is the whole reason the guard is here. An ENOENT on the staging
		// directory means the write itself has gone wrong and must still surface — a catch
		// that swallows everything would turn that into a silently half-written Profile.
		// SAFETY: node's fs throws an Error carrying its errno in `code`; the assertion
		// reads that one optional field and treats its absence as "not one of ours".
		const code = cause instanceof Error ? (cause as NodeJS.ErrnoException).code : undefined;
		if (code === undefined || !DIRECTORY_HANDLE_REFUSED.has(code)) throw cause;
		// The per-file flush above is the part that matters; this one is belt to its braces.
		return;
	}
	try {
		fsyncSync(handle);
	} catch {
		// Some filesystems refuse fsync on a directory handle. The per-file flush above is
		// the part that matters; this one is belt to its braces.
	} finally {
		closeSync(handle);
	}
}

/** Refuses a target that is a symbolic link, whatever it points at. */
function refuseLink(path: string, what: string): void {
	const stats = lstatSync(path, { throwIfNoEntry: false });
	if (stats !== undefined && stats.isSymbolicLink()) {
		throw new OnboardingError(
			"write_failed",
			`${what} is a symbolic link, and onboarding will not write through one.`,
			"Replace it with a real directory, or point $VENATOR_HOME somewhere else.",
		);
	}
}

/**
 * Writes the three files, atomically, into the application data directory.
 *
 * Atomic means the directory, not the file: everything is written into a temporary directory
 * beside the target and moved into place with one rename, so an interruption leaves either
 * the Profile that was there before or the whole new one — never a directory holding a
 * targeting.yaml and no constraints.yaml, which is a Profile that loads and filters nothing.
 *
 * Replacing an existing Profile is two renames with a gap between them. The gap is a Profile
 * that does not exist, and a Profile that does not exist is an error the pipeline reports by
 * name rather than a half-Profile it silently runs — which is the direction to fail in.
 *
 * **The three files are read back before either rename.** Everything below refuses what
 * `loader.py` refuses, and that list is one person's reading of the loader rather than the
 * loader; the emitter is likewise only as correct as its author's list of what can go wrong in
 * a YAML scalar, and it was measurably wrong about one. So the staged directory is handed to
 * `src/venator/profile/` itself (`verify.ts`) while it is still a staging directory, and a
 * Profile the loader will not read is a rejected request rather than a broken Install. A
 * pasted employer name, a search term, or a Profile name carrying a single U+0085 each
 * produced an unloadable Profile from first setup before this was here.
 */
export async function writeProfile(
	proposal: ProfileProposal,
	context: LocationContext = systemContext(),
	capture: ReferenceCapture = captureReference,
): Promise<WrittenProfile> {
	const warnings = [...validateProposal(proposal)];
	if (proposal.referencePdf !== undefined) validateReferencePdf(proposal.referencePdf);
	// Validating is pure and stays outside the queue. Everything from the existence check to
	// the rename is one read-modify-write with an `await` in the middle of it, so it runs alone
	// per Profile: `one-writer.ts` says what two of them did to each other before this.
	return oneWriterPerProfile(profileDirectoryFor(proposal.name, context), () =>
		writeProfileAlone(proposal, warnings, context, capture),
	);
}

/**
 * The write itself, which only ever runs one at a time per Profile.
 *
 * `warnings` arrives already collected and is appended to here, because the last of them is
 * about where the Profile actually landed and that is not known until the rename.
 */
async function writeProfileAlone(
	proposal: ProfileProposal,
	warnings: string[],
	context: LocationContext,
	capture: ReferenceCapture,
): Promise<WrittenProfile> {
	const root = writableProfilesRoot(context);
	const requested = profileDirectoryFor(proposal.name, context);

	mkdirSync(root, { recursive: true, mode: 0o700 });
	refuseLink(root, "The Profiles directory");
	// Re-resolve through the real root: a symlinked parent must not move the target out.
	const realRoot = realpathSync(root);
	const target = join(realRoot, proposal.name);
	if (dirname(target) !== realRoot) {
		throw new OnboardingError("write_failed", "That name does not resolve inside the Profiles directory.", null, "name");
	}
	refuseLink(target, "That Profile");

	const exists = existsSync(target);
	if (exists && !proposal.overwrite) {
		throw new OnboardingError(
			"profile_exists",
			`A Profile named "${proposal.name}" already exists in this Install.`,
			"Choose another name, or send overwrite: true to replace the one that is there.",
			"name",
		);
	}
	if (exists && !lstatSync(target).isDirectory()) {
		throw new OnboardingError("write_failed", "Something that is not a Profile directory is already using that name.");
	}

	// Rendered before anything is created, so a payload the writer refuses is a rejected
	// request rather than a failed write — and so nothing can fail once staging has begun.
	const files: ProfileFileName[] = ["resume.yaml", "constraints.yaml", "targeting.yaml"];
	const documents = new Map<ProfileFileName, string>();
	for (const file of files) {
		const mapping =
			file === "resume.yaml" ? proposal.resume : file === "constraints.yaml" ? proposal.constraints : exists ? proposal.targeting : targetingWithShadowJev(proposal.targeting);
		try {
			documents.set(file, documentFor(file, mapping));
		} catch (cause) {
			if (!(cause instanceof YamlWriteError)) throw cause;
			throw invalidProfile(file.replace(".yaml", ""), `This Profile cannot be written: ${cause.message}.`);
		}
	}

	const staging = mkdtempSync(join(realRoot, `.onboarding-${proposal.name}-`));
	let retired: string | null = null;
	try {
		for (const file of files) {
			writeAndFlush(join(staging, file), documents.get(file) ?? "");
		}
		warnings.push(...await stageResumeReference(staging, exists ? target : null, proposal.referencePdf, context, capture));
		flushDirectory(staging);
		// Before either rename, and therefore before anything a run could load exists: a
		// refusal here leaves the Profile that was on disk exactly as it was, and leaves an
		// absent one absent.
		await requireLoadableProfile(staging, "the Profile", context);
		if (exists) {
			retired = `${staging}.replaced`;
			renameSync(target, retired);
		}
		renameSync(staging, target);
	} catch (cause) {
		// Roll back to whatever was there before, and never let the rollback replace the
		// reason the write failed with a reason the rollback failed.
		try {
			rmSync(staging, { recursive: true, force: true });
			if (retired !== null && !existsSync(target)) renameSync(retired, target);
			else if (retired !== null) rmSync(retired, { recursive: true, force: true });
		} catch {
			process.stderr.write("onboarding: a Profile write failed and could not be fully rolled back\n");
		}
		// The load-back's refusal is already the answer, worded for the person reading it, and
		// it says something different from "the Profile could not be written": the write would
		// have worked, and the document it would have written is one the pipeline cannot read.
		if (cause instanceof OnboardingError) throw cause;
		process.stderr.write(`onboarding: writing a Profile failed: ${cause instanceof Error ? cause.message : "unknown"}\n`);
		throw new OnboardingError(
			"write_failed",
			"The Profile could not be written.",
			"Check that this account can write to its own application data directory, then try again. Nothing was left half-written.",
		);
	}
	if (retired !== null) rmSync(retired, { recursive: true, force: true });

	if (requested !== target) {
		warnings.push("The Profiles directory is reached through a link, so the Profile was written to the directory it really points at.");
	}
	return { name: proposal.name, directory: target, files, replaced: exists, warnings };
}
