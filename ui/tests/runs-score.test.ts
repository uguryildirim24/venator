import assert from "node:assert/strict";
import { spawn, type SpawnOptions } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import { createRunRoutes } from "../server/runs/routes.ts";
import { currentRun, recentRuns, resetRunsForTest, type SpawnChild } from "../server/runs/runner.ts";
import { resetPlansForTest, type DescribeRun } from "../server/runs/tokens.ts";
import { RUN_REQUEST_HEADER } from "../shared/runs.ts";

const made: string[] = [];
const SENTINEL = "unlisted-secret-must-not-enter-score";

function setup() {
	const home = mkdtempSync(join(tmpdir(), "venator-score-"));
	made.push(home);
	const root = join(home, "venator");
	const profile = join(root, "profiles", "search");
	mkdirSync(profile, { recursive: true });
	writeFileSync(join(profile, "targeting.yaml"), "profile:\n  id: synthetic\n");
	const location: LocationContext = {
		platform: "linux", home, workingDirectory: process.cwd(), checkoutRoot: null,
		environment: (name) => name === "XDG_DATA_HOME" ? home : name === "UNLISTED_SECRET" ? SENTINEL : name === "PATH" ? process.env.PATH : undefined,
	};
	return { location, root, profile };
}

function post(path: string, body: Record<string, string>): Request {
	return new Request(`http://127.0.0.1:5170${path}`, {
		method: "POST", headers: { origin: "tauri://localhost", [RUN_REQUEST_HEADER]: "1", "content-type": "application/json" },
		body: JSON.stringify(body),
	});
}

async function finished() {
	const deadline = Date.now() + 5000;
	while (currentRun() !== null) {
		if (Date.now() > deadline) assert.fail("Score did not settle");
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
}

afterEach(() => {
	resetPlansForTest(); resetRunsForTest();
	for (const path of made.splice(0)) rmSync(path, { recursive: true, force: true });
});

test("Score runs the fixed inference command then View, without a unlisted secret or model metadata in its plan", async () => {
	const { location, root, profile } = setup();
	const describe: DescribeRun = async () => ({ outcome: "plan", plan: {
		profileName: "search", profileDirectory: profile, storeRoot: root,
		viewPath: join(root, "build", "venator.db"), boards: 1,
		score: { asOf: "2026-09-29", postingCount: 2, maximumUsd: 0.5, selectionHash: "a".repeat(64) },
	} });
	const started: { argv: readonly string[]; options: SpawnOptions }[] = [];
	const spawnChild: SpawnChild = (_command, argv, options) => {
		started.push({ argv, options });
		return spawn(process.execPath, ["-e", ""], options);
	};
	const routes = createRunRoutes({ location, describe, spawnChild });
	const planned = await routes.request(post("/plan", { kind: "score" }));
	const { plan } = await planned.json();
	assert.equal(planned.status, 200);
	assert.equal(plan.score.postingCount, 2);
	assert.equal(plan.score.maximumUsd, 0.5);
	assert.ok(!JSON.stringify(plan).includes(SENTINEL));
	assert.deepEqual(Object.keys(plan.score).sort(), ["asOf", "maximumUsd", "postingCount", "selectionHash"]);
	assert.equal((await routes.request(post("/", { token: plan.token }))).status, 202);
	await finished();
	assert.deepEqual(started.map((row) => row.argv), [
		["-m", "venator.score.run", "--execute", "--as-of", "2026-09-29", "--profile", "search"],
		["-m", "venator.view.build", "--profile", "search"],
	]);
	assert.ok(started.every((row) => row.options.env?.UNLISTED_SECRET === undefined));
	assert.equal(recentRuns(1)[0]?.viewRebuilt, true);
	assert.equal((await routes.request(post("/", { token: plan.token }))).status, 409);
});

test("a Score plan expires when exact inputs change at the same count", async () => {
	const { location, root, profile } = setup();
	let selectionHash = "a".repeat(64);
	const describe: DescribeRun = async () => ({ outcome: "plan", plan: {
		profileName: "search", profileDirectory: profile, storeRoot: root,
		viewPath: join(root, "build", "venator.db"), boards: 1,
		score: { asOf: "2026-09-29", postingCount: 2, maximumUsd: 0.5, selectionHash },
	} });
	const routes = createRunRoutes({ location, describe, spawnChild: () => assert.fail("must not start") });
	const { plan } = await (await routes.request(post("/plan", { kind: "score" }))).json();
	selectionHash = "b".repeat(64);
	assert.equal((await routes.request(post("/", { token: plan.token }))).status, 409);
});
