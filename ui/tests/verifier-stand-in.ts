/**
 * A stand-in for the Profile verifier, for the two test files that write Profiles.
 *
 * `writeProfile` and `registerEmployers` now hand what they have staged to
 * `onboarding/verify_profile.py` before the rename that makes it real, and they refuse the
 * write when the pipeline cannot read it back. Those two files are about the *filesystem*
 * behaviour — where a Profile lands, what a symbolic link does, what survives an interruption
 * — and they run in `pnpm test`, on a runner that has no `venator` on its Python path. So the
 * subprocess is real and its answer is staged.
 *
 * The technique is `tests/boards.test.ts`'s, for the reason stated at length there: a
 * `#!/bin/sh` file with no extension is three things Windows cannot do, so the stand-in is
 * this process's own `node` — already executable, already absolute — told what to say through
 * `--require`. The code under test still spawns a real process and reads its real stdout,
 * stderr and exit code.
 *
 * `answer` is read from the environment at each launch rather than baked into the file, so one
 * armed stand-in serves a whole file and a single case can make the loader refuse.
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** What the stand-in prints, and what it exits with. Read by the preload at launch. */
export const VERIFIER_ANSWER = "VENATOR_STAND_IN_VERIFY";
export const VERIFIER_EXIT = "VENATOR_STAND_IN_EXIT";

const PRELOAD = "stand-in.cjs";

let armed: { readonly directory: string; readonly before: string | undefined } | null = null;

/**
 * Arms the stand-in for the whole of one test file, and answers with the environment entries a
 * `LocationContext` needs so `pythonInterpreter` resolves to it.
 *
 * The scratch directory it writes into is its own, deliberately: these files assert on the
 * exact contents of the Install root and of the working directory, and a stray file in either
 * would be indistinguishable from the defect those assertions exist to catch.
 */
export function armVerifierStandIn(): ReadonlyMap<string, string> {
	const directory = mkdtempSync(join(tmpdir(), "venator-verifier-"));
	// `fs.writeSync` on the descriptor rather than `process.stdout.write`: a write to a pipe is
	// asynchronous on POSIX, so a stream write followed by `process.exit` is allowed to lose
	// what it wrote, and a stand-in that answered only sometimes would be worse than none.
	writeFileSync(
		join(directory, PRELOAD),
		[
			'const { writeSync } = require("node:fs");',
			`writeSync(1, (process.env[${JSON.stringify(VERIFIER_ANSWER)}] ?? '{"outcome":"loads"}') + "\\n");`,
			`process.exit(Number(process.env[${JSON.stringify(VERIFIER_EXIT)}] ?? "0"));`,
			"",
		].join("\n"),
	);
	// NODE_OPTIONS eats backslashes, which every Windows temporary path is made of, so the
	// path goes in forward-slashed. Node resolves one perfectly well on either host.
	const preload = join(directory, PRELOAD).replaceAll("\\", "/");
	const before = process.env["NODE_OPTIONS"];
	process.env["NODE_OPTIONS"] = `--require "${preload}"`;
	armed = { directory, before };
	return new Map([["VENATOR_PYTHON", process.execPath]]);
}

/** Disarms it and takes the scratch directory with it. */
export function disarmVerifierStandIn(): void {
	if (armed === null) return;
	if (armed.before === undefined) delete process.env["NODE_OPTIONS"];
	else process.env["NODE_OPTIONS"] = armed.before;
	rmSync(armed.directory, { recursive: true, force: true });
	armed = null;
	delete process.env[VERIFIER_ANSWER];
	delete process.env[VERIFIER_EXIT];
}

/** Runs `work` with the pipeline answering `answer` instead of `{"outcome":"loads"}`. */
export async function withVerifierAnswer<T>(answer: string, work: () => Promise<T>): Promise<T> {
	process.env[VERIFIER_ANSWER] = answer;
	try {
		return await work();
	} finally {
		delete process.env[VERIFIER_ANSWER];
	}
}
