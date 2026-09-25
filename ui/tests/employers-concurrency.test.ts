/**
 * Two registrations arriving together, against a server that is actually listening.
 *
 * **The defect.** `registerEmployers` reads `targeting.yaml`, computes the edit, hands the
 * candidate to the pipeline's own loader, and renames its staging file over the target. The
 * load-back is `await`ed, and an `await` is a yield: two requests both read the same file,
 * both decide, and both rename. The second write wins, the first employer is gone, and both
 * requests answered `200` with `"changed": true` and their own board under `"added"` —
 * because each answer is read back out of that request's own stale pre-read rather than out
 * of the file. A route reporting a write that did not happen, on the one route that exists to
 * rescue somebody stranded with no employer registered.
 *
 * **Why it is driven through a listening server rather than by calling the function twice.**
 * The shipped screen disables its button while a request is in flight, so the window cannot be
 * opened from one tab. Two tabs can, and so can anything scripted at the loopback port. The
 * reproduction has to be the thing that is actually possible, which is two sockets — and a
 * server bound to a port is also the only arrangement that proves the request path itself
 * holds no second copy of the ordering.
 *
 * **What is asserted, and why both halves are needed.** The file's final contents *and* every
 * response. A fix that made the file right while still answering `200` for a write that was
 * dropped would be the same defect wearing a different hat, so the outcomes are counted: every
 * board reported as added is in the file, and any request that did not land said so.
 *
 * The port is 0 — the operating system picks one. Nothing here may bind 5170: that is the port
 * a person's own dashboard listens on.
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { after, before, beforeEach, afterEach, test } from "node:test";

import { serve } from "@hono/node-server";

import { createServer } from "../server/app.ts";
import type { LocationContext } from "../server/locations.ts";
import { registerEmployers } from "../server/onboarding/employers.ts";
import { oneWriterPerProfile, writesInFlight } from "../server/onboarding/one-writer.ts";
import { ONBOARDING_REQUEST_HEADER, ONBOARDING_REQUEST_HEADER_VALUE } from "../shared/onboarding.ts";
import { armVerifierStandIn, disarmVerifierStandIn } from "./verifier-stand-in.ts";

const PROFILE = "concurrent";

const TARGETING = [
	"profile:",
	'  name: "concurrent"',
	"sources:",
	"  names: {}",
	"  boards: {}",
	"search:",
	"  queries:",
	'    - "scientist"',
	"",
].join("\n");

const EMPLOYERS: readonly { readonly source: string; readonly board: string; readonly name: string }[] = [
	{ source: "greenhouse", board: "first-employer", name: "First Employer" },
	{ source: "lever", board: "second-employer", name: "Second Employer" },
	{ source: "greenhouse", board: "third-employer", name: "Third Employer" },
];

let home = "";
/** A working directory with no `profiles/` in it, so the checkout leg finds nothing. */
let elsewhere = "";
let restoreHome: string | undefined;
let restorePython: string | undefined;

/** The one server for the whole file, and the address the cases post to. */
let base = "";
let listening: ReturnType<typeof serve> | null = null;

function targetingPath(): string {
	return join(home, "profiles", PROFILE, "targeting.yaml");
}

/**
 * One answer, read only as far as these cases look at it.
 *
 * Deliberately not the response types from `shared/onboarding.ts`: the point of driving a real
 * socket is that nothing here assumes the server answered in the shape it was supposed to, so
 * every field is optional and every case says what it expected to find.
 */
type Answer = {
	readonly status: number;
	readonly body: {
		readonly changed?: boolean;
		readonly added?: readonly { readonly board?: string }[];
		readonly error?: { readonly code?: string };
	};
};

async function register(board: (typeof EMPLOYERS)[number]): Promise<Answer> {
	const response = await fetch(`${base}/api/onboarding/employers`, {
		method: "POST",
		headers: {
			"content-type": "application/json",
			[ONBOARDING_REQUEST_HEADER]: ONBOARDING_REQUEST_HEADER_VALUE,
		},
		body: JSON.stringify({ profile: PROFILE, boards: [board] }),
	});
	// SAFETY: every answer this surface gives is a JSON object — the success shape or the
	// refusal shape — and `Answer` reads only the few fields these cases assert on, each of
	// them optional, so a shape that is not what was expected fails a case rather than throws.
	const body = (await response.json()) as Answer["body"];
	return { status: response.status, body };
}

