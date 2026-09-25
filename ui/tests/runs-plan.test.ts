/**
 * The plan seam: what the pipeline says a run would do, read through a stand-in for it.
 *
 * The answering belongs to `src/venator/` — which Profile resolves, where the stores are,
 * whether this Profile may read them — so what is testable here is the seam: that a document
 * this dashboard does not recognise is reported as a mismatch rather than guessed at, that the
 * pipeline's own refusal wording survives unchanged, and that an Install with no pipeline in
 * it says so rather than looking broken.
 *
 * The branch logic is a pure function of four values — `readPlanResult` — so it is asserted
 * directly rather than through a stand-in program. That is a deliberate departure from the
 * shape `tests/boards.test.ts` uses: its stand-in works by `NODE_OPTIONS=--require`, which
 * needs the child to inherit this process's environment, and a pipeline child here is handed
 * an environment built from a list (`server/runs/environment.ts`) precisely so it inherits
 * nothing. Rather than punch a hole in that for a test, the spawn is separated from the
 * decision and the decision is tested whole, on every host, with no program to start.
 *
 * One case does drive a real child: an interpreter that was named and cannot be started.
 * That one needs no stand-in, because a path that is not a program is the fixture.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import { describeRun, readPlanDocument, readPlanResult, type PlanProcess } from "../server/runs/plan.ts";

/** A machine that named no interpreter: the Install with no pipeline in it. */
const NAMED_NOTHING: LocationContext = {
	platform: "linux",
	home: "/home/someone",
	workingDirectory: "/home/someone",
	checkoutRoot: null,
	environment: () => undefined,
};

function finished(process: Partial<PlanProcess>): PlanProcess {
	return { code: 0, stdout: "", stderr: "", timedOut: false, launchFailed: false, ...process };
}

const ANSWER = {
	profile: { name: "sample", directory: "/Install/profiles/sample", identifier: "sample" },
	storeRoot: "/Install",
	viewPath: "/Install/build/venator.db",
	boards: 35,
};

test("a plan carries the pipeline's own answer about where a run would write", () => {
	const document = readPlanDocument(JSON.stringify(ANSWER));

	assert.equal(document.outcome, "plan");
	assert.deepEqual(document.outcome === "plan" ? document.plan : null, {
		profileName: "sample",
		profileDirectory: "/Install/profiles/sample",
		storeRoot: "/Install",
		viewPath: "/Install/build/venator.db",
		boards: 35,
	});
});

test("a Jev plan carries the exact count, cent cap, date and activation mode", () => {
	const jev = {
		asOf: "2026-09-23",
		mode: "shadow",
		postingCount: 57,
		maximumUsd: 0.02,
		selectionHash: "a".repeat(64),
	};
	const document = readPlanDocument(JSON.stringify({ ...ANSWER, jev }));

	assert.equal(document.outcome, "plan");
	assert.deepEqual(document.outcome === "plan" ? document.plan.jev : null, jev);
});

test("a malformed Jev count or cap is a mismatch rather than a guessed plan", () => {
	for (const jev of [
		{ asOf: "today", mode: "shadow", postingCount: 1, maximumUsd: 0.01, selectionHash: "a".repeat(64) },
		{ asOf: "2026-09-23", mode: "active", postingCount: 1, maximumUsd: 0.01, selectionHash: "a".repeat(64) },
		{ asOf: "2026-09-23", mode: "shadow", postingCount: -1, maximumUsd: 0.01, selectionHash: "a".repeat(64) },
		{ asOf: "2026-09-23", mode: "shadow", postingCount: 1, maximumUsd: -0.01, selectionHash: "a".repeat(64) },
		{ asOf: "2026-09-23", mode: "shadow", postingCount: 1, maximumUsd: 0.01, selectionHash: "bad" },
	]) {
		assert.equal(readPlanDocument(JSON.stringify({ ...ANSWER, jev })).outcome, "mismatch");
	}
});

test("a Profile with no board registered is a plan with zero boards, not a failure", () => {
	const document = readPlanDocument(JSON.stringify({ ...ANSWER, boards: 0 }));

	assert.equal(document.outcome, "plan");
	assert.equal(document.outcome === "plan" ? document.plan.boards : -1, 0);
});

test("a field that did not arrive is a mismatch rather than a zero or an empty path", () => {
	for (const missing of ["profile", "storeRoot", "viewPath", "boards"]) {
		const kept = Object.entries(ANSWER).filter(([key]) => key !== missing);
		assert.equal(readPlanDocument(JSON.stringify(Object.fromEntries(kept))).outcome, "mismatch", missing);
	}
});

