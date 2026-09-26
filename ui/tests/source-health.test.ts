import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { readDatabaseInfo, readSourceHealth } from "../server/queries.ts";

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

test("a CLI fetch appears in the sidebar even with Postings and no app runs", () => {
	const database = emptyViewDatabase();
	try {
		database.prepare(`INSERT INTO postings (key, source, board, title, discovered_at)
			VALUES (?, ?, ?, ?, ?)`).run("greenhouse:acme:1", "greenhouse", "acme", "Scientist", "2026-09-25T09:10:00Z");
		database.prepare(`INSERT INTO source_health (source_key, status, last_attempt_at, last_success_at, count)
			VALUES (?, ?, ?, ?, ?)`).run("greenhouse:acme", "ok", "2026-09-25T09:14:00Z", "2026-09-25T09:14:00Z", 1);
		assert.equal(database.prepare("SELECT COUNT(*) AS count FROM runs").get()?.["count"], 0);
		assert.equal(readDatabaseInfo(database, ":memory:", "pipeline").lastFetchAt, "2026-09-25T09:14:00Z");

		// An older app heartbeat cannot hide a newer CLI check; a newer one does win.
		const addRun = database.prepare("INSERT INTO runs (at, status, stage) VALUES (?, 'ok', 'discover')");
		addRun.run("2026-09-24T10:00:00Z");
		assert.equal(readDatabaseInfo(database, ":memory:", "pipeline").lastFetchAt, "2026-09-25T09:14:00Z");
		addRun.run("2026-09-26T10:00:00Z");
		assert.equal(readDatabaseInfo(database, ":memory:", "pipeline").lastFetchAt, "2026-09-26T10:00:00Z");
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
