/**
 * A stand-in for the resume reader, for the tests that drive `POST /resume/import` with a PDF.
 *
 * The reading itself belongs to `venator.resume.parse` — a PDF parser and, on one of the two
 * paths, a completion on somebody's own subscription. Neither is available on a runner and
 * **neither may ever be reached by a test**, so what is testable here is the seam: which
 * arguments the adapter is spawned with, what arrives on its stdin, and what each answer it can
 * give turns into on the wire.
 *
 * The technique is `tests/verifier-stand-in.ts`'s and `tests/boards.test.ts`'s, for the reason
 * stated at length there: a `#!/bin/sh` file with no extension is three things Windows cannot
 * do, so the stand-in is this process's own `node` — already executable, already absolute —
 * armed through `NODE_OPTIONS=--require`. The reader is spawned as
 * `<interpreter> <parse_resume.py> --mode …`, an argument list with no node options in it, so
 * node preloads the stand-in and the stand-in answers and exits before node tries to read a
 * `.py` file as JavaScript. The code under test still launches a real process and reads its
 * real stdout, stderr and exit code.
 *
 * What it adds over the verifier's stand-in is the recording. This route's whole safety
 * property is *which mode was asked for*, and the only way to assert that a request with no
 * `parse` field never asks for a completion is to look at the argument list the adapter was
 * actually given.
 */

import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** What the stand-in prints, what it exits with, and where it records what it was given. */
export const READER_ANSWER = "VENATOR_STAND_IN_READER";
export const READER_EXIT = "VENATOR_STAND_IN_READER_EXIT";
export const READER_RECORD = "VENATOR_STAND_IN_READER_RECORD";
/**
 * How long the stand-in stays alive after recording, in milliseconds.
 *
 * A reading in flight is a real child process that has not exited yet, and that is the only
 * state the concurrency guard has anything to say about — so the stand-in is given a way to be
 * slow: it records what it was given, waits, and only then answers. The wait is `Atomics.wait`
 * rather than a timer because the stand-in is a preload that ends in `process.exit`, and a
 * timer would need the event loop it is about to leave.
 */
export const READER_DELAY = "VENATOR_STAND_IN_READER_DELAY";

const PRELOAD = "reader-stand-in.cjs";
const ARGV_FILE = "argv.txt";
const STDIN_FILE = "stdin.bin";

let armed: { readonly directory: string; readonly before: string | undefined; readonly python: string | undefined } | null =
	null;

/**
 * What the adapter was asked, as the process actually got it.
 *
 * `argv` starts at the script path, so `argv[0]` is the `.py` file the server addressed by
 * path and everything after it is the option list this side built.
 */
export type ReaderCall = {
	readonly argv: readonly string[];
	readonly stdin: Uint8Array;
};

/**
 * Arms the stand-in and points `pythonInterpreter` at it for the whole of one test file.
 *
 * `VENATOR_PYTHON` is set on this process's own environment rather than handed back as a
 * `LocationContext`, because the route under test resolves `systemContext()` for itself —
 * which is the thing being tested, and faking it would test the fake.
 */
