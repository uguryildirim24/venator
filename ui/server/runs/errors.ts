/**
 * The one error shape the run surface answers with.
 *
 * The body is onboarding's, deliberately — `{ error: { code, message, remedy, field } }` —
 * with one addition: `run` carries the live `RunState` on a `run_in_progress` refusal, so a
 * second press resolves to "you are already watching this run" rather than to a failure. The
 * code set is this surface's own, because a run refusal and a Profile-write refusal are
 * different vocabularies and a shared enum would let either drift into the other's screens.
 *
 * Every message here is written by hand, with two named exceptions that are the opposite of
 * a leak: `store_unreadable` and `unknown_profile` carry the *pipeline's* own sentence,
 * because `venator.paths` and `venator.profile` already name the fix and this dashboard is in
 * no position to say it better. Everything else a subprocess throws is logged to stderr and
 * never returned — those messages carry absolute paths and environment contents.
 */

import type { RunErrorBody, RunErrorCode, RunState } from "../../shared/runs.ts";

/** The literal statuses this surface answers with; Hono types a response on the literal. */
export type RunStatus = 400 | 403 | 404 | 409 | 422 | 503;

const STATUS: ReadonlyMap<RunErrorCode, RunStatus> = new Map([
	["bad_request", 400],
	["forbidden_origin", 403],
	// There is no Profile to run against, or there are two and neither is this Install's
	// implied one. A conflict rather than a bad request: nothing about the request is wrong.
	["no_profile", 409],
	["profile_ambiguous", 409],
	["unknown_profile", 422],
	["run_in_progress", 409],
	["no_run", 404],
	["plan_expired", 409],
	["not_startable", 409],
	// The pipeline is not part of this Install, or the interpreter it names will not start.
	// Nothing the caller did; 503 is what says so.
	["pipeline_absent", 503],
	["store_unreadable", 409],
	["run_failed_to_start", 503],
]);

export class RunError extends Error {
	readonly code: RunErrorCode;
	readonly remedy: string | null;
	readonly field: string | null;
	/** The live run, on `run_in_progress` only. Null everywhere else. */
	readonly run: RunState | null;

	constructor(
		code: RunErrorCode,
		message: string,
		remedy: string | null = null,
		field: string | null = null,
		run: RunState | null = null,
	) {
		super(message);
		this.name = "RunError";
		this.code = code;
		this.remedy = remedy;
		this.field = field;
		this.run = run;
	}

	get status(): RunStatus {
		return STATUS.get(this.code) ?? 503;
	}

	get body(): RunErrorBody {
		return {
			error: { code: this.code, message: this.message, remedy: this.remedy, field: this.field },
			run: this.run,
		};
	}
}
