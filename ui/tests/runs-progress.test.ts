/**
 * Reading a run's progress off a recorded transcript of the loop's output.
 *
 * The loop's three stage lines are a contract — stated with `venator.schedule.loop` and in
 * coordination/CONTRACTS.md — and this reader is held to the discipline `readProbeDocument`
 * follows: a line it does not recognise changes no state. So the cases below are as much
 * about what does *not* move as about what does. A reader that answered plausibly to a line
 * nobody printed would put a stage on screen that never ran.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { RunStage, StageState } from "../shared/runs.ts";
import { initialProgress, readStageLine, settleProgress, splitLines } from "../server/runs/progress.ts";

const AT = "2026-08-28T10:00:00.000Z";
const FETCH: readonly RunStage[] = ["discover", "filters", "view"];

test("employer progress is visible only within an active discovery stage", () => {
	const waiting = initialProgress(FETCH);
	assert.equal(readStageLine(waiting, "discover: employer 1/35 Acme", AT), waiting);
	const running = readStageLine(waiting, "discover: starting", AT);
	const progress = readStageLine(running, "discover: employer 2/35 Example Labs", AT);
	assert.equal(progress.stages[0]?.detail, "Example Labs · 2 of 35 employers");
	assert.equal(progress.stages[0]?.state, "running");
	for (const invalid of ["0/35", "36/35", "1/0", "1/99999999999999999"]) {
		assert.equal(readStageLine(running, `discover: employer ${invalid} Acme`, AT), running);
	}
});

function replay(lines: readonly string[], stages: readonly RunStage[] = FETCH) {
	let state = initialProgress(stages);
	for (const line of lines) state = readStageLine(state, line, AT);
	return state;
}

function states(stages: readonly { readonly stage: RunStage; readonly state: StageState }[]) {
	return stages.map((entry) => [entry.stage, entry.state]);
}

test("a whole successful run lands stage by stage", () => {
	const state = replay([
		"discover: starting",
		"discover: ok",
		"filters: starting",
		"filters: ok",
		"view: starting",
		"view: ok",
	]);

	assert.deepEqual(states(state.stages), [
		["discover", "ok"],
		["filters", "ok"],
		["view", "ok"],
	]);
	assert.equal(state.failure, null);
});

test("a run part way through shows exactly one stage running and the rest waiting", () => {
	const state = replay(["discover: starting", "discover: ok", "filters: starting"]);

	assert.deepEqual(states(state.stages), [
		["discover", "ok"],
		["filters", "running"],
		["view", "waiting"],
	]);
});

test("an ERROR line names the stage it stopped in, in the loop's own words", () => {
	const state = replay([
		"discover: starting",
		"discover: ok",
		"filters: starting",
		"filters: ERROR — Command '['python', '-m', 'venator.match.run']' returned non-zero exit status 1.",
	]);

	assert.equal(state.failure?.stage, "filters");
	assert.match(state.failure?.message ?? "", /returned non-zero exit status 1/u);
	assert.deepEqual(states(state.stages), [
		["discover", "ok"],
		["filters", "failed"],
		["view", "waiting"],
	]);
});

test("everything else the pipeline prints changes no state at all", () => {
	const noise = [
		"Profile sample — profiles/sample",
		"Stores under /Install/ — this Venator checkout, whose data/ is the committed corpus",
		"smartrecruiters:BicycleTherapeutics: 6 postings, 6 new",
		"greenhouse:ginkgobioworks: 41 postings, 3 new",
		"total new postings: 3",
		"built /Install/build/venator.db: 212 Postings, 604 Filter Decisions",
		"",
		"   discover: ok",
		"discover:ok",
		"DISCOVER: OK",
		"submit: starting",
		"commit: starting",
		"discover: finished",
	];
	const before = initialProgress(FETCH);
	const after = replay(noise);

	assert.deepEqual(after, before);
});

test("`ERROR writing heartbeat` is a different event and is deliberately not read as a stage failure", () => {
	// The loop prints this when the stage itself succeeded and the heartbeat could not be
	// written. Treating it as "the stage failed" would say the opposite of what happened.
	const state = replay(["discover: starting", "discover: ok", "discover: ERROR writing heartbeat — disk full"]);

	assert.equal(state.failure, null);
	assert.deepEqual(states(state.stages), [
		["discover", "ok"],
		["filters", "waiting"],
		["view", "waiting"],
	]);
});

test("a stage the run never planned is ignored rather than invented onto the screen", () => {
	// `--only discover,filters,view` was passed, so a commit line means the two sides have
	// drifted. Adding a stage nobody asked for is the wrong answer to that.
	const state = replay(["commit: starting", "commit: ok"]);

	assert.deepEqual(states(state.stages), [
		["discover", "waiting"],
		["filters", "waiting"],
		["view", "waiting"],
	]);

	// And the failing direction, which is the one that is actually reachable: an ERROR line
	// records a stage on the run's failure, so a stage the run never planned must not become
	// the answer to "where did it stop".
	const drifted = replay(["discover: starting", "commit: ERROR — push refused"]);
	assert.equal(drifted.failure, null);
	assert.deepEqual(states(drifted.stages), [
		["discover", "running"],
		["filters", "waiting"],
		["view", "waiting"],
	]);
});

test("a stage still waiting when the run stopped is `not-reached`, not `failed`", () => {
	const state = replay(["discover: starting", "discover: ok", "filters: starting", "filters: ERROR — boom"]);

	// "The Hard Filters failed" and "the view was never rebuilt" are different sentences, and
	// a person acting on a half-finished run needs both of them said correctly.
	assert.deepEqual(states(settleProgress(state, false)), [
		["discover", "ok"],
		["filters", "failed"],
		["view", "not-reached"],
	]);
});

test("a stage still running when the run died is failed, whether or not it printed a line", () => {
	const state = replay(["discover: starting"]);

	assert.deepEqual(states(settleProgress(state, false)), [
		["discover", "failed"],
		["filters", "not-reached"],
		["view", "not-reached"],
	]);
});

test("a successful run's stage list is left exactly as the loop reported it", () => {
	const state = replay(["discover: starting", "discover: ok", "filters: starting", "filters: ok", "view: starting", "view: ok"]);

	assert.deepEqual(settleProgress(state, true), state.stages);
});

test("elapsed time is the loop's timestamps, and an unfinished stage has no end", () => {
	let state = readStageLine(initialProgress(FETCH), "discover: starting", "2026-08-28T10:00:00.000Z");
	state = readStageLine(state, "discover: ok", "2026-08-28T10:02:30.000Z");
	state = readStageLine(state, "filters: starting", "2026-08-28T10:02:31.000Z");

	assert.equal(state.stages[0]?.startedAt, "2026-08-28T10:00:00.000Z");
	assert.equal(state.stages[0]?.endedAt, "2026-08-28T10:02:30.000Z");
	assert.equal(state.stages[1]?.endedAt, null);
});

test("a half line is held back rather than read as an event", () => {
	// A pipe hands over whatever has been written, which is regularly half a line. Reading a
	// stage transition off `discover: st` would put a stage on screen that had not started.
	const first = splitLines("", "discover: sta");
	assert.deepEqual(first.lines, []);
	assert.equal(first.rest, "discover: sta");

	const second = splitLines(first.rest, "rting\nfilters: st");
	assert.deepEqual(second.lines, ["discover: starting"]);
	assert.equal(second.rest, "filters: st");
});

test("a Windows line ending is a line ending", () => {
	const split = splitLines("", "discover: starting\r\ndiscover: ok\r\n");

	assert.deepEqual(split.lines, ["discover: starting", "discover: ok"]);
	assert.deepEqual(states(replay(split.lines).stages), [
		["discover", "ok"],
		["filters", "waiting"],
		["view", "waiting"],
	]);
});
