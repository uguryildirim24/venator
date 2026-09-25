/**
 * Turning "what would a run do" into the one thing that can start one.
 *
 * A plan is issued by asking the pipeline (`plan.ts` → `plan.py`) and is handed back with an
 * opaque token. `POST /api/runs` takes that token and nothing else, so a run's Profile and
 * stage list are fixed by the plan somebody was looking at — a run nobody was shown a plan
 * for is not expressible.
 *
 * The token lives in this process's memory, is single use, and expires. It is not a
 * credential: the loopback binding and the origin allowlist are what keep strangers out
 * (`server/http/local-action-guard.ts`). What it does is bind a start to a specific answer
 * that was recently true.
 *
 * **Every refusal is checked twice**, here at plan time and again at start, because the answer
 * must not depend on how fresh a screen is. Between the two, the Profile can be renamed, the
 * store can change hands, and the boards can be edited; a plan whose Profile no longer
 * resolves the way it did is expired rather than reinterpreted.
 */

import { randomUUID } from "node:crypto";

import { defaultViewDatabase, systemContext, type LocationContext } from "../locations.ts";
import { resolveView } from "../db.ts";
import { discoverProfiles, impliedProfile } from "../onboarding/discovery.ts";
import {
	runKindOf,
	RUN_KIND_STAGES,
	type PlanKind,
	type PlanNote,
	type RunPlan,
	type RunStage,
} from "../../shared/runs.ts";
import { RunError } from "./errors.ts";
import { TYPESAFE_API_KEY_ENVIRONMENT } from "./environment.ts";
import { describeRun, type PipelinePlan, type PlanDocument } from "./plan.ts";

/**
 * How the pipeline is asked what a run would do.
 *
 * A seam for the same reason `SpawnChild` is one in `runner.ts`: the refusals below and the
 * re-check at start are behaviour worth asserting, and a test that needs a Python with
 * `venator` importable on the host is a test that gets skipped somewhere. Production passes
 * nothing and gets the real adapter.
 */
export type DescribeRun = (kind: PlanKind, profile: string, context: LocationContext) => Promise<PlanDocument>;

/**
 * Long enough to read the plan and decide, short enough that what it says is still true.
 *
 * The window does not close the gap between what a plan reported and what a run finds — a
 * Profile edit or a Discover run in a terminal can move it — which is why a `RunState` carries
 * the numbers it was authorised with rather than being rewritten by the numbers it found.
 */
const PLAN_LIFETIME_MS = 10 * 60_000;

/** Nobody issues thousands of these; the cap is here so a leak cannot become a growing map. */
const MAXIMUM_OPEN_PLANS = 32;

type IssuedPlan = {
	readonly plan: RunPlan;
	readonly issuedAt: number;
	used: boolean;
};

const issued = new Map<string, IssuedPlan>();

function sweep(at: number): void {
	for (const [token, entry] of issued) {
		if (entry.used || at - entry.issuedAt > PLAN_LIFETIME_MS) issued.delete(token);
	}
	while (issued.size >= MAXIMUM_OPEN_PLANS) {
		const oldest = issued.keys().next();
		if (oldest.done === true) break;
		issued.delete(oldest.value);
	}
}

/**
 * Which Profile a run would be for.
 *
 * The dashboard's own rule, which `onboarding/discovery.ts` keeps aligned with the pipeline's:
 * the only Profile that is not a scaffold. With none there is nothing to run; with two, the
 * pipeline refuses to guess whose search it is running and the dashboard is in no better
 * position to guess than the pipeline is.
 *
 * The chosen name is then passed to every subprocess explicitly. The run surface never leaves
 * the Profile implicit — the plan states it, the screen shows it, and nobody presses a button
 * whose subject they cannot see.
 */
function chooseProfile(context: LocationContext): string {
	const profiles = discoverProfiles(context);
	const selected = impliedProfile(profiles, context);
	const selectable = profiles.filter((profile) => !profile.scaffold);
	if (selected === null && selectable.length === 0) {
		throw new RunError(
			"no_profile",
			"There is no Profile to run against, so there is nothing to run.",
			"Set one up first: a Profile is what says which employers to look at and what to keep.",
		);
	}
	if (selected === null) {
		throw new RunError(
			"profile_ambiguous",
			`This Install has more than one search set up — ${selectable.map((profile) => profile.name).join(" and ")} — and nothing here says which one to run.`,
			"Running the wrong one would file Postings against the wrong search, so it is not guessed at.",
		);
	}
	return selected.name;
}

