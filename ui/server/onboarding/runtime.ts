/**
 * The dashboard's only coupling to the pipeline's LLM runtime.
 *
 * Everything the UI knows about runtimes comes through here: one subprocess, one JSON
 * document, one normalisation. `python -m venator.llm.probe` owns what a lane is, what state
 * it is in, and what to tell someone about it — this module runs it and passes its words
 * along. `remedy` and `caveat` in particular are reproduced verbatim and never rewritten,
 * because the point of the probe owning that copy is that it cannot drift from the truth.
 *
 * Three properties are load-bearing and are not this module's to soften:
 *
 * - **A probe costs money.** Each lane is a real process launch and a small completion
 *   against the person's own subscription. Nothing here runs on a schedule, on a render or
 *   on a retry; `--all` is asked for explicitly or it does not happen, and a probe already in
 *   flight is refused rather than duplicated.
 * - **There is no fallback between runtimes.** A lane that is missing or not signed in is
 *   reported as exactly that. Nothing here substitutes a different lane, and nothing here
 *   presents the lanes as interchangeable — the Codex lane's own caveat says it is only
 *   partly verified, and dropping that field would be the misrepresentation it exists to
 *   prevent.
 * - **An absent probe is an answer, not a crash.** An Install without the module says so.
 */

import { spawn } from "node:child_process";

import {
	namedPythonInterpreter,
	pipelineWorkingDirectory,
	pythonInterpreter,
	systemContext,
	type LocationContext,
} from "../locations.ts";
import { nonJevInheritedEnvironment } from "../runs/environment.ts";
import type { ProbeOutcome, ProbeUnavailableReason, RuntimeLane, RuntimeState } from "../../shared/onboarding.ts";
import { RUNTIME_STATES } from "../../shared/onboarding.ts";
import { asBoolean, asList, asMapping, asText, at, parseJson } from "./json.ts";
import type { JsonMapping } from "./json.ts";

export type ProbeResult = {
	readonly probe: ProbeOutcome;
	readonly lanes: readonly RuntimeLane[];
};


export type ProbeRequest = "default" | "all" | { readonly lane: string };

/** One lane is a launch and a completion; three lanes are three of each. */
const SINGLE_LANE_TIMEOUT_MS = 60_000;
const ALL_LANES_TIMEOUT_MS = 150_000;

/** A lane name is passed to a subprocess, so it is checked before it gets there. */
const LANE_NAME = /^[a-z][a-z0-9_-]{0,31}$/u;

export class RuntimeProbeBusyError extends Error {
	constructor() {
		super("A runtime check is already running.");
		this.name = "RuntimeProbeBusyError";
	}
}

export class RuntimeLaneNameError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "RuntimeLaneNameError";
	}
}

export function validateLaneName(raw: string): string {
	const lane = raw.trim();
	if (!LANE_NAME.test(lane)) {
		throw new RuntimeLaneNameError("A runtime name holds only lowercase letters, digits, dashes and underscores.");
	}
	return lane;
}

function argumentsFor(request: ProbeRequest): readonly string[] {
	if (request === "default") return ["-m", "venator.llm.probe", "--json"];
	if (request === "all") return ["-m", "venator.llm.probe", "--all", "--json"];
	return ["-m", "venator.llm.probe", "--lane", request.lane, "--json"];
}

type ProbeProcess = {
	readonly code: number | null;
	readonly stdout: string;
	readonly stderr: string;
	readonly timedOut: boolean;
	readonly launchFailed: boolean;
};

