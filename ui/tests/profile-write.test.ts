/**
 * The write itself, against a real temporary Install.
 *
 * These are the adversarial cases: a path that tries to escape the Profiles directory, a
 * symbolic link standing where a Profile should be, an interrupted write, and an existing
 * Profile that must not be replaced by accident. Every one of them ends with an assertion
 * about what is on disk, because that is the only thing that matters here.
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, afterEach, before, beforeEach, test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import { OnboardingError } from "../server/onboarding/errors.ts";
import type { JsonMapping, JsonValue } from "../server/onboarding/json.ts";
import { profileDirectoryFor, writeProfile, type ProfileProposal, type WrittenProfile } from "../server/onboarding/profile.ts";
import { armVerifierStandIn, disarmVerifierStandIn, withVerifierAnswer } from "./verifier-stand-in.ts";

let home = "";
let elsewhere = "";
/**
 * The pipeline's answer, staged.
 *
 * `writeProfile` hands the staged directory to `venator.profile` before either rename, and
 * refuses the write when it does not load. These cases are about what reaches the filesystem,
 * they run where no `venator` is importable, and the load-back has its own file — so here the
 * subprocess is real and what it says is arranged.
 */
let standIn: ReadonlyMap<string, string> = new Map();

function install(): LocationContext {
	const environment = new Map([["VENATOR_HOME", home], ...standIn]);
	return {
		platform: "linux",
		home,
		// A working directory with no profiles/ in it, so the checkout leg finds nothing.
		workingDirectory: elsewhere,
		checkoutRoot: null,
		environment: (name) => environment.get(name),
	};
}

const RESUME: JsonMapping = {
	name: "A Person",
	contact: { location: "Boston, MA", phone: "555-0100", email: "a@b.co", linkedin: "linkedin.com/in/x" },
};

function proposal(name: string, overwrite = false, targeting: JsonMapping = {}): ProfileProposal {
	return { name, overwrite, resume: RESUME, constraints: { screening: { how_heard: null } }, targeting };
}

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

test("a Profile is written into the application data directory and nowhere else", async () => {
	const written = await writeProfile(proposal("someone", false, { search: { queries: ["marker-80"] } }), install());
	assert.equal(written.directory, realpathSync(join(home, "profiles", "someone")));
	assert.deepEqual([...written.files].sort(), ["constraints.yaml", "resume.yaml", "targeting.yaml"]);
	for (const file of written.files) {
		assert.ok(existsSync(join(written.directory, file)), file);
	}
	const targeting = readFileSync(join(written.directory, "targeting.yaml"), "utf8");
	assert.match(targeting, /marker-80/u);
	assert.match(targeting, /qualification_mode: "jev"/u);
	assert.match(targeting, /policy_version: "onboarding-shadow-1"/u);
	assert.match(targeting, /no_sponsorship_nonstudent: "review"/u);
	// Nothing outside the Profiles directory was touched.
	assert.deepEqual(readdirSync(home), ["profiles"]);
	assert.deepEqual(readdirSync(elsewhere), []);
});

/**
 * The same bound `/employers` holds on an employer name, held on a whole Profile.
 *
 * A lone surrogate is a codepoint with no character behind it. It arrives only from a
 * hand-written request — `JSON.parse` mints one from a crafted `"\ud800"` escape — and nothing
 * below this catches it: the emitter escapes it to ASCII, PyYAML reads the escape back, and the
 * load-back therefore answers that the Profile loads. What it reaches is `filters_version()`,
 * which re-serializes `targeting.yaml` through `json.dumps(...).encode("utf-8")` and raises,
 * disabling the Match stage for that Profile permanently.
 *
 * Checked in all three positions a Profile puts text in, because the walk is what makes the
 * bound complete: a value, a value nested inside a list, and a **key** — `sources.names` holds
 * a board token in key position, and a check that only read values would miss it.
 * `tests/profile/test_dashboard_verify_adapter.py` measures the other half: the same Profile
 * loading, and the same hash dying.
 */
