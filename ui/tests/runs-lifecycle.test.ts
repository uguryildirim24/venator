/**
 * Supervising a run: what happens when it finishes, fails, is pressed twice, is stopped, or
 * the app quits underneath it.
 *
 * These are the cases where getting it wrong costs something real. An orphaned child outliving
 * the window is the sharpest edge on this whole surface — a run still polling boards and
 * writing the Install's stores with no window in existence that mentions it — so it is asserted
 * against **real processes**, with a real process group and real signals, rather than against
 * a stand-in object that cannot be orphaned.
 *
 * `startRun` takes the command to start (`SpawnChild`) and supplies everything else itself:
 * the same options, the same group, the same signals, the same close handling. So every case
 * here drives this host's own `node` printing the loop's contracted stage lines, and what is
 * under test is the supervision rather than the pipeline.
 */

import assert from "node:assert/strict";
import { spawn, type SpawnOptions } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import type { RunPlan } from "../shared/runs.ts";
import type { LocationContext } from "../server/locations.ts";
import { RunError } from "../server/runs/errors.ts";
import { currentRun, recentRuns, resetRunsForTest, shutdownRuns, startRun, stopRun, type SpawnChild } from "../server/runs/runner.ts";

const CONTEXT: LocationContext = {
	platform: process.platform,
	home: tmpdir(),
	workingDirectory: process.cwd(),
	checkoutRoot: null,
	environment: (name) => process.env[name],
};

const PLAN: RunPlan = {
	token: "plan_test",
	kind: "fetch-and-filter",
	profile: "sample",
	profileDirectory: "/Install/profiles/sample",
	stages: ["discover", "filters", "view"],
	storeRoot: "/Install",
	viewPath: "/Install/build/venator.db",
	dashboardViewPath: "/Install/build/venator.db",
	boards: 35,
	startable: true,
	notes: [],
};

type Started = { readonly command: string; readonly argv: readonly string[]; readonly options: SpawnOptions };

/**
 * A `SpawnChild` that records what it was asked for and starts this host's own `node` running
 * `script` instead — with the options it was handed, untouched, so the process group, the
 * environment and the pipes are the real ones.
 */
type Scripted = {
	readonly spawn: SpawnChild;
	/** Every start this run asked for, in order, as it asked for it. */
	readonly started: Started[];
};

function scripted(scripts: readonly string[]): Scripted {
	const started: Started[] = [];
	const spawnChild: SpawnChild = (command, argv, options) => {
		const script = scripts[started.length] ?? "";
		started.push({ command, argv, options });
		return spawn(process.execPath, ["-e", script], options);
	};
	return { spawn: spawnChild, started };
}

function settled(): Promise<void> {
	return new Promise((done) => setTimeout(done, 25));
}

/** One turn of the event loop, past the timers phase, so a just-fired timer is off the books. */
function tick(): Promise<void> {
	return new Promise((done) => setImmediate(done));
}

/** Waits until `check` holds, or gives up loudly rather than hanging the suite. */
async function until(check: () => boolean, why: string, limitMs = 15_000): Promise<void> {
	const deadline = Date.now() + limitMs;
	while (!check()) {
		if (Date.now() > deadline) assert.fail(`timed out waiting: ${why}`);
		await settled();
	}
}

/** The loop's contracted output for a whole successful fetch-and-filter run. */
const SUCCEEDS = [
	"discover: starting",
	"discover: ok",
	"filters: starting",
	"filters: ok",
	"view: starting",
	"view: ok",
]
	.map((line) => `console.log(${JSON.stringify(line)});`)
	.join("");

/** Discover finishes, the Hard Filters stop. The half-succeeded run `data/` makes possible. */
const FAILS_IN_FILTERS = [
	'console.log("discover: starting");',
	'console.log("discover: ok");',
	'console.log("filters: starting");',
	'console.error("filters: ERROR — venator.match.run returned non-zero exit status 1.");',
	"process.exit(1);",
].join("");

const RUNS_FOREVER = "setInterval(() => {}, 1000);";

/** Runs `work` and hands back the `RunError` it must raise, rather than a bare `unknown`. */
function refusal(work: () => void): RunError {
	try {
		work();
	} catch (error) {
		assert.ok(error instanceof RunError, `expected a RunError, got ${String(error)}`);
		return error;
	}
	return assert.fail("expected a refusal");
}

afterEach(() => {
	resetRunsForTest();
});

