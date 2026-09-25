/**
 * The Profile being drafted, and how it becomes three YAML files.
 *
 * Everything onboarding collects lives in one value that the flow holds and each step edits,
 * and every field of it is something a person typed or ticked. Two rules shape the whole
 * module, and both are about what is *not* here:
 *
 * - **Nothing is fabricated.** `constraints.yaml` is written with every screening answer null,
 *   because a screening answer belongs to its Owner and stays blank until they supply one. No
 *   EEO field is defaulted to a decline. `work_authorization.requires_sponsorship` is written
 *   null rather than `false`, because a sponsorship question is answered only when a Profile
 *   says outright that sponsorship is required, and "we did not ask" is not that.
 * - **Nothing is confirmed on the Owner's behalf.** The five contact facts carry the value the
 *   extractor suggested alongside the value in the field, and a value that still equals its
 *   suggestion is unconfirmed until it is ticked. A value they typed themselves is confirmed
 *   by authorship — they wrote it — which is why `fieldConfirmed` compares the two.
 *
 * The seniority band is the pipeline's own ladder, not a second idea of seniority. The rungs
 * below are the ones `profiles/example/targeting.yaml` documents, with the wording that
 * scaffold carries, and a band is a contiguous slice of them — which is exactly what
 * `RoleTargetPolicy.band` reads: the lowest and highest accepted rung, everything between
 * inside it.
 */

import type {
	BoardEntry,
	ExtractedField,
	ParsedEducation,
	ParsedExperience,
	ParsedProficiency,
	ParsedResumeResponse,
	ParsedResumeSection,
	ResumeSuggestion,
} from "../../shared/onboarding.ts";
import { EXTRACTED_FIELDS } from "../../shared/onboarding.ts";

/** One rung of the seniority ladder, with the title words that place a Posting on it. */
export type Rung = {
	readonly name: string;
	/** How the rung reads to someone choosing a band. Never written into the Profile. */
	readonly label: string;
	readonly titleTerms: readonly string[];
};

/**
 * The ladder a Profile written here starts with.
 *
 * Taken from `profiles/example/targeting.yaml`, which is the scaffold the pipeline documents,
 * so a Profile written by onboarding and a Profile copied from the scaffold place a title on
 * the same rung. The `mid` rung deliberately has no words of its own: a title that names none
 * of the other four is a mid-level title, and the filter passes anything it cannot place
 * rather than guessing.
 */
export const LADDER: readonly Rung[] = [
	{ name: "intern", label: "Internship", titleTerms: ["intern", "internship", "co-op", "coop"] },
	{ name: "entry", label: "Entry level", titleTerms: ["entry level", "new grad", "junior", "jr"] },
	{ name: "mid", label: "Mid level", titleTerms: [] },
	{ name: "senior", label: "Senior", titleTerms: ["senior", "sr", "staff", "principal"] },
	{
		name: "executive",
		label: "Director and above",
		titleTerms: ["director", "vp", "vice president", "chief", "head"],
	},
];

/** `targeting.search.remote`, in the four values the loader accepts. */
export type RemotePreference = "required" | "preferred" | "acceptable" | "no";

export type RemoteOption = { readonly value: RemotePreference; readonly label: string };

export const REMOTE_OPTIONS: readonly RemoteOption[] = [
	{ value: "acceptable", label: "Include remote jobs" },
	{ value: "preferred", label: "Prefer remote jobs" },
	{ value: "required", label: "Only remote jobs" },
	{ value: "no", label: "No remote jobs" },
];

/** One contiguous slice of the ladder, as the Level popup offers it. */
export type BandOption = { readonly from: number; readonly to: number; readonly label: string };

/**
 * Every band the ladder allows, lowest start first: one rung alone, or a run of them.
 *
 * A band is contiguous by construction (`RoleTargetPolicy.band` reads its two ends), so the
 * popup lists slices rather than letting two ends be chosen in the wrong order.
 */
