/**
 * The board resolver, read through a stand-in for the pipeline module.
 *
 * The resolving itself belongs to `venator.discover.register` and answers for the network the
 * machine is on, so what is testable here is the seam: that a value which is not a web address
 * never becomes a subprocess argument, that the pipeline's own wording for an unresolvable
 * paste survives unchanged, that an employer name is never invented, and that an Install with
 * no pipeline in it says so rather than looking broken.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import { readResolverDocument, resolveBoards, validatePastedUrl } from "../server/onboarding/boards.ts";
import { OnboardingError } from "../server/onboarding/errors.ts";

/** The file `--require` loads inside the stand-in, beside the temporary directory. */
const PRELOAD = "stand-in.cjs";

/**
 * A stand-in pipeline that is a real, startable program on every host.
 *
 * This used to be a `#!/bin/sh` file with no extension, which is three separate
 * things Windows cannot do: it honours no shebang, `CreateProcess` starts nothing
 * without an extension, and `chmod` means nothing there. So on Windows the child
 * never started, `resolveBoards` took its *launch-failed* branch, and the two cases
 * below stopped testing what they say they test — one went red, and the other went
 * green for the wrong reason, because `boards.ts` deliberately reports a launch
 * failure and a pipeline-missing exit as the same answer.
 *
 * What replaces it is this process's own `node`, which is already an executable
 * Windows will start and is already absolute, told what to do through `--require`.
 * The resolver is spawned as `<interpreter> <resolve_board.py> <url>` — an argument
 * list with no options in it — so node preloads the stand-in, and the stand-in
 * answers and exits before node ever tries to read a `.py` file as JavaScript. The
 * code under test still launches a real process and reads its real stdout, stderr
 * and exit code: the ran-and-exited branch, on both hosts.
 */
function stubResolver(answer: { stdout?: string; stderr?: string; exit: number }): LocationContext {
	const directory = mkdtempSync(join(tmpdir(), "venator-boards-"));
	// `fs.writeSync` on the descriptors rather than `process.stdout.write`: writes
	// to a pipe are asynchronous on POSIX, so a stream write followed by
	// `process.exit` is allowed to lose what it wrote. A stand-in that answered
	// only sometimes would be worse than one that never answered.
	const body = [
		'const { writeSync } = require("node:fs");',
		`writeSync(1, ${JSON.stringify(answer.stdout ?? "")});`,
		`writeSync(2, ${JSON.stringify(answer.stderr ?? "")});`,
		`process.exit(${answer.exit});`,
		"",
	].join("\n");
	writeFileSync(join(directory, PRELOAD), body);
	const environment = new Map([["VENATOR_PYTHON", process.execPath]]);
	return {
		platform: "linux",
		home: directory,
		workingDirectory: directory,
		checkoutRoot: null,
		environment: (name) => environment.get(name),
	};
}

/**
 * Run `work` with the stand-in armed, and disarm it again whatever happens.
 *
 * `spawn` here passes no `env`, so the child inherits this process's, which is the
 * only channel a preload can arrive through.
 *
 * The path is rewritten with forward slashes first, and that is not cosmetic:
 * `NODE_OPTIONS` **eats backslashes**. Measured, because the first version of this
 * asserted the opposite —
 *
 *     NODE_OPTIONS='--require "/tmp/back\slash dir/stand-in.cjs"' node …
 *     Error: Cannot find module '/tmp/backslash dir/stand-in.cjs'
 *
 * — which on Windows, where every temporary path is backslashes, would have
 * silently failed to preload and sent every case here down the launch-failed
 * branch again. Node resolves a forward-slashed Windows path perfectly well, and
 * quoting still covers a space. `node --test` runs the cases in one file one
 * after another, so nothing else is spawning while this is set.
 */
