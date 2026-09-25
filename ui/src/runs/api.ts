/**
 * The app's side of the run surface.
 *
 * `src/api.ts` reads the dashboard's `GET`-only API and `src/onboarding/api.ts` writes a
 * Profile; this starts pipeline stages, and it is a third module for the same reason the
 * server mounts a third router: reads and actions do not mix, and keeping the acting calls
 * out of the read client is what makes that visible rather than merely true.
 *
 * Two rules live here rather than in a screen:
 *
 * - **Nothing starts a run on its own.** `startRun` is only ever called from a handler for
 *   something a person pressed. `plan` and `currentRun` are safe to call on a render — the
 *   first spawns an adapter that writes nothing and the second reads one object out of the
 *   server's memory — and that is exactly why the start is a separate call taking a token:
 *   asking what a run would do can never turn into starting one by accident.
 * - **A failure arrives as a value, not as a string.** Every refusal carries a stable `code`,
 *   a `message` written for the person reading it, a `remedy` where there is one, and — on
 *   `run_in_progress` — the run that is already going, so a double press attaches to it
 *   rather than showing an error.
 */

import {
	RUN_REQUEST_HEADER,
	RUN_REQUEST_HEADER_VALUE,
	type PlanKind,
	type PlanResponse,
	type RecentRunsResponse,
	type RunErrorBody,
	type RunErrorCode,
	type RunResponse,
	type RunState,
} from "../../shared/runs.ts";
import { API_BASE_URL } from "../api.ts";

/** A refusal from the run surface, with everything the screen needs to render it. */
export class RunRequestError extends Error {
	readonly code: RunErrorCode | null;
	readonly remedy: string | null;
	/** The run that is already going, on `run_in_progress`. Null everywhere else. */
	readonly run: RunState | null;

	constructor(message: string, code: RunErrorCode | null, remedy: string | null, run: RunState | null) {
		super(message);
		this.name = "RunRequestError";
		this.code = code;
		this.remedy = remedy;
		this.run = run;
	}
}

/**
 * What a browser reports when the loopback server is not answering at all, said once here
 * rather than let through as the platform's own wording — which is a different sentence in
 * every browser and says nothing a person can act on.
 */
const UNREACHABLE = new RunRequestError(
	"The dashboard could not reach its own local server, so nothing was started.",
	null,
	"Check that the Venator dashboard is still running, then try again.",
	null,
);

function failureFrom(status: number, body: RunErrorBody | null): RunRequestError {
	const error = body?.error;
	if (error === undefined) {
		return new RunRequestError(
			`That request was refused (${String(status)}), and no reason came back with it.`,
			null,
			null,
			null,
		);
	}
	return new RunRequestError(error.message, error.code, error.remedy, body?.run ?? null);
}

async function call<Value>(path: string, method: "GET" | "POST", body: string | null): Promise<Value> {
	const headers = new Headers({ accept: "application/json", [RUN_REQUEST_HEADER]: RUN_REQUEST_HEADER_VALUE });
	if (body !== null) headers.set("content-type", "application/json");
	let response: Response;
	// `body` is left off entirely for a `GET` rather than passed as null: `fetch` rejects the
	// pairing, and the two reads on this surface carry no body at all.
	const request: RequestInit = body === null ? { method, headers } : { method, headers, body };
	try {
		response = await fetch(`${API_BASE_URL}/runs${path}`, request);
	} catch {
		throw UNREACHABLE;
	}
	if (!response.ok) {
		const failed: RunErrorBody | null = await response.json().catch(() => null);
		throw failureFrom(response.status, failed);
	}
	// This surface shares shared/runs.ts with the server, so a successful body is already the
	// contract's own type rather than foreign input.
	return await response.json();
}

/**
 * Asks what a run would do, before anything is started.
 *
 * Safe from a render: the adapter it spawns writes nothing, starts nothing and makes no
 * request. The token in the answer is what `startRun` takes.
 */
export function planRun(kind: PlanKind): Promise<PlanResponse> {
	return call<PlanResponse>("/plan", "POST", JSON.stringify({ kind }));
}

/** Starts the run a plan authorised. Only ever from something the Owner pressed. */
export function startRun(token: string): Promise<RunResponse> {
	return call<RunResponse>("", "POST", JSON.stringify({ token }));
}

/** The run this dashboard's server is supervising, if any. Reads memory; spawns nothing. */
export function readCurrentRun(): Promise<RunResponse> {
	return call<RunResponse>("/current", "GET", null);
}

/** The last settled runs this server process remembers. Reads memory; spawns nothing. */
export function readRecentRuns(limit = 5): Promise<RecentRunsResponse> {
	return call<RecentRunsResponse>(`/recent?limit=${String(limit)}`, "GET", null);
}

/** Stops the live run, and everything it spawned. */
export function stopRun(): Promise<RunResponse> {
	return call<RunResponse>("/current/stop", "POST", null);
}