test("a lone surrogate anywhere in a Profile is refused, and no Profile is created", async () => {
	const cases: readonly (readonly [string, JsonMapping])[] = [
		["a search term", { search: { terms: ["scientist\ud800"] } }],
		["a key", { sources: { names: { "lone\ud800board": "Acme" } } }],
		["a nested value", { sources: { names: { acme: "Ac\ud800me" } } }],
	];
	for (const [where, targeting] of cases) {
		const error = await refusal(() => writeProfile(proposal("someone", false, targeting), install()));
		assert.equal(error.code, "invalid_profile", where);
		assert.match(error.field ?? "", /^targeting\./u, `${where}: field was ${String(error.field)}`);
		assert.ok(!existsSync(join(home, "profiles", "someone")), `${where}: a Profile was created anyway`);
	}

	// A *paired* surrogate is an ordinary character above the BMP and is nobody's business to
	// refuse: a search term carrying one is written like any other.
	const written = await writeProfile(
		proposal("someone", false, { search: { terms: ["scientist \u{1F9EC}"] } }),
		install(),
	);
	assert.match(readFileSync(join(written.directory, "targeting.yaml"), "utf8"), /scientist/u);
});

test("an explicit Jev policy or deterministic choice is not replaced on write", async () => {
	const policy = {
		policy_version: "mine-1", restricted_roles: "exclude", temporary_student_authorization_exclusion: "review",
		no_sponsorship_student: "retain", no_sponsorship_nonstudent: "review", unmet_completed_degree: "review",
		domains: ["laboratory_research"],
	};
	const first = await writeProfile(proposal("one", false, { filters: { qualification_mode: "jev", jev: policy } }), install());
	assert.match(readFileSync(join(first.directory, "targeting.yaml"), "utf8"), /policy_version: "mine-1"/u);
	const second = await writeProfile(proposal("two", false, { filters: { qualification_mode: "deterministic" } }), install());
	assert.doesNotMatch(readFileSync(join(second.directory, "targeting.yaml"), "utf8"), /policy_version/u);
});

test("no staging directory survives a successful write", async () => {
	await writeProfile(proposal("someone"), install());
	assert.deepEqual(readdirSync(join(home, "profiles")), ["someone"]);
});

test("an existing Profile is not replaced without being asked", async () => {
	await writeProfile(proposal("someone"), install());
	const error = await refusal(() => writeProfile(proposal("someone"), install()));
	assert.equal(error.code, "profile_exists");
	assert.equal(error.status, 409);
});

test("overwrite replaces the whole Profile and leaves nothing of the old one", async () => {
	await writeProfile(proposal("someone", false, { search: { queries: ["marker-60"] } }), install());
	writeFileSync(join(home, "profiles", "someone", "stray.yaml"), "left behind\n");
	const written = await writeProfile(proposal("someone", true, { search: { queries: ["marker-90"] } }), install());
	assert.equal(written.replaced, true);
	const targeting = readFileSync(join(written.directory, "targeting.yaml"), "utf8");
	assert.match(targeting, /marker-90/u);
	assert.doesNotMatch(targeting, /qualification_mode/u, "overwriting an existing Profile must not turn Jev on by default");
	assert.ok(!existsSync(join(written.directory, "stray.yaml")));
	assert.deepEqual(readdirSync(join(home, "profiles")), ["someone"]);
});

test("a name that tries to leave the Profiles directory never resolves to a path outside it", async () => {
	for (const attempt of ["../escape", "..", "/etc/venator", "a/../../b"]) {
		const error = refusalFrom(() => profileDirectoryFor(attempt, install()));
		assert.equal(error.code, "invalid_profile_name", attempt);
	}
	// And the resolved path for a legal name is always a direct child of the root.
	assert.equal(profileDirectoryFor("someone", install()), join(home, "profiles", "someone"));
});