async function withStandIn<T>(context: LocationContext, work: () => Promise<T>): Promise<T> {
	const preload = join(context.home, PRELOAD).replaceAll("\\", "/");
	assert.ok(!preload.includes('"'), "a quote in the temporary path would break NODE_OPTIONS");
	assert.ok(!preload.includes("\\"), "a backslash would be eaten by the NODE_OPTIONS parser");
	const before = process.env["NODE_OPTIONS"];
	process.env["NODE_OPTIONS"] = `--require "${preload}"`;
	try {
		return await work();
	} finally {
		if (before === undefined) delete process.env["NODE_OPTIONS"];
		else process.env["NODE_OPTIONS"] = before;
		rmSync(context.home, { recursive: true, force: true });
	}
}

test("a value that is not a web address never reaches a command line", () => {
	for (const attempt of ["", "greenhouse.io/acme", "javascript:alert(1)", "file:///etc/passwd", "-x", "a b"]) {
		assert.throws(() => validatePastedUrl(attempt), OnboardingError, attempt);
	}
	assert.equal(validatePastedUrl("  https://boards.greenhouse.io/acme  "), "https://boards.greenhouse.io/acme");
});

test("a resolved board keeps its source, token, route and posting count", () => {
	const resolved = readResolverDocument(
		JSON.stringify({
			url: "https://www.example.com/careers",
			titleHint: "Careers at Example",
			boards: [
				{ source: "ashby", board: "example", route: "careers page", confirmed: true, postingCount: 12 },
				{ source: "workday", board: "example.wd1~Careers", route: "robots.txt", confirmed: false, postingCount: 0 },
			],
		}),
	);
	assert.equal(resolved.boards.length, 2);
	assert.equal(resolved.boards[0]?.board, "example");
	assert.equal(resolved.boards[0]?.postingCount, 12);
	// A board that did not answer is reported as unconfirmed and kept, never dropped.
	assert.equal(resolved.boards[1]?.confirmed, false);
	assert.equal(resolved.boards[1]?.board, "example.wd1~Careers");
});

test("the page title is a hint and never an employer name", () => {
	const resolved = readResolverDocument(
		JSON.stringify({ url: "https://x.example/careers", titleHint: "Careers | Example, Inc.", boards: [] }),
	);
	// It arrives as `titleHint` and there is no field on a board for a name at all: the
	// employer display name is the Owner's to type, and `register.py` refuses to invent one.
	assert.equal(resolved.titleHint, "Careers | Example, Inc.");
	assert.deepEqual([...resolved.boards], []);
});

test("a bounded source count remains explicitly incomplete across the resolver", () => {
	for (const complete of [false, null, "unknown"]) {
		const resolved = readResolverDocument(JSON.stringify({
			url: "https://example.wd1.myworkdayjobs.com/Careers", titleHint: null,
			boards: [{ source: "workday", board: "example.wd1~Careers", confirmed: true, postingCount: 20, complete }],
		}));
		assert.equal(resolved.boards[0]?.confirmed, true);
		assert.equal(resolved.boards[0]?.postingCount, 20);
		assert.equal(resolved.boards[0]?.complete, false);
	}
});

test("the pipeline's own wording for an unresolvable paste is passed through", () => {
	const message = "https://x.example named no Greenhouse, Lever, Ashby or Workday board.";
	try {
		readResolverDocument(JSON.stringify({ failure: { kind: "unresolvable", message } }));
		assert.fail("expected a refusal");
	} catch (error) {
		assert.ok(error instanceof OnboardingError);
		assert.equal(error.code, "unresolvable_url");
		assert.equal(error.message, message);
		assert.equal(error.field, "url");
	}
});

test("a page that could not be reached is separate from a page that named no board", () => {
	try {
		readResolverDocument(JSON.stringify({ failure: { kind: "unreachable", message: "That page could not be reached." } }));
		assert.fail("expected a refusal");
	} catch (error) {
		assert.ok(error instanceof OnboardingError);
		assert.equal(error.code, "resolver_unavailable");
		assert.equal(error.status, 503);
	}
});

