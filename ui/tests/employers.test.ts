/**
 * Registering an employer into a Profile that already exists, against a real temporary Install.
 *
 * This is the only place in this repository that writes into a Profile somebody already has,
 * so the cases here are the adversarial ones: a name that tries to leave the Profiles
 * directory, a Profile in a checkout that would shadow the one being edited, a symbolic link
 * standing where a file should be, and a targeting file written in a way the editor refuses.
 * Every one of them ends with an assertion about what is on disk, because a write that
 * reported a refusal and left something behind is the failure worth catching.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, afterEach, before, beforeEach, test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import type { EmployerRegisterResponse } from "../shared/onboarding.ts";
import { MAXIMUM_EMPLOYERS_PER_REQUEST, registerEmployers } from "../server/onboarding/employers.ts";
import { OnboardingError } from "../server/onboarding/errors.ts";
import { armVerifierStandIn, disarmVerifierStandIn, withVerifierAnswer } from "./verifier-stand-in.ts";

let home = "";
let elsewhere = "";
/**
 * The pipeline's answer, staged.
 *
 * `registerEmployers` hands the edited document to `venator.profile` before the rename, and
 * refuses the write when it does not load. These cases are about what reaches the filesystem,
 * they run where no `venator` is importable, and the load-back has its own file — so here the
 * subprocess is real and what it says is arranged.
 */
let standIn: ReadonlyMap<string, string> = new Map();

function install(checkoutRoot: string | null = null): LocationContext {
	const environment = new Map([["VENATOR_HOME", home], ...standIn]);
	return {
		platform: "linux",
		home,
		workingDirectory: elsewhere,
		checkoutRoot,
		environment: (name) => environment.get(name),
	};
}

const TARGETING = ['profile:', '  name: "mine"', "sources:", "  names: {}", "  boards: {}", "search:", "  queries:", '    - "scientist"', ""].join("\n");

function profileAt(root: string, name: string, targeting: string | null = TARGETING): string {
	const directory = join(root, "profiles", name);
	mkdirSync(directory, { recursive: true });
	if (targeting !== null) writeFileSync(join(directory, "targeting.yaml"), targeting);
	return directory;
}

async function refusal(run: () => Promise<EmployerRegisterResponse>): Promise<OnboardingError> {
	try {
		await run();
	} catch (cause) {
		if (cause instanceof OnboardingError) return cause;
		throw cause;
	}
	throw new Error("that was expected to be refused and was not");
}

const GINKGO = { source: "greenhouse", board: "ginkgobioworks", name: "Ginkgo Bioworks" };

before(() => {
	standIn = armVerifierStandIn();
});

after(() => {
	disarmVerifierStandIn();
});

beforeEach(() => {
	home = mkdtempSync(join(tmpdir(), "venator-home-"));
	elsewhere = mkdtempSync(join(tmpdir(), "venator-cwd-"));
});

afterEach(() => {
	rmSync(home, { recursive: true, force: true });
	rmSync(elsewhere, { recursive: true, force: true });
});

test("an employer lands in the Profile's own targeting file and nowhere else", async () => {
	const directory = profileAt(home, "mine");
	const answer = await registerEmployers("mine", [GINKGO], install());
	assert.equal(answer.profile, "mine");
	assert.deepEqual(answer.added, [GINKGO]);
	assert.equal(answer.registered, 1);
	assert.equal(answer.changed, true);
	const written = readFileSync(join(directory, "targeting.yaml"), "utf8");
	assert.match(written, /  names:\n    ginkgobioworks: "Ginkgo Bioworks"\n/u);
	assert.match(written, /  boards:\n    greenhouse:\n      - "ginkgobioworks"\n/u);
	// One file in the Profile, and nothing anywhere else in the Install.
	assert.deepEqual(readdirSync(directory), ["targeting.yaml"]);
	assert.deepEqual(readdirSync(home), ["profiles"]);
	assert.deepEqual(readdirSync(elsewhere), []);
});

test("nothing but sources is touched, and the rest of the file is byte for byte what it was", async () => {
	const directory = profileAt(home, "mine");
	await registerEmployers("mine", [GINKGO], install());
	const written = readFileSync(join(directory, "targeting.yaml"), "utf8").split("\n");
	assert.deepEqual(written.slice(0, 2), ["profile:", '  name: "mine"']);
	assert.deepEqual(written.slice(written.indexOf("search:")), ["search:", "  queries:", '    - "scientist"', ""]);
});

