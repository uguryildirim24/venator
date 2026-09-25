/**
 * Reading a run's progress off the loop's own output.
 *
 * `venator.schedule.loop` prints three shapes and flushes each as it writes it:
 *
 *     <stage>: starting            stdout
 *     <stage>: ok                  stdout
 *     <stage>: ERROR — <message>   stderr
 *
 * That is a contract, stated with the loop and in coordination/CONTRACTS.md, and not a
 * licence to parse the pipeline's prose. The loop's live `STAGE_ORDER` is
 * `discover | filters | jev | view | commit`. It follows
 * `readProbeDocument`'s discipline exactly: **a line it does not recognise changes no
 * state**, and the authority for what actually happened stays the exit status plus the
 * heartbeat rows the loop wrote. Nothing on screen is inferred from a line that was merely
 * plausible.
 *
 * A stage the run did not plan is ignored on the same rule. The loop is started with `--only`,
 * so a line for a stage nobody asked for means the two sides have drifted, and inventing a
 * stage on screen is the wrong answer to that.
 *
 * Everything here is a pure function over an immutable state, which is why the whole of it is
 * testable against a recorded transcript with no process anywhere.
 */

import { RUN_STAGES, type RunStage, type RunStageProgress } from "../../shared/runs.ts";

/** The state a reader carries between lines. */
export type ProgressState = {
	readonly stages: readonly RunStageProgress[];
	/** Where the loop said it stopped, in the loop's own words. Null until it says so. */
	readonly failure: {
		readonly stage: RunStage;
		readonly message: string;
	} | null;
};

const STAGE_MARK = /^([a-z]+): (starting|ok)$/u;
const EMPLOYER_PROGRESS = /^discover: employer (\d+)\/(\d+) (.{1,200})$/u;

/**
 * The em dash is the loop's, and the space before it is what keeps this from matching the
 * loop's *other* error lines — `<stage>: ERROR writing heartbeat — …` is a different event
 * with a different meaning, and it is deliberately not recognised here.
 */
const STAGE_ERROR = /^([a-z]+): ERROR — (.+)$/u;

export function initialProgress(stages: readonly RunStage[]): ProgressState {
	return {
		stages: stages.map((stage) => ({ stage, state: "waiting", startedAt: null, endedAt: null })),
		failure: null,
	};
}

function named(state: ProgressState, raw: string): RunStage | null {
	const stage = RUN_STAGES.find((candidate) => candidate === raw);
	if (stage === undefined) return null;
	return state.stages.some((entry) => entry.stage === stage) ? stage : null;
}

function replace(
	state: ProgressState,
	stage: RunStage,
	change: (entry: RunStageProgress) => RunStageProgress,
): readonly RunStageProgress[] {
	return state.stages.map((entry) => (entry.stage === stage ? change(entry) : entry));
}

/** Applies one line of the loop's output. Returns the state unchanged for anything else. */
export function readStageLine(state: ProgressState, line: string, at: string): ProgressState {
	const trimmed = line.trimEnd();
	const employer = EMPLOYER_PROGRESS.exec(trimmed);
	if (employer !== null) {
		const index = Number(employer[1]);
		const total = Number(employer[2]);
		if (!Number.isSafeInteger(index) || !Number.isSafeInteger(total) || index < 1 || index > total
			|| !state.stages.some((entry) => entry.stage === "discover" && entry.state === "running")) return state;
		return {
			...state,
			stages: replace(state, "discover", (entry) => ({ ...entry, detail: `${employer[3]} · ${index} of ${total} employers` })),
		};
	}

	const failed = STAGE_ERROR.exec(trimmed);
	if (failed !== null) {
		const stage = named(state, failed[1] ?? "");
		const message = failed[2];
		if (stage === null || message === undefined) return state;
		return {
			stages: replace(state, stage, (entry) => ({ ...entry, state: "failed", endedAt: at })),
			failure: { stage, message },
		};
	}

	const mark = STAGE_MARK.exec(trimmed);
	if (mark === null) return state;
	const stage = named(state, mark[1] ?? "");
	if (stage === null) return state;
	if (mark[2] === "starting") {
		return {
			stages: replace(state, stage, (entry) => ({ ...entry, state: "running", startedAt: at })),
			failure: state.failure,
		};
	}
	return {
		stages: replace(state, stage, (entry) => ({ ...entry, state: "ok", endedAt: at })),
		failure: state.failure,
	};
}

/**
 * The stage list as it stands once the run is over.
 *
 * A stage still `waiting` when the loop stopped was never reached, and saying so is the
 * difference between "the Hard Filters failed" and "the Hard Filters never ran". A stage left
 * `running` is one the loop was inside when it died, which is a failure of that stage whether
 * or not it managed to print a line about it.
 */
export function settleProgress(state: ProgressState, succeeded: boolean): readonly RunStageProgress[] {
	if (succeeded) return state.stages;
	return state.stages.map((entry) => {
		if (entry.state === "waiting") return { ...entry, state: "not-reached" };
		if (entry.state === "running") return { ...entry, state: "failed" };
		return entry;
	});
}

/**
 * Splits a stream chunk into whole lines, keeping the partial tail for the next chunk.
 *
 * A pipe hands over whatever has been written, which is regularly half a line. Reading state
 * off a half-line is how a reader invents an event nobody printed.
 */
export type SplitLines = {
	readonly lines: readonly string[];
	/** The partial last line, to be prepended to the next chunk. */
	readonly rest: string;
};

export function splitLines(buffered: string, chunk: string): SplitLines {
	const combined = buffered + chunk;
	const parts = combined.split(/\r?\n/u);
	const rest = parts.pop() ?? "";
	return { lines: parts, rest };
}
