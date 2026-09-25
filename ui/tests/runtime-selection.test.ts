import assert from "node:assert/strict";
import { test } from "node:test";

import { readyLane } from "../src/views/onboarding/runtime-selection.ts";
import type { RuntimeLane, RuntimeProbeResponse } from "../shared/onboarding.ts";

function lane(name: string, ready: boolean): RuntimeLane {
	return { lane: name, ready, state: "available", detail: null, remedy: null, caveat: null, default: name === "codex" };
}

test("a check of the other runtime cannot switch the document reading", () => {
	const response: RuntimeProbeResponse = {
		probe: { available: true, reason: null, message: null },
		scope: "all", lane: null,
		lanes: [lane("codex", true), lane("claude", false)],
	};
	assert.equal(readyLane(response, "claude"), null);
	assert.equal(readyLane(response, "codex"), "codex");
});
