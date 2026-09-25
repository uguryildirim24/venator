/**
 * What the run surface refuses, and how loudly.
 *
 * Every refusal here is checked twice in the code — once when a plan is issued, so a control
 * is off with the reason beside it, and again when a run is started, so the answer does not
 * depend on how fresh a screen is. Both halves are asserted.
 *
 * The pipeline itself is stood in for (`DescribeRun`), because what these cases are about is
 * the surface's own vocabulary: which refusal, which status, which words. What the pipeline
 * would actually answer belongs to `src/venator/` and to `tests/runs-plan.test.ts`.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import type { RunPlan } from "../shared/runs.ts";
import { RunError } from "../server/runs/errors.ts";
import type { PlanDocument } from "../server/runs/plan.ts";
import { issuePlan, redeemPlan, resetPlansForTest, type DescribeRun } from "../server/runs/tokens.ts";
import { createRunRoutes } from "../server/runs/routes.ts";
import { resetRunsForTest } from "../server/runs/runner.ts";
import { RUN_REQUEST_HEADER } from "../shared/runs.ts";

const DESKTOP_ORIGIN = "tauri://localhost";

const ANSWER = {
	outcome: "plan",
	plan: {
		profileName: "sample",
		profileDirectory: "/Install/profiles/sample",
		storeRoot: "/Install",
		viewPath: "/Install/build/venator.db",
		boards: 35,
	},
} satisfies PlanDocument;

const answers: DescribeRun = () => Promise.resolve(ANSWER);

/** A machine with the Profiles named, written where the dashboard actually looks for them. */
function install(profiles: readonly { readonly name: string; readonly scaffold?: boolean }[]): LocationContext {
	const home = mkdtempSync(join(tmpdir(), "venator-guard-"));
	for (const profile of profiles) {
		const directory = join(home, "venator", "profiles", profile.name);
		mkdirSync(directory, { recursive: true });
		writeFileSync(
			join(directory, "targeting.yaml"),
			`profile:\n  id: ${profile.name}\n${profile.scaffold === true ? "  scaffold: true\n" : ""}`,
		);
	}
	return {
		platform: "linux",
		home,
		// Nowhere the dashboard searches, so only the Profiles written above are found.
		workingDirectory: join(home, "elsewhere"),
		checkoutRoot: null,
		environment: (name) => (name === "XDG_DATA_HOME" ? home : undefined),
	};
}

const made: string[] = [];

function machine(profiles: readonly { readonly name: string; readonly scaffold?: boolean }[]): LocationContext {
	const context = install(profiles);
	made.push(context.home);
	return context;
}

afterEach(() => {
	resetPlansForTest();
	resetRunsForTest();
	for (const home of made.splice(0)) rmSync(home, { recursive: true, force: true });
});

async function refusal(work: () => Promise<RunPlan>): Promise<RunError> {
	try {
		await work();
	} catch (error) {
		assert.ok(error instanceof RunError, `expected a RunError, got ${String(error)}`);
		return error;
	}
	return assert.fail("expected a refusal");
}

test("an Install with no Profile is told there is nothing to run against, and no run is invented", async () => {
	const error = await refusal(() => issuePlan("fetch-and-filter", machine([]), answers));

	assert.equal(error.code, "no_profile");
	assert.equal(error.status, 409);
	assert.match(error.remedy ?? "", /Set one up/u);
});

test("a scaffold is not a Profile, so an Install holding only one has nothing to run either", async () => {
	// `profiles/example/` ships with every checkout. Counting it would aim a run at employers
	// nobody picked.
	const error = await refusal(() => issuePlan("fetch-and-filter", machine([{ name: "example", scaffold: true }]), answers));

	assert.equal(error.code, "no_profile");
});

test("two Profiles and nothing saying which is refused by name rather than guessed at", async () => {
	const context = machine([{ name: "sample" }, { name: "marketing" }]);
	const error = await refusal(() => issuePlan("fetch-and-filter", context, answers));

	assert.equal(error.code, "profile_ambiguous");
	assert.equal(error.status, 409);
	assert.match(error.message, /sample/u);
	assert.match(error.message, /marketing/u);
});

test("a plan names the Profile it is for, where it would write, and what it would run", async () => {
	const plan = await issuePlan("fetch-and-filter", machine([{ name: "sample" }]), answers);

	assert.equal(plan.profile, "sample");
	assert.deepEqual(plan.stages, ["discover", "filters", "view"]);
	assert.equal(plan.storeRoot, "/Install");
	assert.equal(plan.startable, true);
	assert.ok(plan.token.startsWith("plan_"));
});

