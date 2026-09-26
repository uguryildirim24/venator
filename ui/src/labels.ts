/**
 * Identifiers become English here, and only here.
 *
 * The pipeline's vocabulary is machine-shaped — snake_case rule ids, ATS board slugs,
 * kebab-case statuses, composite Posting keys — and none of it belongs in front of the
 * owner. Everything the UI renders passes through this module; the raw values keep living
 * in the decision log, the URL and the SQL, which is where they are useful.
 *
 * A rule the pipeline invents tomorrow degrades to "Housing signal", never to
 * `housing_signal`: a rule id is words with underscores between them, so sentence-casing it
 * produces English. A board token is not — `amgen.wd1~Careers` sentence-cases to
 * `Amgen.wd1~Careers`, which is the identifier with a capital letter on it. So boards get no
 * such fallback; see `employerLabel`.
 */

import type {
	ApplicationStateName,
	DecisionStage,
	DecisionVerdict,
	JevTriageDecision,
	JevTriageState,
	Posting,
	PostingStatus,
} from "../shared/contracts.ts";
import type { ExtractedField, RuntimeState } from "../shared/onboarding.ts";
import type { DegreeLevel, EeoField, HardFilterRule, ScreeningField, TriState } from "../shared/profile-form.ts";
import type { SectionKey } from "./onboarding/draft.ts";
import type { RunKind, RunStage } from "../shared/runs.ts";
import type { HomeList } from "./router.ts";

const SOURCES = new Map<string, string>([
	["adzuna", "Adzuna"],
	["ashby", "Ashby"],
	["avature", "Avature"],
	["greenhouse", "Greenhouse"],
	["icims", "iCIMS"],
	["lever", "Lever"],
	["peopleclick", "PeopleClick"],
	["smartrecruiters", "SmartRecruiters"],
	["talentbrew", "TalentBrew"],
	["workable", "Workable"],
	["workday", "Workday"],
]);

const RULES = new Map<string, string>([
	/** Dedup: this Posting is the same job as one the Owner is already being shown. */
	["duplicate", "Duplicate"],
	["education_fit", "Education fit"],
	["location", "Location"],
	["role_target", "Role target"],
	["work_authorization", "Work authorization"],
]);

