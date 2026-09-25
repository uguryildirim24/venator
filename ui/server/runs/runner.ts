/**
 * One run at a time, supervised by this server process.
 *
 * **What it starts, and nothing else.** `fetch-and-filter` starts `python -m
 * venator.schedule.loop --only discover,filters,view --profile <name>`. `jev-it` starts
 * `python -m venator.schedule.loop --only jev` with the plan-bound date and spend cap, then
 * `python -m venator.view.build --profile <name>`. There is no route into this module that
 * takes a module name, argument list or store path, and `commit` is not a stage any kind
 * names.
 *
 * **Why the loop remains the fetch sequencer.** It already owns the order, stop-at-first-
 * failure and per-stage heartbeat for Discover, Hard Filters and View. Jev it separately
 * runs Jev with the key; a pause leaves a heartbeat before its View rebuild.
 *
 * **One run at a time, and what that guard is not.** It is one process's memory, not a lock.
 * A person who also runs `python -m venator.match.run` in a terminal is outside it, and
 * nothing in `src/venator/` takes a lock — `venator.profile.claim` guards *cross-Profile*
 * mixing, not concurrent same-Profile runs. The guard is here because the stages are not
 * independent: `view.build` rewrites the whole database `openViewDatabase` opens fresh on
 * every request, and two `match.run`s over the same undecided Postings each append their own
 * decision for every one of them.
 *
 * **The child must not outlive the app.** This is the sharpest edge in the whole surface. The
 * loop spawns a stage per subprocess, so signalling the loop alone would leave a grandchild
 * running. So the whole tree is signalled: a process group on POSIX, `taskkill /T` on
 * Windows. Nothing here is ever `unref`'d, and `shutdownRuns` is wired to the server's own
 * exit.
 *
 * **Nothing here writes a file.** Node spawns; the pipeline writes. There is no `fs` call on
 * this surface, and what a run leaves behind it leaves by running the same stages the CLI
 * runs.
 */

import { spawn, type ChildProcess, type SpawnOptions } from "node:child_process";
import { randomUUID } from "node:crypto";

import {
	runKindOf,
	RUN_KIND_STAGES,
	STORE_WRITING_STAGES,
	type RunKind,
	type RunPhase,
	type RunPlan,
	type RunStage,
	type RunState,
} from "../../shared/runs.ts";
import { pipelineWorkingDirectory, pythonInterpreter, systemContext, type LocationContext } from "../locations.ts";
import { jevRunEnvironment, runEnvironment } from "./environment.ts";
import { RunError } from "./errors.ts";
import { applicationIsActive } from "../applications/activity.ts";
import { initialProgress, readStageLine, settleProgress, splitLines, type ProgressState } from "./progress.ts";

/**
 * A ceiling on one run, so a child that has stopped making progress cannot sit there forever
 * holding the single-flight guard. Discover across a Profile's whole registry plus the Hard
 * Filters over the corpus is minutes; an hour is not a schedule, it is the point past which
 * something is wrong and saying so beats waiting.
 */
const RUN_CEILING_MS = 60 * 60_000;

/** How long a stop waits for the loop to put itself down before the tree is killed outright. */
const STOP_GRACE_MS = 5_000;

/** A view rebuild reads `data/` and writes one file. It does not take minutes. */
const RECOVERY_TIMEOUT_MS = 10 * 60_000;

/** The tail of the pipeline's own output kept for a run that did not succeed. */
const TRANSCRIPT_LIMIT = 8 * 1024;

/** How many finished runs this process remembers. Convenience only; not history. */
const RECENT_LIMIT = 5;

/**
 * How a pipeline child is started.
 *
 * The one seam in this module, and it exists for a specific reason: the supervision above —
 * the single-flight guard, the recovery rebuild, and above all killing the whole process tree
 * — is behaviour, and behaviour is only asserted by watching real processes start and stop.
 * A test that needs a Python on the host to do that is a test that gets skipped somewhere,
 * and a skipped guard is how an orphaned child keeps running. So a test
 * supplies its own command and this module supplies everything else: the same options, the
 * same process group, the same signals, the same close handling.
 *
 * Production passes nothing and gets `node:child_process.spawn`.
 */
export type SpawnChild = (command: string, argv: readonly string[], options: SpawnOptions) => ChildProcess;

