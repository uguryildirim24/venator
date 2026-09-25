/**
 * The platform the onboarding surface reports, and the one rule it must never break.
 *
 * Which operating system this is decides which install route a runtime's next step
 * describes, and an install route is a set of instructions somebody follows literally. So the
 * mapping is closed: the three platforms Anthropic documents a route for, and `unknown` for
 * everything else. What is asserted here is that "everything else" really is everything else
 * — a platform Node reports that this contract does not name resolves to `unknown` and never
 * to its nearest neighbour, because `unknown` has copy of its own and a wrong guess does not.
 *
 * The end-to-end assertion runs the probe route against a stub interpreter, so the response
 * really is the one the server builds. Nothing real is launched and no subscription is spent:
 * the stub prints a lanes document and exits.
 */

import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { hostPlatform } from "../server/platform.ts";
import { createOnboardingRoutes } from "../server/onboarding/routes.ts";
import { ONBOARDING_REQUEST_HEADER, ONBOARDING_REQUEST_HEADER_VALUE, PLATFORMS } from "../shared/onboarding.ts";

test("the three platforms with a documented install route are named", () => {
	assert.equal(hostPlatform("darwin"), "macos");
	assert.equal(hostPlatform("win32"), "windows");
	assert.equal(hostPlatform("linux"), "linux");
});

test("a platform this contract does not name is unknown, never the nearest neighbour", () => {
	// Every other member of NodeJS.Platform. `android` and the BSDs are the tempting ones —
	// they are Unix and the Linux route would look plausible — and that is exactly why they
	// resolve to unknown instead: Anthropic documents no route for them.
	assert.equal(hostPlatform("freebsd"), "unknown");
	assert.equal(hostPlatform("openbsd"), "unknown");
	assert.equal(hostPlatform("netbsd"), "unknown");
	assert.equal(hostPlatform("sunos"), "unknown");
	assert.equal(hostPlatform("aix"), "unknown");
	assert.equal(hostPlatform("android"), "unknown");
	assert.equal(hostPlatform("cygwin"), "unknown");
	assert.equal(hostPlatform("haiku"), "unknown");
});

test("whatever this machine is, the answer is one of the four the contract allows", () => {
	assert.ok(PLATFORMS.includes(hostPlatform()));
});

test("the probe route reports the platform of the machine the server is running on", async () => {
	const directory = mkdtempSync(join(tmpdir(), "venator-platform-"));
	const interpreter = join(directory, "python-stub");
	writeFileSync(interpreter, "#!/bin/sh\necho '{\"lanes\": []}'\n", { mode: 0o700 });
	chmodSync(interpreter, 0o700);
	const previous = process.env["VENATOR_PYTHON"];
	process.env["VENATOR_PYTHON"] = interpreter;
	try {
		const response = await createOnboardingRoutes().request("/runtime/probe", {
			method: "POST",
			headers: {
				[ONBOARDING_REQUEST_HEADER]: ONBOARDING_REQUEST_HEADER_VALUE,
				"content-type": "application/json",
			},
			body: "{}",
		});
		assert.equal(response.status, 200);
		const body = await response.json();
		assert.equal(body.platform, hostPlatform());
		assert.ok(PLATFORMS.includes(body.platform));
	} finally {
		if (previous === undefined) delete process.env["VENATOR_PYTHON"];
		else process.env["VENATOR_PYTHON"] = previous;
		rmSync(directory, { recursive: true, force: true });
	}
});