function sentenceCase(value: string): string {
	const words = value.replaceAll("_", " ").replaceAll("-", " ").trim();
	if (words === "") return "";
	return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * An identifier rendered as a name rather than as a sentence.
 *
 * `sentenceCase` is right for a rule or a state, which is a phrase: "Education fit". It is
 * wrong for something that is called something — `anthropic_api` came out as "Anthropic api",
 * which reads like a typo rather than like the name of a product.
 */
function nameCase(value: string): string {
	const words = value.replaceAll("_", " ").replaceAll("-", " ").trim().split(/\s+/u);
	return words.map((word) => (word === "" ? "" : word.charAt(0).toUpperCase() + word.slice(1))).join(" ");
}

/** Which Hard Filter fired, in English. */
export function ruleLabel(rule: string): string {
	return RULES.get(rule) ?? sentenceCase(rule);
}

export function sourceLabel(source: string): string {
	return SOURCES.get(source) ?? sentenceCase(source);
}

/**
 * The employer, or null when nobody knows it.
 *
 * `postings.company` is the one source of truth: the Discover adapters set it where the
 * board tells them, and `venator.view.build` fills the rest from the Profile's
 * `targeting.yaml: sources.names`, the same registry Dedup resolves employers through.
 * There is deliberately no board-token map here to back it up. A second copy of that
 * registry in TypeScript drifts — it sat at twelve entries while the Profile grew to
 * thirty-five, and the twenty-three boards it had never heard of rendered their tokens.
 *
 * So an unmapped board degrades to null rather than to its token. `originLabel` then says
 * "via Workday" — where the Posting came from, which the UI does know — instead of
 * captioning it `Amgen.wd1~Careers`. A board token is an ATS routing string, never a name
 * anyone would say out loud, and the honest reading of a missing name is that it is
 * missing. Registering the board's display name in the Profile is what fixes it.
 */
export function employerLabel(posting: Posting): string | null {
	const carried = posting.company?.trim() ?? "";
	return carried === "" ? null : carried;
}

/** What to caption a Posting with: its employer, or failing that where it was found. */
export function originLabel(posting: Posting): string {
	return employerLabel(posting) ?? `via ${sourceLabel(posting.source)}`;
}

/**
 * A Posting's identity for a human: the source and the employer's own listing number,
 * rather than the composite key the pipeline joins on.
 */
export function postingReference(posting: Posting): string {
	const identifier = posting.key.slice(posting.key.lastIndexOf(":") + 1);
	const employer = employerLabel(posting);
	const origin = employer === null ? sourceLabel(posting.source) : `${sourceLabel(posting.source)} · ${employer}`;
	return identifier === "" ? origin : `${origin} · listing ${identifier}`;
}

/** Where a Posting stands, in the same words the sidebar uses. */
export function statusLabel(status: PostingStatus): string {
	if (status === "queued") return "Potential matches";
	if (status === "hard-killed") return "Excluded";
	if (status === "needs-review") return "Needs review";
	if (status === "applied") return "Saved & applied";
	if (status === "closed") return "Closed";
	if (status === "no-text") return "No job text to read";
	if (status === "too-long") return "Job text too long to read";
	if (status === "protected") return "Reserved for evaluation";
	if (status === "not-filtered") return "Awaiting Hard Filters";
	return "Awaiting Jev";
}

/** The sidebar's name for each home list; the toolbar title reuses the same words. */
export function homeListLabel(list: HomeList): string {
	if (list === "queued") return "For you";
	if (list === "needs-review") return "Explore";
	if (list === "unscored") return "Awaiting Jev";
	if (list === "applied") return "Applications";
	if (list === "saved") return "Saved";
	if (list === "dismissed") return "Dismissed";
	return "Excluded";
}

/** A count of Postings in words, for a toolbar subtitle: "7 Postings, 3 new". */
export function postingCountLabel(total: number, fresh: number): string {
	const count = `${total.toLocaleString("en-US")} ${total === 1 ? "Posting" : "Postings"}`;
	return fresh > 0 ? `${count}, ${fresh.toLocaleString("en-US")} new` : count;
}

/** Human readable labels for the evidence assessment's explicit states. */
export function assessmentStatusLabel(status: "suitable" | "needs_review" | "not_suitable" | "unassessed"): string {
	if (status === "suitable") return "Potential match";
	if (status === "needs_review") return "Needs review";
	if (status === "not_suitable") return "Not suitable";
	return "Not assessed";
}

export function listingStatusLabel(status: "open" | "closed" | "unknown"): string {
	if (status === "open") return "Open";
	if (status === "closed") return "Closed";
	return "Listing status unknown";
}

export function descriptionKindLabel(kind: string): string {
	if (kind === "full") return "Full description";
	if (kind === "snippet") return "Snippet";
	if (kind === "missing") return "Description missing";
	return sentenceCase(kind);
}

export function opportunityTypeLabel(kind: string): string {
	if (kind === "job") return "Job";
	if (kind === "talent_pool") return "Talent pool";
	if (kind === "program") return "Program";
	return "Opportunity type unknown";
}

export function sourceHealthStatusLabel(status: string): string {
	if (status === "ok" || status === "healthy" || status === "success") return "Healthy";
	if (status === "failed" || status === "error") return "Failed";
	if (status === "stale") return "Stale";
	return sentenceCase(status);
}

/**
 * The Track stage's lifecycle states, in English.
 *
 * Each label is written from the transition in coordination/CONTRACTS.md, not from the state
 * word, because three of the seven words mean something different in ordinary hiring English
 * than they mean in this schema — and each of those three inverts who did what:
 *
 * - `rejected` is the Owner declining a Posting in review (actor: owner), not an employer
 *   declining the Owner. An employer's refusal arrives as an `outcome` event and lands on
 *   `concluded`, so the two were captioned the wrong way round.
 * - `filled` is `approved --fill--> filled`, the pipeline dry-run-filling the employer's
 *   form. It is not the employer filling the role. The Dry Run stays in the label because
 *   nothing here submits anything and that difference is the whole invariant.
 * - `concluded` is `--outcome-->`, an employer response on record. It is re-recordable and
 *   `interview` is one of its values, so the application is often not concluded at all.
 *
 * `tools/label-contract.ts` pins every row of this map against those clauses and runs inside
 * `pnpm smoke`, so the next drift fails a check instead of sitting on screen.
 */
const APPLICATION_STATES = {
	queued: "In queue",
	approved: "Saved",
	prepared: "Documents ready",
	rejected: "Dismissed",
	filled: "Application assisted",
	submitted: "Submitted",
	concluded: "Employer responded",
	withdrawn: "Withdrawn",
} satisfies Record<ApplicationStateName, string>;

export function applicationStateLabel(state: ApplicationStateName): string {
	return APPLICATION_STATES[state];
}

/**
 * What the header's stamp says when the dashboard is reading the sample database instead of
 * the Owner's own view.
 *
 * "Fixture" is the file's name in the repository and testing jargon besides — it is not in
 * CONTEXT.md's vocabulary and nobody says it out loud. The sample database holds Postings
 * that look entirely real, employers and all, so the badge has to do more than mark the data
 * as different: someone who has never seen the pipeline run must read it and understand that
 * these Postings are not the results of their own search.
 */
export const SAMPLE_DATA_STAMP = "Sample Postings — not yours";

/** The same fact at length, for the stamp's tooltip. */
export const SAMPLE_DATA_DETAIL =
	"Sample Postings that ship with the app, shown because the dashboard found no view database " +
	"built from your own search.";

/**
 * A runtime's name, as someone would say it.
 *
 * A lane is a lowercase identifier chosen by the pipeline, so it goes through here like every
 * other identifier. The fallback capitalises rather than guesses: a runtime this dashboard has
 * never heard of is still named by the probe, and `Mistral` is the honest rendering of
 * `mistral` in a way that no invented display name would be. It capitalises every word rather
 * than only the first, because a lane is a name and not a sentence.
 */
const RUNTIMES = new Map<string, string>([
	["claude", "Claude"],
	["codex", "Codex"],
]);

export function runtimeLabel(lane: string): string {
	return RUNTIMES.get(lane) ?? nameCase(lane);
}

/**
 * What an assistant is called on the setup screen: the plan a person signed in with.
 *
 * `runtimeLabel` names the program; this names what someone chose. A lane with no entry is
 * named by `runtimeLabel`, so a runtime this build never heard of still reads as a name.
 */
const ASSISTANTS = new Map<string, string>([
	["claude", "Claude"],
	["codex", "ChatGPT"],
]);

export function assistantLabel(lane: string): string {
	return ASSISTANTS.get(lane) ?? runtimeLabel(lane);
}

/**
 * The eight runtime states, as the status line under a vendor button says them.
 *
 * Each is written from the state's own meaning in `shared/onboarding.ts` and
 * `ui/ONBOARDING-API.md`, and the distinctions those two files insist on survive the short
 * form: `not_installed`, `not_spawnable` and `not_signed_in` are three different instructions,
 * and `configured` is never "Connected", because nothing was contacted to check it.
 *
 * What to *do* about a state is never written here. That is `remedy`, it belongs to the probe,
 * and it is rendered exactly as it arrives under this line.
 */
const RUNTIME_STATES = {
	available: "Connected",
	not_installed: "Not found on this Mac",
	not_spawnable: "Can’t be started on this Mac",
	not_signed_in: "Not signed in",
	not_configured: "No key saved",
	configured: "Key saved, not checked",
	timed_out: "Didn’t answer in time",
	failed: "Something went wrong",
} satisfies Record<RuntimeState, string>;

export function runtimeStateLabel(state: RuntimeState): string {
	return RUNTIME_STATES[state];
}

/** The five contact facts the resume step reads, in English. */
const EXTRACTED_FIELDS = {
	name: "Name",
	location: "City",
	phone: "Phone",
	email: "Email",
	linkedin: "LinkedIn",
} satisfies Record<ExtractedField, string>;

export function extractedFieldLabel(field: ExtractedField): string {
	return EXTRACTED_FIELDS[field];
}

/**
 * The three longer sections of a resume, in English.
 *
 * `technical_proficiencies` is `resume.yaml`'s own key and is the renderer's word for the row
 * of tools and languages a resume carries. On screen it is "Skills and tools", because that is
 * what a person calls it and `pnpm smoke` fails the build if the identifier itself reaches
 * visible text.
 */
const RESUME_SECTIONS = {
	education: "Education",
	technical_proficiencies: "Skills and tools",
	experience: "Experience",
} satisfies Record<SectionKey, string>;

export function resumeSectionLabel(section: SectionKey): string {
	return RESUME_SECTIONS[section];
}

/**
 * One field of one of those sections, in English.
 *
 * The keys are `resume.yaml`'s, read off `src/venator/resume/render.py`, and several of them
 * are the same word in two sections — `org` is a school in one and an employer in the other —
 * so each section names its own. A key with no entry falls back to sentence case, which is
 * English for these because every one of them is words with an underscore between them.
 */
const RESUME_FIELDS = new Map<string, string>([
	["education.org", "School"],
	["education.location", "Where"],
	["education.date", "When"],
	["education.degree", "Degree"],
	["education.gpa", "Grade"],
	["education.coursework", "Coursework"],
	["technical_proficiencies.label", "Heading"],
	["technical_proficiencies.items", "What it lists"],
	["experience.org", "Employer"],
	["experience.role", "Role"],
	["experience.location", "Where"],
	["experience.dates", "When"],
	["experience.bullets", "What you did"],
]);

export function resumeFieldLabel(section: SectionKey, field: string): string {
	return RESUME_FIELDS.get(`${section}.${field}`) ?? sentenceCase(field);
}

export function runStatusLabel(status: string): string {
	if (status === "ok") return "completed";
	if (status === "failed") return "failed";
	if (status === "error") return "stopped on an error";
	return sentenceCase(status).toLowerCase();
}

/**
 * The pipeline stages a run is made of, in the Owner's words.
 *
 * `discover`, `filters` and `view` are the loop's own identifiers and belong on a command
 * line; what a person watching a run needs is the sentence. `filters` is *the Hard Filters*
 * (CONTEXT.md), never "matching" and never "filtering out" — a Filter Decision is recorded
 * for every Posting, including the ones that pass.
 */
const RUN_STAGES = {
	discover: "Fetching jobs",
	filters: "Checking eligibility",
	jev: "Checking with Jev",
	view: "Updating the dashboard",
} satisfies Record<RunStage, string>;

export function runStageLabel(stage: RunStage): string {
	return RUN_STAGES[stage];
}

/** What each stage leaves behind, said once so a half-finished run can be described. */
const RUN_STAGE_RESULTS = {
	discover: "Jobs that were found are in your data.",
	filters: "Eligibility decisions were recorded.",
	jev: "Jev results were recorded when available.",
	view: "The dashboard is showing what has been recorded.",
} satisfies Record<RunStage, string>;

export function runStageResultLabel(stage: RunStage): string {
	return RUN_STAGE_RESULTS[stage];
}

/** What a person is asking for when they start a run. */
const RUN_KINDS = {
	"fetch-and-filter": "Refresh jobs",
	"jev-it": "Jev it",
} satisfies Record<RunKind, string>;

export function runKindLabel(kind: RunKind): string {
	return RUN_KINDS[kind];
}

/** One Filter Decision, as the What changed sheet heads it: "Hard Filters: excluded". */
export function decisionTitle(stage: DecisionStage, verdict: DecisionVerdict): string {
	const where = stage === "hard_filter" ? "Hard Filters" : "Evidence review";
	const what = verdict === "kill" ? "excluded" : verdict === "queue" ? "queued" : "passed";
	return `${where}: ${what}`;
}

/**
 * The TrackEvents, as the History sheet says them. Each is written from the transition in
 * `src/venator/track/store.py`, the same way `APPLICATION_STATES` is: `reject` is the Owner
 * dismissing a Posting, `fill` is a Dry Run of the employer's form, and `submit` is the
 * Owner's own report that they applied.
 */
const TRACK_EVENTS = new Map<string, string>([
	["approve", "Saved"],
	["reject", "Dismissed"],
	["restore", "Restored"],
	["prepare", "Documents prepared"],
	["fill", "Form filled in a Dry Run"],
	["submit", "You marked it applied"],
	["outcome", "Employer response recorded"],
	["withdraw", "Withdrawn"],
]);

export function trackEventLabel(event: string): string {
	return TRACK_EVENTS.get(event) ?? sentenceCase(event);
}

/** Where a résumé fact came from, in words: the section of `resume.yaml` it sits in. */
export function evidenceSourceLabel(source: string): string {
	const section = /^profile\.resume\.([a-z_]+)/u.exec(source)?.[1];
	if (section === "education" || section === "technical_proficiencies" || section === "experience") {
		return resumeSectionLabel(section);
	}
	return section === undefined ? "Your Profile" : sentenceCase(section);
}

/**
 * A requirement sentence the assessment quoted from the Posting, and what it found.
 *
 * The assessment writes these as a prefix and the employer's own clause:
 * `Qualification not established: A degree in progress in biology`. The prefix is the finding
 * and the clause is where it belongs on the page. `required` is true only when the prefix
 * says the requirement is not optional; nothing else is inferred.
 */
export type RequirementFinding = {
	readonly clause: string;
	readonly title: string;
	readonly subtitle: string | null;
	readonly required: boolean;
};

const REQUIREMENT_PREFIXES: readonly { readonly pattern: RegExp; readonly title: string; readonly subtitle: string | null; readonly required: boolean }[] = [
	{ pattern: /^Qualification not established:\s*/u, title: "Not on your résumé", subtitle: "Required", required: true },
	{ pattern: /^Optional qualification gap:\s*/u, title: "Optional", subtitle: "Not on your résumé", required: false },
	{ pattern: /^No confirmed resume evidence for [^:]+:\s*/u, title: "Not on your résumé", subtitle: null, required: false },
	{ pattern: /^Credential or eligibility requirement needs review:\s*/u, title: "Credential or eligibility", subtitle: "Needs a look", required: false },
];

export function requirementFinding(text: string): RequirementFinding | null {
	for (const prefix of REQUIREMENT_PREFIXES) {
		const match = prefix.pattern.exec(text);
		if (match === null) continue;
		const clause = text.slice(match[0].length).trim();
		if (clause === "") return null;
		return { clause, title: prefix.title, subtitle: prefix.subtitle, required: prefix.required };
	}
	return null;
}

/** Where a whole-Posting sentence belongs: beside the header, the requirements, or the Hard Filters. */
export function assessmentNotePlace(text: string): "posting" | "requirements" | "filters" {
	if (text.startsWith("hard-filter fact ")) return "filters";
	if (/^(?:no established|Shared office tools)/u.test(text)) return "requirements";
	return "posting";
}

/** What a Hard Filter fact's recorded value means, for the few values that are words. */
const HARD_FILTER_VALUES = new Map<string, string>([
	["unknown", "not stated"],
	["unassessed", "not checked"],
	["unplaced", "not placed in your band"],
	["remote_unknown", "remote status not stated"],
	["enroll_unknown", "enrollment not stated"],
	["reloc_unknown", "relocation support not stated"],
]);

/**
 * One assessment sentence about the whole Posting, in plain words.
 *
 * The assessment writes these for the log: `greenhouse application_deadline is not listed`,
 * `hard-filter fact location was unknown (hard_filter decision)`. The margin says what they
 * mean. A sentence this does not recognise is shown as written, minus any field name in it.
 */
export function assessmentNoteLabel(text: string): string {
	const fact = /^hard-filter fact (.+?) was ([a-z_]+)(?: \(hard_filter decision\))?$/u.exec(text);
	if (fact !== null) {
		const [, rule = "", value = ""] = fact;
		return `${sentenceCase(rule)}: ${HARD_FILTER_VALUES.get(value) ?? sentenceCase(value).toLowerCase()}`;
	}
	const field = /^(?:[a-z]+ )?(application_deadline|workplace_type|opportunity_type|listing_status|last_verified_at)\b/u.exec(text)?.[1];
	if (field === "application_deadline") return "No application deadline listed";
	if (field === "workplace_type") return "Work arrangement not stated";
	if (field === "opportunity_type") return "Not clear it is a specific opening";
	if (field === "listing_status") return "Not verified open";
	if (field === "last_verified_at") return "Never verified";
	if (text.startsWith("the full job description is missing")) return "The full description is missing";
	if (text.startsWith("the description is a snippet")) return "Only a snippet of the description, so the requirements were not read";
	if (text.startsWith("the posting does not state a location")) return "No location stated";
	if (text.startsWith("no established resume evidence")) return "No résumé evidence for the core work yet";
	if (text.startsWith("no established overlap")) return "No overlap with confirmed résumé facts yet";
	if (text.startsWith("this is a program")) return "A program, not a specific opening";
	if (text.startsWith("this is a talent pool")) return "A talent pool, not a specific opening";
	const readable = text
		.replaceAll(/\s*\(hard_filter decision\)/gu, "")
		.replaceAll(/^hard filter:\s*/giu, "")
		.replaceAll(/\b[a-z]+(?:_[a-z]+)+\b/gu, (identifier) => identifier.replaceAll("_", " "));
	return readable.charAt(0).toUpperCase() + readable.slice(1);
}

const JEV_DECISIONS = {
	prioritize: "Priority for review",
	review: "Review",
	exclude: "Skip",
	unassessed: "Unavailable",
} satisfies Record<JevTriageDecision, string>;

/**
 * The last Jev it pause, as the short line under the run controls (ui/DESIGN.md, "Sidebar").
 * How many Postings still wait is the line above it, from the plan, so this says only why
 * Jev stopped and what resumes it.
 */
export function jevPauseLabel(reason: string): string {
	const state = {
		"no-key": "Jev paused: no API key. Add one and press Jev it again.",
		"out-of-credit": "Jev paused: out of credit. Press Jev it again once there is some.",
		"account-unusable": "Jev paused: account unavailable. Press Jev it again once it is back.",
		"cap-reached": "Jev paused at the spending cap. Press Jev it again to continue.",
		"transport-failed": "Jev paused: connection unavailable. Press Jev it again to continue.",
	};
	return Object.entries(state).find(([code]) => code === reason)?.[1] ?? "Jev paused. Press Jev it again to continue.";
}

export function jevDecisionLabel(decision: JevTriageDecision): string {
	return JEV_DECISIONS[decision];
}

const JEV_STATES = {
	current: "Current",
	stale: "Out of date",
	unavailable: "Jev unavailable",
} satisfies Record<JevTriageState, string>;

export function jevStateLabel(state: JevTriageState): string {
	return JEV_STATES[state];
}

/** The firing rule, in English. Identifiers never reach the screen as themselves. */
export function jevRuleLabel(rule: string): string {
	return sentenceCase(rule);
}

export function earlyReviewLabel(): string {
	return "Jev diagnostics";
}

/** What early review is, said every time it is shown. */
export const EARLY_REVIEW_CAVEAT = "Unavailable and out-of-date Jev results. These Postings remain in Awaiting Jev.";

export function jevSkipLabel(): string {
	return "Jev: skip";
}

export function jevListLabel(decision: "prioritize" | "review"): string {
	return decision === "prioritize" ? "Jev: look first" : "Jev: review";
}

/**
 * The rungs `profiles/example/targeting.yaml` documents, as the Level popup says them. A rung
 * a Profile names on its own is a name and is rendered as one.
 */
const RUNGS = new Map<string, string>([
	["intern", "Internship"],
	["entry", "Entry level"],
	["mid", "Mid level"],
	["senior", "Senior"],
	["executive", "Director and above"],
]);

export function rungLabel(rung: string): string {
	return RUNGS.get(rung) ?? nameCase(rung);
}

/** `schema.py: DEGREE_LEVELS`, in English. */
const DEGREE_LEVELS = {
	high_school: "High school",
	associate: "Associate degree",
	bachelor: "Bachelor’s degree",
	master: "Master’s degree",
	doctorate: "Doctorate",
} satisfies Record<DegreeLevel, string>;

export function degreeLevelLabel(level: DegreeLevel): string {
	return DEGREE_LEVELS[level];
}

/** A YAML true, false or null on the Profile screen. Null is "not answered", never a No. */
const TRI_STATES = {
	unanswered: "Not answered",
	yes: "Yes",
	no: "No",
} satisfies Record<TriState, string>;

export function triStateLabel(state: TriState): string {
	return TRI_STATES[state];
}

/** What each Hard Filter does, under its name on the Profile screen. */
const HARD_FILTER_LINES = {
	work_authorization: "Skips jobs whose wording rules you out.",
	education_fit: "Skips jobs that ask for more education or years than you have.",
	role_target: "Keeps jobs at your level and skips other functions.",
	eligibility: "Skips jobs outside your timing and pay.",
} satisfies Record<HardFilterRule, string>;

export function hardFilterLine(rule: HardFilterRule): string {
	return HARD_FILTER_LINES[rule];
}

/** The screening answers `constraints.yaml` holds, as a form asks them. */
const SCREENING_FIELDS = {
	salary_expectations: "Salary expectations",
	resides_near_posting: "Live near the job",
	prior_employment_at_company: "Worked there before",
	relatives_at_company: "Relatives there",
	how_heard: "How you heard",
	country: "Country",
} satisfies Record<ScreeningField, string>;

export function screeningFieldLabel(field: ScreeningField): string {
	return SCREENING_FIELDS[field];
}

const EEO_FIELDS = {
	gender: "Gender",
	hispanic_latino: "Hispanic or Latino",
	veteran_status: "Veteran status",
	disability_status: "Disability status",
} satisfies Record<EeoField, string>;

export function eeoFieldLabel(field: EeoField): string {
	return EEO_FIELDS[field];
}
