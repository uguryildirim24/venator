/**
 * An existing Profile as a form, and a form back into the Profile's own files.
 *
 * **This reads YAML, and it is still not a second loader.** `src/venator/profile/` decides
 * what a Profile means, and every save below still ends in `editProfileDocuments`, which
 * stages the result and has the Python loader read it back before anything is renamed. What
 * this module adds is presentation and patching: the three files become fields a person can
 * read, and what they changed goes back into the *same document* — comments, key order, the
 * Jev policy, an entry's `primary` flag, everything the form does not show — rather than
 * into a re-rendering of what the form knows about.
 *
 * Three rules keep that honest:
 *
 * - **Only a difference is written.** The form the person opened is computed again from the
 *   files they opened, and a field that still equals it touches nothing. A file with no
 *   difference in it is returned byte for byte; one with a difference is re-emitted by the
 *   parser that read it, which keeps every comment and every key, at the cost of normalising
 *   indentation and the odd blank line.
 * - **A scalar is edited in place.** `node.value = …` keeps the comment written beside it;
 *   replacing the node would not. A blank résumé field deletes its key, because the renderer
 *   reads an absent key as "not stated" and an empty string as a fact. A blank answer in
 *   `constraints.yaml` becomes `null`, because there null is the value a form is never filled
 *   from.
 * - **The schema is YAML 1.1**, which is PyYAML's. The parser resolves `yes` and `no` the way
 *   the loader will, and the emitter quotes a typed `no` so it stays text.
 */

import { isMap, isScalar, isSeq, parseDocument, type Document, type Pair, type Scalar, type YAMLMap, type YAMLSeq } from "yaml";

import {
	DEGREE_LEVELS,
	HARD_FILTER_RULES,
	isDegreeLevel,
	isRemotePreference,
	isTriState,
	validateProfileForm,
	type ConstraintsForm,
	type EducationForm,
	type EmployerForm,
	type ExperienceForm,
	type ProficiencyForm,
	type ProfileDocuments,
	type ProfileForm,
	type ResumeForm,
	type TargetingForm,
	type TriState,
} from "../../shared/profile-form.ts";
import { systemContext, type LocationContext } from "../locations.ts";
import { BOARD_TOKEN, MAXIMUM_NAME_LENGTH, MAXIMUM_TOKEN_LENGTH, SOURCE_NAME } from "./employers.ts";
import { OnboardingError } from "./errors.ts";
import { asList, asMapping, asNumber, asText, at, type JsonMapping, type JsonValue } from "./json.ts";
import { editProfileDocuments } from "./profile-edit.ts";
import { LONE_SURROGATE } from "./yaml.ts";

type ProfileFile = keyof ProfileDocuments;

const FILE_LABELS = {
	"resume.yaml": "résumé facts",
	"constraints.yaml": "application answers",
	"targeting.yaml": "targeting",
} satisfies Record<ProfileFile, string>;

/** `loader.py: _education_fit_policy` uses this when the Profile names no ceiling. */
const DEFAULT_IMPLAUSIBLE_YEARS = 15;

const EDUCATION_FIELDS = ["org", "location", "date", "degree", "gpa", "coursework"] as const;
const PROFICIENCY_FIELDS = ["label", "items"] as const;
const EXPERIENCE_FIELDS = ["org", "role", "location", "dates"] as const;
const SCREENING_FIELDS = ["salary_expectations", "resides_near_posting", "prior_employment_at_company", "relatives_at_company", "how_heard", "country"] as const;
const EEO_FIELDS = ["gender", "hispanic_latino", "veteran_status", "disability_status"] as const;

/** Long enough for any résumé bullet; short enough that a form cannot be a file upload. */
const MAXIMUM_TEXT_LENGTH = 20_000;
const MAXIMUM_LIST_LENGTH = 400;

/** The codepoints `yaml.ts` escapes because PyYAML refuses or folds them. Refused here: a form has no place for them. */
const UNREADABLE_TO_THE_LOADER = /[\u007F-\u009F\u2028\u2029\uFFFE\uFFFF]/u;

type ParsedDocument = Document;

// ---------------------------------------------------------------- reading nodes

function parse(file: ProfileFile, text: string): ParsedDocument {
	const document = parseDocument(text, { version: "1.1" });
	if (document.errors.length > 0 || (document.contents !== null && !isMap(document.contents))) {
		throw new OnboardingError(
			"invalid_profile",
			`This Profile's ${FILE_LABELS[file]} file is not a YAML document the Profile screen can show.`,
			"Open the file in a text editor to repair it; the screen edits it once it reads as one mapping.",
			file,
		);
	}
	return document;
}

function rootOf(document: ParsedDocument): YAMLMap | null {
	return isMap(document.contents) ? document.contents : null;
}

