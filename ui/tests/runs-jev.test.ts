/** The Jev run contract: plan-bound work and cap, then two fixed commands. */

import assert from "node:assert/strict";
import { spawn, type SpawnOptions } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import type { PlanDocument } from "../server/runs/plan.ts";
import { createRunRoutes } from "../server/runs/routes.ts";
import { nonJevInheritedEnvironment } from "../server/runs/environment.ts";
import { currentRun, recentRuns, resetRunsForTest, type SpawnChild } from "../server/runs/runner.ts";
import { resetPlansForTest, type DescribeRun } from "../server/runs/tokens.ts";
import { RUN_REQUEST_HEADER } from "../shared/runs.ts";

const DESKTOP_ORIGIN = "tauri://localhost";
const SENTINEL = "typesafe-key-that-must-not-enter-a-child";
const made: string[] = [];

type Started = { readonly argv: readonly string[]; readonly options: SpawnOptions };
type RequestBody = Readonly<Record<string, string>>;
type Scripted = { readonly spawnChild: SpawnChild; readonly started: readonly Started[] };

function machine(apiKey: string | null = SENTINEL): LocationContext {
	const home = mkdtempSync(join(tmpdir(), "venator-jev-run-"));
	made.push(home);
	const profile = join(home, "venator", "profiles", "sample");
	mkdirSync(profile, { recursive: true });
	writeFileSync(join(profile, "targeting.yaml"), "profile:\n  id: sample\n");
	return {
		platform: "linux",
		home,
		workingDirectory: process.cwd(),
		checkoutRoot: null,
		environment: (name) => {
			if (name === "XDG_DATA_HOME") return home;
			if (name === "PATH") return process.env.PATH;
			if (name === "TYPESAFE_API_KEY") return apiKey ?? undefined;
			return undefined;
		},
	};
}

function answer(context: LocationContext, count: number, selectionHash = "a".repeat(64)): Extract<PlanDocument, { readonly outcome: "plan" }> {
	const root = join(context.home, "venator");
	return {
		outcome: "plan",
		plan: {
			profileName: "sample",
			profileDirectory: join(root, "profiles", "sample"),
			storeRoot: root,
			viewPath: join(root, "build", "venator.db"),
			boards: 35,
			jev: {
				asOf: "2026-09-23",
				mode: "shadow",
				postingCount: count,
				maximumUsd: count === 0 ? 0 : 0.01,
				selectionHash,
			},
		},
	};
}

function post(path: string, body: RequestBody): Request {
	return new Request(`http://127.0.0.1:5170${path}`, {
		method: "POST",
		headers: {
			origin: DESKTOP_ORIGIN,
			[RUN_REQUEST_HEADER]: "1",
			"content-type": "application/json",
		},
		body: JSON.stringify(body),
	});
}

function scripted(onJevStart: () => void, jevScript = ""): Scripted {
	const started: Started[] = [];
	const spawnChild: SpawnChild = (_command, argv, options) => {
		started.push({ argv, options });
		const isJev = argv[1] === "venator.schedule.loop" && argv[3] === "jev";
		if (isJev) onJevStart();
		return spawn(process.execPath, ["-e", isJev ? jevScript : ""], options);
	};
	return { spawnChild, started };
}

async function until(check: () => boolean, why: string): Promise<void> {
	const deadline = Date.now() + 5_000;
	while (!check()) {
		if (Date.now() > deadline) assert.fail(`timed out waiting: ${why}`);
		await new Promise((done) => setTimeout(done, 20));
	}
}

afterEach(() => {
	resetPlansForTest();
	resetRunsForTest();
	for (const path of made.splice(0)) rmSync(path, { recursive: true, force: true });
});

test("inherited onboarding children cannot receive the dashboard's Jev key", () => {
	const previous = process.env.TYPESAFE_API_KEY;
	process.env.TYPESAFE_API_KEY = SENTINEL;
	try {
		assert.equal(nonJevInheritedEnvironment()["TYPESAFE_API_KEY"], undefined);
		assert.equal(nonJevInheritedEnvironment()["PATH"], process.env.PATH);
	} finally {
		if (previous === undefined) delete process.env.TYPESAFE_API_KEY;
		else process.env.TYPESAFE_API_KEY = previous;
	}
});