test("a symbolic link standing where a Profile would go is refused rather than written through", async () => {
	const target = mkdtempSync(join(tmpdir(), "venator-target-"));
	mkdirSync(join(home, "profiles"), { recursive: true });
	symlinkSync(target, join(home, "profiles", "someone"));
	try {
		const error = await refusal(() => writeProfile(proposal("someone", true), install()));
		assert.equal(error.code, "write_failed");
		assert.match(error.message, /symbolic link/u);
		assert.deepEqual(readdirSync(target), [], "nothing was written through the link");
	} finally {
		rmSync(target, { recursive: true, force: true });
	}
});

test("a same-named checkout Profile does not block writing to the Install", async () => {
	mkdirSync(join(elsewhere, "profiles", "someone"), { recursive: true });
	const checkoutFile = join(elsewhere, "profiles", "someone", "targeting.yaml");
	writeFileSync(checkoutFile, "profile:\n  name: someone\n");
	await writeProfile(proposal("someone", false), install());
	assert.ok(existsSync(join(home, "profiles", "someone", "targeting.yaml")));
	assert.equal(readFileSync(checkoutFile, "utf8"), "profile:\n  name: someone\n");
});

test("a payload the writer cannot render creates nothing at all", async () => {
	// Every file is rendered before the staging directory exists, so a refusal here happens
	// before anything is on disk rather than halfway through putting it there.
	let nested: JsonValue = { leaf: 1 };
	for (let depth = 0; depth < 40; depth += 1) nested = { down: nested };
	const error = await refusal(() => writeProfile(proposal("someone", false, { deep: nested }), install()));
	assert.equal(error.code, "invalid_profile");
	assert.ok(!existsSync(join(home, "profiles", "someone")));
	assert.deepEqual(readdirSync(join(home, "profiles")), []);
});

test("a refused overwrite leaves the Profile that was already there untouched", async () => {
	await writeProfile(proposal("someone", false, { search: { queries: ["marker-60"] } }), install());
	let nested: JsonValue = { leaf: 1 };
	for (let depth = 0; depth < 40; depth += 1) nested = { down: nested };
	await refusal(() => writeProfile(proposal("someone", true, { deep: nested }), install()));
	assert.match(readFileSync(join(home, "profiles", "someone", "targeting.yaml"), "utf8"), /marker-60/u);
	assert.deepEqual(readdirSync(join(home, "profiles")), ["someone"]);
});

test("a staging directory is dot-prefixed, so a leftover is never resolvable as a Profile", async () => {
	// `select.py: available_profiles` skips a dot directory, which is what makes an
	// interrupted write invisible to the pipeline rather than half-visible.
	const written = await writeProfile(proposal("someone"), install());
	assert.ok(!written.directory.split("/").some((part) => part.startsWith(".onboarding-")));
	assert.deepEqual(
		readdirSync(join(home, "profiles")).filter((entry) => entry.startsWith(".")),
		[],
	);
});

test("a dangling symbolic link at the target is refused, not written over", async () => {
	// existsSync() is false for a dangling link, so the "already exists" check would miss it;
	// the link check is what catches it.
	mkdirSync(join(home, "profiles"), { recursive: true });
	symlinkSync("/nowhere/at/all", join(home, "profiles", "someone"));
	const error = await refusal(() => writeProfile(proposal("someone"), install()));
	assert.equal(error.code, "write_failed");
	assert.match(error.message, /symbolic link/u);
});

test("a Profile written here is refused if it would load with no Hard Filter", async () => {
	const error = await refusal(() =>
		writeProfile(proposal("someone", false, { filters: { role_target: { levels: [{ name: "mid" }] } } }), install()),
	);
	assert.equal(error.code, "invalid_profile");
	assert.ok(!existsSync(join(home, "profiles", "someone")), "nothing was written for a refused Profile");
});

test("a written Profile keeps a person's words as text, whatever they typed", async () => {
	const written = await writeProfile(
		proposal("someone", false, {
			search: { queries: ["no", "yes: maybe", "- dash"], locations: [] },
		}),
		install(),
	);
	const text = readFileSync(join(written.directory, "targeting.yaml"), "utf8");
	assert.match(text, /- "no"/u);
	assert.match(text, /- "yes: maybe"/u);
	assert.match(text, /- "- dash"/u);
});