test("a Profile with no board registered is a plan that says so, and is not startable", async () => {
	const noBoards: DescribeRun = () => Promise.resolve({ outcome: "plan", plan: { ...ANSWER.plan, boards: 0 } });
	const plan = await issuePlan("fetch-and-filter", machine([{ name: "sample" }]), noBoards);

	// Not an error: nothing has gone wrong. A Profile written by setup starts with an empty
	// registry, and Discover only answers for an employer somebody registered.
	assert.equal(plan.startable, false);
	assert.equal(plan.notes[0]?.code, "no-boards");
	assert.match(plan.notes[0]?.message ?? "", /registers no employer/u);
});

test("a plan that says there is nothing to fetch cannot be started either", async () => {
	const context = machine([{ name: "sample" }]);
	const noBoards: DescribeRun = () => Promise.resolve({ outcome: "plan", plan: { ...ANSWER.plan, boards: 0 } });
	const plan = await issuePlan("fetch-and-filter", context, noBoards);

	const error = await refusal(() => redeemPlan(plan.token, context, noBoards));
	assert.equal(error.code, "not_startable");
});

test("a run that would rebuild a view this dashboard is not reading says so before it runs", async () => {
	const elsewhere: DescribeRun = () =>
		Promise.resolve({ outcome: "plan", plan: { ...ANSWER.plan, viewPath: "/somewhere/else/build/venator.db" } });
	const plan = await issuePlan("fetch-and-filter", machine([{ name: "sample" }]), elsewhere);

	const note = plan.notes.find((entry) => entry.code === "view-elsewhere");
	assert.ok(note !== undefined, "the checkout-versus-application-data asymmetry has to be said before the run");
	// Prose only: a filesystem path is machine vocabulary and belongs beside the note, not in
	// it. The paths are on the plan.
	assert.ok(!note.message.includes("/somewhere/else"));
	assert.equal(plan.viewPath, "/somewhere/else/build/venator.db");
	assert.notEqual(plan.dashboardViewPath, plan.viewPath);
});

test("a token starts one run and is then gone", async () => {
	const context = machine([{ name: "sample" }]);
	const plan = await issuePlan("fetch-and-filter", context, answers);

	assert.equal((await redeemPlan(plan.token, context, answers)).profile, "sample");
	const error = await refusal(() => redeemPlan(plan.token, context, answers));
	assert.equal(error.code, "plan_expired");
	assert.equal(error.status, 409);
});

test("a token nobody issued is refused in the same words as one that has aged out", async () => {
	const error = await refusal(() => redeemPlan("plan_made-up", machine([{ name: "sample" }]), answers));

	assert.equal(error.code, "plan_expired");
});

test("everything the plan stated is checked again at the start, not trusted", async () => {
	const context = machine([{ name: "sample" }]);
	const plan = await issuePlan("fetch-and-filter", context, answers);
	// Between the plan and the press, the store moved. What the person was shown is no longer
	// what would happen, so it is refused rather than reinterpreted.
	const moved: DescribeRun = () =>
		Promise.resolve({ outcome: "plan", plan: { ...ANSWER.plan, storeRoot: "/somewhere/else" } });

	const error = await refusal(() => redeemPlan(plan.token, context, moved));
	assert.equal(error.code, "plan_expired");
});

test("a Profile that has been renamed since the plan was issued is refused too", async () => {
	const context = machine([{ name: "sample" }]);
	const plan = await issuePlan("fetch-and-filter", context, answers);
	const renamed: DescribeRun = () =>
		Promise.resolve({ outcome: "plan", plan: { ...ANSWER.plan, profileDirectory: "/Install/profiles/sample-2" } });

	assert.equal((await refusal(() => redeemPlan(plan.token, context, renamed))).code, "plan_expired");
});

test("an Install with no pipeline in it is a 503 that names the terminal way to do it", async () => {
	const absent: DescribeRun = () =>
		Promise.resolve({ outcome: "absent", message: "Running the pipeline is not part of this Install." });
	const error = await refusal(() => issuePlan("fetch-and-filter", machine([{ name: "sample" }]), absent));

	assert.equal(error.code, "pipeline_absent");
	assert.equal(error.status, 503);
	assert.match(error.remedy ?? "", /venator\.schedule\.loop --only discover,filters,view/u);
});