/**
 * An Install rooted at one spelling of the application data directory.
 *
 * The cases above drive a socket, and a socket cannot carry this: one server process reads one
 * `$VENATOR_HOME`, so two spellings of it in flight together is a `LocationContext` each. What
 * is under test is the queue's key rather than the request path, and the key is computed from
 * the directory these produce, so this is the same two writers arriving together by the same
 * route — named twice.
 */
function installAt(root: string): LocationContext {
	const environment = new Map([
		["VENATOR_HOME", root],
		["VENATOR_PYTHON", process.env["VENATOR_PYTHON"] ?? process.execPath],
	]);
	return {
		platform: process.platform,
		home: root,
		workingDirectory: elsewhere,
		checkoutRoot: null,
		environment: (name) => environment.get(name),
	};
}

/** The boards a response claims it put in the file, whether it landed or not. */
function claimedAdded(answer: Answer): readonly string[] {
	return (answer.body.added ?? []).map((entry) => entry.board ?? "");
}

before(async () => {
	const standIn = armVerifierStandIn();
	elsewhere = mkdtempSync(join(tmpdir(), "venator-elsewhere-"));
	restorePython = process.env["VENATOR_PYTHON"];
	process.env["VENATOR_PYTHON"] = standIn.get("VENATOR_PYTHON") ?? process.execPath;
	// The port is not known until the socket is bound, and `serve` binds on the next tick.
	base = await new Promise<string>((bound) => {
		listening = serve({ fetch: createServer().fetch, hostname: "127.0.0.1", port: 0 }, (address) => {
			bound(`http://127.0.0.1:${String(address.port)}`);
		});
	});
});

after(() => {
	listening?.close();
	rmSync(elsewhere, { recursive: true, force: true });
	disarmVerifierStandIn();
	if (restorePython === undefined) delete process.env["VENATOR_PYTHON"];
	else process.env["VENATOR_PYTHON"] = restorePython;
});

beforeEach(() => {
	home = mkdtempSync(join(tmpdir(), "venator-home-"));
	restoreHome = process.env["VENATOR_HOME"];
	process.env["VENATOR_HOME"] = home;
	mkdirSync(join(home, "profiles", PROFILE), { recursive: true });
	writeFileSync(targetingPath(), TARGETING);
});

afterEach(() => {
	if (restoreHome === undefined) delete process.env["VENATOR_HOME"];
	else process.env["VENATOR_HOME"] = restoreHome;
	rmSync(home, { recursive: true, force: true });
});

test("two registrations sent at once both reach the file, and both answers are true", async () => {
	const [first, second] = await Promise.all([register(EMPLOYERS[0]!), register(EMPLOYERS[1]!)]);

	const file = readFileSync(targetingPath(), "utf8");
	for (const answer of [first, second]) {
		assert.equal(answer.status, 200, `an answer was ${String(answer.status)}: ${JSON.stringify(answer.body)}`);
		for (const board of claimedAdded(answer)) {
			// The whole property: what a 200 says it added is in the file. Before the queue,
			// one of these two was answered `"changed": true` for a write the other overwrote.
			assert.ok(file.includes(board), `${board} was reported as registered and is not in the file`);
		}
	}
	assert.ok(file.includes("first-employer"), "the first employer is not in the file");
	assert.ok(file.includes("second-employer"), "the second employer is not in the file");
	assert.ok(file.includes("First Employer"), "the first employer's display name is not in the file");
	assert.ok(file.includes("Second Employer"), "the second employer's display name is not in the file");
});

test("and three of them, so the queue is a queue rather than a pair", async () => {
	const answers = await Promise.all(EMPLOYERS.map(register));

	const file = readFileSync(targetingPath(), "utf8");
	for (const answer of answers) {
		assert.equal(answer.status, 200, `an answer was ${String(answer.status)}: ${JSON.stringify(answer.body)}`);
		for (const board of claimedAdded(answer)) {
			assert.ok(file.includes(board), `${board} was reported as registered and is not in the file`);
		}
	}
	for (const employer of EMPLOYERS) {
		assert.ok(file.includes(employer.board), `${employer.board} is not in the file`);
		assert.ok(file.includes(employer.name), `${employer.name} is not in the file`);
	}
});

