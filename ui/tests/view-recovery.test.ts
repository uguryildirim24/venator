import assert from "node:assert/strict";
import type { ChildProcess } from "node:child_process";
import { EventEmitter } from "node:events";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { DatabaseSync } from "node:sqlite";

import { buildEmptyViewDatabase } from "../fixtures/make-fixture.ts";
import { ensureReadableView } from "../server/view-recovery.ts";
import { readOnboardingState } from "../server/onboarding/state.ts";

test("a fresh Install can enter setup even when an old view file has a stale schema", () => {
	const home = mkdtempSync(join(tmpdir(), "venator-new-install-view-"));
	try {
		mkdirSync(join(home, "build"));
		const database = new DatabaseSync(join(home, "build", "venator.db"));
		database.exec("CREATE TABLE postings (old_column TEXT)");
		database.close();
		const context = { platform: process.platform, home, workingDirectory: home, checkoutRoot: null, environment: (name: string) => name === "VENATOR_HOME" ? home : undefined };
		const state = readOnboardingState(context);
		assert.equal(state.configured, false);
		assert.equal(state.view.kind, "empty");
	} finally {
		rmSync(home, { recursive: true, force: true });
	}
});

// A view from before the Jev pause columns cannot answer the summary query. Two
// simultaneous reads must share the same rebuild, and a later read must not rerun it.
test("a view missing Jev pause columns is rebuilt once", async () => {
	const home = mkdtempSync(join(tmpdir(), "venator-view-recovery-"));
	const previous = process.env["VENATOR_HOME"];
	try {
		process.env["VENATOR_HOME"] = home;
		const profile = join(home, "profiles", "fictional");
		mkdirSync(profile, { recursive: true });
		writeFileSync(join(profile, "targeting.yaml"), "profile:\n  id: fictional\n");
		const path = join(home, "build", "venator.db");
		mkdirSync(join(home, "build"));
		buildEmptyViewDatabase(path);
		const stale = new DatabaseSync(path);
		stale.exec("DROP TABLE runs; CREATE TABLE runs (id INTEGER PRIMARY KEY, at TEXT, status TEXT, stage TEXT)");
		stale.close();
		let launches = 0;
		const stage = () => {
			launches++;
			const child = new EventEmitter();
			queueMicrotask(() => { rmSync(path); buildEmptyViewDatabase(path); child.emit("close", 0); });
			// SAFETY: The View stage only subscribes to close/error; EventEmitter provides both.
			return child as ChildProcess;
		};
		const context = { platform: process.platform, home, workingDirectory: home, checkoutRoot: null, environment: (name: string) => name === "VENATOR_HOME" ? home : undefined };
		await Promise.all([ensureReadableView(context, stage), ensureReadableView(context, stage)]);
		await ensureReadableView(context, stage);
		assert.equal(launches, 1);
	} finally {
		if (previous === undefined) delete process.env["VENATOR_HOME"];
		else process.env["VENATOR_HOME"] = previous;
		rmSync(home, { recursive: true, force: true });
	}
});