/** A scalar as text: the string itself, blank for null, and the source spelling of anything else. */
function scalarText(node: Scalar): string {
	const value = node.value;
	if (value === null || value === undefined) return "";
	if (Object.getPrototypeOf(value) === String.prototype) return String(value);
	return node.source ?? String(value);
}

function scalarNumber(node: Scalar): number | null {
	const value = node.value;
	return value !== null && Object.getPrototypeOf(value) === Number.prototype && Number.isInteger(value) ? Number(value) : null;
}

function triStateOf(node: Scalar | undefined): TriState {
	if (node === undefined) return "unanswered";
	if (node.value === true) return "yes";
	if (node.value === false) return "no";
	return "unanswered";
}

function keyText(pair: Pair): string {
	return isScalar(pair.key) ? scalarText(pair.key) : String(pair.key);
}

/** The pair under `key`, looked up by the key's text so a quoted key and a plain one agree. */
function pairAt(parent: YAMLMap | null, key: string): Pair | undefined {
	return parent?.items.find((pair) => keyText(pair) === key);
}

function mapAt(parent: YAMLMap | null, key: string): YAMLMap | null {
	const node = pairAt(parent, key)?.value;
	return isMap(node) ? node : null;
}

function seqAt(parent: YAMLMap | null, key: string): YAMLSeq | null {
	const node = pairAt(parent, key)?.value;
	return isSeq(node) ? node : null;
}

function scalarAt(parent: YAMLMap | null, key: string): Scalar | undefined {
	const node = pairAt(parent, key)?.value;
	return isScalar(node) ? node : undefined;
}

function textAt(parent: YAMLMap | null, key: string): string {
	const node = scalarAt(parent, key);
	return node === undefined ? "" : scalarText(node);
}

function stringsAt(parent: YAMLMap | null, key: string): readonly string[] {
	const list = seqAt(parent, key);
	if (list === null) return [];
	return list.items.flatMap((item) => (isScalar(item) ? [scalarText(item)] : []));
}

/** True when the key is written and is not null: `loader.py`'s `config.get(rule) is not None`. */
function configuredAt(parent: YAMLMap | null, key: string): boolean {
	const pair = pairAt(parent, key);
	if (pair === undefined) return false;
	return !(pair.value === null || (isScalar(pair.value) && (pair.value.value === null || pair.value.value === undefined)));
}

// ---------------------------------------------------------------- the form of a document

function entriesAt<Entry>(root: YAMLMap | null, section: string, read: (item: YAMLMap | null, origin: number) => Entry): readonly Entry[] {
	const list = seqAt(root, section);
	if (list === null) return [];
	return list.items.map((item, origin) => read(isMap(item) ? item : null, origin));
}

function resumeFormOf(document: ParsedDocument): ResumeForm {
	const root = rootOf(document);
	const contact = mapAt(root, "contact");
	return {
		name: textAt(root, "name"),
		contact: { location: textAt(contact, "location"), phone: textAt(contact, "phone"), email: textAt(contact, "email"), linkedin: textAt(contact, "linkedin") },
		education: entriesAt(root, "education", (item, origin) => ({
			origin, org: textAt(item, "org"), location: textAt(item, "location"), date: textAt(item, "date"),
			degree: textAt(item, "degree"), gpa: textAt(item, "gpa"), coursework: textAt(item, "coursework"),
		})),
		technical_proficiencies: entriesAt(root, "technical_proficiencies", (item, origin) => ({ origin, label: textAt(item, "label"), items: textAt(item, "items") })),
		experience: entriesAt(root, "experience", (item, origin) => ({
			origin, org: textAt(item, "org"), role: textAt(item, "role"), location: textAt(item, "location"), dates: textAt(item, "dates"), bullets: stringsAt(item, "bullets"),
		})),
	};
}

function constraintsFormOf(document: ParsedDocument): ConstraintsForm {
	const root = rootOf(document);
	const authorization = mapAt(root, "work_authorization");
	const screening = mapAt(root, "screening");
	const eeo = mapAt(screening, "eeo");
	return {
		work_authorization: {
			status: textAt(authorization, "status"),
			requires_sponsorship: triStateOf(scalarAt(authorization, "requires_sponsorship")),
			authorized_to_work: triStateOf(scalarAt(authorization, "authorized_to_work")),
		},
		screening: {
			salary_expectations: textAt(screening, "salary_expectations"),
			resides_near_posting: textAt(screening, "resides_near_posting"),
			prior_employment_at_company: textAt(screening, "prior_employment_at_company"),
			relatives_at_company: textAt(screening, "relatives_at_company"),
			how_heard: textAt(screening, "how_heard"),
			country: textAt(screening, "country"),
			eeo: { gender: textAt(eeo, "gender"), hispanic_latino: textAt(eeo, "hispanic_latino"), veteran_status: textAt(eeo, "veteran_status"), disability_status: textAt(eeo, "disability_status") },
		},
	};
}