test("registering the same employer twice writes nothing the second time", async () => {
	const directory = profileAt(home, "mine");
	await registerEmployers("mine", [GINKGO], install());
	const once = readFileSync(join(directory, "targeting.yaml"), "utf8");
	const again = await registerEmployers("mine", [GINKGO], install());
	assert.equal(again.changed, false);
	assert.equal(again.added.length, 0);
	assert.deepEqual(again.alreadyRegistered, [GINKGO]);
	// Byte for byte what it was: nothing was added, so `registerEmployers` opened nothing to
	// write, and the token is in the list once rather than twice.
	assert.equal(readFileSync(join(directory, "targeting.yaml"), "utf8"), once);
});

test("a Profile this Install does not have is refused, and no directory is created for it", async () => {
	mkdirSync(join(home, "profiles"), { recursive: true });
	const error = await refusal(() => registerEmployers("absent", [GINKGO], install()));
	assert.equal(error.code, "profile_absent");
	assert.deepEqual(readdirSync(join(home, "profiles")), []);
});

test("the Install's Profile wins over a checkout copy", async () => {
	const directory = profileAt(home, "mine");
	const checkoutCopy = profileAt(elsewhere, "mine");
	const before = readFileSync(join(checkoutCopy, "targeting.yaml"), "utf8");
	await registerEmployers("mine", [GINKGO], install(elsewhere));
	assert.notEqual(readFileSync(join(directory, "targeting.yaml"), "utf8"), before);
	assert.equal(readFileSync(join(checkoutCopy, "targeting.yaml"), "utf8"), before);
});

test("a name that tries to leave the Profiles directory is refused", async () => {
	profileAt(home, "mine");
	for (const name of ["../mine", "..", "/etc", "mine/../../mine", ".hidden"]) {
		const error = await refusal(() => registerEmployers(name, [GINKGO], install()));
		assert.ok(
			error.code === "invalid_profile_name" || error.code === "profile_absent",
			`${name} was refused as ${error.code}`,
		);
	}
});

test("a Profile directory that is a symbolic link is refused, and what it points at is untouched", async () => {
	const real = profileAt(home, "real");
	symlinkSync(real, join(home, "profiles", "linked"));
	const before = readFileSync(join(real, "targeting.yaml"), "utf8");
	const error = await refusal(() => registerEmployers("linked", [GINKGO], install()));
	assert.equal(error.code, "write_failed");
	assert.equal(readFileSync(join(real, "targeting.yaml"), "utf8"), before);
});

test("a targeting file that is a symbolic link is refused, and what it points at is untouched", async () => {
	const directory = profileAt(home, "mine", null);
	const outside = join(home, "somewhere-else.yaml");
	writeFileSync(outside, TARGETING);
	symlinkSync(outside, join(directory, "targeting.yaml"));
	const error = await refusal(() => registerEmployers("mine", [GINKGO], install()));
	assert.equal(error.code, "write_failed");
	assert.equal(readFileSync(outside, "utf8"), TARGETING);
});

test("a directory with no targeting file is not a Profile and is refused", async () => {
	profileAt(home, "mine", null);
	const error = await refusal(() => registerEmployers("mine", [GINKGO], install()));
	assert.equal(error.code, "profile_absent");
});

test("a targeting file written in a way the editor will not touch is reported, not rewritten", async () => {
	const directory = profileAt(home, "mine", "profile:\n  name: mine\nsources: &shared\n  names: {}\n  boards: {}\n");
	const before = readFileSync(join(directory, "targeting.yaml"), "utf8");
	const error = await refusal(() => registerEmployers("mine", [GINKGO], install()));
	assert.equal(error.code, "targeting_unreadable");
	assert.match(error.message, /shape this dashboard will not edit/u);
	assert.equal(readFileSync(join(directory, "targeting.yaml"), "utf8"), before);
	assert.deepEqual(readdirSync(directory), ["targeting.yaml"]);
});

test("a board with no employer name is refused before the file is opened", async () => {
	const directory = profileAt(home, "mine");
	const before = readFileSync(join(directory, "targeting.yaml"), "utf8");
	const error = await refusal(() => registerEmployers("mine", [{ ...GINKGO, name: "  " }], install()));
	assert.equal(error.code, "invalid_profile");
	assert.equal(error.field, "boards.0.name");
	assert.equal(readFileSync(join(directory, "targeting.yaml"), "utf8"), before);
});

