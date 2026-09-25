/**
 * A stored Match Score is historical data. Nothing writes a new one, so a row that carries one
 * still decodes in the decision history, its number never orders the board, and it never
 * crosses the wire, so no score can reach the screen.
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { DatabaseSync } from "node:sqlite";

import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { readPostingDetail, readPostingEntries } from "../server/queries.ts";

const SCORED = "greenhouse:ginkgobioworks:5022683007";
const UNSCORED = "greenhouse:ginkgobioworks:5022683008";

function viewWithOneScoredPosting(): DatabaseSync {
	const database = emptyViewDatabase();
	const posting = database.prepare(
		`INSERT INTO postings (key, source, board, company, title, location, url, posted_at,
		   discovered_at, description_html)
		 VALUES (?, 'greenhouse', 'ginkgobioworks', 'Ginkgo Bioworks', ?, 'Boston, MA',
		   'https://example.invalid/posting', NULL, ?, '')`,
	);
	// The scored Posting is the older one, so a board that ordered by score would put it first.
	posting.run(SCORED, "Senior Scientist", "2026-08-18T19:00:00+00:00");
	posting.run(UNSCORED, "Scientist", "2026-08-19T19:00:00+00:00");
	const decision = database.prepare(
		`INSERT INTO decisions (posting_key, stage, verdict, rule, score, reason, filters_version, decided_at)
		 VALUES (?, ?, ?, NULL, ?, 'historical score', '43ea01e7c82a', ?)`,
	);
	decision.run(SCORED, "hard_filter", "pass", null, "2026-08-18T20:10:11+00:00");
	decision.run(SCORED, "llm_score", "queue", 99, "2026-08-18T20:17:26+00:00");
	decision.run(UNSCORED, "hard_filter", "pass", null, "2026-08-19T20:10:11+00:00");
	return database;
}

const EVERY_POSTING = { search: null, status: null, rule: null, limit: 10 } as const;

test("a historical Match Score still decodes and takes the Hard Filter outcome", () => {
	const database = viewWithOneScoredPosting();
	try {
		const entries = readPostingEntries(database, { ...EVERY_POSTING, sort: "discovered" });
		const scored = entries.find((entry) => entry.posting.key === SCORED);
		assert.equal(scored?.hardFilter?.verdict, "pass");
		assert.equal(scored?.status, "unscored");
		assert.equal(entries.find((entry) => entry.posting.key === UNSCORED)?.status, "unscored");
		const detail = readPostingDetail(database, SCORED);
		assert.equal(detail?.status, "unscored");
		const historical = detail?.decisions.find((row) => row.stage === "llm_score");
		assert.equal(historical?.verdict, "queue");
		assert.equal(historical === undefined ? true : Object.hasOwn(historical, "score"), false);
		assert.equal(JSON.stringify(detail).includes("99"), false);
	} finally {
		database.close();
	}
});

test("a historical Match Score kill still shows the Hard Filter outcome", () => {
	const database = emptyViewDatabase();
	try {
		database
			.prepare(
				`INSERT INTO postings (key, source, board, company, title, location, url, posted_at,
				   discovered_at, description_html)
				 VALUES ('greenhouse:acme:1', 'greenhouse', 'acme', 'Acme', 'Role', 'Boston, MA',
				   'https://example.invalid/posting', NULL, '2026-08-18T19:00:00+00:00', '')`,
			)
			.run();
		const decision = database.prepare(
			`INSERT INTO decisions (posting_key, stage, verdict, rule, score, reason, filters_version, decided_at)
			 VALUES ('greenhouse:acme:1', ?, ?, NULL, ?, 'historical', '43ea01e7c82a', '2026-08-18T20:00:00+00:00')`,
		);
		decision.run("hard_filter", "pass", null);
		decision.run("llm_score", "kill", 12);
		const entries = readPostingEntries(database, { ...EVERY_POSTING, sort: "discovered" });
		assert.equal(entries[0]?.status, "unscored");
	} finally {
		database.close();
	}
});

test("a historical Match Score never orders the board", () => {
	const database = viewWithOneScoredPosting();
	try {
		const entries = readPostingEntries(database, { ...EVERY_POSTING, sort: "verified" });
		assert.deepEqual(entries.map((entry) => entry.posting.key), [UNSCORED, SCORED]);
	} finally {
		database.close();
	}
});