test("the loop is started with the stages of the run that was asked for, and never with `commit`", async () => {
	const { spawn: spawnChild, started } = scripted([SUCCEEDS]);
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => currentRun() === null, "the run to finish");

	assert.equal(started.length, 1);
	assert.deepEqual(started[0]?.argv, [
		"-m",
		"venator.schedule.loop",
		"--only",
		"discover,filters,view",
		"--profile",
		"sample",
		"--interactive-discover",
	]);
	// The whole of what can be asked for: a module, a stage list from a closed set, and a
	// Profile name. No store path, no argument passthrough, and no `commit`.
	assert.ok(!started[0]?.argv.includes("commit"));
});

test("the child is given a built environment, not this process's", async () => {
	const typesafeSentinel = "typesafe-key-for-fetch-containment";
	const context: LocationContext = {
		...CONTEXT,
		environment: (name) => (name === "TYPESAFE_API_KEY" ? typesafeSentinel : CONTEXT.environment(name)),
	};
	const { spawn: spawnChild, started } = scripted([SUCCEEDS]);
	startRun(PLAN, context, spawnChild);
	await until(() => currentRun() === null, "the run to finish");

	const environment = started[0]?.options.env ?? {};
	assert.equal(environment["PYTHONUNBUFFERED"], "1");
	// The Install's chosen document runtime is explicit; Refresh receives no Jev credential.
	assert.equal(environment["VENATOR_LLM_RUNTIME"], "claude");
	assert.equal(environment["TYPESAFE_API_KEY"], undefined);
	assert.ok(!Object.values(environment).includes(typesafeSentinel));
	// `$GIT_DIR` would send the loop's commit stage at whatever repository the environment
	// named, past its own work-tree gate.
	assert.equal(environment["GIT_DIR"], undefined);
});

test("a run that finishes reports every stage done, and says the view was rebuilt", async () => {
	const { spawn: spawnChild } = scripted([SUCCEEDS]);
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => currentRun() === null, "the run to finish");

	const finished = recentRuns(5)[0];
	assert.equal(finished?.phase, "succeeded");
	assert.deepEqual(
		finished?.stages.map((entry) => entry.state),
		["ok", "ok", "ok"],
	);
	assert.equal(finished?.failure, null);
	assert.equal(finished?.viewRebuilt, true);
	assert.equal(finished?.viewRecovered, false);
});

test("a run that stops halfway says which stage, and says what is already in the data", async () => {
	const { spawn: spawnChild, started } = scripted([FAILS_IN_FILTERS, ""]);
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => currentRun() === null, "the run to settle");

	const finished = recentRuns(5)[0];
	assert.equal(finished?.phase, "failed");
	assert.equal(finished?.failure?.stage, "filters");
	// `data/` is append-only, so Discover's Postings are there. Saying "the run failed" and
	// stopping would read as "nothing happened", and the opposite is true.
	assert.equal(finished?.wroteRows, true);
	assert.deepEqual(
		finished?.stages.map((entry) => entry.state),
		["ok", "failed", "ok"],
	);
	// The pipeline's own words, kept for the person reading them, and bounded.
	assert.match(finished?.failure?.transcript ?? "", /venator\.match\.run returned non-zero/u);

	// And the recovery pass: without it a person is left with new Postings they cannot see,
	// because the dashboard reads build/venator.db and nothing else.
	assert.equal(started.length, 2);
	assert.deepEqual(started[1]?.argv, ["-m", "venator.view.build", "--profile", "sample"]);
	assert.equal(finished?.viewRecovered, true);
});

/**
 * How many timers this process is holding open.
 *
 * A leaked `setTimeout` is invisible from the outside except here: it keeps the event loop —
 * and so a desktop sidecar that has nothing left to do — alive until it fires. Node drops a
 * one-shot timer from this list before running it, so a settled run must leave the count
 * where it found it.
 */
function armedTimers(): number {
	return process.getActiveResourcesInfo().filter((resource) => resource === "Timeout").length;
}

test("a run that has ended leaves no timer armed, the recovery rebuild's included", async () => {
	const { spawn: spawnChild, started } = scripted([FAILS_IN_FILTERS, ""]);
	const before = armedTimers();
	startRun(PLAN, CONTEXT, spawnChild);
	// The ceiling timer, armed for as long as the run is going. Asserted so that the count
	// after cannot be green because nothing was ever armed in the first place.
	assert.ok(armedTimers() > before, "a live run arms a timer");

	await until(() => currentRun() === null, "the run and its recovery rebuild to settle");
	assert.equal(started.length, 2, "the recovery rebuild ran, so its timer was armed too");
	await tick();
	assert.equal(armedTimers(), before);
});

