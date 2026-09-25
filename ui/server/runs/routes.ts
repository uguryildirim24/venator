/**
 * The run surface — starting the pipeline stages a person could otherwise only start from a
 * terminal, and starting nothing else.
 *
 * **Why it exists.** Venator ships to people who never open a terminal. Discovering Postings,
 * running the Hard Filters and rebuilding the view are all commands typed into a shell, so
 * the installed app was unusable by the person it is for: setup ended by telling them the
 * stages are each started by them, and nothing in the app started any of them.
 *
 * **What it may never do,** and the enumeration is the guarantee rather than a summary of one:
 *
 * - It spawns exactly four listed commands. `runs/plan.py --kind <kind>` reports what a run
 *   would do and writes nothing. `fetch-and-filter` runs `python -m venator.schedule.loop
 *   --only discover,filters,view --profile <name>`. `jev-it` runs `python -m
 *   venator.schedule.loop --only jev` with the plan-bound date and spend cap, then `python
 *   -m venator.view.build --profile <name>`. The same view command is the recovery pass after
 *   a run stopped after writing rows. Every argv is selected by kind in `runner.ts`; none is
 *   assembled from request data.
 * - The loop stage set is the closed four of `shared/runs.ts`: `discover`, `filters`,
 *   `jev`, `view`. `RUN_KINDS` names `fetch-and-filter` and `jev-it`. There is no route that takes a
 *   module name, no argument passthrough, and no way
 *   to reach `venator.browser.fill`, `venator.browser.recon`, `venator.fill.plan` or
 *   `venator.track.record` from HTTP. `commit` is never named: a button in a shipped app must
 *   not commit somebody's Postings into whatever repository the app is sitting in.
 * - Nothing here reaches an employer's form, records a TrackEvent, changes a Posting's
 *   lifecycle state, or enables approve/reject. The never-submit invariant is untouched, and
 *   the browser guards it rests on — `install_dry_run_guards`, the literal `never_submit:
 *   true` on every FillPlan, the disabled approve/reject controls — are not addressed by any
 *   route here. Discover `GET`s ATS listing APIs exactly as the CLI already does; the other
 *   two stages read and write local files.
 * - Node writes nothing. What a run writes, it writes by running the same stages the CLI runs:
 *   appended Postings, appended Filter Decisions, appended heartbeats, and a rebuilt
 *   disposable view. There is no `fs` call on this surface.
 * - `POST` and `GET` only. There is no `PUT`, `PATCH` or `DELETE` anywhere on any of this
 *   server's three surfaces.
 *
 * **Mounted before `/api`, and that is load-bearing.** Hono's CORS middleware answers a
 * preflight from inside itself, and the first matching router wins; a router mounted at `/api`
 * with `allowMethods: ["GET","OPTIONS"]` therefore answers the preflight for every sibling
 * path under it, and a cross-origin `POST` is refused by the browser before it is sent.
 * Cross-origin is the desktop case: the webview page is `tauri://localhost` while the API is
 * `http://127.0.0.1:5170`. `tests/runs-mounting.test.ts` is what keeps the order from being
 * quietly undone.
 */

import { Hono } from "hono";
import { cors } from "hono/cors";

import {
	PLAN_KINDS,
	RUN_REQUEST_HEADER,
	RUN_REQUEST_HEADER_VALUE,
	type PlanKind,
	type PlanResponse,
	type RecentRunsResponse,
	type RunResponse,
} from "../../shared/runs.ts";
import { checkLocalAction, LOCAL_ACTION_ORIGINS } from "../http/local-action-guard.ts";
import { asMapping, asText, at, JsonParseError, parseJson } from "../onboarding/json.ts";
import type { JsonMapping } from "../onboarding/json.ts";
import { systemContext, type LocationContext } from "../locations.ts";
import { jevKey } from "../install-settings.ts";