type LiveRun = {
	readonly id: string;
	readonly plan: RunPlan;
	/**
	 * The plan's kind, narrowed to something that can actually run.
	 *
	 * A `RunPlan` carries a `PlanKind`. Narrowing once at the start keeps the plan and run
	 * closed sets aligned before either kind can become argv.
	 */
	readonly kind: RunKind;
	readonly startedAt: string;
	readonly context: LocationContext;
	readonly spawnChild: SpawnChild;
	endedAt: string | null;
	child: ChildProcess;
	progress: ProgressState;
	transcript: string;
	/** Set by `stopRun`, so an exit caused by us is reported as stopped rather than failed. */
	stopping: boolean;
	/** Set by the ceiling, so a killed run says it ran out of time rather than that it failed. */
	timedOut: boolean;
	/** True while the recovery view rebuild is running; the run stays live until it settles. */
	recovering: boolean;
	viewRecovered: boolean;
	/** The Jev child reached the OS and may therefore have appended qualification rows. */
	jevStarted: boolean;
	settled: RunPhase | null;
	failure: RunState["failure"];
	timer: NodeJS.Timeout | null;
	killTimer: NodeJS.Timeout | null;
};

let live: LiveRun | null = null;
let recent: RunState[] = [];
/** Set once the server is going away, so nothing new is spawned on the way out. */
let shuttingDown = false;

function now(): string {
	return new Date().toISOString();
}

function append(run: LiveRun, chunk: string): void {
	// A child can echo only a fragment of its credential (or write more than the
	// transcript limit between fragments). Replacing full keys after concatenating
	// output cannot make that safe. Jev output is never exposed to the dashboard.
	if (run.kind === "jev-it") return;
	const combined = run.transcript + chunk;
	run.transcript = combined.length > TRANSCRIPT_LIMIT ? combined.slice(-TRANSCRIPT_LIMIT) : combined;
}

/**
 * Whether this run has left rows in `data/`.
 *
 * Two different facts, and both are the pipeline's rather than a guess. A store-writing stage
 * that reached `ok` obviously wrote. So did a `discover` that merely *started*: it appends per
 * board, inside the loop over the registry (`src/venator/discover/run.py`), so a Discover
 * killed halfway has stored the boards it got to. The Hard Filters are the other way round —
 * they build every decision in memory and append once at the end (`src/venator/match/run.py`)
 * — which is why only Discover is named here.
 */
function wroteRows(run: LiveRun): boolean {
	if (run.kind === "jev-it" && run.jevStarted) return true;
	return run.progress.stages.some((entry) => {
		if (entry.stage === "discover") return entry.state !== "waiting" && entry.state !== "not-reached";
		return STORE_WRITING_STAGES.includes(entry.stage) && entry.state === "ok";
	});
}

function viewRebuilt(progress: ProgressState): boolean {
	return progress.stages.some((entry) => entry.stage === "view" && entry.state === "ok");
}

/** What to say about a run that did not finish, per stage, written for the person reading it. */
function failureFor(run: LiveRun, phase: RunPhase): RunState["failure"] {
	if (phase === "succeeded") return null;
	const stage = run.progress.failure?.stage ?? null;
	const transcript = run.transcript.trim() === "" ? null : run.transcript.trim();
	if (run.timedOut) {
		return {
			stage,
			message: "This run was still going after an hour, so it was stopped.",
			remedy: "Anything it had already finished is in your data. Starting it again picks up from there.",
			transcript,
		};
	}
	if (phase === "stopped") {
		return {
			stage,
			message: "You stopped this run.",
			remedy: "Anything it had already finished is in your data. Starting it again picks up from there.",
			transcript,
		};
	}
	return {
		stage,
		message:
			stage === null
				? "The pipeline stopped before it finished, without saying which stage it was in."
				: `The pipeline stopped during ${STAGE_SENTENCE[stage]}.`,
		remedy: "What the stages before it finished is already in your data; starting the run again is safe.",
		transcript,
	};
}

/**
 * How each stage is named to a person.
 *
 * `CONTEXT.md`'s vocabulary, not the loop's identifiers: `filters` is the Hard Filters, and
 * the raw stage names stay off screen the way every other machine word does.
 */
const STAGE_SENTENCE = {
	discover: "fetching Postings",
	filters: "the Hard Filters",
	jev: "checking with Jev",
	view: "rebuilding what the dashboard reads",
} satisfies Record<RunStage, string>;