test("the recovery rebuild is not started when the view stage is what failed", async () => {
	const failsInView = [
		'console.log("discover: starting");',
		'console.log("discover: ok");',
		'console.log("filters: starting");',
		'console.log("filters: ok");',
		'console.log("view: starting");',
		'console.error("view: ERROR — the view could not be written");',
		"process.exit(1);",
	].join("");
	const { spawn: spawnChild, started } = scripted([failsInView]);
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => currentRun() === null, "the run to settle");

	// "The same command, run again" is only sense when it has not just refused.
	assert.equal(started.length, 1);
	assert.equal(recentRuns(5)[0]?.failure?.stage, "view");
});

test("a second press while a run is going attaches to it rather than starting another", async () => {
	const { spawn: spawnChild, started } = scripted([RUNS_FOREVER]);
	const first = startRun(PLAN, CONTEXT, spawnChild);

	const refused = refusal(() => {
		startRun(PLAN, CONTEXT, spawnChild);
	});
	assert.equal(refused.code, "run_in_progress");
	assert.equal(refused.status, 409);
	// The live run rides on the refusal, so a double press resolves to "you are already
	// watching this" rather than to a failure.
	assert.equal(refused.run?.id, first.id);
	assert.equal(started.length, 1);

	stopRun();
	await until(() => currentRun() === null, "the stopped run to settle");
});

