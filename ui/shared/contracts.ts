/**
 * Domain contracts for the review dashboard, mirroring coordination/CONTRACTS.md.
 * Server and app both import these; the server is the only place SQL rows are decoded
 * into them, so every field below is already parsed and safe to render.
 */

export type DecisionStage = "hard_filter" | "llm_score";

/** hard_filter records pass|kill; llm_score records queue|kill. */
export type DecisionVerdict = "pass" | "kill" | "queue";

/**
 * One recorded Filter Decision. There is no silent drop: every Posting has one per stage reached.
 *
 * A historical llm_score row still loads, and its Match Score stays in the view: no score
 * crosses the wire, so none can reach the screen (`ui/DESIGN.md`, principle 4).
 */
export type FilterDecision = {
	readonly id: number;
	readonly postingKey: string;
	readonly stage: DecisionStage;
	readonly verdict: DecisionVerdict;
	/** Which Hard Filter fired; null on a pass and on every llm_score decision. */
	readonly rule: string | null;
	/** Null only when the engine recorded no reason — a policy violation the UI calls out. */
	readonly reason: string | null;
	readonly filtersVersion: string | null;
	readonly decidedAt: string | null;
	/** False when a later replay superseded this decision for its stage. */
	readonly latest: boolean;
};

export type Posting = {
	readonly key: string;
	readonly source: string;
	readonly board: string;
	/** The employer, carried by the view since the company column landed. Null if unmapped. */
	readonly company: string | null;
	readonly title: string;
	/** Absent in some ATS payloads; rendered as an explicit gap rather than an empty cell. */
	readonly location: string | null;
	readonly url: string;
	readonly postedAt: string | null;
	readonly discoveredAt: string;
};

/**
 * Where a Posting stands on the dashboard.
 *
 * `needs-review` is Jev review (the Explore list);
 * `applied` is a Posting with application progress (the Applications list). A historical
 * Match Score does not choose this value.
 */
export type PostingStatus = "unscored" | "hard-killed" | "queued" | "needs-review" | "applied" | "closed" | "no-text" | "too-long" | "protected" | "not-filtered";

export const POSTING_STATUSES: readonly PostingStatus[] = [
	"unscored",
	"hard-killed",
	"queued",
	"needs-review",
	"applied",
	"closed",
	"no-text",
	"too-long",
	"protected",
	"not-filtered",
];

/** Where an application stands after the pipeline queued it — the Track stage's lifecycle. */
export type ApplicationStateName =
	| "queued"
	| "approved"
	| "prepared"
	| "rejected"
	| "filled"
	| "submitted"
	| "concluded"
	| "withdrawn";

export const APPLICATION_STATE_NAMES: readonly ApplicationStateName[] = [
	"queued",
	"approved",
	"prepared",
	"rejected",
	"filled",
	"submitted",
	"concluded",
	"withdrawn",
];

export type ApplicationState = {
	readonly state: ApplicationStateName;
	readonly detail: string | null;
	readonly since: string | null;
};

/**
 * Which application states a home list is narrowed to.
 *
 * `saved` is `approved`, `dismissed` is `rejected`, and `active` is every other state with
 * application progress. The three are disjoint and together they are the `applied` status.
 */
export type ApplicationFilter = "saved" | "dismissed" | "active";

export const APPLICATION_FILTERS: readonly ApplicationFilter[] = ["saved", "dismissed", "active"];

/** A Posting plus its effective decision at each stage — the unit both list views render. */
export type PostingEntry = {
	readonly posting: Posting;
	readonly status: PostingStatus;
	readonly hardFilter: FilterDecision | null;
	/** Null until the Track stage records a lifecycle row for this Posting. */
	readonly application: ApplicationState | null;
	readonly assessment?: JobAssessment;
	readonly jev: { readonly decision: "prioritize" | "review" | "exclude"; readonly mode: JevTriageMode } | null;
	/** Discovered in the latest completed fetch (`DatabaseInfo.lastFetchAt`). Nothing is stored. */
	readonly isNew: boolean;
};

/**
 * One block of a Posting's reader page: a heading, a paragraph or a list item, as plain text.
 *
 * The page is a reader extraction of the employer's HTML, not the HTML. No markup, style or
 * script survives it; the original stays behind Read Full Description in a sandboxed frame.
 */
export type ReaderBlock = {
	readonly kind: "heading" | "paragraph" | "item";
	readonly text: string;
};

/** One TrackEvent, oldest first. `event` is the Track store's own word; labels.ts says it. */
export type TrackEvent = {
	readonly id: number;
	readonly event: string;
	readonly actor: string | null;
	readonly detail: string | null;
	readonly at: string | null;
};

export type PostingDetail = {
	readonly posting: Posting;
	readonly status: PostingStatus;
	readonly descriptionHtml: string;
	/** The reader page, extracted from `descriptionHtml`. Empty when there is no description. */
	readonly page: readonly ReaderBlock[];
	/** Full history, oldest first, including superseded replays. */
	readonly decisions: readonly FilterDecision[];
	/** Every TrackEvent recorded for this Posting, oldest first. */
	readonly trackEvents: readonly TrackEvent[];
	readonly application: ApplicationState | null;
	readonly assessment?: JobAssessment;
	readonly jev: PostingEntry["jev"];
	/** Current Jev row, preferring promoted over shadow; absent on a view built before Jev. */
	readonly jevTriage?: JevTriage;
};