function employersOf(sources: YAMLMap | null): readonly EmployerForm[] {
	const names = new Map<string, string>();
	for (const pair of mapAt(sources, "names")?.items ?? []) {
		if (isScalar(pair.value)) names.set(keyText(pair), scalarText(pair.value));
	}
	const employers: EmployerForm[] = [];
	for (const pair of mapAt(sources, "boards")?.items ?? []) {
		if (!isSeq(pair.value)) continue;
		const source = keyText(pair);
		for (const item of pair.value.items) {
			if (!isScalar(item)) continue;
			const board = scalarText(item);
			employers.push({ source, board, name: names.get(board) ?? "" });
		}
	}
	return employers;
}

function degreeAt(parent: YAMLMap | null, key: string) {
	const text = textAt(parent, key);
	return isDegreeLevel(text) ? text : null;
}

function targetingFormOf(document: ParsedDocument): TargetingForm {
	const root = rootOf(document);
	const search = mapAt(root, "search");
	const filters = mapAt(root, "filters");
	const roleTarget = mapAt(filters, "role_target");
	const educationFit = mapAt(filters, "education_fit");
	const remote = textAt(search, "remote");
	const kill = scalarAt(educationFit, "experience_years_kill");
	const implausible = scalarAt(educationFit, "experience_years_implausible");
	return {
		profileName: textAt(mapAt(root, "profile"), "name"),
		search: { queries: stringsAt(search, "queries"), locations: stringsAt(search, "locations"), remote: isRemotePreference(remote) ? remote : null },
		employers: employersOf(mapAt(root, "sources")),
		filters: {
			enabled: stringsAt(filters, "enabled"),
			configured: HARD_FILTER_RULES.filter((rule) => configuredAt(filters, rule)),
			role_target: {
				levels: entriesAt(roleTarget, "levels", (item) => textAt(item, "name")).filter((name) => name !== ""),
				accept: stringsAt(roleTarget, "accept"),
				exclude_terms: stringsAt(mapAt(roleTarget, "exclude"), "terms"),
			},
			education_fit: {
				holds: degreeAt(educationFit, "holds"),
				in_progress: degreeAt(educationFit, "in_progress"),
				experience_years_kill: kill === undefined ? null : scalarNumber(kill),
				experience_years_implausible: (implausible === undefined ? null : scalarNumber(implausible)) ?? DEFAULT_IMPLAUSIBLE_YEARS,
			},
		},
	};
}

/** The three files as the Profile screen shows them. Reads only. */
export function formOf(documents: ProfileDocuments): ProfileForm {
	return {
		resume: resumeFormOf(parse("resume.yaml", documents["resume.yaml"])),
		constraints: constraintsFormOf(parse("constraints.yaml", documents["constraints.yaml"])),
		targeting: targetingFormOf(parse("targeting.yaml", documents["targeting.yaml"])),
	};
}

// ---------------------------------------------------------------- writing nodes

type ScalarValue = string | number | boolean | null;

/** An empty `{}` or `[]` becomes block style once something is put in it. */
function asBlock<Collection extends YAMLMap | YAMLSeq>(node: Collection): Collection {
	if (node.flow === true && node.items.length === 0) node.flow = false;
	return node;
}

function newMap(document: ParsedDocument): YAMLMap {
	const node = document.createNode({});
	// SAFETY: `createNode({})` builds a YAMLMap; the overload's return type is widened over every input.
	return node as YAMLMap;
}

function newSeq(document: ParsedDocument, values: readonly ScalarValue[] | readonly JsonMapping[]): YAMLSeq {
	const node = document.createNode([...values]);
	// SAFETY: `createNode([...])` builds a YAMLSeq; the overload's return type is widened over every input.
	return node as YAMLSeq;
}

/** The mapping at `path`, created as a block mapping where a key is missing or holds null. */
function ensureMap(document: ParsedDocument, path: readonly string[]): YAMLMap {
	let current: YAMLMap | null = rootOf(document);
	if (current === null) {
		current = newMap(document);
		document.contents = current;
	}
	for (const key of path) {
		const pair = pairAt(current, key);
		if (pair !== undefined && isMap(pair.value)) {
			current = asBlock(pair.value);
			continue;
		}
		const created = newMap(document);
		// Keep the pair — and the comment written above its key — and replace only the value.
		if (pair === undefined) current.set(key, created);
		else pair.value = created;
		current = created;
	}
	return current;
}

/** Edits the scalar in place so the comment beside it stays; creates it where there is none. */
function setScalar(document: ParsedDocument, path: readonly string[], value: ScalarValue): void {
	const key = path[path.length - 1];
	if (key === undefined) return;
	const parent = ensureMap(document, path.slice(0, -1));
	const pair = pairAt(parent, key);
	if (pair !== undefined && isScalar(pair.value)) {
		pair.value.value = value;
		return;
	}
	if (pair === undefined) parent.set(key, value);
	else pair.value = document.createNode(value);
}