test("a board token that is not one is refused, and none of the batch is written", async () => {
	const directory = profileAt(home, "mine");
	const before = readFileSync(join(directory, "targeting.yaml"), "utf8");
	for (const board of ["", "with space", 'with"quote', "with\\backslash", "with:colon", "with/slash", "with#hash"]) {
		const error = await refusal(() => registerEmployers("mine", [GINKGO, { ...GINKGO, board }], install()));
		assert.equal(error.code, "invalid_profile");
		assert.equal(error.field, "boards.1.board");
	}
	assert.equal(readFileSync(join(directory, "targeting.yaml"), "utf8"), before);
});

test("a job board this pipeline could not name is refused", async () => {
	profileAt(home, "mine");
	for (const source of ["Greenhouse", "green-house", "1greenhouse", "green house", ""]) {
		const error = await refusal(() => registerEmployers("mine", [{ ...GINKGO, source }], install()));
		assert.equal(error.field, "boards.0.source");
	}
});

test("no staging directory survives a write, successful or refused", async () => {
	const directory = profileAt(home, "mine");
	await registerEmployers("mine", [GINKGO], install());
	assert.deepEqual(readdirSync(directory), ["targeting.yaml"]);
});

/**
 * The load-back, and the input bounds that keep it from ever being reached.
 *
 * A registration lands only if `src/venator/profile/` reads the edited document back
 * (`verify.ts`), and the check happens between the staging and the rename — so the case that
 * matters most is the refusal: the file that was on disk has to be the file that is still on
 * disk, byte for byte.
 */

test("a registration the pipeline cannot read back leaves the file byte for byte what it was", async () => {
	const directory = profileAt(home, "mine");
	const before = readFileSync(join(directory, "targeting.yaml"), "utf8");
	const error = await withVerifierAnswer('{"outcome":"refused","message":"targeting.yaml is not valid YAML"}', () =>
		refusal(() => registerEmployers("mine", [GINKGO], install())),
	);
	assert.equal(error.code, "unloadable_profile");
	assert.equal(error.status, 422);
	// The loader's sentence names an absolute path inside a staging directory: logged, never
	// answered with.
	assert.ok(!error.message.includes("targeting.yaml"));
	assert.equal(readFileSync(join(directory, "targeting.yaml"), "utf8"), before);
	// And no staging directory was left behind for the next run to trip over.
	assert.deepEqual(readdirSync(directory), ["targeting.yaml"]);
});

test("an Install that cannot read a Profile back registers nothing", async () => {
	const directory = profileAt(home, "mine");
	const before = readFileSync(join(directory, "targeting.yaml"), "utf8");
	process.env["VENATOR_STAND_IN_EXIT"] = "3";
	try {
		const error = await refusal(() => registerEmployers("mine", [GINKGO], install()));
		assert.equal(error.code, "verification_unavailable");
		assert.equal(error.status, 503);
	} finally {
		delete process.env["VENATOR_STAND_IN_EXIT"];
	}
	assert.equal(readFileSync(join(directory, "targeting.yaml"), "utf8"), before);
});

test("four board tokens YAML 1.1 would collapse into three stay four keys", async () => {
	// Bare, `yes`, `no`, `on` and `null` load as `True`, `False`, `True` and `None`: three keys,
	// and one employer gone from `sources.names` without a word. Dedup resolves an ATS Posting's
	// employer through that map, so a board with no entry drops out of every duplicate group.
	const directory = profileAt(home, "mine");
	const tokens = ["yes", "no", "on", "null"];
	const answer = await registerEmployers(
		"mine",
		tokens.map((board) => ({ source: "greenhouse", board, name: `Employer ${board}` })),
		install(),
	);
	assert.equal(answer.added.length, 4);
	const written = readFileSync(join(directory, "targeting.yaml"), "utf8");
	for (const token of tokens) {
		assert.match(written, new RegExp(`^ {4}"${token}": "Employer ${token}"$`, "mu"), token);
	}
});

test("an employer name that is not one line of text is refused before the file is opened", async () => {
	const directory = profileAt(home, "mine");
	const before = readFileSync(join(directory, "targeting.yaml"), "utf8");
	// U+0085 is the one that started this — a line break to PyYAML, above U+001F so JSON leaves
	// it alone, and not matched by JavaScript's own `\s`. Written as an escape: a literal one
	// would be as invisible in this file as it was in the defect.
	for (const name of ["Acme\u0085Bio", "Acme\u2028Bio", "Acme\nBio", "Acme\u0000Bio", "Acme\uFFFEBio"]) {
		const error = await refusal(() => registerEmployers("mine", [{ ...GINKGO, name }], install()));
		assert.equal(error.code, "invalid_profile", name);
		assert.equal(error.field, "boards.0.name", name);
	}
	// A name in any script, with any punctuation a person would actually type, still registers.
	const legible = ["Ginkgo Bioworks, Inc.", "Ærø Biosciences", "東京バイオ", "Zoë & Co — Labs"];
	for (const [index, name] of legible.entries()) {
		const answer = await registerEmployers("mine", [{ ...GINKGO, board: `b-${String(index)}`, name }], install());
		assert.equal(answer.added.length, 1, name);
	}
	assert.notEqual(readFileSync(join(directory, "targeting.yaml"), "utf8"), before);
});