test("a store this Profile may not read is refused in the pipeline's own words", async () => {
	const sentence =
		"data/decisions/ is claimed by Profile 'marketing'; this run is for 'sample' — give the second Profile its own --decisions-dir";
	const claimed: DescribeRun = () => Promise.resolve({ outcome: "failure", kind: "store", message: sentence });
	const error = await refusal(() => issuePlan("fetch-and-filter", machine([{ name: "sample" }]), claimed));

	assert.equal(error.code, "store_unreadable");
	// Passed through rather than rewritten: `venator.paths` and `venator.profile.claim`
	// already name the fix, and this dashboard is in no position to say it better.
	assert.equal(error.message, sentence);
});

test("a pipeline answering in a shape this dashboard does not know is named, never guessed at", async () => {
	const drifted: DescribeRun = () => Promise.resolve({ outcome: "mismatch" });
	const error = await refusal(() => issuePlan("fetch-and-filter", machine([{ name: "sample" }]), drifted));

	assert.equal(error.code, "pipeline_absent");
	assert.match(error.message, /shape this dashboard does not know/u);
});

// --- the surface itself -------------------------------------------------------------------

/** A request the surface will accept on its face: allowed origin, the header, JSON. */
function get(path: string): Request {
	return new Request(`http://127.0.0.1:5170${path}`, {
		headers: { origin: DESKTOP_ORIGIN, [RUN_REQUEST_HEADER]: "1" },
	});
}

function post(path: string, body: string): Request {
	return new Request(`http://127.0.0.1:5170${path}`, {
		method: "POST",
		headers: { origin: DESKTOP_ORIGIN, [RUN_REQUEST_HEADER]: "1", "content-type": "application/json" },
		body,
	});
}

test("a request without the header is refused, because that header is what forces the preflight", async () => {
	const response = await createRunRoutes().request(
		new Request("http://127.0.0.1:5170/current", { headers: { origin: DESKTOP_ORIGIN } }),
	);

	assert.equal(response.status, 400);
	const body = await response.json();
	assert.equal(body.error.code, "bad_request");
});

test("a request from a page that is not part of this Install is refused by origin", async () => {
	const response = await createRunRoutes().request(
		new Request("http://127.0.0.1:5170/current", {
			headers: { origin: "https://boards.example.com", [RUN_REQUEST_HEADER]: "1" },
		}),
	);

	assert.equal(response.status, 403);
	assert.equal((await response.json()).error.code, "forbidden_origin");
});

test("a run this dashboard cannot start is refused by name against the set it can", async () => {
	const response = await createRunRoutes().request(
		post("/plan", JSON.stringify({ kind: "submit-everything" })),
	);

	assert.equal(response.status, 400);
	const body = await response.json();
	assert.equal(body.error.code, "bad_request");
	assert.equal(body.error.field, "kind");
	assert.match(body.error.remedy, /fetch-and-filter/u);
});

test("a start with no plan behind it is refused before anything is spawned", async () => {
	const response = await createRunRoutes().request(post("/", JSON.stringify({})));

	assert.equal(response.status, 400);
	const body = await response.json();
	assert.equal(body.error.field, "token");
	// A run nobody was shown a plan for is not expressible, and this is where that is enforced.
	assert.match(body.error.message, /A run is started against a plan/u);
});

test("a start against a token this process never issued is refused as out of date", async () => {
	const response = await createRunRoutes().request(
		post("/", JSON.stringify({ token: "plan_not-mine" })),
	);

	assert.equal(response.status, 409);
	assert.equal((await response.json()).error.code, "plan_expired");
});

test("with nothing going, the live run is null and stopping is refused rather than pretended", async () => {
	const routes = createRunRoutes();

	const current = await routes.request(get("/current"));
	assert.equal(current.status, 200);
	assert.deepEqual(await current.json(), { run: null });

	const stopped = await routes.request(
		new Request("http://127.0.0.1:5170/current/stop", {
			method: "POST",
			headers: { origin: DESKTOP_ORIGIN, [RUN_REQUEST_HEADER]: "1" },
		}),
	);
	assert.equal(stopped.status, 404);
	assert.equal((await stopped.json()).error.code, "no_run");
});

test("recent runs are this process's memory and start empty", async () => {
	const response = await createRunRoutes().request(get("/recent"));

	assert.deepEqual(await response.json(), { runs: [] });
});

test("a body that is not JSON is refused without quoting the parser back", async () => {
	const response = await createRunRoutes().request(post("/plan", "{not json"));

	assert.equal(response.status, 400);
	assert.equal((await response.json()).error.message, "The request body is not valid JSON.");
});