/** A policy value cleared: null where the key is written, nothing where it never was. */
function clearScalar(document: ParsedDocument, path: readonly string[]): void {
	if (document.hasIn(path)) setScalar(document, path, null);
}

function deleteKey(document: ParsedDocument, path: readonly string[]): void {
	if (document.hasIn(path)) document.deleteIn(path);
}

function setStrings(document: ParsedDocument, path: readonly string[], values: readonly string[]): void {
	const key = path[path.length - 1];
	if (key === undefined) return;
	const parent = ensureMap(document, path.slice(0, -1));
	const pair = pairAt(parent, key);
	const created = newSeq(document, values);
	if (pair === undefined) parent.set(key, created);
	else pair.value = created;
}

function sameStrings(left: readonly string[], right: readonly string[]): boolean {
	return left.length === right.length && left.every((value, index) => value === right[index]);
}

// ---------------------------------------------------------------- patching each file

type Origins = { readonly baselineCount: number; readonly section: string };

/**
 * Checks that every `origin` names one entry of the file that was opened, once.
 *
 * A form built from a different file than `original` would otherwise patch the wrong entry
 * or delete one that was never shown; both are refused as a stale form.
 */
function checkOrigins(entries: readonly { readonly origin: number | null }[], origins: Origins): void {
	const seen = new Set<number>();
	for (const entry of entries) {
		if (entry.origin === null) continue;
		if (!Number.isInteger(entry.origin) || entry.origin < 0 || entry.origin >= origins.baselineCount || seen.has(entry.origin)) {
			throw new OnboardingError("bad_request", "This Profile changed since you opened it. Reopen it before saving so you do not erase those changes.", null, `resume.${origins.section}`);
		}
		seen.add(entry.origin);
	}
}

/** The list under `section`, made a block list and created where a key is missing or null. */
function ensureSeq(document: ParsedDocument, parent: YAMLMap, key: string): YAMLSeq {
	const pair = pairAt(parent, key);
	if (pair !== undefined && isSeq(pair.value)) return asBlock(pair.value);
	const created = newSeq(document, []);
	created.flow = false;
	if (pair === undefined) parent.set(key, created);
	else pair.value = created;
	return created;
}

type TextEntry = Readonly<Record<string, string | number | null | readonly string[]>>;

function textOf(entry: TextEntry, field: string): string {
	const value = entry[field];
	return value === undefined || value === null || Object.getPrototypeOf(value) !== String.prototype ? "" : String(value);
}

/**
 * Patches one résumé section: edited entries in place by `origin`, removed entries deleted
 * from the highest index down, new entries appended with only the fields they state.
 */
function patchEntries<Entry extends TextEntry & { readonly origin: number | null }>(
	document: ParsedDocument,
	section: string,
	baseline: readonly Entry[],
	form: readonly Entry[],
	fields: readonly string[],
	bullets: boolean,
): boolean {
	checkOrigins(form, { baselineCount: baseline.length, section });
	const kept = new Set(form.flatMap((entry) => (entry.origin === null ? [] : [entry.origin])));
	const added = form.filter((entry) => entry.origin === null);
	const edited = form.filter((entry) => entry.origin !== null);
	const removed = baseline.map((_, index) => index).filter((index) => !kept.has(index));
	let changed = false;
	const root = ensureMap(document, []);
	const list = removed.length > 0 || added.length > 0 || edited.length > 0 ? ensureSeq(document, root, section) : null;
	if (list === null) return false;
	for (const entry of edited) {
		if (entry.origin === null) continue;
		const before = baseline[entry.origin];
		const item = list.items[entry.origin];
		let node = isMap(item) ? item : null;
		const bulletsBefore = before === undefined ? [] : stringsOf(before);
		const bulletsNow = stringsOf(entry);
		const differs = fields.some((field) => before === undefined || textOf(before, field) !== textOf(entry, field)) || (bullets && !sameStrings(bulletsBefore, bulletsNow));
		if (!differs) continue;
		changed = true;
		if (node === null) {
			node = newMap(document);
			list.items[entry.origin] = node;
		}
		for (const field of fields) {
			const value = textOf(entry, field);
			if (before !== undefined && node !== null && textOf(before, field) === value && isMap(item)) continue;
			const pair = pairAt(node, field);
			if (value.trim() === "") {
				if (pair !== undefined) node.delete(pair.key);
			} else if (pair !== undefined && isScalar(pair.value)) pair.value.value = value;
			else if (pair === undefined) node.set(field, value);
			else pair.value = document.createNode(value);
		}
		if (bullets && !sameStrings(bulletsBefore, bulletsNow)) {
			const pair = pairAt(node, "bullets");
			if (bulletsNow.length === 0) {
				if (pair !== undefined) node.delete(pair.key);
			} else {
				const created = newSeq(document, bulletsNow);
				if (pair === undefined) node.set("bullets", created);
				else pair.value = created;
			}
		}
	}
	for (const index of [...removed].sort((left, right) => right - left)) {
		list.items.splice(index, 1);
		changed = true;
	}
	for (const entry of added) {
		const stated: Record<string, string | readonly string[]> = {};
		for (const field of fields) {
			const value = textOf(entry, field);
			if (value.trim() !== "") stated[field] = value;
		}
		const bulletsNow = stringsOf(entry);
		if (bullets && bulletsNow.length > 0) stated["bullets"] = bulletsNow;
		list.add(document.createNode(stated));
		changed = true;
	}
	return changed;
}