function snapshot(run: LiveRun): RunState {
	const phase: RunPhase = run.settled ?? "running";
	const terminal = run.settled !== null && !run.recovering;
	return {
		id: run.id,
		kind: run.kind,
		profile: run.plan.profile,
		// A run whose recovery rebuild is still going is still going. The phase turns terminal
		// once there is nothing left running, so a screen that stops polling on a terminal
		// phase cannot miss the last thing that happened.
		phase: terminal ? phase : "running",
		startedAt: run.startedAt,
		endedAt: terminal ? run.endedAt : null,
		stages: terminal ? settleProgress(run.progress, phase === "succeeded") : run.progress.stages,
		wroteRows: wroteRows(run),
		viewRebuilt: viewRebuilt(run.progress),
		viewRecovered: run.viewRecovered,
		failure: terminal ? run.failure : null,
	};
}

/** Signals the child and everything it spawned. See this module's header for why the tree. */
export function signalTree(child: ChildProcess, signal: NodeJS.Signals, context: LocationContext): void {
	const pid = child.pid;
	if (pid === undefined) return;
	if (context.platform === "win32") {
		// Windows has no process group to signal. `taskkill /T` walks the tree, and `/F` is
		// what actually ends a Python process that is blocked in a network read. There is no
		// graceful half of this on Windows, which is stated rather than pretended otherwise.
		const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
		killer.on("error", () => child.kill(signal));
		return;
	}
	try {
		// Negative pid: the process group the child leads, which is every stage it spawned.
		process.kill(-pid, signal);
	} catch {
		// Already gone, or never got a group. Falling back to the child alone is worse than
		// the group but better than nothing, and the close handler settles it either way.
		child.kill(signal);
	}
}

function finish(run: LiveRun, phase: RunPhase): void {
	run.settled = phase;
	run.failure = failureFor(run, phase);
	if (run.timer !== null) clearTimeout(run.timer);
	if (run.killTimer !== null) clearTimeout(run.killTimer);
	run.timer = null;
	run.killTimer = null;

	const needsRecovery =
		phase !== "succeeded" &&
		!shuttingDown &&
		wroteRows(run) &&
		!viewRebuilt(run.progress) &&
		// Not when the view stage is what failed: "the same command, run again" is only sense
		// when it has not just refused.
		run.progress.failure?.stage !== "view";
	if (!needsRecovery) {
		retire(run);
		return;
	}
	run.recovering = true;
	rebuildView(run);
}

function retire(run: LiveRun): void {
	run.endedAt = now();
	const state = snapshot(run);
	recent = [state, ...recent].slice(0, RECENT_LIMIT);
	if (live === run) live = null;
}

/**
 * The recovery pass: rebuild the disposable view so a half-finished run is visible.
 *
 * Without it, a run that dies in the Hard Filters leaves a person with new Postings they
 * cannot see and no way to make them appear — the dashboard reads `build/venator.db` and
 * nothing else. It is the same command the `view` stage runs, run again, and it writes only
 * the disposable view. It is never started while the server is shutting down.
 */
export function spawnViewStage(profile: string, context: LocationContext, start: SpawnChild = spawn, stdio: SpawnOptions["stdio"] = ["ignore", "pipe", "pipe"]): ChildProcess {
	return start(pythonInterpreter(context), ["-m", "venator.view.build", "--profile", profile], {
		cwd: pipelineWorkingDirectory(context),
		stdio,
		env: runEnvironment(context),
		detached: context.platform !== "win32",
	});
}

function rebuildView(run: LiveRun): void {
	run.progress = {
		stages: run.progress.stages.map((entry) =>
			entry.stage === "view" ? { ...entry, state: "running", startedAt: now() } : entry,
		),
		failure: run.progress.failure,
	};
	// A stop is about the child that was going when it was pressed, and this is a different
	// one. Left set — by the stop that settled the loop, which is the ordinary way a run
	// reaches here — `stopRun` would take its already-stopping early return and never signal
	// this child at all, so the Stop the panel is still drawing would do nothing.
	run.stopping = false;
	const child = spawnViewStage(run.plan.profile, run.context, run.spawnChild, ["ignore", "ignore", "pipe"]);
	run.child = child;
	// Held on the run rather than in this closure, so anything that ends the run early clears
	// it. A stray ten-minute timer keeps a Node process alive on its own.
	run.timer = setTimeout(() => signalTree(child, "SIGKILL", run.context), RECOVERY_TIMEOUT_MS);
	child.stderr?.setEncoding("utf8");
	child.stderr?.on("data", (chunk: string) => append(run, chunk));
	child.on("error", () => settleRecovery(run, false));
	child.on("close", (code) => settleRecovery(run, code === 0));
}