export function armReaderStandIn(): void {
	const directory = mkdtempSync(join(tmpdir(), "venator-reader-"));
	// `fs.writeSync` on the descriptor rather than `process.stdout.write`: a write to a pipe is
	// asynchronous on POSIX, so a stream write followed by `process.exit` is allowed to lose
	// what it wrote, and a stand-in that answered only sometimes would be worse than none.
	writeFileSync(
		join(directory, PRELOAD),
		[
			'const { readFileSync, writeFileSync, writeSync } = require("node:fs");',
			'const { join } = require("node:path");',
			`const record = process.env[${JSON.stringify(READER_RECORD)}];`,
			"if (record) {",
			// One argument per line. None of them can hold a newline — a script path, `--mode`,
			// a mode, `--lane` and a lane name — and a text file keeps the reader here free of
			// a JSON parse whose result would have to be narrowed from nothing.
			`  writeFileSync(join(record, ${JSON.stringify(ARGV_FILE)}), process.argv.slice(1).join("\\n"));`,
			// Read to EOF, which is what the adapter itself does. A parent that wrote nothing
			// closes the pipe and this is an empty buffer; a parent that died leaves an error,
			// and an unrecorded stdin is a better answer than a stand-in that hangs.
			"  let body = Buffer.alloc(0);",
			"  try { body = readFileSync(0); } catch {}",
			`  writeFileSync(join(record, ${JSON.stringify(STDIN_FILE)}), body);`,
			"}",
			// Recorded first, held second: a test that waits for the argv file to appear is then
			// waiting for a child that is alive rather than for one that is already gone.
			`const held = Number(process.env[${JSON.stringify(READER_DELAY)}] ?? "0");`,
			"if (held > 0) { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, held); }",
			`writeSync(1, (process.env[${JSON.stringify(READER_ANSWER)}] ?? '{"outcome":"no_runtime"}') + "\\n");`,
			`process.exit(Number(process.env[${JSON.stringify(READER_EXIT)}] ?? "0"));`,
			"",
		].join("\n"),
	);
	// NODE_OPTIONS eats backslashes, which every Windows temporary path is made of, so the path
	// goes in forward-slashed. Node resolves one perfectly well on either host.
	const preload = join(directory, PRELOAD).replaceAll("\\", "/");
	const before = process.env["NODE_OPTIONS"];
	const python = process.env["VENATOR_PYTHON"];
	process.env["NODE_OPTIONS"] = `--require "${preload}"`;
	process.env["VENATOR_PYTHON"] = process.execPath;
	process.env[READER_RECORD] = directory;
	armed = { directory, before, python };
}

/** Disarms it and takes the scratch directory with it. */
export function disarmReaderStandIn(): void {
	if (armed === null) return;
	if (armed.before === undefined) delete process.env["NODE_OPTIONS"];
	else process.env["NODE_OPTIONS"] = armed.before;
	if (armed.python === undefined) delete process.env["VENATOR_PYTHON"];
	else process.env["VENATOR_PYTHON"] = armed.python;
	rmSync(armed.directory, { recursive: true, force: true });
	armed = null;
	delete process.env[READER_ANSWER];
	delete process.env[READER_EXIT];
	delete process.env[READER_RECORD];
	delete process.env[READER_DELAY];
}

/** Forgets the last call, so "was the adapter spawned at all?" is a question with an answer. */
export function forgetReaderCall(): void {
	if (armed === null) return;
	rmSync(join(armed.directory, ARGV_FILE), { force: true });
	rmSync(join(armed.directory, STDIN_FILE), { force: true });
}

/** What the adapter was last spawned with, or null when it was never spawned. */
export function lastReaderCall(): ReaderCall | null {
	if (armed === null) return null;
	let argv: readonly string[];
	try {
		argv = readFileSync(join(armed.directory, ARGV_FILE), "utf8").split("\n");
	} catch {
		return null;
	}
	let stdin = new Uint8Array(0);
	try {
		stdin = new Uint8Array(readFileSync(join(armed.directory, STDIN_FILE)));
	} catch {
		stdin = new Uint8Array(0);
	}
	return { argv, stdin };
}

/** Runs `work` with the adapter answering `answer` instead of `{"outcome":"no_runtime"}`. */
export async function withReaderAnswer<T>(answer: string, work: () => Promise<T>): Promise<T> {
	process.env[READER_ANSWER] = answer;
	forgetReaderCall();
	try {
		return await work();
	} finally {
		delete process.env[READER_ANSWER];
	}
}

/** Runs `work` with the adapter exiting `code` — 3 is an Install with no pipeline in it. */
export async function withReaderExit<T>(code: number, work: () => Promise<T>): Promise<T> {
	process.env[READER_EXIT] = String(code);
	forgetReaderCall();
	try {
		return await work();
	} finally {
		delete process.env[READER_EXIT];
	}
}

/**
 * Runs `work` with the adapter holding for `held` milliseconds before it answers.
 *
 * The scratch files are deliberately **not** forgotten here, unlike the two above: a test that
 * holds a reading open watches the argv file appear and then clears it itself, so that "was a
 * second child spawned?" is a question with an answer.
 */
export async function withReaderDelay<T>(held: number, work: () => Promise<T>): Promise<T> {
	process.env[READER_DELAY] = String(held);
	try {
		return await work();
	} finally {
		delete process.env[READER_DELAY];
	}
}