function stringsOf(entry: TextEntry): readonly string[] {
	const value = entry["bullets"];
	return value !== undefined && value !== null && Array.isArray(value) ? value : [];
}

function patchResume(document: ParsedDocument, baseline: ResumeForm, form: ResumeForm): boolean {
	let changed = false;
	const text = (path: readonly string[], before: string, now: string) => {
		if (before === now) return;
		changed = true;
		if (now.trim() === "") deleteKey(document, path);
		else setScalar(document, path, now);
	};
	text(["name"], baseline.name, form.name);
	for (const field of ["location", "phone", "email", "linkedin"] as const) text(["contact", field], baseline.contact[field], form.contact[field]);
	if (patchEntries<EducationForm>(document, "education", baseline.education, form.education, EDUCATION_FIELDS, false)) changed = true;
	if (patchEntries<ProficiencyForm>(document, "technical_proficiencies", baseline.technical_proficiencies, form.technical_proficiencies, PROFICIENCY_FIELDS, false)) changed = true;
	if (patchEntries<ExperienceForm>(document, "experience", baseline.experience, form.experience, EXPERIENCE_FIELDS, true)) changed = true;
	return changed;
}

function triStateValue(state: TriState): boolean | null {
	if (state === "yes") return true;
	if (state === "no") return false;
	return null;
}

function patchConstraints(document: ParsedDocument, baseline: ConstraintsForm, form: ConstraintsForm): boolean {
	let changed = false;
	const answer = (path: readonly string[], before: string, now: string) => {
		if (before === now) return;
		changed = true;
		if (now.trim() === "") clearScalar(document, path);
		else setScalar(document, path, now);
	};
	answer(["work_authorization", "status"], baseline.work_authorization.status, form.work_authorization.status);
	for (const field of ["requires_sponsorship", "authorized_to_work"] as const) {
		const before = baseline.work_authorization[field];
		const now = form.work_authorization[field];
		if (before === now) continue;
		changed = true;
		const value = triStateValue(now);
		if (value === null) clearScalar(document, ["work_authorization", field]);
		else setScalar(document, ["work_authorization", field], value);
	}
	for (const field of SCREENING_FIELDS) answer(["screening", field], baseline.screening[field], form.screening[field]);
	for (const field of EEO_FIELDS) answer(["screening", "eeo", field], baseline.screening.eeo[field], form.screening.eeo[field]);
	return changed;
}

function employerKey(employer: EmployerForm): string {
	return `${employer.source}\u0000${employer.board}`;
}

function patchEmployers(document: ParsedDocument, baseline: readonly EmployerForm[], form: readonly EmployerForm[]): boolean {
	const before = new Map(baseline.map((employer) => [employerKey(employer), employer]));
	const now = new Map(form.map((employer) => [employerKey(employer), employer]));
	const removed = baseline.filter((employer) => !now.has(employerKey(employer)));
	const added = form.filter((employer) => !before.has(employerKey(employer)));
	const renamed = form.filter((employer) => {
		const was = before.get(employerKey(employer));
		return was !== undefined && was.name !== employer.name;
	});
	if (removed.length === 0 && added.length === 0 && renamed.length === 0) return false;
	const sources = ensureMap(document, ["sources"]);
	for (const employer of removed) {
		const list = seqAt(mapAt(sources, "boards"), employer.source);
		if (list === null) continue;
		const index = list.items.findIndex((item) => isScalar(item) && scalarText(item) === employer.board);
		if (index >= 0) list.items.splice(index, 1);
	}
	const registered = new Set(form.map((employer) => employer.board));
	const names = mapAt(sources, "names");
	if (names !== null) {
		for (const employer of removed) {
			const pair = pairAt(names, employer.board);
			if (!registered.has(employer.board) && pair !== undefined) names.delete(pair.key);
		}
	}
	for (const employer of added) {
		const boards = ensureMap(document, ["sources", "boards"]);
		ensureSeq(document, boards, employer.source).add(document.createNode(employer.board));
	}
	for (const employer of [...added, ...renamed]) {
		if (employer.name.trim() === "") {
			const registry = mapAt(sources, "names");
			const pair = pairAt(registry, employer.board);
			if (registry !== null && pair !== undefined) registry.delete(pair.key);
			continue;
		}
		const registry = ensureMap(document, ["sources", "names"]);
		const pair = pairAt(registry, employer.board);
		if (pair !== undefined && isScalar(pair.value)) pair.value.value = employer.name;
		else if (pair === undefined) registry.set(employer.board, employer.name);
		else pair.value = document.createNode(employer.name);
	}
	return true;
}

