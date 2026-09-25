/**
 * What a run would do, asked of the pipeline rather than worked out here.
 *
 * One subprocess — `runs/plan.py`, the adapter beside this file — one JSON document, one
 * normalisation. Which Profile a run loads, where it writes, which view it rebuilds and
 * whether this Install's Filter Decisions belong to that Profile are all questions
 * `src/venator/` already answers; the adapter composes those answers and this module reads
 * them. That is the arrangement `onboarding/boards.ts` has with `venator.discover.register`
 * and `onboarding/runtime.ts` has with `venator.llm.probe`, and it exists for the same
 * measured reason: a second copy of a pipeline rule in TypeScript drifts, and this repository
 * has already paid for one.
 *
 * Asking costs nothing. `plan.py` writes no file, creates no store, starts no stage and makes
 * no request — so unlike the runtime probe this is safe to call from a screen a person opened.
 *
 * A shape this module does not recognise is reported as a mismatch rather than guessed at,
 * on `readProbeDocument`'s own principle: naming the drift beats rendering something wrong.
 */

import { spawn } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { PlanKind } from "../../shared/runs.ts";
import {
	namedPythonInterpreter,
	pipelineWorkingDirectory,
	pythonInterpreter,
	systemContext,
	type LocationContext,
} from "../locations.ts";
// The server's JSON value model. It sits under `onboarding/` because that is where it was
// first needed, not because it is an onboarding rule; nothing about reading a mapping out of
// a parsed document belongs to one surface. Imported rather than copied for the reason the
// whole of this file exists.
import { asNumber, asMapping, asText, at, parseJson } from "../onboarding/json.ts";
import { runEnvironment } from "./environment.ts";

/** The adapter that composes the pipeline's own answers. Beside this file, and only here. */
const PLAN_SCRIPT = join(fileURLToPath(new URL(".", import.meta.url)), "plan.py");

/**
 * Loading a Profile and listing a decisions directory. There is no network and no LLM in it,
 * so this bounds a machine that is thrashing rather than work that legitimately takes time.
 */
// Counting current Jev work requires replaying the local Posting and qualification stores.
const PLAN_TIMEOUT_MS = 120_000;

/** Exit 3 from the adapter: the pipeline is not importable in this Install. */
const PIPELINE_MISSING_EXIT = 3;

/** What the pipeline says a run would do. Every field is the pipeline's answer, not ours. */
export type PipelineJevPlan = {
	readonly asOf: string;
	readonly mode: "shadow" | "promoted";
	readonly postingCount: number;
	readonly maximumUsd: number;
	readonly selectionHash: string;
};

export type PipelinePlan = {
	readonly profileName: string;
	readonly profileDirectory: string;
	readonly storeRoot: string;
	readonly viewPath: string;
	readonly boards: number;
	readonly jev?: PipelineJevPlan | null;
};

/**
 * Why the pipeline will not describe a run.
 *
 * `profile` and `store` carry the pipeline's own sentence, which already names the fix.
 * `failed` carries none: the adapter classified it as unrecognised and put the original on
 * stderr, where the server logs it.
 */
export type PlanFailureKind = "profile" | "store" | "failed";

export type PlanDocument =
	| { readonly outcome: "plan"; readonly plan: PipelinePlan }
	| { readonly outcome: "failure"; readonly kind: PlanFailureKind; readonly message: string | null }
	/** The adapter answered in a shape this dashboard does not know. Never guessed at. */
	| { readonly outcome: "mismatch" }
	/** The pipeline is not part of this Install, or the interpreter would not start. */
	| { readonly outcome: "absent"; readonly message: string }
	| { readonly outcome: "timed-out" };

const PLAN_FAILURE_KINDS: readonly PlanFailureKind[] = ["profile", "store", "failed"];

/**
 * Reads the adapter's document.
 *
 * Exported for its own tests. Every field is required to be the type it is: a `boards` that
 * arrived as text, or a `viewPath` that did not arrive, is a mismatch rather than a zero or
 * an empty string, because both of those would be rendered to a person as a fact.
 */