/** The pipeline's answer, or the refusal it is. */
async function askPipeline(
	kind: PlanKind,
	profile: string,
	context: LocationContext,
	describe: DescribeRun,
): Promise<PipelinePlan> {
	const document = await describe(kind, profile, context);
	if (document.outcome === "absent") {
		throw new RunError(
			"pipeline_absent",
			document.message,
			"A run can still be started from a terminal in a checkout: `python -m venator.schedule.loop --only discover,filters,view`.",
		);
	}
	if (document.outcome === "timed-out") {
		throw new RunError(
			"pipeline_absent",
			"Working out what a run would do did not finish, so nothing was started.",
			"Try again; if it keeps happening, something on this machine is holding the pipeline up.",
		);
	}
	if (document.outcome === "mismatch") {
		throw new RunError(
			"pipeline_absent",
			"The pipeline answered in a shape this dashboard does not know, so nothing is being described rather than something being guessed at.",
			"This dashboard and the pipeline it runs are out of step with each other.",
		);
	}
	if (document.outcome === "failure") {
		if (document.kind === "profile") {
			throw new RunError(
				"unknown_profile",
				document.message ?? "That Profile could not be loaded.",
				null,
				"profile",
			);
		}
		if (document.kind === "store") {
			throw new RunError(
				"store_unreadable",
				document.message ?? "This Install's stored Postings and Filter Decisions could not be read.",
			);
		}
		throw new RunError(
			"pipeline_absent",
			"Working out what a run would do failed, and the pipeline did not say why.",
			"The dashboard's own log has what it printed.",
		);
	}
	return document.plan;
}

/**
 * What is worth saying about this plan that is not a refusal.
 *
 * Neither of these is an error and neither stops anything from being true — but both are
 * things a person would otherwise discover by running a pipeline successfully and watching
 * nothing happen.
 */
function notesFor(
	kind: PlanKind,
	boards: number,
	viewPath: string,
	dashboardViewPath: string,
	postingCount: number,
	keyAvailable: boolean,
): readonly PlanNote[] {
	const notes: PlanNote[] = [];
	if (kind === "fetch-and-filter" && boards === 0) {
		notes.push({
			code: "no-boards",
			message:
				"This Profile registers no employer, so a run has nothing to fetch from. Register one and this control turns on.",
		});
	}
	if (kind === "jev-it" && postingCount === 0) {
		notes.push({
			code: "nothing-to-qualify",
			message: "No current passing Posting with description text needs Jev. Refresh Postings and Hard Filters if you expected more.",
		});
	}
	if (kind === "jev-it" && !keyAvailable) {
		// Said as a note, not a refusal: the plan still counts what is waiting, and once a key
		// is saved Jev it can run them. Nothing about the key is said but that it is missing.
		notes.push({
			code: "jev-key-missing",
			message: "No TypeSafe key is saved, so these Postings wait for Jev until you add one.",
		});
	}

	if (viewPath !== dashboardViewPath) {
		// Deliberately without the two paths. A filesystem path is machine vocabulary and this
		// is prose on a screen — the same rule `ui/tools/smoke.ts` enforces about board tokens
		// and rule identifiers, and it caught this: a checkout whose directory happens to
		// contain a banned word would put it in front of the Owner. The paths are still in the
		// plan (`viewPath`, `dashboardViewPath`), the panel hangs them off the note's `title`,
		// and the dashboard header already names the database it is reading.
		notes.push({
			code: "view-elsewhere",
			message:
				"A run would rebuild a different view database from the one this dashboard is reading, so what it finds would not show up here. The header names the one being read.",
		});
	}
	return notes;
}

/**
 * The stages this plan would run. `RUN_KIND_STAGES` is indexed by `RunKind`, so a plan kind
 * outside the startable closed set cannot compile into argv.
 */
function stagesFor(kind: PlanKind): readonly RunStage[] {
	const runKind = runKindOf(kind);
	return runKind === null ? [] : RUN_KIND_STAGES[runKind];
}

/** Where the dashboard is reading, said as a path even when nothing is built there yet. */
function dashboardView(context: LocationContext): string {
	const view = resolveView(context);
	// An `empty` view is a path where one *would* appear, which is exactly what a plan needs
	// to compare against; a `fixture` is the sample database and is nobody's real view, so the
	// comparison is made against where this Install's own view belongs.
	if (view.kind === "fixture") return defaultViewDatabase(context);
	return view.path;
}

function hasTypesafeApiKey(context: LocationContext): boolean {
	return (context.environment(TYPESAFE_API_KEY_ENVIRONMENT) ?? "").trim() !== "";
}

/**
 * Issues a plan for `kind`, and a token.
 *
 * The token starts the run the plan describes if the Profile registers at least one board.
 */