function patchTargeting(document: ParsedDocument, baseline: TargetingForm, form: TargetingForm): boolean {
	let changed = false;
	const strings = (path: readonly string[], before: readonly string[], now: readonly string[]) => {
		if (sameStrings(before, now)) return;
		changed = true;
		setStrings(document, path, now);
	};
	strings(["search", "queries"], baseline.search.queries, form.search.queries);
	strings(["search", "locations"], baseline.search.locations, form.search.locations);
	if (baseline.search.remote !== form.search.remote) {
		changed = true;
		if (form.search.remote === null) clearScalar(document, ["search", "remote"]);
		else setScalar(document, ["search", "remote"], form.search.remote);
	}
	if (patchEmployers(document, baseline.employers, form.employers)) changed = true;
	const filters = baseline.filters;
	strings(["filters", "enabled"], filters.enabled, form.filters.enabled);
	strings(["filters", "role_target", "accept"], filters.role_target.accept, form.filters.role_target.accept);
	strings(["filters", "role_target", "exclude", "terms"], filters.role_target.exclude_terms, form.filters.role_target.exclude_terms);
	const fit = form.filters.education_fit;
	for (const field of ["holds", "in_progress"] as const) {
		if (filters.education_fit[field] === fit[field]) continue;
		changed = true;
		const level = fit[field];
		if (level === null) clearScalar(document, ["filters", "education_fit", field]);
		else setScalar(document, ["filters", "education_fit", field], level);
	}
	if (filters.education_fit.experience_years_kill !== fit.experience_years_kill) {
		changed = true;
		if (fit.experience_years_kill === null) clearScalar(document, ["filters", "education_fit", "experience_years_kill"]);
		else setScalar(document, ["filters", "education_fit", "experience_years_kill"], fit.experience_years_kill);
	}
	return changed;
}

function emit(document: ParsedDocument): string {
	return document.toString({ lineWidth: 0 });
}

/**
 * The three files with the form's differences written into them.
 *
 * `original` is what the person opened: the form is diffed against the form of *those* texts,
 * so an untouched field is untouched in the file whatever it holds, and a file with no
 * difference comes back as the very string that went in.
 */
export function patchProfileDocuments(original: ProfileDocuments, form: ProfileForm): ProfileDocuments {
	const resume = parse("resume.yaml", original["resume.yaml"]);
	const constraints = parse("constraints.yaml", original["constraints.yaml"]);
	const targeting = parse("targeting.yaml", original["targeting.yaml"]);
	const resumeChanged = patchResume(resume, resumeFormOf(parse("resume.yaml", original["resume.yaml"])), form.resume);
	const constraintsChanged = patchConstraints(constraints, constraintsFormOf(parse("constraints.yaml", original["constraints.yaml"])), form.constraints);
	const targetingChanged = patchTargeting(targeting, targetingFormOf(parse("targeting.yaml", original["targeting.yaml"])), form.targeting);
	return {
		"resume.yaml": resumeChanged ? emit(resume) : original["resume.yaml"],
		"constraints.yaml": constraintsChanged ? emit(constraints) : original["constraints.yaml"],
		"targeting.yaml": targetingChanged ? emit(targeting) : original["targeting.yaml"],
	};
}

// ---------------------------------------------------------------- reading a form off the wire

function refuse(field: string, message = "Enter the Profile form as the screen sends it."): OnboardingError {
	return new OnboardingError("bad_request", message, null, field);
}

function textField(mapping: JsonMapping, key: string, field: string): string {
	const value = asText(at(mapping, key));
	if (value === null) throw refuse(field);
	if (value.length > MAXIMUM_TEXT_LENGTH) throw refuse(field, "That is longer than a Profile field holds.");
	if (LONE_SURROGATE.test(value) || UNREADABLE_TO_THE_LOADER.test(value) || /[^\P{Cc}\n\t]/u.test(value)) {
		throw refuse(field, "That field carries a control character the Profile can’t hold.");
	}
	return value;
}

function stringsField(mapping: JsonMapping, key: string, field: string): readonly string[] {
	const list = asList(at(mapping, key));
	if (list === null) throw refuse(field);
	if (list.length > MAXIMUM_LIST_LENGTH) throw refuse(field, "That is more entries than a Profile list holds.");
	return list.map((item, index) => textField({ item }, "item", `${field}.${String(index)}`));
}