async function contextWithJevKey(context: LocationContext, readKey: (context: LocationContext) => Promise<string | null>): Promise<LocationContext> {
	if ((context.environment("TYPESAFE_API_KEY") ?? "").trim()) return context;
	const key = await readKey(context);
	if (key === null) return context;
	return { ...context, environment: (name) => name === "TYPESAFE_API_KEY" ? key : context.environment(name) };
}
import { RunError } from "./errors.ts";
import { describeRun } from "./plan.ts";
import { issuePlan, redeemPlan, type DescribeRun } from "./tokens.ts";
import { currentRun, recentRuns, startRun, stopRun, type SpawnChild } from "./runner.ts";

/** Two short JSON objects are the whole of this surface's input. Nothing large arrives here. */
const MAXIMUM_JSON_BYTES = 4 * 1024;

const DEFAULT_RECENT = 5;
const MAXIMUM_RECENT = 20;

/**
 * Ten minutes on the preflight, because `GET /runs/current` is polled while a run is going and
 * every one of those is preflighted: the header that makes the origin allowlist a gate is
 * exactly what makes the request non-simple.
 */
const PREFLIGHT_MAX_AGE_SECONDS = 600;

function requireRunHeader(header: string | undefined, origin: string | undefined): void {
	const refusal = checkLocalAction(RUN_REQUEST_HEADER, RUN_REQUEST_HEADER_VALUE, header, origin);
	if (refusal !== null) throw new RunError(refusal.reason, refusal.message, refusal.remedy);
}

function readJsonBody(text: string): JsonMapping {
	if (text.length > MAXIMUM_JSON_BYTES) {
		throw new RunError("bad_request", "That request is larger than this surface accepts.");
	}
	if (text.trim() === "") return {};
	try {
		const mapping = asMapping(parseJson(text));
		if (mapping === null) throw new RunError("bad_request", "The request body must be a JSON object.");
		return mapping;
	} catch (cause) {
		if (cause instanceof JsonParseError) throw new RunError("bad_request", "The request body is not valid JSON.");
		throw cause;
	}
}

/**
 * Which plan was asked for.
 *
 * A kind, never a stage list. The request cannot widen what a run is: an unrecognised kind is
 * refused by name against the closed set rather than resolved to a default. The comparison is
 * against the literal strings and nothing else — no trimming, no case folding, no prefix
 * match — so `"Fetch-and-filter"` and `" fetch-and-filter "` are not in the set.
 *
 * The set here is `PLAN_KINDS`, which is what may be described before a start. The remedy says
 * "describe" because this route issues plans and never starts one itself.
 */
function kindFrom(body: JsonMapping): PlanKind {
	const raw = asText(at(body, "kind"));
	const kind = PLAN_KINDS.find((candidate) => candidate === raw);
	if (kind === undefined) {
		throw new RunError(
			"bad_request",
			`There is no run called “${raw ?? ""}”.`,
			`This dashboard can describe: ${PLAN_KINDS.join(", ")}.`,
			"kind",
		);
	}
	return kind;
}

function parseRecentLimit(raw: string | undefined): number {
	if (raw === undefined || raw === "") return DEFAULT_RECENT;
	const limit = Number.parseInt(raw, 10);
	if (!Number.isInteger(limit) || limit < 1) {
		throw new RunError("bad_request", `Invalid limit “${raw}”; expected a positive integer.`);
	}
	return Math.min(limit, MAXIMUM_RECENT);
}

export type RunRouteDependencies = {
	readonly location?: LocationContext;
	readonly describe?: DescribeRun;
	readonly spawnChild?: SpawnChild;
	readonly readJevKey?: (context: LocationContext) => Promise<string | null>;
};