test("the same employer twice at once lands once and is reported as already registered once", async () => {
	const answers = await Promise.all([register(EMPLOYERS[0]!), register(EMPLOYERS[0]!)]);

	const file = readFileSync(targetingPath(), "utf8");
	const landed = answers.filter((answer) => answer.body.changed === true);
	const already = answers.filter((answer) => answer.body.changed === false);
	assert.equal(landed.length, 1, "exactly one of the two writes should have changed anything");
	assert.equal(already.length, 1, "the other should have been told it was already registered");
	// Once, not twice: the second request read the file the first one wrote.
	assert.equal(file.split("first-employer").length - 1, 2, `the board is not registered exactly once:\n${file}`);
});

test("the queue holds one entry per write in flight, not one per Profile ever named", async () => {
	// Both halves are needed, and the first is the one that says a queue is there at all: an
	// empty map is what a *removed* queue reports too, so "nothing is left behind afterwards"
	// passes against `oneWriterPerProfile` replaced by `return work()`. So a write is held
	// open first and the entry has to be in the map while it runs.
	let release = (): void => {};
	const running = new Promise<void>((resume) => {
		release = resume;
	});
	const held = oneWriterPerProfile(join(home, "profiles", PROFILE), () => running);
	assert.equal(writesInFlight(), 1, "a write that is deliberately still running is not in the queue");
	release();
	await held;

	await Promise.all(EMPLOYERS.map(register));
	// The entry is dropped a microtask after the last write for that Profile settles, so this
	// is read after yielding rather than on the same tick. A queue that only ever grew would
	// pass every case above and leak one entry per Profile name for the life of the process.
	await new Promise<void>((yielded) => {
		setImmediate(() => {
			yielded();
		});
	});

	assert.equal(writesInFlight(), 0);
});

/**
 * One directory, two spellings — and two of the three platforms this ships to spell it twice.
 *
 * `validateProfileName` accepts `concurrent` and `Concurrent` as two names, and a byte-exact
 * queue key holds them as two queues. On APFS and on NTFS they are one directory, so the two
 * writes read the same `targeting.yaml`, both rename over it, and one employer is gone with
 * both requests answering `200` — the defect the first case in this file closes, walking back
 * in through the spelling.
 *
 * The shipped screen cannot produce the pair: it sends the name it read off a
 * `DiscoveredProfile`, one canonical spelling. It takes something hand-written at the loopback
 * port, which is the threat model this file adopts at the top.
 *
 * This filesystem is case-sensitive and cannot be made otherwise here, so the *consequence* is
 * reproduced rather than the platform: two Install roots whose strings differ only in case and
 * which are one directory underneath. Where the filesystem already compares that way it is
 * true with nothing arranged; where it does not, a symbolic link makes it true. Either way the
 * writer is the real `registerEmployers` and the file both edit is one file.
 */
test("two spellings of one Install root are one queue, not two", async () => {
	const shouted = join(dirname(home), basename(home).toUpperCase());
	const alreadyOneDirectory = existsSync(join(shouted, "profiles", PROFILE, "targeting.yaml"));
	if (!alreadyOneDirectory) symlinkSync(home, shouted);
	try {
		const answers = await Promise.all([
			registerEmployers(PROFILE, [EMPLOYERS[0]!], installAt(home)),
			registerEmployers(PROFILE, [EMPLOYERS[1]!], installAt(shouted)),
		]);

		const file = readFileSync(targetingPath(), "utf8");
		for (const answer of answers) {
			for (const entry of answer.added) {
				assert.ok(file.includes(entry.board), `${entry.board} was reported as registered and is not in the file`);
			}
		}
		assert.ok(file.includes("first-employer"), "the first employer is not in the file");
		assert.ok(file.includes("second-employer"), "the second employer is not in the file");
	} finally {
		// The link, never the directory it points at: `afterEach` takes the real one.
		if (!alreadyOneDirectory) rmSync(shouted, { force: true });
	}
});

/**
 * And a trailing dot, which is the other way one directory gets two spellings.
 *
 * Windows drops one from a directory name, so `concurrent.` and `concurrent` are one Profile
 * there. That cannot be arranged on this filesystem the way the case above is: a link named
 * `concurrent.` standing beside the Profile is refused as a link before anything is written
 * through it, and rightly. So this asks the queue itself, which is where the spelling is
 * decided — the two writes are held apart and their order is what is asserted, because
 * "the second one waits" is the property and the map's size is only how it looks from outside.
 */