function mappingField(mapping: JsonMapping, key: string, field: string): JsonMapping {
	const value = asMapping(at(mapping, key));
	if (value === null) throw refuse(field);
	return value;
}

function listField(mapping: JsonMapping, key: string, field: string): readonly JsonMapping[] {
	const list = asList(at(mapping, key));
	if (list === null) throw refuse(field);
	if (list.length > MAXIMUM_LIST_LENGTH) throw refuse(field, "That is more entries than a Profile list holds.");
	return list.map((item, index) => {
		const entry = asMapping(item);
		if (entry === null) throw refuse(`${field}.${String(index)}`);
		return entry;
	});
}

function originField(mapping: JsonMapping, field: string): number | null {
	const value = at(mapping, "origin");
	if (value === null) return null;
	const origin = asNumber(value);
	if (origin === null || !Number.isInteger(origin) || origin < 0) throw refuse(`${field}.origin`);
	return origin;
}

function triStateField(mapping: JsonMapping, key: string, field: string): TriState {
	const value = textField(mapping, key, field);
	if (!isTriState(value)) throw refuse(field);
	return value;
}

function degreeField(mapping: JsonMapping, key: string, field: string) {
	const value = at(mapping, key);
	if (value === null) return null;
	const text = asText(value);
	if (text === null || !isDegreeLevel(text)) throw refuse(field, `Choose one of ${DEGREE_LEVELS.join(", ")}.`);
	return text;
}

function wholeNumberField(mapping: JsonMapping, key: string, field: string): number | null {
	const value = at(mapping, key);
	if (value === null) return null;
	const number = asNumber(value);
	if (number === null || !Number.isInteger(number) || number < 0) throw refuse(field, "Years is a whole number.");
	return number;
}

function resumeFromBody(body: JsonMapping): ResumeForm {
	const resume = mappingField(body, "resume", "resume");
	const contact = mappingField(resume, "contact", "resume.contact");
	return {
		name: textField(resume, "name", "resume.name"),
		contact: {
			location: textField(contact, "location", "resume.contact.location"),
			phone: textField(contact, "phone", "resume.contact.phone"),
			email: textField(contact, "email", "resume.contact.email"),
			linkedin: textField(contact, "linkedin", "resume.contact.linkedin"),
		},
		education: listField(resume, "education", "resume.education").map((entry, index) => {
			const field = `resume.education.${String(index)}`;
			return {
				origin: originField(entry, field), org: textField(entry, "org", `${field}.org`), location: textField(entry, "location", `${field}.location`),
				date: textField(entry, "date", `${field}.date`), degree: textField(entry, "degree", `${field}.degree`),
				gpa: textField(entry, "gpa", `${field}.gpa`), coursework: textField(entry, "coursework", `${field}.coursework`),
			};
		}),
		technical_proficiencies: listField(resume, "technical_proficiencies", "resume.technical_proficiencies").map((entry, index) => {
			const field = `resume.technical_proficiencies.${String(index)}`;
			return { origin: originField(entry, field), label: textField(entry, "label", `${field}.label`), items: textField(entry, "items", `${field}.items`) };
		}),
		experience: listField(resume, "experience", "resume.experience").map((entry, index) => {
			const field = `resume.experience.${String(index)}`;
			return {
				origin: originField(entry, field), org: textField(entry, "org", `${field}.org`), role: textField(entry, "role", `${field}.role`),
				location: textField(entry, "location", `${field}.location`), dates: textField(entry, "dates", `${field}.dates`),
				bullets: stringsField(entry, "bullets", `${field}.bullets`),
			};
		}),
	};
}

function constraintsFromBody(body: JsonMapping): ConstraintsForm {
	const constraints = mappingField(body, "constraints", "constraints");
	const authorization = mappingField(constraints, "work_authorization", "constraints.work_authorization");
	const screening = mappingField(constraints, "screening", "constraints.screening");
	const eeo = mappingField(screening, "eeo", "constraints.screening.eeo");
	return {
		work_authorization: {
			status: textField(authorization, "status", "constraints.work_authorization.status"),
			requires_sponsorship: triStateField(authorization, "requires_sponsorship", "constraints.work_authorization.requires_sponsorship"),
			authorized_to_work: triStateField(authorization, "authorized_to_work", "constraints.work_authorization.authorized_to_work"),
		},
		screening: {
			salary_expectations: textField(screening, "salary_expectations", "constraints.screening.salary_expectations"),
			resides_near_posting: textField(screening, "resides_near_posting", "constraints.screening.resides_near_posting"),
			prior_employment_at_company: textField(screening, "prior_employment_at_company", "constraints.screening.prior_employment_at_company"),
			relatives_at_company: textField(screening, "relatives_at_company", "constraints.screening.relatives_at_company"),
			how_heard: textField(screening, "how_heard", "constraints.screening.how_heard"),
			country: textField(screening, "country", "constraints.screening.country"),
			eeo: {
				gender: textField(eeo, "gender", "constraints.screening.eeo.gender"),
				hispanic_latino: textField(eeo, "hispanic_latino", "constraints.screening.eeo.hispanic_latino"),
				veteran_status: textField(eeo, "veteran_status", "constraints.screening.eeo.veteran_status"),
				disability_status: textField(eeo, "disability_status", "constraints.screening.eeo.disability_status"),
			},
		},
	};
}