test("plan, token and run pass the key only to Jev, then rebuild the view", async () => {
	const location = machine();
	let remaining = 2;
	const describe: DescribeRun = (kind, _profile, context) => {
		assert.equal(kind, "jev-it");
		return Promise.resolve(answer(context, remaining));
	};
	const { spawnChild, started } = scripted(() => {
		remaining = 0;
	});
	const routes = createRunRoutes({ location, describe, spawnChild });

	const planned = await routes.request(post("/plan", { kind: "jev-it" }));
	const planBody = await planned.json();
	assert.equal(planned.status, 200, JSON.stringify(planBody));
	assert.equal(planBody.plan.kind, "jev-it");
	assert.equal(planBody.plan.jev.postingCount, 2);
	assert.equal(planBody.plan.jev.maximumUsd, 0.01);
	assert.equal(
		planBody.plan.jev.summary,
		"2 Postings passed the Hard Filters, have description text, and have no Jev result under the current release.",
	);
	assert.ok(!JSON.stringify(planBody).includes(SENTINEL));
	assert.ok(!planBody.plan.notes.some((note: { code: string }) => note.code === "jev-key-missing"));

	const startedResponse = await routes.request(post("/", { token: planBody.plan.token }));
	const startedBody = await startedResponse.json();
	assert.equal(startedResponse.status, 202);
	assert.ok(!JSON.stringify(startedBody).includes(SENTINEL));
	await until(() => currentRun() === null, "Jev and the view rebuild to finish");

	assert.deepEqual(started.map((entry) => entry.argv), [
		[
			"-m",
			"venator.schedule.loop",
			"--only",
			"jev",
			"--as-of",
			"2026-09-23",
			"--profile",
			"sample",
		],
		["-m", "venator.view.build", "--profile", "sample"],
	]);
	const jevEnvironment = started[0]?.options.env ?? {};
	const viewEnvironment = started[1]?.options.env ?? {};
	assert.equal(jevEnvironment["TYPESAFE_API_KEY"], SENTINEL);
	assert.equal(jevEnvironment["VENATOR_JEV_MAX_USD"], "0.01");
	assert.equal(viewEnvironment["TYPESAFE_API_KEY"], undefined);
	assert.ok(!Object.values(viewEnvironment).includes(SENTINEL));

	const reused = await routes.request(post("/", { token: planBody.plan.token }));
	assert.equal(reused.status, 409);
	assert.equal((await reused.json()).error.code, "plan_expired");

	const second = await routes.request(post("/plan", { kind: "jev-it" }));
	assert.equal(second.status, 200);
	const secondBody = await second.json();
	assert.equal(secondBody.plan.jev.postingCount, 0);
	assert.equal(secondBody.plan.jev.maximumUsd, 0);
	assert.equal(secondBody.plan.startable, false);
	assert.equal(secondBody.plan.notes[0]?.code, "nothing-to-qualify");
});

test("Refresh neither looks up the saved key nor passes a key to the sequencer", async () => {
	const location = machine();
	const describe: DescribeRun = (_kind, _profile, context) => Promise.resolve({
		...answer(context, 2),
		plan: { ...answer(context, 2).plan, jev: null },
	});
	const { spawnChild, started } = scripted(() => assert.fail("Refresh must not start Jev"));
	const routes = createRunRoutes({
		location, describe, spawnChild,
		readJevKey: async () => assert.fail("Refresh must not read the Keychain"),
	});
	const planned = await routes.request(post("/plan", { kind: "fetch-and-filter" }));
	assert.equal(planned.status, 200);
	const body = await planned.json();
	const response = await routes.request(post("/", { token: body.plan.token }));
	assert.equal(response.status, 202);
	await until(() => currentRun() === null, "Refresh to finish");
	assert.deepEqual(started[0]?.argv.slice(0, 4), ["-m", "venator.schedule.loop", "--only", "discover,filters,view"]);
	assert.equal(started[0]?.options.env?.["TYPESAFE_API_KEY"], undefined);
});

