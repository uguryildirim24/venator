/**
 * The run surface's contracts, mirroring ui/RUN-API.md.
 *
 * The dashboard can start pipeline stages a person could otherwise only start from a
 * terminal. Everything it may start is written out here, literally, and the server compiles
 * against the same file the screens do — so widening what a run can be is an edit somebody
 * makes on purpose and a reviewer sees in the diff.
 *
 * Three closed sets do that work, and they are deliberately separate:
 *
 * - `RunStage` is the vocabulary of `venator.schedule.loop` minus `commit`. It is what the
 *   progress reader recognises on the loop's stdout and what a `RunState` reports about.
 * - `RunKind` is what a request may **start**. A request never names stages — it names a
 *   kind, and the server looks the stages up in `RUN_KIND_STAGES`. So there is no route that
 *   takes a module name, no argument passthrough, and no way to assemble a stage list from
 *   input.
 * - `PlanKind` is what a request may **ask about**. It is kept as a named type because a plan
 *   and a start remain separate operations, even though every described kind is now startable.
 *
 * `commit` is absent from all three on purpose. A button in a shipped app must not commit
 * somebody's Postings into whatever repository the app happens to be sitting in; the loop's
 * own work-tree gate protects only the Install that has no repository at all, which is the
 * one case a developer running the dashboard out of their clone is not.
 *
 * Nothing on this surface reaches an employer. The never-submit invariant is untouched: no
 * route here applies to anything, records a TrackEvent, changes a Posting's lifecycle state,
 * or enables approve/reject.
 */

/**
 * A header no cross-origin page can set without a preflight the origin allowlist then
 * refuses. Its own name rather than onboarding's, because a run request that claimed to be an
 * onboarding request would be a small lie in the one place the code should be literal.
 */
export const RUN_REQUEST_HEADER = "x-venator-run";
export const RUN_REQUEST_HEADER_VALUE = "1";

/** The stages of `venator.schedule.loop` a run may include. `commit` is not one of them. */
export type RunStage = "discover" | "filters" | "jev" | "view";

export const RUN_STAGES: readonly RunStage[] = ["discover", "filters", "jev", "view"];

/**
 * What a person can **start**.
 *
 * Fetching Postings, recording Filter Decisions and rebuilding the disposable view.
 */
export type RunKind = "fetch-and-filter" | "jev-it";

export const RUN_KINDS: readonly RunKind[] = ["fetch-and-filter", "jev-it"];

/**
 * What a person can **ask about** before a start. Keeping this closed set separate makes a
 * plan request explicit and keeps future described-only work from becoming startable by
 * accident.
 */
export type PlanKind = RunKind;

export const PLAN_KINDS: readonly PlanKind[] = ["fetch-and-filter", "jev-it"];

/**
 * The `RunKind` this plan kind is, or null if the two closed sets drift.
 *
 * The one place the two sets meet, and it narrows rather than asserts: a caller that wants to
 * start something has to handle the null, and the type-checker is what makes it.
 */
export function runKindOf(kind: PlanKind): RunKind | null {
	return RUN_KINDS.find((candidate) => candidate === kind) ?? null;
}

/**
 * The stages each kind runs, in the loop's own order.
 *
 * `view` is in the list because the dashboard reads `build/venator.db` and nothing else: a
 * run that appends to `data/` and does not rebuild the view changes nothing on screen.
 */
export const RUN_KIND_STAGES = {
	"fetch-and-filter": ["discover", "filters", "view"],
	"jev-it": ["view"],
} satisfies Record<RunKind, readonly RunStage[]>;

/**
 * Which stages append to `data/`, and so leave something behind when a run stops halfway.
 *
 * `data/` is append-only, so this is not bookkeeping: it is what lets a half-finished run be
 * described as what actually happened rather than as one undifferentiated failure.
 */
export const STORE_WRITING_STAGES: readonly RunStage[] = ["discover", "filters", "jev"];

/** Something true about this plan that is not a refusal. Nothing here is an error. */
export type PlanNoteCode =
	/** The Profile registers no board, so Discover has nothing to poll. */
	| "no-boards"
	/** The run would rebuild a view database this dashboard is not the one reading. */
	| "view-elsewhere"
	/** No Posting currently needs Jev under this release. */
	| "nothing-to-qualify"
	/** The dashboard server cannot authorise a Jev child without its TypeSafe credential. */
	| "jev-key-missing";

export type PlanNote = {
	readonly code: PlanNoteCode;
	/**
	 * Prose, and prose only. No filesystem path, no board token, no rule identifier — a note
	 * is rendered as copy, and copy is held to `CONTEXT.md`'s vocabulary like every other
	 * sentence on screen. Machine detail travels in the plan's own fields beside it.
	 */
	readonly message: string;
};

