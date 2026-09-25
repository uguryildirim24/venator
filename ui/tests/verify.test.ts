/**
 * The load-back, read through a stand-in for the pipeline's loader.
 *
 * Whether a Profile loads is `src/venator/profile/`'s answer and is tested on that side
 * (`tests/profile/test_dashboard_verify_adapter.py`). What is testable here is the seam: that
 * every answer the adapter can give becomes the right outcome, that a shape this dashboard has
 * never seen is reported as a mismatch rather than read as success, and — the case the whole
 * thing exists for — that an Install with no pipeline in it refuses the write rather than
 * writing unverified.
 *
 * The branch functions are pure, so most of this needs no child process at all. The two that
 * do use `tests/boards.test.ts`'s stand-in, for the reason stated at length there.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";

import type { LocationContext } from "../server/locations.ts";
import { OnboardingError } from "../server/onboarding/errors.ts";
import {
	readVerifyDocument,
	readVerifyResult,
	requireLoadableProfile,
	verifyProfileDirectory,
	type VerifyProcess,
} from "../server/onboarding/verify.ts";
import { armVerifierStandIn, disarmVerifierStandIn, VERIFIER_ANSWER, VERIFIER_EXIT } from "./verifier-stand-in.ts";

let standIn: ReadonlyMap<string, string> = new Map();
let scratch = "";

function install(): LocationContext {
	const environment = new Map([["VENATOR_HOME", scratch], ...standIn]);
	return {
		platform: "linux",
		home: scratch,
		workingDirectory: scratch,
		checkoutRoot: null,
		environment: (name) => environment.get(name),
	};
}

function finished(answer: Partial<VerifyProcess>): VerifyProcess {
	return { code: 0, stdout: "", stderr: "", timedOut: false, launchFailed: false, ...answer };
}

before(() => {
	standIn = armVerifierStandIn();
	scratch = mkdtempSync(join(tmpdir(), "venator-verify-"));
});

after(() => {
	disarmVerifierStandIn();
	rmSync(scratch, { recursive: true, force: true });
});

test("the three answers the adapter can give are read as themselves", () => {
	assert.deepEqual(readVerifyDocument('{"outcome": "loads"}'), { outcome: "loads" });
	assert.deepEqual(readVerifyDocument('{"outcome": "refused", "message": "not valid YAML"}'), {
		outcome: "refused",
		message: "not valid YAML",
	});
	assert.deepEqual(readVerifyDocument('{"outcome": "failed"}'), { outcome: "failed" });
});

test("anything else the adapter could say is a mismatch, and never read as success", () => {
	for (const document of [
		"",
		"not json",
		"[]",
		'"loads"',
		"{}",
		'{"outcome": "ok"}',
		'{"outcome": true}',
		'{"loaded": true}',
		// The one that matters: a document naming an outcome this dashboard does not know
		// must not be read as the nearest one it does.
		'{"outcome": "loads_with_warnings"}',
	]) {
		assert.deepEqual(readVerifyDocument(document), { outcome: "mismatch" }, document);
	}
});

test("a process that did not answer is an outcome rather than a crash", () => {
	assert.deepEqual(readVerifyResult(finished({ launchFailed: true })), { outcome: "absent" });
	assert.deepEqual(readVerifyResult(finished({ timedOut: true })), { outcome: "timed-out" });
	// Exit 3 is the adapter saying `venator` is not importable in this Install.
	assert.deepEqual(readVerifyResult(finished({ code: 3 })), { outcome: "absent" });
	assert.deepEqual(readVerifyResult(finished({ code: 1, stderr: "Traceback" })), { outcome: "failed" });
	assert.deepEqual(readVerifyResult(finished({ code: 0, stdout: '{"outcome": "loads"}' })), { outcome: "loads" });
});

test("a Profile the loader reads is the only outcome that lets a write proceed", async () => {
	process.env[VERIFIER_ANSWER] = '{"outcome":"loads"}';
	await requireLoadableProfile(scratch, "the Profile", install());
	delete process.env[VERIFIER_ANSWER];
});

test("every other outcome refuses the write, and says which kind of refusal it is", async () => {
	const cases: readonly (readonly [string, string, string])[] = [
		// A Profile the pipeline read and rejected: the request produced a document the arbiter
		// of what a Profile means will not accept, so it is the person's to correct.
		['{"outcome":"refused","message":"/home/someone/.local/x is not valid YAML"}', "unloadable_profile", "0"],
		['{"outcome":"failed"}', "unloadable_profile", "0"],
		['{"outcome":"who knows"}', "unloadable_profile", "0"],
		// The check could not be run at all. Nothing about the request was wrong.
		["", "verification_unavailable", "3"],
	];
	for (const [answer, code, exit] of cases) {
		process.env[VERIFIER_ANSWER] = answer;
		process.env[VERIFIER_EXIT] = exit;
		try {
			await requireLoadableProfile(scratch, "the Profile", install());
			throw new Error(`${answer} was expected to be refused and was not`);
		} catch (cause) {
			assert.ok(cause instanceof OnboardingError, `${answer} answered ${String(cause)}`);
			assert.equal(cause.code, code, answer);
			// The loader's own sentence names an absolute path inside a staging directory. It
			// goes to this server's stderr; what a browser is told is constructed here.
			assert.ok(!cause.message.includes("/home/someone"), "a path reached the answer");
			assert.match(cause.message, /the Profile was not written/u);
		}
	}
	delete process.env[VERIFIER_ANSWER];
	delete process.env[VERIFIER_EXIT];
});

test("the answer comes off a real subprocess's stdout", async () => {
	process.env[VERIFIER_ANSWER] = '{"outcome":"refused","message":"nope"}';
	const outcome = await verifyProfileDirectory(scratch, install());
	assert.deepEqual(outcome, { outcome: "refused", message: "nope" });
	delete process.env[VERIFIER_ANSWER];
});