export async function issuePlan(
	kind: PlanKind,
	context: LocationContext = systemContext(),
	describe: DescribeRun = describeRun,
): Promise<RunPlan> {
	const profile = chooseProfile(context);
	const answer = await askPipeline(kind, profile, context, describe);
	if (kind === "jev-it" && answer.jev == null) {
		throw new RunError(
			"pipeline_absent",
			"The pipeline did not describe the Jev run, so nothing was guessed.",
			"This dashboard and the pipeline it runs are out of step with each other.",
		);
	}
	const dashboardViewPath = dashboardView(context);
	const answerJev = answer.jev ?? null;
	const postingCount = answerJev?.postingCount ?? 0;
	const keyAvailable = hasTypesafeApiKey(context);
	const notes = notesFor(kind, answer.boards, answer.viewPath, dashboardViewPath, postingCount, keyAvailable);
	const jev = answerJev === null ? null : {
		...answerJev,
		summary:
			`${answerJev.postingCount} Postings passed the Hard Filters, have description text, and have no Jev result under the current release.` +
			(keyAvailable ? "" : " Jev will pause until an API key is available."),
	};
	const plan: RunPlan = {
		token: `plan_${randomUUID()}`,
		kind,
		profile: answer.profileName,
		profileDirectory: answer.profileDirectory,
		stages: stagesFor(kind),
		storeRoot: answer.storeRoot,
		viewPath: answer.viewPath,
		dashboardViewPath,
		boards: answer.boards,
		jev,
		// A plan remains visible at zero work; starting it would do nothing, so it does not.
		startable:
			runKindOf(kind) !== null &&
			(kind === "fetch-and-filter" ? answer.boards > 0 : postingCount > 0),
		notes,
	};
	const at = Date.now();
	sweep(at);
	issued.set(plan.token, { plan, issuedAt: at, used: false });
	return plan;
}

/**
 * Redeems a token, re-checking everything the plan was issued under.
 *
 * The re-check is the point. A plan is a statement about this Install ten minutes ago, and the
 * things it states — which Profile is implied, where the store is, whether this Profile may
 * read it — can all have changed since. A plan that no longer describes what would happen is
 * expired rather than honoured.
 */
export async function redeemPlan(
	token: string,
	context: LocationContext = systemContext(),
	describe: DescribeRun = describeRun,
): Promise<RunPlan> {
	const entry = issued.get(token);
	const expired = new RunError(
		"plan_expired",
		"What this run was going to do is no longer current, so it was not started.",
		"Ask again what the run would do, and start it from that.",
	);
	if (entry === undefined || entry.used || Date.now() - entry.issuedAt > PLAN_LIFETIME_MS) {
		issued.delete(token);
		throw expired;
	}
	entry.used = true;
	issued.delete(token);

	// The kind first, and separately from `startable`, because the two say different things.
	// `startable` is about this Install now. The closed kind is about the surface in principle.
	if (runKindOf(entry.plan.kind) === null) {
		throw new RunError(
			"not_startable",
			"That plan does not name a run this dashboard can start, so nothing was started.",
			"Ask for a new plan before you try again.",
		);
	}

	if (!entry.plan.startable) {
		throw new RunError(
			"not_startable",
			entry.plan.notes[0]?.message ?? "There is nothing for this run to do.",
			"Nothing has gone wrong; there is simply nothing to fetch yet.",
		);
	}

	// Checked again rather than trusted: the answer must not depend on how fresh a screen is.
	const profile = chooseProfile(context);
	const answer = await askPipeline(entry.plan.kind, profile, context, describe);
	const currentDashboardViewPath = dashboardView(context);
	const plannedJev = entry.plan.jev ?? null;
	const currentJev = answer.jev ?? null;
	const jevChanged =
		currentJev === null
			? plannedJev !== null
			: plannedJev === null ||
				currentJev.asOf !== plannedJev.asOf ||
				currentJev.mode !== plannedJev.mode ||
				currentJev.postingCount !== plannedJev.postingCount ||
				currentJev.maximumUsd !== plannedJev.maximumUsd ||
				currentJev.selectionHash !== plannedJev.selectionHash;
	if (
		profile !== entry.plan.profile ||
		answer.profileDirectory !== entry.plan.profileDirectory ||
		answer.storeRoot !== entry.plan.storeRoot ||
		answer.viewPath !== entry.plan.viewPath ||
		currentDashboardViewPath !== entry.plan.dashboardViewPath ||
		answer.boards !== entry.plan.boards ||
		jevChanged
	) {
		throw expired;
	}
	return entry.plan;
}

/** Test seam: forgets every open plan. Never called by the server. */
export function resetPlansForTest(): void {
	issued.clear();
}