test("an answer this dashboard cannot describe is named rather than guessed at", () => {
	for (const document of ['{"boards":[{"source":"ashby"}]}', '{"boards":"lots"}', "not json at all"]) {
		assert.throws(() => readResolverDocument(document), OnboardingError, document);
	}
});

test("an Install with no pipeline in it says so rather than failing", async () => {
	// Exit 3 is `boards.ts`'s PIPELINE_MISSING_EXIT: an interpreter that started
	// and could not import `venator`. That is the Install with no pipeline in it,
	// and it keeps this wording — which is why this case has to reach the child at
	// all. A launch failure is a different fact and now says so when an
	// interpreter was named; the case below is that one.
	const context = stubResolver({ stderr: "No module named venator\n", exit: 3 });
	await withStandIn(context, async () => {
		try {
			await resolveBoards("https://boards.greenhouse.io/acme", context);
			assert.fail("expected a refusal");
		} catch (error) {
			assert.ok(error instanceof OnboardingError);
			assert.equal(error.code, "resolver_unavailable");
			assert.match(error.message, /not part of this Install/u);
		}
	});
});

test("a resolver that cannot be launched at all is the same kind of answer", async () => {
	const environment = new Map([["VENATOR_PYTHON", "/nonexistent/python"]]);
	try {
		await resolveBoards("https://boards.greenhouse.io/acme", {
			platform: "linux",
			home: "/tmp",
			workingDirectory: "/tmp",
			checkoutRoot: null,
			environment: (name) => environment.get(name),
		});
		assert.fail("expected a refusal");
	} catch (error) {
		assert.ok(error instanceof OnboardingError);
		assert.equal(error.code, "resolver_unavailable");
	}
});

/**
 * The desktop bundle ships an interpreter and the Tauri host names it in
 * `VENATOR_BUNDLED_PYTHON`, so on a Windows Install with no system Python the resolver is part
 * of the Install and an interpreter that will not start is the thing that failed. Saying the
 * pipeline is absent there sends the person to fix something that is already installed.
 */
test("an interpreter the bundle named and could not start is reported as the interpreter", async () => {
	const bundled = "/nonexistent/resources/python/x86_64-pc-windows-msvc/python.exe";
	const environment = new Map([["VENATOR_BUNDLED_PYTHON", bundled]]);
	try {
		await resolveBoards("https://boards.greenhouse.io/acme", {
			platform: "linux",
			home: "/tmp",
			workingDirectory: "/tmp",
			checkoutRoot: null,
			environment: (name) => environment.get(name),
		});
		assert.fail("expected a refusal");
	} catch (error) {
		assert.ok(error instanceof OnboardingError);
		assert.equal(error.code, "resolver_unavailable");
		// Resolved rather than typed: `namedPythonInterpreter` resolves the environment's value
		// against the working directory, so on Windows this POSIX fixture is spawned as
		// `D:\nonexistent\...`. A literal here was green on Linux and red on the Windows leg.
		assert.ok(
			error.message.includes(resolve("/tmp", bundled)),
			`the refusal does not name the interpreter: ${error.message}`,
		);
		assert.doesNotMatch(error.message, /not part of this Install/u);
	}
});

test("a resolver that answers with a board is read end to end", async () => {
	const document = JSON.stringify({
		url: "https://boards.greenhouse.io/acme",
		titleHint: null,
		boards: [{ source: "greenhouse", board: "acme", route: "pasted URL", confirmed: true, postingCount: 4 }],
	});
	const context = stubResolver({ stdout: document, exit: 0 });
	await withStandIn(context, async () => {
		const resolved = await resolveBoards("https://boards.greenhouse.io/acme", context);
		assert.equal(resolved.boards[0]?.source, "greenhouse");
		assert.equal(resolved.boards[0]?.board, "acme");
		assert.equal(resolved.titleHint, null);
	});
});
