/**
 * The runtime probe, read through a stand-in for the pipeline module.
 *
 * The probe itself lives on the Python side and answers for the machine it runs on, so what
 * is testable here is the normalisation: that the eight states stay eight, that `remedy` and
 * `caveat` arrive unchanged, that a state this dashboard has never heard of is reported as a
 * mismatch rather than guessed at, and that an Install with no probe in it says so instead of
 * failing.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import { probeRuntimes, readProbeDocument, RuntimeLaneNameError, validateLaneName } from "../server/onboarding/runtime.ts";

const CLAUDE_LANE = {
	lane: "claude",
	state: "available",
	ready: true,
	detail: "signed in as someone",
	remedy: null,
	caveat: null,
	default: true,
};

const CODEX_LANE = {
	lane: "codex",
	state: "not_signed_in",
	ready: false,
	detail: "the CLI is installed and exited 1",
	remedy: "Run `codex login` in a terminal.",
	caveat: "This runtime is only partly verified: no authenticated run has ever happened.",
	default: false,
};

/**
 * A stand-in probe module, run by a real Python interpreter.
 *
 * WHY A REAL INTERPRETER
 * ----------------------
 *
 * This used to be a `#!/bin/sh` file with no extension, which is three things
 * Windows cannot do: it honours no shebang, `CreateProcess` starts nothing
 * without an extension, and `chmod` means nothing there. The four cases below
 * that reach a child process were red on the Windows leg for the fixture rather
 * than for the code.
 *
 * `tests/boards.test.ts` fixes its equivalent by pointing `VENATOR_PYTHON` at
 * this process's own `node`. That does not transfer here, and the obstacle is
 * exact: `argumentsFor` builds `["-m", "venator.llm.probe", …]`, `-m` is the
 * *first* argument, and node rejects it in its C++ option parser before any
 * `--require` preload can run —
 *
 *     $ NODE_OPTIONS="--require ./stand-in.cjs" ./node-copy -m venator.llm.probe --json
 *     ./node-copy: bad option: -m          (exit 9, nothing preloaded)
 *
 * The only executables that take `-m <module>` are Python ones. So these cases
 * run against a real interpreter — which is what `probeRuntimes` spawns in
 * production anyway, here and in `boards.ts`, from two live onboarding routes.
 *
 * That respects the boundary `CLAUDE.md` draws rather than crossing it. The rule
 * there is that the two halves never *import* from each other, and nothing here
 * does: `ui/` still imports only from `ui/`, `pnpm test` still never invokes
 * `uv`, neither toolchain's package manager becomes a dependency of the other's,
 * and the module the child runs is written into a scratch directory by this file
 * — the pipeline's own `src/venator/llm/probe.py` is never on the path and never
 * runs. What is out of date is that section's other sentence, "they meet only at
 * the JSONL files under `data/`": these two routes have been spawning the
 * pipeline for as long as onboarding has existed. That is reported on the pull
 * request for the Owner to word, not edited here.
 *
 * HOW THE CHILD FINDS THE STAND-IN
 * --------------------------------
 *
 * `-m` puts the child's working directory at the front of `sys.path`, and
 * `pipelineWorkingDirectory` resolves to `context.workingDirectory` here
 * (no checkout, no application data directory), which is the scratch directory.
 * So the package written there is what `-m venator.llm.probe` resolves to, with
 * no `PYTHONPATH` and no mutation of this process's environment — nothing global
 * to set, restore, or leak into a neighbouring case. It also shadows a real
 * `venator` that happens to be installed on the machine running the suite,
 * which is what keeps the "not installed" case deterministic: the package is
 * always written, and only `probe.py` is left out.
 */

/** An absolute interpreter path, or null on a machine that has no Python. */
function findPython(): string | null {
	// Asked of the interpreter rather than of PATH: `pythonInterpreter` resolves
	// `VENATOR_PYTHON` against the working directory, so a bare name would become
	// a path inside the scratch directory. `sys.executable` is absolute.
	// A Windows Store alias answers a bare `python3`/`python` with a non-zero exit
	// and no output, which is why the status and the text are both checked.
	for (const candidate of ["python3", "python"]) {
		const asked = spawnSync(candidate, ["-c", "import sys; sys.stdout.write(sys.executable)"], {
			encoding: "utf8",
			windowsHide: true,
			// Bounded on purpose. This pull request exists because a missing ceiling
			// turned a bad input into a 29-minute hung job; a discovery step with no
			// deadline of its own would be the same shape of mistake.
			timeout: 15_000,
		});
		const answer = (asked.stdout ?? "").trim();
		if (asked.status === 0 && answer !== "") return answer;
	}
	return null;
}