/** The register route's own rules for a source, a token and a name (`employers.ts`), so one Profile is refused by neither and accepted by both. */
function employerFromBody(entry: JsonMapping, field: string): EmployerForm {
	const source = textField(entry, "source", `${field}.source`);
	const board = textField(entry, "board", `${field}.board`);
	const name = textField(entry, "name", `${field}.name`).trim();
	if (!SOURCE_NAME.test(source)) throw refuse(`${field}.source`, "That is not a job board this pipeline knows how to poll.");
	if (board.length > MAXIMUM_TOKEN_LENGTH || !BOARD_TOKEN.test(board)) throw refuse(`${field}.board`, "That is not a board this pipeline can register.");
	if (name.length > MAXIMUM_NAME_LENGTH || /[\n\t]/u.test(name)) throw refuse(`${field}.name`, "Type the employer's name as one line.");
	return { source, board, name };
}

function targetingFromBody(body: JsonMapping): TargetingForm {
	const targeting = mappingField(body, "targeting", "targeting");
	const search = mappingField(targeting, "search", "targeting.search");
	const filters = mappingField(targeting, "filters", "targeting.filters");
	const roleTarget = mappingField(filters, "role_target", "targeting.filters.role_target");
	const educationFit = mappingField(filters, "education_fit", "targeting.filters.education_fit");
	const remoteValue = at(search, "remote");
	const remote = remoteValue === null ? null : asText(remoteValue);
	if (remoteValue !== null && (remote === null || !isRemotePreference(remote))) throw refuse("targeting.search.remote");
	const employers = listField(targeting, "employers", "targeting.employers").map((entry, index) => employerFromBody(entry, `targeting.employers.${String(index)}`));
	const keys = new Set(employers.map(employerKey));
	if (keys.size !== employers.length) throw refuse("targeting.employers", "That board is listed twice.");
	return {
		profileName: textField(targeting, "profileName", "targeting.profileName"),
		search: { queries: stringsField(search, "queries", "targeting.search.queries"), locations: stringsField(search, "locations", "targeting.search.locations"), remote: remote !== null && isRemotePreference(remote) ? remote : null },
		employers,
		filters: {
			enabled: stringsField(filters, "enabled", "targeting.filters.enabled"),
			configured: stringsField(filters, "configured", "targeting.filters.configured"),
			role_target: {
				levels: stringsField(roleTarget, "levels", "targeting.filters.role_target.levels"),
				accept: stringsField(roleTarget, "accept", "targeting.filters.role_target.accept"),
				exclude_terms: stringsField(roleTarget, "exclude_terms", "targeting.filters.role_target.exclude_terms"),
			},
			education_fit: {
				holds: degreeField(educationFit, "holds", "targeting.filters.education_fit.holds"),
				in_progress: degreeField(educationFit, "in_progress", "targeting.filters.education_fit.in_progress"),
				experience_years_kill: wholeNumberField(educationFit, "experience_years_kill", "targeting.filters.education_fit.experience_years_kill"),
				experience_years_implausible: wholeNumberField(educationFit, "experience_years_implausible", "targeting.filters.education_fit.experience_years_implausible") ?? DEFAULT_IMPLAUSIBLE_YEARS,
			},
		},
	};
}

/** The form as the request carried it, or a `bad_request` naming the field that was not one. */
export function profileFormFromBody(value: JsonValue | undefined): ProfileForm {
	const body = asMapping(value);
	if (body === null) throw refuse("form");
	return { resume: resumeFromBody(body), constraints: constraintsFromBody(body), targeting: targetingFromBody(body) };
}

/**
 * Saves a form into the Profile the person opened.
 *
 * The differences are written into the three files, the result is checked by the same rules
 * the screen holds Save on, and then `editProfileDocuments` does what it always did: refuse
 * a stale `original`, stage, have the loader read it back, and rename or roll back.
 */
export async function saveProfileForm(name: string, form: ProfileForm, original: ProfileDocuments, context: LocationContext = systemContext()): Promise<void> {
	const patched = patchProfileDocuments(original, form);
	const issue = validateProfileForm(formOf(patched))[0];
	if (issue !== undefined) throw new OnboardingError("invalid_profile", issue.message, null, issue.field);
	await editProfileDocuments(name, patched, original, context);
}