function runProbe(request: ProbeRequest, context: LocationContext): Promise<ProbeProcess> {
	const timeout = request === "all" ? ALL_LANES_TIMEOUT_MS : SINGLE_LANE_TIMEOUT_MS;
	return new Promise((settle) => {
		const child = spawn(pythonInterpreter(context), [...argumentsFor(request)], {
			cwd: pipelineWorkingDirectory(context),
			stdio: ["ignore", "pipe", "pipe"],
			env: nonJevInheritedEnvironment(context),
		});
		let stdout = "";
		let stderr = "";
		let timedOut = false;
		let launchFailed = false;
		const timer = setTimeout(() => {
			timedOut = true;
			child.kill("SIGKILL");
		}, timeout);
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

function unavailable(reason: ProbeUnavailableReason, message: string): ProbeResult {
	return { probe: { available: false, reason, message }, lanes: [] };
}

function readState(value: string | null): RuntimeState | null {
	return RUNTIME_STATES.find((candidate) => candidate === value) ?? null;
}

function readLane(entry: JsonMapping): RuntimeLane | null {
	const lane = asText(at(entry, "lane"));
	const state = readState(asText(at(entry, "state")));
	if (lane === null || lane === "" || state === null) return null;
	return {
		lane,
		state,
		ready: asBoolean(at(entry, "ready")) ?? false,
		detail: asText(at(entry, "detail")),
		remedy: asText(at(entry, "remedy")),
		caveat: asText(at(entry, "caveat")),
		default: asBoolean(at(entry, "default")) ?? false,
	};
}

/**
 * Reads the probe's document.
 *
 * `lanes` may be the whole document or a `lanes` key inside it, because a probe that answers
 * for one lane and a probe that answers for all of them are the same question. Anything else
 * — a missing lane name, a state this dashboard has never heard of — is reported as a
 * contract mismatch rather than guessed at, on the same principle the view database follows:
 * naming the drift beats rendering something wrong.
 */
export function readProbeDocument(text: string): ProbeResult {
	const document = parseJson(text);
	const mapping = asMapping(document);
	// A one-lane answer may arrive as the lane itself, a list of one, or a lanes key.
	const single = mapping !== null && at(mapping, "lane") !== undefined ? [document] : null;
	const entries = single ?? asList(mapping === null ? document : at(mapping, "lanes"));
	if (entries === null) {
		return unavailable("contract_mismatch", "The runtime check answered in a shape this dashboard does not know.");
	}
	const lanes: RuntimeLane[] = [];
	for (const entry of entries) {
		const laneMapping = asMapping(entry);
		const lane = laneMapping === null ? null : readLane(laneMapping);
		if (lane === null) {
			return unavailable(
				"contract_mismatch",
				"The runtime check reported a runtime this dashboard does not know how to describe, so none is being shown rather than one being guessed at.",
			);
		}
		lanes.push(lane);
	}
	return { probe: { available: true, reason: null, message: null }, lanes };
}

/**
 * What to say when the interpreter would not start at all.
 *
 * Two different facts, and saying the wrong one has been the whole of this seam's dishonesty.
 * An Install that named no interpreter and found none has no pipeline in it, and that is what
 * the original sentence describes. A desktop bundle ships an interpreter and the host names it,
 * so on that machine the check *is* part of the Install and what failed is the interpreter —
 * telling that person the pipeline is not installed sends them to fix something that is already
 * there. The path is named because it is the only thing that makes the failure actionable, and
 * it is this Install's own path rather than anything a Posting or an employer supplied.
 */
function launchFailureMessage(context: LocationContext): string {
	const named = namedPythonInterpreter(context);
	if (named === null) {
		return "The runtime check is not part of this Install, so nothing could be asked about the runtimes on this machine.";
	}
	return `The interpreter this Install runs the pipeline with could not be started, so nothing could be asked about the runtimes on this machine: ${named}`;
}

let inFlight = false;

/**
 * Runs the probe once and normalises the result.
 *
 * Throws only for a caller's own mistake — a bad lane name, or a probe already running.
 * Everything else, including a probe that is not installed at all, comes back as a result.
 */
export async function probeRuntimes(
	request: ProbeRequest,
	context: LocationContext = systemContext(),
): Promise<ProbeResult> {
	if (request !== "default" && request !== "all") validateLaneName(request.lane);
	if (inFlight) throw new RuntimeProbeBusyError();
	inFlight = true;
	try {
		const finished = await runProbe(request, context);
		if (finished.launchFailed) {
			return unavailable("not_installed", launchFailureMessage(context));
		}
		if (finished.timedOut) {
			return unavailable("timed_out", "The runtime check did not finish in time, so nothing is being reported about it.");
		}
		if (finished.code === 2) {
			// Exit 2 is a usage error: this module built the command, so this is its own bug.
			process.stderr.write(`onboarding: the runtime check rejected its arguments: ${finished.stderr.slice(0, 400)}\n`);
			return unavailable("failed", "The runtime check could not be asked the question the dashboard asked it.");
		}
		if (finished.code !== 0) {
			process.stderr.write(`onboarding: the runtime check exited ${String(finished.code)}: ${finished.stderr.slice(0, 400)}\n`);
			return unavailable(
				"not_installed",
				"The runtime check could not be run in this Install, so nothing could be asked about the runtimes on this machine.",
			);
		}
		try {
			return readProbeDocument(finished.stdout);
		} catch {
			return unavailable("contract_mismatch", "The runtime check answered with something that is not readable as a result.");
		}
	} finally {
		inFlight = false;
	}
}