test("stopping ends the run, and everything the loop had spawned under it", async () => {
	const directory = mkdtempSync(join(tmpdir(), "venator-run-"));
	const witness = join(directory, "grandchild.log");
	writeFileSync(witness, "");
	try {
		// A loop is not one process: it spawns a stage per subprocess. Signalling only the
		// loop would leave the stage running with nothing on screen about it. So the child here starts a
		// grandchild that keeps working, and what is asserted is that the grandchild stops.
		const keepsWorking = `const { appendFileSync } = require("node:fs"); setInterval(() => appendFileSync(${JSON.stringify(witness)}, "."), 20);`;
		const withGrandchild = [
			'const { spawn } = require("node:child_process");',
			`spawn(process.execPath, ["-e", ${JSON.stringify(keepsWorking)}], { stdio: "ignore" });`,
			'console.log("discover: starting");',
			"setInterval(() => {}, 1000);",
		].join("");
		// Two scripts, and the second is not padding: Discover had started, so the stop settles
		// a run that wrote rows without rebuilding the view, and the recovery pass runs. It is
		// named here so a reader is not left to infer why this run spawns twice.
		const { spawn: spawnChild, started } = scripted([withGrandchild, ""]);
		startRun(PLAN, CONTEXT, spawnChild);

		await until(() => readFileSync(witness, "utf8").length > 0, "the grandchild to start working");
		const stopped = stopRun();
		assert.equal(stopped.phase, "running");

		await until(() => currentRun() === null, "the stopped run to settle");
		const grew = readFileSync(witness, "utf8").length;
		await new Promise((done) => setTimeout(done, 400));

		assert.equal(readFileSync(witness, "utf8").length, grew, "the grandchild was still working after the stop");
		const finished = recentRuns(5)[0];
		assert.equal(finished?.phase, "stopped");
		assert.match(finished?.failure?.message ?? "", /You stopped this run/u);
		assert.equal(started.length, 2, "the stop settled a run that had written rows, so the view was recovered");
		assert.deepEqual(started[1]?.argv, ["-m", "venator.view.build", "--profile", "sample"]);
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

test("a stop reaches the recovery rebuild, because a stop is about a child and not a phase", async () => {
	const directory = mkdtempSync(join(tmpdir(), "venator-run-"));
	const witness = join(directory, "rebuild.log");
	writeFileSync(witness, "");
	try {
		// The loop runs until it is stopped, having started Discover — so rows were written,
		// the view was never rebuilt, and settling that run starts the recovery
		// `venator.view.build`. That rebuild is a child this server spawned after the person
		// had already pressed Stop, and it stands in here for one that is slow enough to be
		// caught: it keeps working and keeps saying so, so "a process is going" is a fact read
		// off the filesystem rather than a phase read off this module.
		const loop = ['console.log("discover: starting");', "setInterval(() => {}, 1000);"].join("");
		const rebuild = `const { appendFileSync } = require("node:fs"); setInterval(() => appendFileSync(${JSON.stringify(witness)}, "."), 20);`;
		const { spawn: spawnChild, started } = scripted([loop, rebuild]);
		const before = armedTimers();
		startRun(PLAN, CONTEXT, spawnChild);
		await until(() => currentRun()?.stages[0]?.state === "running", "Discover to start");

		stopRun();
		await until(() => started.length === 2, "the recovery rebuild to start");
		assert.deepEqual(started[1]?.argv, ["-m", "venator.view.build", "--profile", "sample"]);
		await until(() => readFileSync(witness, "utf8").length > 0, "the recovery rebuild to be working");

		// What the person is looking at: the surface says running, so the panel is drawing Stop.
		assert.equal(currentRun()?.phase, "running");

		// So pressing it must reach that child. Refusing here was the contradiction: `no_run`,
		// "there was nothing to stop", with a process demonstrably going and `startRun` refused
		// for as long as it kept going.
		const stopped = stopRun();
		assert.equal(stopped.phase, "running");

		await until(() => currentRun() === null, "the stopped recovery rebuild to retire the run");
		const grew = readFileSync(witness, "utf8").length;
		await new Promise((done) => setTimeout(done, 400));
		assert.equal(readFileSync(witness, "utf8").length, grew, "the recovery rebuild was still working after the stop");

		// The view was not rebuilt, and the run says so rather than claiming it was.
		const finished = recentRuns(5)[0];
		assert.equal(finished?.phase, "stopped");
		assert.equal(finished?.viewRecovered, false);

		// The stop armed a five-second grace timer against a child that then died here rather
		// than in `finish`. Left armed it holds this process open on its own.
		await tick();
		assert.equal(armedTimers(), before);

		// And the single-flight guard is released, so a person is not locked out until the
		// recovery timeout would have fired.
		const { spawn: next } = scripted([SUCCEEDS]);
		startRun(PLAN, CONTEXT, next);
		await until(() => currentRun() === null, "the next run to finish");
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

test("stopping when nothing is going is refused rather than pretended", () => {
	assert.equal(
		refusal(() => {
			stopRun();
		}).code,
		"no_run",
	);
});

test("the server going away ends the run and starts nothing on the way out", async () => {
	const { spawn: spawnChild, started } = scripted([
		['console.log("discover: starting");', 'console.log("discover: ok");', "setInterval(() => {}, 1000);"].join(""),
		"",
	]);
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => currentRun()?.stages[0]?.state === "ok", "discover to finish");

	shutdownRuns();
	await until(() => currentRun() === null, "the run to settle after shutdown");

	// Discover had written rows and the view was never rebuilt, which is exactly when a
	// recovery pass would normally run. On the way out it must not: the process is leaving.
	assert.equal(started.length, 1);
	assert.equal(recentRuns(5)[0]?.viewRecovered, false);
});

test("a recovery rebuild that is still going when the app quits is killed too", async () => {
	// The run's loop has already settled by then, so this is the orphan the shutdown handler
	// would miss if it only looked at runs that had not finished. `venator.view.build` writes
	// a file; a copy of it left running against a database nothing is watching is exactly the
	// kind of leftover this module exists to prevent.
	const { spawn: spawnChild, started } = scripted([FAILS_IN_FILTERS, RUNS_FOREVER]);
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => started.length === 2, "the recovery rebuild to start");

	shutdownRuns();

	// It runs forever unless something kills it, so settling at all is the assertion.
	await until(() => currentRun() === null, "the recovery rebuild to be killed and the run to settle");
	assert.equal(recentRuns(5)[0]?.viewRecovered, false);
});

test("a run whose child never starts is a failure, and quotes nothing back", async () => {
	const spawnChild: SpawnChild = (_command, _argv, options) =>
		spawn(join(tmpdir(), "venator-no-such-interpreter"), [], options);
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => currentRun() === null, "the failed launch to settle");

	const finished = recentRuns(5)[0];
	assert.equal(finished?.phase, "failed");
	assert.equal(finished?.failure?.transcript, null);
});

test("a finished run is remembered, newest first, and never called history", async () => {
	const { spawn: spawnChild } = scripted([SUCCEEDS, SUCCEEDS]);
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => currentRun() === null, "the first run to finish");
	const first = recentRuns(5)[0]?.id;
	startRun(PLAN, CONTEXT, spawnChild);
	await until(() => currentRun() === null, "the second run to finish");

	const remembered = recentRuns(5);
	assert.equal(remembered.length, 2);
	assert.notEqual(remembered[0]?.id, first);
	assert.equal(remembered[1]?.id, first);
});