const PYTHON = findPython();

/**
 * Whether a missing interpreter is allowed to be a skip.
 *
 * On a contributor's machine it is: not everyone working on the dashboard has a
 * Python on PATH, and a hard failure there would be noise about someone else's
 * toolchain. On a runner it is not. CI has an interpreter, and a case that
 * quietly skips on the one platform it was written to cover is exactly the hole
 * this repository has already spent two pull requests closing for the browser
 * tests — a guard that cannot fail is not a guard.
 *
 * `GITHUB_ACTIONS` is what this repository already keys on for "am I on a
 * runner" (`.github/scripts/filters_version_guard.py` and
 * `never_submit_coverage_guard.py` both read it). `CI` is honoured as well
 * because it is the broader convention and every runner sets it: widening what
 * counts as CI can only make this refusal stricter, never weaker.
 */
const ON_A_RUNNER = process.env["GITHUB_ACTIONS"] === "true" || process.env["CI"] === "true";

/**
 * A scratch directory holding a `venator.llm.probe` for the child to run.
 *
 * `body` is the module's source, or null to leave `probe.py` out and let a real
 * interpreter produce its own "No module named venator.llm.probe" on stderr and
 * its own exit 1 — the genuine article rather than an imitation of it.
 */
function stubProbe(body: string | null): LocationContext {
	const directory = mkdtempSync(join(tmpdir(), "venator-probe-"));
	const packageDirectory = join(directory, "venator", "llm");
	mkdirSync(packageDirectory, { recursive: true });
	writeFileSync(join(directory, "venator", "__init__.py"), "");
	writeFileSync(join(packageDirectory, "__init__.py"), "");
	if (body !== null) writeFileSync(join(packageDirectory, "probe.py"), body);
	const environment = new Map([["VENATOR_PYTHON", PYTHON ?? ""]]);
	return {
		platform: "linux",
		home: directory,
		workingDirectory: directory,
		checkoutRoot: null,
		environment: (name) => environment.get(name),
	};
}

/** Declares a case that needs a real interpreter: skipped locally, failed on a runner. */
function spawningTest(name: string, body: () => Promise<void>): void {
	test(name, async (context) => {
		if (PYTHON === null) {
			assert.ok(
				!ON_A_RUNNER,
				"this case spawns a real Python interpreter and neither `python3` nor `python` " +
					"answered on PATH. On a contributor's machine that is a skip; on a runner it " +
					"is a failure, because a case that quietly skips is indistinguishable from a " +
					"case that passed.",
			);
			context.skip("no Python interpreter on PATH to spawn");
			return;
		}
		await body();
	});
}

/**
 * Appends what the child was asked, then answers with an empty lane list.
 *
 * Two details in three lines of Python, both of which have already gone wrong:
 *
 * `print(..., file=...)` rather than a backslash-n inside the string, because
 * this is a template literal — a single backslash-n here is written into the
 * module as a real newline and leaves Python looking at an unterminated string.
 * No escape at all beats a doubled one the next person has to notice.
 *
 * `newline="\n"` on the handle, because Python's text mode translates a newline
 * to CRLF on Windows and nowhere else. Without it the record read back as
 * `venator.llm.probe --json\r` on the Windows leg and matched nothing. The
 * assertion is not the place to absorb that: what the child was asked is the
 * claim, and a line terminator this fixture chose is not part of it. So the
 * fixture writes the same bytes on both hosts and the assertion stays exact.
 */
const RECORD_ARGUMENTS = `import sys
from pathlib import Path

record = Path(__file__).resolve().parents[2] / "argv"
with record.open("a", encoding="utf-8", newline="\\n") as handle:
    print(f"{__spec__.name} {' '.join(sys.argv[1:])}", file=handle)
sys.stdout.write('{"lanes":[]}')
`;