function settleRecovery(run: LiveRun, rebuilt: boolean): void {
	if (run.timer !== null) clearTimeout(run.timer);
	// And the grace timer a stop of *this* child armed. `finish` clears both, but a recovery
	// rebuild settles here instead, so without this a stop during recovery leaves a five-second
	// timer armed past `retire` — which holds a Node process open on its own.
	if (run.killTimer !== null) clearTimeout(run.killTimer);
	run.timer = null;
	run.killTimer = null;
	run.viewRecovered = rebuilt;
	run.progress = {
		stages: run.progress.stages.map((entry) =>
			entry.stage === "view" ? { ...entry, state: rebuilt ? "ok" : "failed", endedAt: now() } : entry,
		),
		failure: run.progress.failure,
	};
	run.recovering = false;
	// The failure sentence is recomputed so it is written against the stage list as it now
	// stands; the phase it describes is the one the loop settled on and is not revisited.
	run.failure = failureFor(run, run.settled ?? "failed");
	retire(run);
}

/** The run this process is supervising, or null. In memory; no store is read. */
export function currentRun(): RunState | null {
	return live === null ? null : snapshot(live);
}

/**
 * The last few runs this process has held.
 *
 * Convenience, and never described as history: the durable record of a run is
 * `data/runs.jsonl` and the `runs` table. A restarted server remembers nothing, which
 * is honest rather than a gap.
 */
export function recentRuns(limit: number): readonly RunState[] {
	return recent.slice(0, limit);
}

/** The second fixed command in a Jev run, started only after Jev exits successfully. */
function startJevView(run: LiveRun): void {
	run.progress = readStageLine(run.progress, "view: starting", now());
	const child = spawnViewStage(run.plan.profile, run.context, run.spawnChild);
	run.child = child;
	child.stdout?.setEncoding("utf8");
	child.stderr?.setEncoding("utf8");
	child.stdout?.on("data", (chunk: string) => append(run, chunk));
	child.stderr?.on("data", (chunk: string) => append(run, chunk));
	child.on("error", () => {
		if (run.settled !== null) return;
		run.progress = readStageLine(run.progress, "view: ERROR — view command did not start", now());
		finish(run, "failed");
	});
	child.on("close", (code) => {
		if (run.settled !== null) return;
		if (run.stopping || run.timedOut) {
			finish(run, "stopped");
			return;
		}
		run.progress = readStageLine(
			run.progress,
			code === 0 ? "view: ok" : "view: ERROR — view command failed",
			now(),
		);
		finish(run, code === 0 ? "succeeded" : "failed");
	});
}

/**
 * Starts the run a plan authorised.
 *
 * Refuses with the live run attached when one is already going, so a double press resolves to
 * "you are already watching this" rather than to a failure.
 */