/**
 * What a run would do, before it does it.
 *
 * The token is the whole authorisation: `POST /api/runs` takes nothing else, so a run nobody
 * was shown a plan for is not expressible. It is opaque, single-use, this process only, and
 * short-lived.
 */
export type JevRunPlan = {
	readonly asOf: string;
	readonly mode: "shadow" | "promoted";
	readonly postingCount: number;
	readonly maximumUsd: number;
	/** Opaque binding for the selected inputs, even if count and allowance stay unchanged. */
	readonly selectionHash: string;
	/** The two plan facts in words, ready for the press that authorises them. */
	readonly summary: string;
};

export type RunPlan = {
	readonly token: string;
	/**
	 * A `PlanKind`, narrowed again at redemption so the plan and run closed sets cannot drift
	 * without a refusal.
	 */
	readonly kind: PlanKind;
	readonly profile: string;
	readonly profileDirectory: string;
	/**
	 * What would actually run, in order.
	 */
	readonly stages: readonly RunStage[];
	/** Where the run would write, as `venator.paths` resolves it inside the child. */
	readonly storeRoot: string;
	/** The view database that run would rebuild. */
	readonly viewPath: string;
	/** The view database this dashboard is reading. The two differing is a note, not an error. */
	readonly dashboardViewPath: string;
	/** How many boards the Profile registers. Zero is why a first run can find nothing. */
	readonly boards: number;
	/** Present only for the `jev-it` kind. No credential or secret-derived value is included. */
	readonly jev?: JevRunPlan | null;
	/** False when running would be pointless rather than wrong; `notes` says why. */
	readonly startable: boolean;
	readonly notes: readonly PlanNote[];
};

export type PlanResponse = { readonly plan: RunPlan };

export type StageState = "waiting" | "running" | "ok" | "failed" | "not-reached";

export type RunPhase = "running" | "succeeded" | "failed" | "stopped";

export type RunStageProgress = {
	readonly stage: RunStage;
	readonly state: StageState;
	readonly startedAt: string | null;
	readonly endedAt: string | null;
	readonly detail?: string;
};

export type RunFailure = {
	/** Where it stopped, or null when the loop failed without naming a stage. */
	readonly stage: RunStage | null;
	/** Written for the person reading it. */
	readonly message: string;
	readonly remedy: string | null;
	/**
	 * The bounded tail of what the pipeline printed, shown only on a run that did not
	 * succeed and captioned as the pipeline's own output rather than as dashboard prose.
	 */
	readonly transcript: string | null;
};

/**
 * One run, as this server process remembers it.
 *
 * In memory and nowhere else. The durable record of a run is `data/runs.jsonl` and
 * the `runs` table it is materialised into; this is what the screen watches while it happens.
 */
export type RunState = {
	/** This process only. Not a database id, and not a record of anything. */
	readonly id: string;
	/** A `RunKind`, narrowed from the plan's `PlanKind`: what is running is what can be run. */
	readonly kind: RunKind;
	readonly profile: string;
	readonly phase: RunPhase;
	readonly startedAt: string;
	readonly endedAt: string | null;
	readonly stages: readonly RunStageProgress[];
	/** A stage that appends to `data/` reached `ok`, so this run left rows behind. */
	readonly wroteRows: boolean;
	/**
	 * The view stage reached `ok`, so the dashboard is showing what this run wrote. True after
	 * a recovery rebuild too — the fact being reported is that the dashboard is current, and
	 * `viewRecovered` below is what says the run got there the second way.
	 */
	readonly viewRebuilt: boolean;
	/**
	 * The view was rebuilt by a recovery pass after the run stopped early.
	 *
	 * Without it, a run that dies in `filters` leaves a person with new Postings they cannot
	 * see and no way to make them appear. It is the same command, run again, and it writes
	 * only the disposable view.
	 */
	readonly viewRecovered: boolean;
	readonly failure: RunFailure | null;
};

export type RunResponse = { readonly run: RunState | null };

export type RecentRunsResponse = { readonly runs: readonly RunState[] };

/** Every refusal this surface answers with. */
export type RunErrorCode =
	| "bad_request"
	| "forbidden_origin"
	| "no_profile"
	| "profile_ambiguous"
	| "unknown_profile"
	| "run_in_progress"
	| "no_run"
	| "plan_expired"
	| "not_startable"
	| "pipeline_absent"
	| "store_unreadable"
	| "run_failed_to_start";

/**
 * The onboarding surface's error body, with one addition.
 *
 * `run` carries the live `RunState` on a `run_in_progress` refusal, so a second press
 * resolves to "you are already watching this run" rather than to an error. It is null
 * everywhere else.
 */
export type RunErrorBody = {
	readonly error: {
		readonly code: RunErrorCode;
		readonly message: string;
		readonly remedy: string | null;
		readonly field: string | null;
	};
	readonly run: RunState | null;
};
