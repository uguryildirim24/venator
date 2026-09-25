/** The dashboard exposes one fixed, safe pipeline plan and refuses every other shape. */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import { createRunRoutes } from "../server/runs/routes.ts";
import { resetRunsForTest } from "../server/runs/runner.ts";
import { issuePlan, resetPlansForTest, type DescribeRun } from "../server/runs/tokens.ts";
import {
	runKindOf,
	RUN_KINDS,
	RUN_KIND_STAGES,
	RUN_REQUEST_HEADER,
	PLAN_KINDS,
} from "../shared/runs.ts";
import type { PlanDocument } from "../server/runs/plan.ts";

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

const made: string[] = [];

function machine(): LocationContext {
	const home = mkdtempSync(join(tmpdir(), "venator-plan-"));
	made.push(home);
	const directory = join(home, "venator", "profiles", "sample");
	mkdirSync(directory, { recursive: true });
	writeFileSync(join(directory, "targeting.yaml"), "profile:\n  id: sample\n");
	return {
		platform: "linux",
		home,
		workingDirectory: join(home, "elsewhere"),
		checkoutRoot: null,
		environment: (name) => (name === "XDG_DATA_HOME" ? home : undefined),
	};
}

afterEach(() => {
	resetPlansForTest();
	resetRunsForTest();
	for (const home of made.splice(0)) rmSync(home, { recursive: true, force: true });
});

function post(path: string, body: string): Request {
	return new Request(`http://127.0.0.1:5170${path}`, {
		method: "POST",
		headers: { origin: DESKTOP_ORIGIN, [RUN_REQUEST_HEADER]: "1", "content-type": "application/json" },
		body,
	});
}

test("an ordinary plan names the closed pipeline stages", async () => {
	const plan = await issuePlan("fetch-and-filter", machine(), answers);

	assert.deepEqual(plan.stages, ["discover", "filters", "view"]);
	assert.equal(plan.startable, true);
});

test("no run kind the dashboard can start commits", () => {
	const startable: readonly string[] = RUN_KINDS.flatMap((kind) => [...RUN_KIND_STAGES[kind]]);
	assert.equal(startable.includes("commit"), false);
	assert.deepEqual(PLAN_KINDS.filter((kind) => runKindOf(kind) !== null), [...RUN_KINDS]);
	assert.equal(runKindOf("fetch-and-filter"), "fetch-and-filter");
});

test("every unknown or malformed kind is refused", async () => {
	const routes = createRunRoutes();
	const hostile = [
		'{"kind":"score"}',
		'{"kind":"judge"}',
		'{"kind":"commit"}',
		'{"kind":"Judge"}',
		'{"kind":"SCORE"}',
		'{"kind":" score "}',
		'{"kind":"score\\n"}',
		'{"kind":"score "}',
		'{"kind":"fetch-and-filter,judge"}',
		'{"kind":"discover,filters,judge"}',
		'{"kind":"__proto__"}',
		'{"kind":"constructor"}',
		'{"kind":"toString"}',
		'{"kind":["score"]}',
		'{"kind":{"kind":"score"}}',
		'{"kind":null}',
		'{"kind":1}',
		'{"stages":["judge"]}',
		'{"only":"judge"}',
		'{"kind":"fetch-and-filter","kind":"judge"}',
	];

	for (const body of hostile) {
		const response = await routes.request(post("/plan", body));
		assert.equal(response.status, 400, body);
		const answer = await response.json();
		assert.equal(answer.error.code, "bad_request", body);
		assert.equal(answer.error.field, "kind", body);
		assert.match(answer.error.remedy, /describe/u, body);
	}
});

test("a plan is not a run: asking for one leaves nothing going and nothing remembered", async () => {
	const context = machine();
	let asked = 0;
	const counting: DescribeRun = (kind, profile, where) => {
		asked += 1;
		return answers(kind, profile, where);
	};

	await issuePlan("fetch-and-filter", context, counting);

	assert.equal(asked, 1);
	const routes = createRunRoutes();
	const current = await routes.request(
		new Request("http://127.0.0.1:5170/current", {
			headers: { origin: DESKTOP_ORIGIN, [RUN_REQUEST_HEADER]: "1" },
		}),
	);
	assert.deepEqual(await current.json(), { run: null });
	const recent = await routes.request(
		new Request("http://127.0.0.1:5170/recent", {
			headers: { origin: DESKTOP_ORIGIN, [RUN_REQUEST_HEADER]: "1" },
		}),
	);
	assert.deepEqual(await recent.json(), { runs: [] });
});