test("a saved Keychain key is passed only to the Jev it sequencer", async () => {
	const location = machine(null);
	const describe: DescribeRun = (_kind, _profile, context) => Promise.resolve(answer(context, 2));
	const { spawnChild, started } = scripted(() => undefined);
	const routes = createRunRoutes({ location, describe, spawnChild, readJevKey: async () => SENTINEL });
	const planned = await routes.request(post("/plan", { kind: "jev-it" }));
	const body = await planned.json();
	assert.equal(planned.status, 200);
	assert.ok(!body.plan.jev.summary.includes("API key is available"));
	const response = await routes.request(post("/", { token: body.plan.token }));
	assert.equal(response.status, 202);
	await until(() => currentRun() === null, "Jev it to finish");
	assert.equal(started[0]?.options.env?.["TYPESAFE_API_KEY"], SENTINEL);
	assert.equal(started[1]?.options.env?.["TYPESAFE_API_KEY"], undefined);
});

test("a missing server key pauses Jev without exposing a credential", async () => {
	const location = machine(null);
	const describe: DescribeRun = (_kind, _profile, context) => Promise.resolve(answer(context, 2));
	const { spawnChild, started } = scripted(() => undefined);
	const routes = createRunRoutes({ location, describe, spawnChild });

	const planned = await routes.request(post("/plan", { kind: "jev-it" }));
	const planBody = await planned.json();
	assert.equal(planned.status, 200);
	assert.equal(planBody.plan.startable, true);
	assert.match(planBody.plan.jev.summary, /pause until an API key/u);
	// The note is what the Awaiting Jev list reads to say "Jev needs a key". It is prose only.
	const missing = planBody.plan.notes.find((note: { code: string }) => note.code === "jev-key-missing");
	assert.equal(missing?.message, "No TypeSafe key is saved, so these Postings wait for Jev until you add one.");

	const response = await routes.request(post("/", { token: planBody.plan.token }));
	assert.equal(response.status, 202);
	await until(() => currentRun() === null, "Jev and the view rebuild to finish");
	assert.equal(started[0]?.options.env?.["TYPESAFE_API_KEY"], undefined);
});

test("Jev child output is withheld even when it writes only fragments of a key", async () => {
	const location = machine();
	const describe: DescribeRun = (_kind, _profile, context) => Promise.resolve(answer(context, 1));
	const { spawnChild, started } = scripted(
		() => undefined,
		'const key = process.env.TYPESAFE_API_KEY; process.stderr.write(key.slice(0, 10)); process.exit(1);',
	);
	const routes = createRunRoutes({ location, describe, spawnChild });
	const planned = await routes.request(post("/plan", { kind: "jev-it" }));
	const planBody = await planned.json();

	const response = await routes.request(post("/", { token: planBody.plan.token }));
	assert.equal(response.status, 202);
	await until(() => currentRun() === null, "the failed Jev run and recovery view to finish");

	const finished = recentRuns(1)[0];
	assert.equal(started[0]?.options.env?.["TYPESAFE_API_KEY"], SENTINEL);
	assert.equal(started[1]?.options.env?.["TYPESAFE_API_KEY"], undefined);
	assert.ok(!JSON.stringify(finished).includes(SENTINEL));
	assert.equal(finished?.failure?.transcript, null);
});

test("a Jev token is stale when its exact selection changes, even at the same count and cap", async () => {
	const location = machine();
	let selectionHash = "a".repeat(64);
	const describe: DescribeRun = (_kind, _profile, context) => Promise.resolve(answer(context, 2, selectionHash));
	const { spawnChild, started } = scripted(() => undefined);
	const routes = createRunRoutes({ location, describe, spawnChild });

	const planned = await routes.request(post("/plan", { kind: "jev-it" }));
	const plannedBody = await planned.json();
	assert.equal(planned.status, 200, JSON.stringify(plannedBody));
	const token = plannedBody.plan.token;
	selectionHash = "b".repeat(64);
	const response = await routes.request(post("/", { token }));

	assert.equal(response.status, 409);
	assert.equal((await response.json()).error.code, "plan_expired");
	assert.deepEqual(started, []);
});