/** The refusal an awaited write made. */
async function refusal(run: () => Promise<WrittenProfile>): Promise<OnboardingError> {
	try {
		await run();
	} catch (error) {
		assert.ok(error instanceof OnboardingError, `expected an onboarding refusal, got ${String(error)}`);
		return error;
	}
	throw new assert.AssertionError({ message: "expected a refusal, and none came" });
}

/** The refusal a call that answers without waiting made. */
function refusalFrom(run: () => string): OnboardingError {
	try {
		run();
	} catch (error) {
		assert.ok(error instanceof OnboardingError, `expected an onboarding refusal, got ${String(error)}`);
		return error;
	}
	throw new assert.AssertionError({ message: "expected a refusal, and none came" });
}

/**
 * The load-back, wired in.
 *
 * `writeProfile` renders three files, stages them, and hands the staging directory to
 * `src/venator/profile/` before either rename. Everything the validator above refuses is one
 * person's reading of the loader; this is the loader. The three cases are the three things
 * that have to be true of it: a refusal creates nothing, a refusal over an existing Profile
 * leaves that Profile exactly as it was, and an Install that cannot run the check does not
 * write unverified.
 */

test("a Profile the pipeline cannot read back is refused, and nothing is left on disk", async () => {
	const error = await withVerifierAnswer('{"outcome":"refused","message":"targeting.yaml is not valid YAML"}', () =>
		refusal(() => writeProfile(proposal("someone"), install())),
	);
	assert.equal(error.code, "unloadable_profile");
	assert.equal(error.status, 422);
	// The loader's sentence names an absolute path inside a staging directory. It is logged,
	// never answered with.
	assert.ok(!error.message.includes("targeting.yaml"));
	assert.ok(!existsSync(join(home, "profiles", "someone")));
	assert.deepEqual(readdirSync(join(home, "profiles")), []);
});

test("a refused load-back over an existing Profile leaves it byte for byte what it was", async () => {
	const written = await writeProfile(proposal("someone", false, { search: { queries: ["marker-60"] } }), install());
	const before = new Map(written.files.map((file) => [file, readFileSync(join(written.directory, file), "utf8")]));
	const error = await withVerifierAnswer('{"outcome":"refused","message":"no"}', () =>
		refusal(() => writeProfile(proposal("someone", true, { search: { queries: ["marker-90"] } }), install())),
	);
	assert.equal(error.code, "unloadable_profile");
	for (const [file, text] of before) {
		assert.equal(readFileSync(join(written.directory, file), "utf8"), text, file);
	}
	// And the replacement is not sitting half-done beside it either.
	assert.deepEqual(readdirSync(join(home, "profiles")), ["someone"]);
});

test("an Install that cannot read a Profile back writes none", async () => {
	// Exit 3 from the adapter is "`venator` is not importable here". Writing anyway would put
	// exactly the defect this check exists for onto the one kind of Install that cannot see it.
	const error = await withVerifierAnswer("", async () => {
		process.env["VENATOR_STAND_IN_EXIT"] = "3";
		try {
			return await refusal(() => writeProfile(proposal("someone"), install()));
		} finally {
			delete process.env["VENATOR_STAND_IN_EXIT"];
		}
	});
	assert.equal(error.code, "verification_unavailable");
	assert.equal(error.status, 503);
	assert.deepEqual(readdirSync(join(home, "profiles")), []);
});

