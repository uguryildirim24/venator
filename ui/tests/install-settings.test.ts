import assert from "node:assert/strict";
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { documentRuntime, saveDocumentRuntime } from "../server/install-settings.ts";
import { nonJevInheritedEnvironment, runEnvironment, jevRunEnvironment } from "../server/runs/environment.ts";
import type { LocationContext } from "../server/locations.ts";

test("a saved runtime is the only runtime for document children; no key goes to non-Jev children", async () => {
	const home = mkdtempSync(join(tmpdir(), "venator-settings-"));
	try {
		const context: LocationContext = {
			platform: "darwin", home, workingDirectory: home, checkoutRoot: null,
			environment: (name) => name === "VENATOR_HOME" ? home : name === "TYPESAFE_API_KEY" ? "sentinel" : name === "VENATOR_LLM_RUNTIME" ? "api" : undefined,
		};
		assert.equal(documentRuntime(context), "claude");
		await saveDocumentRuntime("codex", context);
		assert.equal(documentRuntime(context), "codex");
		assert.deepEqual(JSON.parse(readFileSync(join(home, "settings.json"), "utf8")), { runtime: "codex" });
		assert.equal(runEnvironment(context)["VENATOR_LLM_RUNTIME"], "codex");
		assert.equal(runEnvironment(context)["TYPESAFE_API_KEY"], undefined);
		assert.equal(jevRunEnvironment(context)["TYPESAFE_API_KEY"], "sentinel");
		assert.equal(nonJevInheritedEnvironment(context)["TYPESAFE_API_KEY"], undefined);
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});