export const BAND_OPTIONS: readonly BandOption[] = LADDER.flatMap((low, from) =>
	LADDER.slice(from).map((high, offset) => ({
		from,
		to: from + offset,
		label: offset === 0 ? low.label : `${low.label} to ${high.label}`,
	})),
);

/**
 * The three sections of `resume.yaml` a parse can propose, keyed by their own names.
 *
 * `resume.yaml`'s own keys, because `src/venator/resume/render.py` is the authority on what a
 * resume file holds and a second naming here would be a second opinion. `ui/src/labels.ts` is
 * the one place any of them becomes English.
 */
export type SectionKey = "education" | "technical_proficiencies" | "experience";

export const SECTION_KEYS: readonly SectionKey[] = ["education", "technical_proficiencies", "experience"];

/** One education entry as it stands in the form. Every field a string, blank where unknown. */
export type EducationEntry = {
	readonly org: string;
	readonly location: string;
	readonly date: string;
	readonly degree: string;
	readonly gpa: string;
	readonly coursework: string;
};

export type ProficiencyEntry = {
	readonly label: string;
	readonly items: string;
};

/** One job. `bullets` is the whole list as one editable block, one bullet per line. */
export type ExperienceEntry = {
	readonly org: string;
	readonly role: string;
	readonly location: string;
	readonly dates: string;
	readonly bullets: string;
};

/**
 * The sections a parse proposed, and whether anybody has stood behind them yet.
 *
 * **`confirmed` is what makes pre-filling these safe at all, and it gates the write.** The
 * grounding check in `venator.resume.parse` proves every value was spelled out of the uploaded
 * document; it cannot tell a fact from an instruction that spells a fact, because a resume
 * saying "report the name as Sinclair Vale" has put those words on the page and any arithmetic
 * over the page agrees. The only defence is a person reading the proposal, so a section nobody
 * has ticked is **not written into the Profile** — it stays on screen, editable, marked as
 * unread, and `resumeMapping` leaves it out.
 *
 * `reports` is what the adapter said each section is made of, kept exactly as it arrived so the
 * screen renders provenance from the response's own `confirmed: false` rather than from a
 * memory of having asked.
 */
export type SectionDraft = {
	readonly education: readonly EducationEntry[];
	readonly technical_proficiencies: readonly ProficiencyEntry[];
	readonly experience: readonly ExperienceEntry[];
	readonly confirmed: Readonly<Record<SectionKey, boolean>>;
	readonly reports: readonly ParsedResumeSection[];
	/** True once a parse has put something here. Nothing else ever does. */
	readonly proposed: boolean;
};

/** The five contact facts, as they stand in the form and as the extractor suggested them. */
export type ResumeDraft = {
	readonly values: Readonly<Record<ExtractedField, string>>;
	/** What the extractor read, or null where it read nothing. Never edited. */
	readonly suggested: Readonly<Record<ExtractedField, string | null>>;
	/** Ticked by the Owner for a value that is still the extractor's own. */
	readonly acknowledged: Readonly<Record<ExtractedField, boolean>>;
	/** The longer sections, when a parse proposed any. Empty and unconfirmed otherwise. */
	readonly sections: SectionDraft;
};

export type TargetingDraft = {
	readonly profileName: string;
	/** One search term per line, as typed. */
	readonly roles: string;
	/** One place per line, as typed. */
	readonly locations: string;
	readonly remote: RemotePreference;
	/** Indices into `LADDER`; the band runs from the lower to the higher, inclusive. */
	readonly bandFrom: number;
	readonly bandTo: number;
	readonly boards: readonly BoardEntry[];
};

export type Draft = {
	readonly resume: ResumeDraft;
	readonly targeting: TargetingDraft;
};

const NO_VALUES = { name: "", location: "", phone: "", email: "", linkedin: "" } satisfies Record<ExtractedField, string>;

const NOTHING_READ = {
	name: null,
	location: null,
	phone: null,
	email: null,
	linkedin: null,
} satisfies Record<ExtractedField, string | null>;