export function readPlanDocument(text: string): PlanDocument {
	const document = asMapping(parseJson(text));
	if (document === null) return { outcome: "mismatch" };

	const failure = asMapping(at(document, "failure"));
	if (failure !== null) {
		const raw = asText(at(failure, "kind"));
		const kind = PLAN_FAILURE_KINDS.find((candidate) => candidate === raw);
		if (kind === undefined) return { outcome: "mismatch" };
		return { outcome: "failure", kind, message: asText(at(failure, "message")) };
	}

	const profile = asMapping(at(document, "profile"));
	if (profile === null) return { outcome: "mismatch" };
	const profileName = asText(at(profile, "name"));
	const profileDirectory = asText(at(profile, "directory"));
	const storeRoot = asText(at(document, "storeRoot"));
	const viewPath = asText(at(document, "viewPath"));
	const boards = asNumber(at(document, "boards"));
	const rawJev = at(document, "jev");
	const jevDocument = rawJev === undefined ? null : asMapping(rawJev);
	let jev: PipelineJevPlan | null = null;
	if (jevDocument !== null) {
		const asOf = asText(at(jevDocument, "asOf"));
		const rawMode = asText(at(jevDocument, "mode"));
		const postingCount = asNumber(at(jevDocument, "postingCount"));
		const maximumUsd = asNumber(at(jevDocument, "maximumUsd"));
		const selectionHash = asText(at(jevDocument, "selectionHash"));
		if (
			asOf === null ||
			!/^\d{4}-\d{2}-\d{2}$/u.test(asOf) ||
			(rawMode !== "shadow" && rawMode !== "promoted") ||
			postingCount === null ||
			!Number.isInteger(postingCount) ||
			postingCount < 0 ||
			maximumUsd === null ||
			!Number.isFinite(maximumUsd) ||
			maximumUsd < 0 ||
			selectionHash === null ||
			!/^[a-f0-9]{64}$/u.test(selectionHash)
		) {
			return { outcome: "mismatch" };
		}
		jev = { asOf, mode: rawMode, postingCount, maximumUsd, selectionHash };
	} else if (rawJev !== undefined) {
		return { outcome: "mismatch" };
	}
	if (
		profileName === null ||
		profileName === "" ||
		profileDirectory === null ||
		storeRoot === null ||
		viewPath === null ||
		boards === null ||
		!Number.isInteger(boards)
	) {
		return { outcome: "mismatch" };
	}
	const plan: PipelinePlan = { profileName, profileDirectory, storeRoot, viewPath, boards };
	return {
		outcome: "plan",
		plan: jev === null ? plan : { ...plan, jev },
	};
}

/**
 * What to say when the interpreter would not start at all.
 *
 * The same two facts `onboarding/runtime.ts` distinguishes, for the same reason: an Install
 * that named no interpreter and found none has no pipeline in it, while a desktop bundle that
 * shipped one and cannot start it is a broken interpreter, and telling that person the
 * pipeline is not installed sends them to fix something that is already there.
 */
function launchFailureMessage(context: LocationContext): string {
	const named = namedPythonInterpreter(context);
	if (named === null) {
		return "Running the pipeline is not part of this Install, so nothing can be started from here.";
	}
	return `The interpreter this Install runs the pipeline with could not be started, so no run can begin: ${named}`;
}

/** What the adapter's process did, as the four facts the outcome is decided from. */
export type PlanProcess = {
	readonly code: number | null;
	readonly stdout: string;
	readonly stderr: string;
	readonly timedOut: boolean;
	readonly launchFailed: boolean;
};

function runPlanScript(kind: PlanKind, profile: string, context: LocationContext): Promise<PlanProcess> {
	return new Promise((settle) => {
		const child = spawn(
			pythonInterpreter(context),
			[PLAN_SCRIPT, "--profile", profile, "--kind", kind],
			{
				cwd: pipelineWorkingDirectory(context),
				stdio: ["ignore", "pipe", "pipe"],
				env: runEnvironment(context),
			},
		);
		let stdout = "";
		let stderr = "";
		let timedOut = false;
		let launchFailed = false;
		const timer = setTimeout(() => {
			timedOut = true;
			child.kill("SIGKILL");
		}, PLAN_TIMEOUT_MS);
		child.stdout?.setEncoding("utf8");
		child.stderr?.setEncoding("utf8");
		child.stdout?.on("data", (chunk: string) => {
			stdout += chunk;
		});
		child.stderr?.on("data", (chunk: string) => {
			stderr += chunk;
		});
		child.on("error", () => {
			launchFailed = true;
		});
		child.on("close", (code) => {
			clearTimeout(timer);
			settle({ code, stdout, stderr, timedOut, launchFailed });
		});
	});
}

/**
 * Asks the pipeline what a run for `profile` would do.
 *
 * The Profile is always named. The run surface never leaves it implicit — the plan states
 * which Profile it is for and the screen shows that name, so nobody presses a button whose
 * subject they cannot see.
 */
export async function describeRun(
	kind: PlanKind,
	profile: string,
	context: LocationContext = systemContext(),
): Promise<PlanDocument> {
	return readPlanResult(await runPlanScript(kind, profile, context), context);
}

/**
 * What a finished adapter process means.
 *
 * Separated from the spawn so every branch is a pure function of four values and can be
 * asserted without a child process — which is what makes the branches testable identically on
 * every host, rather than on the one where a stand-in program happens to be startable.
 */
export function readPlanResult(finished: PlanProcess, context: LocationContext): PlanDocument {
	if (finished.launchFailed) return { outcome: "absent", message: launchFailureMessage(context) };
	if (finished.timedOut) return { outcome: "timed-out" };
	if (finished.code === PIPELINE_MISSING_EXIT) {
		return { outcome: "absent", message: launchFailureMessage(context) };
	}
	if (finished.code !== 0) {
		// Logged rather than returned: an adapter that exited non-zero prints paths and
		// environment contents, and none of that is the caller's to see.
		process.stderr.write(`runs: the plan adapter exited ${String(finished.code)}: ${finished.stderr.slice(0, 400)}\n`);
		return { outcome: "absent", message: launchFailureMessage(context) };
	}
	try {
		return readPlanDocument(finished.stdout);
	} catch {
		return { outcome: "mismatch" };
	}
}