test("a board token carrying a character the loader folds is refused", async () => {
	// `BOARD_TOKEN` excludes whitespace, and JavaScript's `\s` is not the loader's line-break
	// set: it matches U+2028 and U+2029 and does not match U+0085. A token is emitted as a key
	// as well as a list item, so one of these breaks the document in two places.
	profileAt(home, "mine");
	for (const board of ["acme\u0085corp", "acme\u007fcorp", "acme\uFFFFcorp"]) {
		const error = await refusal(() => registerEmployers("mine", [{ ...GINKGO, board }], install()));
		assert.equal(error.code, "invalid_profile", board);
		assert.equal(error.field, "boards.0.board", board);
	}
});

/**
 * A lone surrogate: a codepoint with no character behind it.
 *
 * Not a paste — `JSON.parse` mints one from a crafted `"\ud800"` escape, so this arrives by a
 * hand-written request at the loopback port. Everything above the validator carries it without
 * complaint: the emitter escapes it back to ASCII, PyYAML reads that escape, and the load-back
 * therefore answers that the Profile loads. What it reaches is `filters_version()`, which
 * re-serializes `targeting.yaml` through `json.dumps(...).encode("utf-8")` and raises
 * `UnicodeEncodeError: surrogates not allowed` — so every `match.run` for that Profile fails
 * from then on, permanently, over a request that was answered `200`.
 *
 * `tests/profile/test_dashboard_verify_adapter.py` measures the other half of that: the same
 * document loading and the same hash dying. This is the door it is refused at.
 */
test("a lone surrogate is refused in a name and in a board token, and nothing is written", async () => {
	const directory = profileAt(home, "mine");
	const before = readFileSync(join(directory, "targeting.yaml"), "utf8");

	const nameError = await refusal(() =>
		registerEmployers("mine", [{ ...GINKGO, name: "Lone\ud800Surrogate" }], install()),
	);
	assert.equal(nameError.code, "invalid_profile");
	assert.equal(nameError.field, "boards.0.name");

	const boardError = await refusal(() =>
		registerEmployers("mine", [{ ...GINKGO, board: "lone\ud800board" }], install()),
	);
	assert.equal(boardError.code, "invalid_profile");
	assert.equal(boardError.field, "boards.0.board");

	// A *paired* surrogate is an ordinary character above the BMP and is nobody's business to
	// refuse: an employer whose name carries one still registers.
	const answer = await registerEmployers(
		"mine",
		[{ source: "greenhouse", board: "astral", name: "Acme \u{1F3E2} Labs" }],
		install(),
	);
	assert.equal(answer.added.length, 1);
	assert.match(readFileSync(join(directory, "targeting.yaml"), "utf8"), /Acme .* Labs/u);
	assert.notEqual(readFileSync(join(directory, "targeting.yaml"), "utf8"), before);
});

test("more employers than one request may carry is the route's own bound", async () => {
	// Stated here because `coordination/CONTRACTS.md` states it: 25 per request, and the reason
	// is that this stays one edit to one file rather than a paste that rewrites a Profile.
	assert.equal(MAXIMUM_EMPLOYERS_PER_REQUEST, 25);
});

test("a token or a name longer than one is refused", async () => {
	profileAt(home, "mine");
	const longToken = "a".repeat(201);
	const tokenError = await refusal(() => registerEmployers("mine", [{ ...GINKGO, board: longToken }], install()));
	assert.equal(tokenError.field, "boards.0.board");
	const longName = "A".repeat(201);
	const nameError = await refusal(() => registerEmployers("mine", [{ ...GINKGO, name: longName }], install()));
	assert.equal(nameError.field, "boards.0.name");
	// And 200 exactly is fine on both, so the bound is the bound and not one short of it.
	const answer = await registerEmployers(
		"mine",
		[{ source: "greenhouse", board: "a".repeat(200), name: "A".repeat(200) }],
		install(),
	);
	assert.equal(answer.added.length, 1);
});