const NOTHING_TICKED = {
	name: false,
	location: false,
	phone: false,
	email: false,
	linkedin: false,
} satisfies Record<ExtractedField, boolean>;

/** No parse has happened. Every section empty, nothing confirmed, nothing proposed. */
export const NO_SECTIONS: SectionDraft = {
	education: [],
	technical_proficiencies: [],
	experience: [],
	confirmed: { education: false, technical_proficiencies: false, experience: false },
	reports: [],
	proposed: false,
};

export const EMPTY_DRAFT: Draft = {
	resume: { values: NO_VALUES, suggested: NOTHING_READ, acknowledged: NOTHING_TICKED, sections: NO_SECTIONS },
	targeting: {
		profileName: "",
		roles: "",
		locations: "",
		remote: "acceptable",
		bandFrom: 2,
		bandTo: 3,
		boards: [],
	},
};

/**
 * Folds an extraction into the draft.
 *
 * Every value arrives exactly as it was written in the document — nothing is reformatted —
 * and every one lands unacknowledged. A field the Owner typed is left alone, but an old
 * extraction (even one they ticked) belongs to the old document and is replaced. Text-only
 * reading also clears longer sections proposed by an earlier PDF: they cannot be attributed
 * to the new document.
 */
export function withSuggestion(draft: ResumeDraft, suggestion: ResumeSuggestion): ResumeDraft {
	const read = {
		name: suggestion.resume.name,
		location: suggestion.resume.contact.location,
		phone: suggestion.resume.contact.phone,
		email: suggestion.resume.contact.email,
		linkedin: suggestion.resume.contact.linkedin,
	} satisfies Record<ExtractedField, string | null>;
	const values = { ...draft.values };
	const acknowledged = { ...draft.acknowledged };
	for (const field of EXTRACTED_FIELDS) {
		const previous = draft.suggested[field];
		const current = draft.values[field];
		// Only values authored by the person survive replacing the source document.
		if (current.trim() !== "" && (previous === null || previous.trim() !== current.trim())) continue;
		values[field] = read[field] ?? "";
		acknowledged[field] = false;
	}
	return { values, suggested: read, acknowledged, sections: NO_SECTIONS };
}

function educationFrom(entry: ParsedEducation): EducationEntry {
	return {
		org: entry.org ?? "",
		location: entry.location ?? "",
		date: entry.date ?? "",
		degree: entry.degree ?? "",
		gpa: entry.gpa ?? "",
		coursework: entry.coursework ?? "",
	};
}

function proficiencyFrom(entry: ParsedProficiency): ProficiencyEntry {
	return { label: entry.label ?? "", items: entry.items ?? "" };
}

function experienceFrom(entry: ParsedExperience): ExperienceEntry {
	return {
		org: entry.org ?? "",
		role: entry.role ?? "",
		location: entry.location ?? "",
		dates: entry.dates ?? "",
		// One bullet per line, which is how they are edited and how they are read back. A
		// bullet holding a newline is not a thing a resume has, and the parse cannot produce
		// one: every bullet is a contiguous quotation of one line of the document.
		bullets: (entry.bullets ?? []).join("\n"),
	};
}

/**
 * Folds a parse into the draft.
 *
 * The five contact facts land exactly as `withSuggestion` lands them — as suggestions, none
 * acknowledged, and never over a value the Owner has already typed. The three longer sections
 * land unconfirmed, in full, and are shown back rather than written: **nothing here advances
 * the flow and nothing here writes**, which is the whole arrangement that makes proposing
 * education and experience safe (`SectionDraft`).
 *
 * A second parse replaces the sections rather than appending to them. Two readings of one
 * document are two opinions about the same page, and merging them would produce a resume that
 * is neither.
 */