test("a person's own words survive the round trip the loader is asked about", async () => {
	// The two values that produced an unloadable Profile from first setup: a search query and
	// an employer display name, each carrying one U+0085. Written as escapes here for the same
	// reason as in yaml.test.ts — a literal one would be invisible in this file too. (A
	// Profile's own name cannot carry one: `validateProfileName` allows letters, digits, dots,
	// dashes and underscores, and the identity block must match the directory name.)
	const written = await writeProfile(
		proposal("someone", false, {
			search: { queries: ["bench\u2028scientist"], locations: [] },
			sources: { names: { acme: "Acme\u0085Bio" }, boards: { greenhouse: ["acme"] } },
		}),
		install(),
	);
	const text = readFileSync(join(written.directory, "targeting.yaml"), "utf8");
	assert.match(text, /Acme\\u0085Bio/u);
	assert.match(text, /bench\\u2028scientist/u);
	// Nothing invisible reached the file.
	assert.ok(!/[\u007F-\u009F\u2028\u2029]/u.test(text));
});

test("final Save captures the original PDF inside staging and retains it when facts are edited", async () => {
	const pdf = new TextEncoder().encode("%PDF-1.7 original bytes");
	let captures = 0;
	const capture = async (source: string, directory: string) => {
		captures += 1;
		assert.deepEqual(readFileSync(source), Buffer.from(pdf));
		assert.ok(directory.includes(".onboarding-"));
		assert.equal(existsSync(profileDirectoryFor("layout", install())), false);
		for (const file of ["layout.json", "regular.ttf", "bold.ttf", "italic.ttf"]) writeFileSync(join(directory, file), "captured");
		return { supported: true as const };
	};
	const first = await writeProfile({ ...proposal("layout"), referencePdf: pdf }, install(), capture);
	assert.deepEqual(readFileSync(join(first.directory, "resume-reference/source.pdf")), Buffer.from(pdf));
	const changed = await writeProfile({ ...proposal("layout", true), resume: { ...RESUME, name: "Edited name" } }, install(), capture);
	assert.equal(captures, 1);
	assert.equal(readFileSync(join(changed.directory, "resume-reference/layout.json"), "utf8"), "captured");
	assert.deepEqual(readFileSync(join(changed.directory, "resume-reference/source.pdf")), Buffer.from(pdf));
});

test("unsupported capture saves the original and status, removes partial assets, and warns", async () => {
	const pdf = new TextEncoder().encode("%PDF-1.7 unsupported");
	const written = await writeProfile({ ...proposal("unsupported"), referencePdf: pdf }, install(), async (_source, directory) => {
		writeFileSync(join(directory, "layout.json"), "partial");
		writeFileSync(join(directory, "regular.ttf"), "partial");
		return { supported: false, reason: "Image-only layout" };
	});
	assert.deepEqual(readdirSync(join(written.directory, "resume-reference")).sort(), ["source.pdf", "status.json"]);
	assert.deepEqual(readFileSync(join(written.directory, "resume-reference/source.pdf")), Buffer.from(pdf));
	assert.ok(written.warnings.includes("Your résumé was saved, but its layout cannot be reproduced yet."));
});

test("capture failure or incomplete success preserves the previous profile and reference", async () => {
	const original = await writeProfile(proposal("rollback"), install());
	const reference = join(original.directory, "resume-reference");
	mkdirSync(reference);
	writeFileSync(join(reference, "source.pdf"), "%PDF-original");
	const before = readFileSync(join(original.directory, "resume.yaml"));
	for (const capture of [async () => { throw new Error("capture failed"); }, async () => ({ supported: true as const })]) {
		await assert.rejects(writeProfile({ ...proposal("rollback", true), referencePdf: new TextEncoder().encode("%PDF-new") }, install(), capture));
		assert.deepEqual(readFileSync(join(original.directory, "resume.yaml")), before);
		assert.equal(readFileSync(join(reference, "source.pdf"), "utf8"), "%PDF-original");
		assert.deepEqual(readdirSync(join(home, "profiles")), ["rollback"]);
	}
});

test("an invalid PDF is refused before staging or capture", async () => {
	let called = false;
	await assert.rejects(writeProfile({ ...proposal("bad-pdf"), referencePdf: new TextEncoder().encode("not a pdf") }, install(), async () => { called = true; return { supported: true }; }));
	assert.equal(called, false);
	assert.equal(existsSync(profileDirectoryFor("bad-pdf", install())), false);
});