export function createRunRoutes(dependencies: RunRouteDependencies = {}): Hono {
	const runs = new Hono();
	const location = dependencies.location ?? systemContext();
	const describe = dependencies.describe ?? describeRun;
	const readJevKey = dependencies.readJevKey ?? jevKey;

	runs.use(
		"*",
		cors({
			origin: [...LOCAL_ACTION_ORIGINS],
			allowMethods: ["GET", "POST", "OPTIONS"],
			allowHeaders: ["Content-Type", RUN_REQUEST_HEADER],
			maxAge: PREFLIGHT_MAX_AGE_SECONDS,
		}),
	);

	/**
	 * What a run would do before it runs.
	 *
	 * Safe from a screen: it spawns `plan.py`, which writes nothing, starts nothing and makes
	 * no request. That is what separates it from `/api/onboarding/runtime/probe`, which costs a
	 * completion and is therefore never called on a render.
	 *
	 * It answers with a single-use token. That token is the only input `POST /api/runs` takes,
	 * which is how "state what a run would do before it runs" becomes a property of the server
	 * rather than a convention of one screen.
	 */
	runs.post("/plan", async (context) => {
		requireRunHeader(context.req.header(RUN_REQUEST_HEADER), context.req.header("origin"));
		const body = readJsonBody(await context.req.text());
		const kind = kindFrom(body);
		const response: PlanResponse = {
			plan: await issuePlan(kind, kind === "jev-it" ? await contextWithJevKey(location, readJevKey) : location, describe),
		};
		return context.json(response);
	});

	/**
	 * Start.
	 *
	 * The token and nothing else: the Profile and stages are fixed by the plan the person was
	 * looking at, so a run nobody was shown a plan for is not expressible.
	 */
	runs.post("/", async (context) => {
		requireRunHeader(context.req.header(RUN_REQUEST_HEADER), context.req.header("origin"));
		const body = readJsonBody(await context.req.text());
		const token = asText(at(body, "token"));
		if (token === null || token === "") {
			throw new RunError(
				"bad_request",
				"A run is started against a plan, and none was named.",
				"Ask what the run would do first; the answer is what authorises it.",
				"token",
			);
		}
		const plan = await redeemPlan(token, location, describe);
		const executionContext = plan.kind === "jev-it" ? await contextWithJevKey(location, readJevKey) : location;
		const response: RunResponse = {
			run:
				dependencies.spawnChild === undefined
					? startRun(plan, executionContext)
					: startRun(plan, executionContext, dependencies.spawnChild),
		};
		return context.json(response, 202);
	});

	/** The live run, from this process's own memory. No store is read, nothing is spawned. */
	runs.get("/current", (context) => {
		requireRunHeader(context.req.header(RUN_REQUEST_HEADER), context.req.header("origin"));
		const response: RunResponse = { run: currentRun() };
		return context.json(response);
	});

	/**
	 * Stop the live run.
	 *
	 * It exists so a person can stop a fetch without quitting the app.
	 * It signals the whole process tree, so a stage the loop spawned goes with it.
	 */
	runs.post("/current/stop", (context) => {
		requireRunHeader(context.req.header(RUN_REQUEST_HEADER), context.req.header("origin"));
		const response: RunResponse = { run: stopRun() };
		return context.json(response);
	});

	/**
	 * The last few runs this process has held.
	 *
	 * Convenience, and never described as history: a restarted server remembers nothing, and
	 * the durable record of a run is `data/runs.jsonl` and the `runs` table.
	 */
	runs.get("/recent", (context) => {
		requireRunHeader(context.req.header(RUN_REQUEST_HEADER), context.req.header("origin"));
		const response: RecentRunsResponse = { runs: recentRuns(parseRecentLimit(context.req.query("limit"))) };
		return context.json(response);
	});

	runs.onError((error, context) => {
		if (error instanceof RunError) {
			return context.json(error.body, error.status);
		}
		// Nothing else is described to the caller: what throws here is process machinery, and
		// its messages carry absolute paths and environment contents.
		process.stderr.write(`runs: unhandled failure: ${error.message}\n`);
		const wrapped = new RunError("run_failed_to_start", "Something went wrong handling that request.");
		return context.json(wrapped.body, wrapped.status);
	});

	return runs;
}