test("a trailing dot is the same queue too, because one platform drops it", async () => {
	const directory = join(home, "profiles", PROFILE);
	const order: string[] = [];
	let release = (): void => {};
	const running = new Promise<void>((resume) => {
		release = resume;
	});

	const first = oneWriterPerProfile(directory, async () => {
		await running;
		order.push("first");
	});
	const second = oneWriterPerProfile(`${directory}.`, () => {
		order.push("second");
		return Promise.resolve();
	});
	assert.equal(writesInFlight(), 1, "the two spellings are queued apart, so neither waits on the other");
	release();
	await Promise.all([first, second]);

	// Against a byte-exact key the second write never waits, and lands first.
	assert.deepEqual(order, ["first", "second"]);
});

test("a symbolic-link spelling of one Profile directory is the same queue", async () => {
	const physicalRoot = mkdtempSync(join(tmpdir(), "venator-physical-root-"));
	const linkedRoot = `${physicalRoot}-link`;
	symlinkSync(physicalRoot, linkedRoot);
	// Neither suffix exists yet. The queue must identify the shared existing root.
	const physicalDirectory = join(physicalRoot, "profiles", PROFILE);
	const linkedDirectory = join(linkedRoot, "profiles", PROFILE);
	const order: string[] = [];
	let release = (): void => {};
	const running = new Promise<void>((resume) => {
		release = resume;
	});

	try {
		const first = oneWriterPerProfile(physicalDirectory, async () => {
			await running;
			order.push("first");
		});
		const second = oneWriterPerProfile(linkedDirectory, () => {
			order.push("second");
			return Promise.resolve();
		});
		assert.equal(writesInFlight(), 1, "two paths to one Profile directory were queued apart");
		release();
		await Promise.all([first, second]);
		assert.deepEqual(order, ["first", "second"]);
	} finally {
		rmSync(linkedRoot, { force: true });
		rmSync(physicalRoot, { recursive: true, force: true });
	}
});

/**
 * The Profile write had the same window, and the same fix closes it.
 *
 * `writeProfile` checks whether the Profile is already there, stages three files, `await`s the
 * load-back, and then renames. Two creations of the same name arriving together both saw an
 * absent Profile, both staged, and both renamed: one `201` was for a Profile that the other
 * request then replaced, and neither person was told. `overwrite: false` says outright that
 * replacing is not what was asked for, so exactly one of the two must be refused with the
 * answer that already exists for it.
 */
test("two creations of the same Profile at once are one write and one honest refusal", async () => {
	const proposal = {
		name: "raced",
		overwrite: false,
		resume: {
			name: "A Person",
			contact: { location: "Boston, MA", phone: "555-0100", email: "a@b.co", linkedin: "linkedin.com/in/x" },
		},
		constraints: { screening: { how_heard: null } },
		targeting: { search: { queries: ["raced"] } },
	};
	const create = async (): Promise<Answer> => {
		const response = await fetch(`${base}/api/onboarding/profile`, {
			method: "POST",
			headers: {
				"content-type": "application/json",
				[ONBOARDING_REQUEST_HEADER]: ONBOARDING_REQUEST_HEADER_VALUE,
			},
			body: JSON.stringify(proposal),
		});
		// SAFETY: as above — JSON on every path, read only as far as this case asserts on it.
		return { status: response.status, body: (await response.json()) as Answer["body"] };
	};

	const answers = await Promise.all([create(), create()]);

	const created = answers.filter((answer) => answer.status === 201);
	const refused = answers.filter((answer) => answer.status === 409);
	assert.equal(created.length, 1, `statuses were ${answers.map((answer) => String(answer.status)).join(", ")}`);
	assert.equal(refused.length, 1, "the second creation should have been told the name is taken");
	assert.ok(existsSync(join(home, "profiles", "raced", "targeting.yaml")), "the Profile that was created is not there");
	// One Profile of that name, and no staging directory left behind by the request that lost.
	// `PROFILE` is the one every case in this file starts with; nothing else may be there.
	assert.deepEqual([...readdirSync(join(home, "profiles"))].sort(), [PROFILE, "raced"].sort());
});