test("a field of the wrong type is a mismatch too — a rendered fact must not be a coercion", () => {
	assert.equal(readPlanDocument(JSON.stringify({ ...ANSWER, boards: "35" })).outcome, "mismatch");
	assert.equal(readPlanDocument(JSON.stringify({ ...ANSWER, boards: 1.5 })).outcome, "mismatch");
	assert.equal(readPlanDocument(JSON.stringify({ ...ANSWER, viewPath: 7 })).outcome, "mismatch");
	assert.equal(readPlanDocument(JSON.stringify({ ...ANSWER, profile: { name: "", directory: "/x" } })).outcome, "mismatch");
});

test("a failure kind this dashboard has never heard of is a mismatch, never a guess", () => {
	assert.equal(readPlanDocument(JSON.stringify({ failure: { kind: "weather", message: "hm" } })).outcome, "mismatch");
});

test("the pipeline's own refusal wording survives unchanged", () => {
	const sentence =
		"no Profile named 'nope' in profiles/, /Install/profiles/ — available: example, sample";
	const document = readPlanDocument(JSON.stringify({ failure: { kind: "profile", message: sentence } }));

	assert.equal(document.outcome, "failure");
	assert.equal(document.outcome === "failure" ? document.message : null, sentence);
});

test("a store this Profile may not read comes back as the store failure it is", () => {
	const document = readPlanDocument(
		JSON.stringify({ failure: { kind: "store", message: "data/decisions/ belongs to marketing" } }),
	);

	assert.equal(document.outcome, "failure");
	assert.equal(document.outcome === "failure" ? document.kind : null, "store");
});

test("an unclassified failure carries no message at all rather than a subprocess's own text", () => {
	const document = readPlanDocument(JSON.stringify({ failure: { kind: "failed", message: null } }));

	assert.equal(document.outcome, "failure");
	assert.equal(document.outcome === "failure" ? document.message : "x", null);
});

test("an Install with no pipeline in it says so, in the sentence for an Install that named none", () => {
	const document = readPlanResult(finished({ code: 3, stderr: "plan: the pipeline could not be imported\n" }), NAMED_NOTHING);

	assert.equal(document.outcome, "absent");
	assert.match(
		document.outcome === "absent" ? document.message : "",
		/Running the pipeline is not part of this Install/u,
	);
});

test("an adapter that exited non-zero for any other reason is absent too, and quotes nothing back", () => {
	const document = readPlanResult(
		finished({ code: 1, stderr: "Traceback: /home/someone/.venv/lib/secrets everywhere\n" }),
		NAMED_NOTHING,
	);

	assert.equal(document.outcome, "absent");
	// The subprocess's own text carries paths and environment contents. It is logged, never
	// returned.
	assert.ok(document.outcome === "absent" && !document.message.includes("Traceback"));
	assert.ok(document.outcome === "absent" && !document.message.includes(".venv"));
});

test("an adapter that did not finish in time says that, rather than that it is missing", () => {
	assert.equal(readPlanResult(finished({ timedOut: true, code: null }), NAMED_NOTHING).outcome, "timed-out");
});

test("a launch that never happened is absent, whatever the exit code says", () => {
	assert.equal(readPlanResult(finished({ launchFailed: true, code: null }), NAMED_NOTHING).outcome, "absent");
});

test("an adapter that answered is read as the plan it printed", () => {
	const document = readPlanResult(finished({ stdout: `${JSON.stringify(ANSWER)}\n` }), NAMED_NOTHING);

	assert.equal(document.outcome, "plan");
	assert.equal(document.outcome === "plan" ? document.plan.storeRoot : null, "/Install");
});

test("an adapter that printed nothing readable is a mismatch, not a crash", () => {
	assert.equal(readPlanResult(finished({ stdout: "not json at all\n" }), NAMED_NOTHING).outcome, "mismatch");
	assert.equal(readPlanResult(finished({ stdout: "" }), NAMED_NOTHING).outcome, "mismatch");
});

test("an Install that named an interpreter and could not start it says that instead, with the path", async () => {
	const directory = mkdtempSync(join(tmpdir(), "venator-plan-"));
	const named = join(directory, "not-an-interpreter");
	const context: LocationContext = {
		platform: "linux",
		home: directory,
		workingDirectory: directory,
		checkoutRoot: null,
		environment: (name) => (name === "VENATOR_PYTHON" ? named : undefined),
	};
	try {
		const document = await describeRun("fetch-and-filter", "sample", context);

		assert.equal(document.outcome, "absent");
		// The path is what makes it actionable, and it is this Install's own path rather than
		// anything a Posting or an employer supplied.
		assert.match(document.outcome === "absent" ? document.message : "", /could not be started/u);
		assert.ok(document.outcome === "absent" && document.message.includes(named));
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});