/**
 * Answers with whatever the case wrote beside the package.
 *
 * `read_text` is text mode, so it would translate a CRLF — harmless here because
 * the document is `JSON.stringify` output, which is a single line with no CR or
 * LF byte in it at all. An escaped newline inside a JSON string is two
 * characters and nothing translates it.
 */
const ANSWER_FROM_FILE = `import sys
from pathlib import Path

sys.stdout.write((Path(__file__).resolve().parents[2] / "answer.json").read_text(encoding="utf-8"))
`;

/** Exit 2 is a usage error: the dashboard built the command, so it is the dashboard's bug. */
const USAGE_ERROR = "import sys\n\nsys.exit(2)\n";

test("a lane's own words are passed along unchanged", () => {
	const result = readProbeDocument(JSON.stringify({ lanes: [CLAUDE_LANE, CODEX_LANE] }));
	assert.equal(result.probe.available, true);
	assert.equal(result.lanes.length, 2);
	const codex = result.lanes[1];
	assert.equal(codex?.remedy, CODEX_LANE.remedy);
	// The caveat is the field that stops Codex reading as equivalent to the Claude lane.
	assert.equal(codex?.caveat, CODEX_LANE.caveat);
	assert.equal(codex?.state, "not_signed_in");
	assert.equal(codex?.default, false);
});

test("all eight states survive the round trip, and configured is not available", () => {
	const states = [
		"available",
		"not_installed",
		"not_spawnable",
		"not_signed_in",
		"not_configured",
		"configured",
		"timed_out",
		"failed",
	];
	const lanes = states.map((state, index) => ({ ...CLAUDE_LANE, lane: `lane${index}`, state, default: false }));
	const result = readProbeDocument(JSON.stringify({ lanes }));
	assert.deepEqual(
		result.lanes.map((lane) => lane.state),
		states,
	);
	// A key on file and a key that was confirmed to work are different claims.
	assert.notEqual(result.lanes[5]?.state, "available");
	// And a runtime that is installed in a shape this platform cannot start is not the same
	// claim as one that is not installed — the person has different work to do.
	assert.notEqual(result.lanes[2]?.state, "not_installed");
});

test("a bare list and a single lane object are both read", () => {
	assert.equal(readProbeDocument(JSON.stringify([CLAUDE_LANE])).lanes.length, 1);
	assert.equal(readProbeDocument(JSON.stringify(CLAUDE_LANE)).lanes[0]?.lane, "claude");
});

test("a state this dashboard has never heard of is a mismatch, never a guess", () => {
	const result = readProbeDocument(JSON.stringify({ lanes: [{ ...CLAUDE_LANE, state: "maybe" }] }));
	assert.equal(result.probe.available, false);
	assert.equal(result.probe.reason, "contract_mismatch");
	assert.deepEqual([...result.lanes], []);
});

test("a lane with no name is a mismatch too", () => {
	assert.equal(readProbeDocument(JSON.stringify({ lanes: [{ state: "available" }] })).probe.reason, "contract_mismatch");
});

test("a runtime name that is not a name never reaches a command line", () => {
	for (const attempt of ["--all", "a b", "$(rm -rf /)", "Claude", "", "-x"]) {
		assert.throws(() => validateLaneName(attempt), RuntimeLaneNameError, attempt);
	}
	assert.equal(validateLaneName(" claude "), "claude");
});

spawningTest("an Install with no probe in it says so rather than failing", async () => {
	// No `probe.py`, so the interpreter itself produces the "No module named
	// venator.llm.probe" and the exit 1 — the answer a real Install with no
	// pipeline in it gives, rather than a fixture's imitation of one.
	const context = stubProbe(null);
	try {
		const result = await probeRuntimes("default", context);
		assert.equal(result.probe.available, false);
		assert.equal(result.probe.reason, "not_installed");
		assert.match(result.probe.message ?? "", /not be run in this Install/u);
		assert.deepEqual([...result.lanes], []);
	} finally {
		rmSync(context.home, { recursive: true, force: true });
	}
});

test("a probe that cannot be launched at all is the same kind of answer", async () => {
	const environment = new Map([["VENATOR_PYTHON", "/nonexistent/python"]]);
	const result = await probeRuntimes("default", {
		platform: "linux",
		home: "/tmp",
		workingDirectory: "/tmp",
		checkoutRoot: null,
		environment: (name) => environment.get(name),
	});
	assert.equal(result.probe.available, false);
	assert.equal(result.probe.reason, "not_installed");
});

