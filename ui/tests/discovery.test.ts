/**
 * Whether this Install has a Profile, and which one the pipeline would pick.
 *
 * The case that matters most is the scaffold: a fresh checkout ships `profiles/example/`, and
 * counting it would walk a person who has configured nothing straight past onboarding into
 * somebody else's search.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import { discoverProfiles, impliedProfile, readsAsScaffold } from "../server/onboarding/discovery.ts";

let home = "";
let checkout = "";

function install(): LocationContext {
	const environment = new Map([["VENATOR_HOME", home]]);
	return {
		platform: "linux",
		home,
		workingDirectory: checkout,
		checkoutRoot: null,
		environment: (name) => environment.get(name),
	};
}

function makeProfile(root: string, name: string, targeting: string): void {
	const directory = join(root, "profiles", name);
	mkdirSync(directory, { recursive: true });
	writeFileSync(join(directory, "targeting.yaml"), targeting);
}

beforeEach(() => {
	home = mkdtempSync(join(tmpdir(), "venator-home-"));
	checkout = mkdtempSync(join(tmpdir(), "venator-checkout-"));
});

afterEach(() => {
	rmSync(home, { recursive: true, force: true });
	rmSync(checkout, { recursive: true, force: true });
});

test("a scaffold is recognised, and a comment about one is not", () => {
	const directory = mkdtempSync(join(tmpdir(), "venator-scan-"));
	try {
		const path = join(directory, "targeting.yaml");
		writeFileSync(path, "profile:\n  name: example\n  scaffold: true\n");
		assert.equal(readsAsScaffold(path), true);

		writeFileSync(path, "profile:\n  name: sample\n  # scaffold: true\n");
		assert.equal(readsAsScaffold(path), false);

		writeFileSync(path, "profile:\n  name: sample\n  scaffold: false\n");
		assert.equal(readsAsScaffold(path), false);

		// A `scaffold:` under some other block is not the Profile's own flag.
		writeFileSync(path, "search:\n  scaffold: true\nprofile:\n  name: sample\n");
		assert.equal(readsAsScaffold(path), false);

		writeFileSync(path, "profile:\n  scaffold: true # a template\n");
		assert.equal(readsAsScaffold(path), true);

		assert.equal(readsAsScaffold(join(directory, "absent.yaml")), false);
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

test("an Install holding only a scaffold has no Profile", () => {
	makeProfile(checkout, "example", "profile:\n  name: example\n  scaffold: true\n");
	const profiles = discoverProfiles(install());
	assert.equal(profiles.length, 1);
	assert.equal(profiles[0]?.scaffold, true);
	assert.equal(impliedProfile(profiles), null);
});

test("an explicit Profile selects the example scaffold over a real Profile", () => {
	makeProfile(checkout, "example", "profile:\n  name: example\n  scaffold: true\n");
	makeProfile(checkout, "sample", "profile:\n  name: sample\n");
	const context = install();
	const named: LocationContext = {
		...context,
		environment: (name) => name === "VENATOR_PROFILE" ? "example" : context.environment(name),
	};
	assert.equal(impliedProfile(discoverProfiles(named), named)?.name, "example");
	assert.equal(impliedProfile(discoverProfiles(context), context)?.name, "sample");
});

test("one real Profile is the implied one; two are ambiguous", () => {
	makeProfile(home, "someone", "profile:\n  name: someone\n");
	const one = discoverProfiles(install());
	assert.equal(impliedProfile(one)?.name, "someone");

	makeProfile(home, "second", "profile:\n  name: second\n");
	assert.equal(impliedProfile(discoverProfiles(install())), null);
});

test("the Install wins a name over a checkout, and is labelled as such", () => {
	makeProfile(checkout, "someone", "profile:\n  name: someone\n");
	makeProfile(home, "someone", "profile:\n  name: someone\n");
	const profiles = discoverProfiles(install());
	assert.equal(profiles.length, 1);
	assert.equal(profiles[0]?.kind, "install");
	assert.equal(profiles[0]?.directory, join(home, "profiles", "someone"));
});

test("a directory without targeting.yaml is not a Profile, even if it holds resume facts", () => {
	mkdirSync(join(home, "profiles", "empty"), { recursive: true });
	makeProfile(checkout, "someone", "profile:\n  name: someone\n");
	mkdirSync(join(home, "profiles", "someone"), { recursive: true });
	writeFileSync(join(home, "profiles", "someone", "resume.yaml"), "name: Someone\n");
	const profiles = discoverProfiles(install());
	assert.equal(profiles.length, 1);
	assert.equal(profiles[0]?.kind, "checkout");
});

test("a dot directory is skipped, so a staging directory is never mistaken for a Profile", () => {
	mkdirSync(join(home, "profiles", ".onboarding-someone-abc"), { recursive: true });
	writeFileSync(join(home, "profiles", ".onboarding-someone-abc", "targeting.yaml"), "profile:\n  name: x\n");
	assert.deepEqual([...discoverProfiles(install())], []);
});