export type JevTriageDecision = "prioritize" | "review" | "exclude" | "unassessed";
export type JevTriageState = "current" | "stale" | "unavailable";
export type JevTriageMode = "shadow" | "promoted";

export const JEV_TRIAGE_DECISIONS: readonly JevTriageDecision[] = [
	"prioritize",
	"review",
	"exclude",
	"unassessed",
];
export const JEV_TRIAGE_STATES: readonly JevTriageState[] = ["current", "stale", "unavailable"];
export const JEV_TRIAGE_MODES: readonly JevTriageMode[] = ["shadow", "promoted"];

/**
 * One Jev triage row as the dashboard renders it. Machine ids stay off screen.
 *
 * `fit_probability` and `fit_score` stay in the view. Neither crosses the wire, so no Jev
 * number can reach the screen: early review is spoken as its decision label.
 */
export type JevTriage = {
	readonly postingKey: string;
	readonly mode: JevTriageMode;
	readonly state: JevTriageState;
	readonly decision: JevTriageDecision;
	readonly primaryRule: string | null;
	readonly exclusions: readonly string[];
	readonly reviewFlags: readonly string[];
	readonly diagnosticFlags: readonly string[];
	readonly qualifierVersion: string | null;
	readonly assessmentKey: string | null;
	readonly inputVersion: string | null;
	readonly policyHash: string | null;
	readonly modelId: string | null;
	readonly asOfMonth: string;
	readonly decidedAt: string | null;
	readonly reason: string | null;
};

/** A Posting plus its Jev triage row — the unit the Early review list renders. */
export type JevTriageEntry = {
	readonly posting: Posting;
	readonly triage: JevTriage;
};

export type JevTriageResponse = {
	readonly entries: readonly JevTriageEntry[];
	readonly total: number;
	readonly limit: number;
	readonly offset: number;
};

export type JobAssessment = {
	readonly status: "suitable" | "needs_review" | "not_suitable" | "unassessed";
	readonly summary: string;
	readonly evidence: readonly { readonly requirement: string; readonly candidateEvidence: string; readonly source: string; readonly basis?: "related_skill" }[];
	readonly assessedBy: "deterministic" | "jev";
	readonly assessedAsOf: string | null;
	readonly conflicts: readonly string[];
	readonly unknowns: readonly string[];
	readonly listingStatus: "open" | "closed" | "unknown";
	readonly lastVerifiedAt: string | null;
	readonly descriptionKind: string;
	readonly applyUrl: string;
	readonly opportunityType: string;
};

export type SourceHealth = {
	readonly key: string;
	readonly employer?: string | null;
	readonly status: string;
	readonly lastAttemptAt: string | null;
	readonly lastSuccessAt: string | null;
	readonly count: number;
	readonly message: string | null;
	/** Counts from the effective stored jobs for this source board; null on an older view. */
	readonly coverage: SourceCoverage | null;
};

export type SourceCoverage = {
	/** Jobs currently known to Venator; this is not the employer's total inventory. */
	readonly knownJobs: number;
	/** Known jobs with usable full text and current source/detail verification as applicable. */
	readonly fullVerifiedDetails: number;
	/** Known, still-actionable jobs whose detail evidence needs a check. */
	readonly needsDetailCheck: number;
};

/** Counts for the inspector tiles and the home lists. */
export type Funnel = {
	readonly discovered: number;
	readonly unscored: number;
	readonly hardKilled: number;
	readonly queued: number;
	/** Home list `needs-review`. */
	readonly needsReview: number;
	/** Home list `applied`: every Posting with application progress. */
	readonly applied: number;
	readonly closed: number;
	readonly noText: number;
	readonly tooLong: number;
	readonly protected: number;
	readonly notFiltered: number;
	/** The part of `applied` in application state `approved`. */
	readonly saved: number;
	/** The part of `applied` in application state `rejected`. */
	readonly dismissed: number;
};

/**
 * Which database the API opened, so the app can say "fixture" out loud.
 *
 * Three kinds, not two. `empty` is what an Install that has never run anything is served —
 * the contract's schema with no rows in it, standing in for a view `python -m
 * venator.view.build` has not written yet, with `path` naming where it will appear. Sample
 * Postings are `fixture`, and they are served only to a run that asked for them
 * (`server/db.ts`), so `fixture` on this field always means somebody asked.
 */
export type DatabaseInfo = {
	readonly path: string;
	readonly kind: "pipeline" | "fixture" | "empty";
	readonly filtersVersions: readonly string[];
	readonly lastDecisionAt: string | null;
	/**
	 * When the latest completed fetch finished: the newest `runs` heartbeat for a Discover
	 * stage that reported ok. Null when no fetch has completed. It is what "new" is counted from.
	 */
	readonly lastFetchAt: string | null;
	readonly jevPause: { readonly reason: string; readonly waiting: number } | null;
};

export type SummaryResponse = {
	/** All Postings before the Owner's location view choice, for first-run detection only. */
	readonly unfilteredDiscovered: number;
	readonly funnel: Funnel;
	readonly database: DatabaseInfo;
	readonly sources?: readonly SourceHealth[];
};

export type PostingsResponse = {
	readonly entries: readonly PostingEntry[];
	/** Total matching the query before the response limit was applied. */
	readonly total: number;
	/** How many of `total` were discovered in the latest completed fetch. */
	readonly newCount: number;
	readonly limit: number;
	readonly offset?: number;
};

export type ErrorResponse = {
	readonly error: string;
};