export function startRun(
	plan: RunPlan,
	context: LocationContext = systemContext(),
	spawnChild: SpawnChild = spawn,
): RunState {
	if (applicationIsActive()) {
		throw new RunError("run_in_progress", "An application operation is running.", "Wait for it to finish before refreshing jobs.");
	}
	if (shuttingDown) {
		throw new RunError(
			"run_failed_to_start",
			"The dashboard is shutting down, so no run was started.",
			"Start it again once the app is running.",
		);
	}
	if (live !== null) {
		throw new RunError(
			"run_in_progress",
			"A run is already going.",
			"Wait for it to finish, or stop it first — the stages write to the same files and must not overlap.",
			null,
			snapshot(live),
		);
	}
	// The structural gate: the plan kind must still narrow through the same closed set that
	// indexes `RUN_KIND_STAGES`. No request supplies stages or argv.
	const kind = runKindOf(plan.kind);
	if (kind === null) {
		throw new RunError(
			"not_startable",
			"That plan does not name a run this dashboard can start, so nothing was started.",
			"Ask for a new plan before you try again.",
		);
	}
	const stages = RUN_KIND_STAGES[kind];
	let argv: readonly string[];
	let environment = runEnvironment(context);
	if (kind === "jev-it") {
		if (plan.jev == null) {
			throw new RunError("not_startable", "That plan does not describe a Jev run.");
		}
		environment = { ...jevRunEnvironment(context), VENATOR_JEV_MAX_USD: plan.jev.maximumUsd.toFixed(2) };
		argv = [
			"-m",
			"venator.schedule.loop",
			"--only",
			"jev",
			"--as-of",
			plan.jev.asOf,
			"--profile",
			plan.profile,
		];
	} else {
		argv = [
			"-m",
			"venator.schedule.loop",
			"--only",
			stages.join(","),
			"--profile",
			plan.profile,
			"--interactive-discover",
		];
	}
	const child = spawnChild(pythonInterpreter(context), argv, {
		cwd: pipelineWorkingDirectory(context),
		stdio: kind === "jev-it" ? ["ignore", "ignore", "ignore"] : ["ignore", "pipe", "pipe"],
		env: environment,
		// Its own process group, so a stop reaches the whole closed command tree.
		detached: context.platform !== "win32",
	});

	const run: LiveRun = {
		id: `run_${randomUUID()}`,
		plan,
		kind,
		startedAt: now(),
		endedAt: null,
		context,
		spawnChild,
		child,
		progress: initialProgress(stages),
		transcript: "",
		stopping: false,
		timedOut: false,
		recovering: false,
		viewRecovered: false,
		jevStarted: false,
		settled: null,
		failure: null,
		timer: null,
		killTimer: null,
	};
	live = run;

	run.timer = setTimeout(() => {
		run.timedOut = true;
		signalTree(run.child, "SIGKILL", context);
	}, RUN_CEILING_MS);

	if (kind === "jev-it") {
		child.on("spawn", () => {
			run.jevStarted = true;
		});
		child.on("error", () => {
			run.transcript = "";
			finish(run, "failed");
		});
		child.on("close", (code) => {
			if (run.settled !== null) return;
			if (run.stopping || run.timedOut) {
				finish(run, "stopped");
				return;
			}
			if (code !== 0) {
				finish(run, "failed");
				return;
			}
			startJevView(run);
		});
	} else {
		let outRest = "";
		let errRest = "";
		child.stdout?.setEncoding("utf8");
		child.stderr?.setEncoding("utf8");
		child.stdout?.on("data", (chunk: string) => {
			append(run, chunk);
			const { lines, rest } = splitLines(outRest, chunk);
			outRest = rest;
			for (const line of lines) run.progress = readStageLine(run.progress, line, now());
		});
		child.stderr?.on("data", (chunk: string) => {
			append(run, chunk);
			const { lines, rest } = splitLines(errRest, chunk);
			errRest = rest;
			for (const line of lines) run.progress = readStageLine(run.progress, line, now());
		});
		child.on("error", () => {
			// The interpreter would not start. Nothing ran, so nothing was written.
			run.transcript = "";
			finish(run, "failed");
		});
		child.on("close", (code) => {
			if (run.settled !== null) return;
			if (run.stopping || run.timedOut) finish(run, "stopped");
			else finish(run, code === 0 ? "succeeded" : "failed");
		});
	}

	return snapshot(run);
}

/**
 * Stops the live run: the whole tree, politely, then not politely.
 *
 * A stop is about a **child**, not about a phase. The recovery rebuild is a child this server
 * started, so while one is going there is something to stop and this accepts — even though the
 * loop has already settled. Refusing there was a contradiction a person could see: the surface
 * reported `running`, the panel therefore drew Stop, and pressing it answered "there is no run
 * going" while a `venator.view.build` was demonstrably going.
 */
export function stopRun(): RunState {
	const run = live;
	if (run === null || (run.settled !== null && !run.recovering)) {
		throw new RunError("no_run", "There is no run going, so there was nothing to stop.");
	}
	if (run.stopping) return snapshot(run);
	run.stopping = true;
	signalTree(run.child, "SIGTERM", run.context);
	run.killTimer = setTimeout(() => signalTree(run.child, "SIGKILL", run.context), STOP_GRACE_MS);
	return snapshot(run);
}

/**
 * Ends any live run because the server is going away.
 *
 * A run outliving the window it was started from is the failure this whole module is most
 * careful about, so this is deliberately blunt: the tree is killed outright, and no recovery
 * rebuild is started on the way out.
 */
export function shutdownRuns(): void {
	shuttingDown = true;
	const run = live;
	if (run === null) return;
	// Whatever child this run currently has, including the recovery rebuild: a run whose loop
	// has already settled can still own a live `venator.view.build`, and that is the same
	// orphan by a different name.
	run.stopping = true;
	signalTree(run.child, "SIGKILL", run.context);
}

/**
 * Test seam: ends anything live and forgets this process's run memory.
 *
 * Never called by the server. It kills before it forgets, in that order, because a reset that
 * dropped the reference first would leave exactly the orphan this module exists to prevent.
 */
export function resetRunsForTest(): void {
	const run = live;
	if (run !== null) signalTree(run.child, "SIGKILL", run.context);
	if (run !== null && run.timer !== null) clearTimeout(run.timer);
	if (run !== null && run.killTimer !== null) clearTimeout(run.killTimer);
	live = null;
	recent = [];
	shuttingDown = false;
}