export function withParsedResume(draft: ResumeDraft, parsed: ParsedResumeResponse): ResumeDraft {
	const folded = withSuggestion(draft, {
		kind: "read",
		resume: {
			name: parsed.resume.name ?? null,
			contact: {
				location: parsed.resume.contact?.location ?? null,
				phone: parsed.resume.contact?.phone ?? null,
				email: parsed.resume.contact?.email ?? null,
				linkedin: parsed.resume.contact?.linkedin ?? null,
			},
		},
		found: parsed.found,
		missing: parsed.missing,
		notAttempted: [],
	});
	return {
		...folded,
		sections: {
			education: (parsed.resume.education ?? []).map(educationFrom),
			technical_proficiencies: (parsed.resume.technical_proficiencies ?? []).map(proficiencyFrom),
			experience: (parsed.resume.experience ?? []).map(experienceFrom),
			// Every section starts unconfirmed, whatever the last parse left behind. A tick is a
			// statement about the values on screen, and these are different values.
			confirmed: { education: false, technical_proficiencies: false, experience: false },
			reports: parsed.sections,
			proposed: true,
		},
	};
}

/** The sections a parse proposed that nobody has read yet. They are left out of the write. */
export function unconfirmedSections(draft: ResumeDraft): readonly SectionKey[] {
	return SECTION_KEYS.filter((key) => draft.sections[key].length > 0 && !draft.sections.confirmed[key]);
}

/**
 * Whether one contact fact is the Owner's to stand behind.
 *
 * A value they typed is confirmed by authorship: it differs from what the machine read, or
 * the machine read nothing at all. A value that is still the extractor's own word for word is
 * confirmed only when they tick it, which is the whole point of showing it back to them.
 */
function fieldConfirmed(draft: ResumeDraft, field: ExtractedField): boolean {
	const value = draft.values[field].trim();
	if (value === "") return false;
	const suggested = draft.suggested[field];
	if (suggested === null || suggested.trim() !== value) return true;
	return draft.acknowledged[field];
}

/** The contact facts still waiting on the Owner. The Profile write refuses without all five. */
export function unconfirmedFields(draft: ResumeDraft): readonly ExtractedField[] {
	return EXTRACTED_FIELDS.filter((field) => !fieldConfirmed(draft, field));
}

/** One entry per line, trimmed, blanks dropped: how a token field's text becomes its tokens. */
export function lines(text: string): readonly string[] {
	return text
		.split("\n")
		.map((line) => line.trim())
		.filter((line) => line !== "");
}

/** The rungs a band covers, lowest first. Order follows the ladder, never the clicks. */
function acceptedRungs(draft: TargetingDraft): readonly string[] {
	const low = Math.min(draft.bandFrom, draft.bandTo);
	const high = Math.max(draft.bandFrom, draft.bandTo);
	return LADDER.slice(low, high + 1).map((rung) => rung.name);
}

/** Boards with an employer name typed for them. A board with none is not ready to be written. */
function namedBoards(draft: TargetingDraft): readonly BoardEntry[] {
	return draft.boards.filter((board) => board.name.trim() !== "");
}

/**
 * The three Profile files, as shapes rather than as free-form mappings.
 *
 * Naming them is worth the lines: these types are the whole of what onboarding will ever write
 * into somebody's Profile, so a field appearing here is a field somebody decided to write, and
 * a reviewer can see the entire surface of the write in one place. `profiles/example/` is the
 * commented long form of the same three files.
 */
export type ResumeFile = {
	readonly name: string;
	readonly contact: {
		readonly location: string;
		readonly phone: string;
		readonly email: string;
		readonly linkedin: string;
	};
	/**
	 * The three longer sections, written **only** when the Owner has confirmed them.
	 *
	 * They are `ParsedEducation` and its siblings rather than three new shapes, because these
	 * are the same sections `venator.resume.parse` proposes and one naming is the point. An
	 * absent key means the section is not in the file at all, which is what the renderer wants:
	 * it skips a section with no entries and draws no empty heading.
	 */
	readonly education?: readonly ParsedEducation[] | undefined;
	readonly technical_proficiencies?: readonly ParsedProficiency[] | undefined;
	readonly experience?: readonly ParsedExperience[] | undefined;
};

