import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { readSourceHealth } from "../server/queries.ts";

test("source health decodes measured coverage from the current view", () => {
	const database = emptyViewDatabase();
	try {
		database
			.prepare(
				`INSERT INTO source_health
				 (source_key, status, last_attempt_at, last_success_at, count, message,
				  known_jobs, full_verified_details, needs_detail_check)
				 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			)
			.run("workday:acme", "partial", "2026-09-05T12:00:00Z", null, 1200, "partial board", 2, 1, 1);

		assert.deepEqual(readSourceHealth(database), [
			{
				key: "workday:acme",
				employer: null,
				status: "partial",
				lastAttemptAt: "2026-09-05T12:00:00Z",
				lastSuccessAt: null,
				count: 1200,
				message: "partial board",
				coverage: { knownJobs: 2, fullVerifiedDetails: 1, needsDetailCheck: 1 },
			},
		]);
	} finally {
		database.close();
	}
});

test("an older view reports coverage as unmeasured instead of inventing zeroes", () => {
	const database = new DatabaseSync(":memory:");
	try {
		database.exec(`
			CREATE TABLE postings (source TEXT, board TEXT, company TEXT);
			CREATE TABLE source_health (
				source_key TEXT PRIMARY KEY, status TEXT, last_attempt_at TEXT,
				last_success_at TEXT, count INTEGER, message TEXT
			);
		`);
		database.prepare("INSERT INTO source_health VALUES (?, ?, ?, ?, ?, ?)").run(
			"greenhouse:acme",
			"ok",
			"2026-09-05T12:00:00Z",
			null,
			3,
			null,
		);

		assert.equal(readSourceHealth(database)[0]?.coverage, null);
	} finally {
		database.close();
	}
});