/**
 * The sentence a Windows Install with no system Python used to get, and why it was wrong.
 *
 * The desktop bundle ships an interpreter and the Tauri host names it in
 * `VENATOR_BUNDLED_PYTHON`, so on that machine the runtime check *is* part of the Install. If
 * the interpreter will not start, telling that person the check is not installed sends them to
 * fix something that is already there. The interpreter is named because it is the only thing
 * that makes the failure actionable.
 *
 * A real launch failure, not a simulated one: the path does not exist, so `spawn` emits the
 * error event `probeRuntimes` reads.
 *
 * The expectation is `resolve`d rather than typed, because the value in the environment is not
 * the value that gets spawned: `namedPythonInterpreter` resolves it against the working
 * directory, and on Windows that gives this POSIX-shaped fixture a drive letter and
 * backslashes. Written as a literal, this case was green on Linux and red on the Windows leg —
 * an expectation that only holds on the host that wrote it. Resolving it here applies the same
 * documented rule the code applies, and is a different function from the one under test.
 */
test("a bundled interpreter that will not start is reported as the interpreter, not as a missing Install", async () => {
	const bundled = "/nonexistent/resources/python/x86_64-pc-windows-msvc/python.exe";
	const environment = new Map([["VENATOR_BUNDLED_PYTHON", bundled]]);
	const result = await probeRuntimes("default", {
		platform: "linux",
		home: "/tmp",
		workingDirectory: "/tmp",
		checkoutRoot: null,
		environment: (name) => environment.get(name),
	});
	assert.equal(result.probe.available, false);
	assert.ok(
		result.probe.message?.includes(resolve("/tmp", bundled)),
		`the failure does not name the interpreter it could not start: ${String(result.probe.message)}`,
	);
	assert.ok(
		!result.probe.message?.includes("not part of this Install"),
		`the failure still claims the check is absent: ${String(result.probe.message)}`,
	);
});

spawningTest("no flag asks about the default runtime only; --all has to be asked for by name", async () => {
	const context = stubProbe(RECORD_ARGUMENTS);
	const record = join(context.home, "argv");
	try {
		await probeRuntimes("default", context);
		await probeRuntimes("all", context);
		await probeRuntimes({ lane: "codex" }, context);
		const lines = readFileSync(record, "utf8").trim().split("\n");
		// Each line is the module the interpreter resolved, then the arguments it
		// was left holding. `-m venator.llm.probe` is not in the text because the
		// interpreter consumed it — and `__spec__.name` reading back as
		// `venator.llm.probe` is what proves it was there, more directly than the
		// string compare this used to do: a module that did not run cannot report
		// its own name.
		assert.equal(lines[0], "venator.llm.probe --json");
		assert.ok(!(lines[0] ?? "").includes("--all"), "the default request must not probe every runtime");
		assert.equal(lines[1], "venator.llm.probe --all --json");
		assert.equal(lines[2], "venator.llm.probe --lane codex --json");
	} finally {
		rmSync(context.home, { recursive: true, force: true });
	}
});

spawningTest("a lane answer reaches the caller intact through a real subprocess", async () => {
	// The document goes in a file rather than into the module's source, so nothing
	// in it has to survive a round of shell or source-literal quoting on the way
	// to the child. The lane's own words are the thing under test.
	const context = stubProbe(ANSWER_FROM_FILE);
	writeFileSync(join(context.home, "answer.json"), JSON.stringify({ lanes: [CODEX_LANE] }), "utf8");
	try {
		const result = await probeRuntimes({ lane: "codex" }, context);
		assert.equal(result.probe.available, true);
		assert.equal(result.lanes[0]?.caveat, CODEX_LANE.caveat);
		assert.equal(result.lanes[0]?.remedy, CODEX_LANE.remedy);
	} finally {
		rmSync(context.home, { recursive: true, force: true });
	}
});

spawningTest("a usage error is treated as this dashboard's own bug, not as a runtime state", async () => {
	const context = stubProbe(USAGE_ERROR);
	try {
		const result = await probeRuntimes("default", context);
		assert.equal(result.probe.available, false);
		assert.equal(result.probe.reason, "failed");
	} finally {
		rmSync(context.home, { recursive: true, force: true });
	}
});