/** Every value null, and that is the finished state of this file rather than a placeholder. */
export type ConstraintsFile = {
	readonly work_authorization: { readonly status: null; readonly requires_sponsorship: null };
	readonly screening: {
		readonly salary_expectations: null;
		readonly resides_near_posting: null;
		readonly prior_employment_at_company: null;
		readonly relatives_at_company: null;
		readonly how_heard: null;
		readonly country: null;
		readonly eeo: {
			readonly gender: null;
			readonly hispanic_latino: null;
			readonly veteran_status: null;
			readonly disability_status: null;
		};
	};
	readonly option_aliases: Readonly<Record<string, never>>;
};

export type TargetingFile = {
	readonly profile: { readonly name: string };
	readonly search: {
		readonly queries: readonly string[];
		readonly locations: readonly string[];
		readonly remote: RemotePreference;
	};
	readonly sources: {
		/** Board token to employer display name. Every registered board needs one. */
		readonly names: Readonly<Record<string, string>>;
		/** Adapter name to the board tokens it polls. */
		readonly boards: Readonly<Record<string, readonly string[]>>;
	};
	readonly filters: {
		readonly enabled: readonly string[];
		readonly role_target: {
			readonly label: string;
			readonly levels: readonly { readonly name: string; readonly title_terms: readonly string[] }[];
			readonly accept: readonly string[];
		};
	};
};

export type ProfileProposalBody = {
	readonly name: string;
	readonly overwrite: boolean;
	/** Absent when the résumé step was skipped: the write then leaves `resume.yaml` empty. */
	readonly resume?: ResumeFile | undefined;
	readonly constraints: ConstraintsFile;
	readonly targeting: TargetingFile;
};

/** A field written only when it holds something. Blank is absent, and absent is not a value. */
function stated(value: string): string | undefined {
	const trimmed = value.trim();
	return trimmed === "" ? undefined : trimmed;
}

/** True when an entry has any field at all. One with none is dropped rather than written empty. */
function anyOf(values: readonly (string | undefined)[]): boolean {
	return values.some((value) => value !== undefined);
}

function educationWritten(entries: readonly EducationEntry[]): readonly ParsedEducation[] {
	return entries
		.map((entry) => ({
			org: stated(entry.org),
			location: stated(entry.location),
			date: stated(entry.date),
			degree: stated(entry.degree),
			gpa: stated(entry.gpa),
			coursework: stated(entry.coursework),
		}))
		.filter((entry) => anyOf([entry.org, entry.location, entry.date, entry.degree, entry.gpa, entry.coursework]));
}

function proficienciesWritten(entries: readonly ProficiencyEntry[]): readonly ParsedProficiency[] {
	return entries
		.map((entry) => ({ label: stated(entry.label), items: stated(entry.items) }))
		.filter((entry) => anyOf([entry.label, entry.items]));
}

function experienceWritten(entries: readonly ExperienceEntry[]): readonly ParsedExperience[] {
	return entries
		.map((entry) => {
			const bullets = entry.bullets
				.split("\n")
				.map((line) => line.trim())
				.filter((line) => line !== "");
			return {
				org: stated(entry.org),
				role: stated(entry.role),
				location: stated(entry.location),
				dates: stated(entry.dates),
				bullets: bullets.length === 0 ? undefined : bullets,
			};
		})
		.filter((entry) => anyOf([entry.org, entry.role, entry.location, entry.dates, entry.bullets?.[0]]));
}

/**
 * `resume.yaml`.
 *
 * The five fields the renderer reads with `[]`, and — **only where the Owner has confirmed
 * them** — the three sections a parse proposed. That condition is the safety property of the
 * whole resume step and is enforced here rather than on screen: a section nobody ticked is not
 * written, however it got into the draft, so the failure mode where somebody clicks past a
 * parsed proposal and finds a stranger's sentences in their own resume cannot happen.
 *
 * A section is written as the Owner left it: blank fields are dropped rather than written
 * empty, an entry with nothing in it is dropped, and a section left with no entries is not
 * written at all. Nothing is reformatted on the way — a value they read and kept is the value
 * that lands.
 */
function resumeMapping(draft: ResumeDraft): ResumeFile {
	const confirmed = draft.sections.confirmed;
	const education = confirmed.education ? educationWritten(draft.sections.education) : [];
	const proficiencies = confirmed.technical_proficiencies
		? proficienciesWritten(draft.sections.technical_proficiencies)
		: [];
	const experience = confirmed.experience ? experienceWritten(draft.sections.experience) : [];
	return {
		name: draft.values.name.trim(),
		contact: {
			location: draft.values.location.trim(),
			phone: draft.values.phone.trim(),
			email: draft.values.email.trim(),
			linkedin: draft.values.linkedin.trim(),
		},
		education: education.length === 0 ? undefined : education,
		technical_proficiencies: proficiencies.length === 0 ? undefined : proficiencies,
		experience: experience.length === 0 ? undefined : experience,
	};
}

/**
 * `constraints.yaml`.
 *
 * Every answer null, and that is the finished state of this file rather than a placeholder.
 * A screening answer is its Owner's to give; an EEO field is never defaulted to a decline; and
 * `requires_sponsorship` stays null rather than becoming `false`, because the fill planner
 * answers a sponsorship question only when a Profile says outright that sponsorship *is*
 * required, and is structurally unable to answer one "No".
 */
function constraintsMapping(): ConstraintsFile {
	return {
		work_authorization: { status: null, requires_sponsorship: null },
		screening: {
			salary_expectations: null,
			resides_near_posting: null,
			prior_employment_at_company: null,
			relatives_at_company: null,
			how_heard: null,
			country: null,
			eeo: { gender: null, hispanic_latino: null, veteran_status: null, disability_status: null },
		},
		option_aliases: {},
	};
}

function boardsBySource(boards: readonly BoardEntry[]) {
	const grouped = new Map<string, string[]>();
	for (const board of boards) {
		const existing = grouped.get(board.source) ?? [];
		if (!existing.includes(board.board)) existing.push(board.board);
		grouped.set(board.source, existing);
	}
	return Object.fromEntries(grouped);
}

function boardNames(boards: readonly BoardEntry[]) {
	return Object.fromEntries(boards.map((board) => [board.board, board.name.trim()]));
}

/**
 * `targeting.yaml`.
 *
 * The seniority band is written as a named filter. Restrictive work arrangements
 * are enforced from search.remote independently. Role/place text guides search;
 * it is not enough to invent geographic exclusions.
 */
function targetingMapping(draft: TargetingDraft): TargetingFile {
	const boards = namedBoards(draft);
	return {
		profile: { name: draft.profileName.trim() },
		search: {
			queries: lines(draft.roles),
			locations: lines(draft.locations),
			remote: draft.remote,
		},
		sources: { names: boardNames(boards), boards: boardsBySource(boards) },
		filters: {
			enabled: ["role_target"],
			role_target: {
				label: "seniority band",
				levels: LADDER.map((rung) => ({ name: rung.name, title_terms: [...rung.titleTerms] })),
				accept: [...acceptedRungs(draft)],
			},
		},
	};
}

/**
 * The body of `POST /api/onboarding/profile`: a name, and the three files' own shapes.
 *
 * `withResume` is false when the résumé step was skipped. The mapping is then left out rather
 * than sent with five blanks, which the write would refuse; an absent mapping is written as an
 * empty file (ui/ONBOARDING-API.md).
 */
export function profileProposal(draft: Draft, overwrite: boolean, withResume = true): ProfileProposalBody {
	return {
		name: draft.targeting.profileName.trim(),
		overwrite,
		resume: withResume ? resumeMapping(draft.resume) : undefined,
		constraints: constraintsMapping(),
		targeting: targetingMapping(draft.targeting),
	};
}
