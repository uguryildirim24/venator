import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { countPostingEntries, readFunnel, readPostingDetail, readPostingEntries } from "../server/queries.ts";
import { buildMargin } from "../src/margin.ts";
import { jevSkipLabel, statusLabel } from "../src/labels.ts";

const query = { status: null, rule: null, search: null, sort: "verified" as const, limit: 10 };

test("current Jev routes passed Postings; résumé assessment, stale Jev and historical scores do not", () => {
	const db = emptyViewDatabase();
	try {
		for (const key of ["priority", "review", "skip", "stale", "missing", "killed", "applied"]) {
			db.prepare("INSERT INTO postings (key, source, board, title, url, discovered_at) VALUES (?, 'greenhouse', 'acme', 'Role', 'https://example.test/job', '2026-01-01')").run(key);
			db.prepare("INSERT INTO decisions (posting_key, stage, verdict, score, reason, decided_at) VALUES (?, 'llm_score', 'queue', 99, 'legacy', '2026-01-01')").run(key);
			db.prepare("INSERT INTO assessments (posting_key,status,summary,evidence,conflicts,unknowns,listing_status,last_verified_at,description_kind) VALUES (?, 'not_suitable', 'Evidence conflict', '[]', '[]', '[]', 'open', '2026-01-01', 'full')").run(key);
		}
		const insert = db.prepare(`INSERT INTO jev_triage (posting_key,mode,state,decision,fit_probability,fit_score,exclusions,review_flags,diagnostic_flags,qualifier_version,assessment_key,input_version,as_of_month)
			VALUES (?, 'shadow', ?, ?, ?, ?, '[]', '[]', '[]', 'jev-current', ?, 'profile-policy-current', '2026-09')`);
		for (const [key, decision] of [["priority", "prioritize"], ["review", "review"], ["skip", "exclude"]] as const) {
			insert.run(key, "current", decision, 0.7, 0.7, `ak-${key}`);
			db.prepare("INSERT INTO decisions (posting_key, stage, verdict, decided_at) VALUES (?, 'hard_filter', 'pass', '2026-01-02')").run(key);
		}
		// A passing Hard Filter can still have a useful unknown fact in the margin.
		db.prepare("UPDATE assessments SET unknowns = ? WHERE posting_key = 'skip'")
			.run(JSON.stringify(["hard-filter fact location was unknown (hard_filter decision)"]));
		insert.run("stale", "stale", "prioritize", 0.9, 0.9, "ak-old");
		db.prepare("INSERT INTO decisions (posting_key, stage, verdict, rule, decided_at) VALUES ('killed', 'hard_filter', 'kill', 'eligibility', '2026-01-02')").run();
		db.prepare("INSERT INTO application_states (posting_key, state) VALUES ('applied', 'prepared')").run();
		const statuses = Object.fromEntries(readPostingEntries(db, query).map((entry) => [entry.posting.key, entry.status]));
		assert.deepEqual(statuses, { applied: "applied", killed: "hard-killed", missing: "not-filtered", priority: "queued", review: "needs-review", skip: "hard-killed", stale: "not-filtered" });
		assert.equal(readFunnel(db).unscored, 0);
		assert.equal(countPostingEntries(db, { ...query, status: "hard-killed" }), 2);
		const skipped = readPostingDetail(db, "skip");
		assert.ok(skipped);
		assert.equal(skipped.jev?.mode, "shadow");
		assert.equal(jevSkipLabel(), "Jev: skip");
		db.prepare("UPDATE jev_triage SET mode = 'promoted' WHERE state = 'current'").run();
		db.prepare("UPDATE jev_selection SET mode = 'promoted'").run();
		const promoted = readPostingDetail(db, "skip");
		assert.equal(promoted?.jev?.mode, "promoted");
		assert.equal(jevSkipLabel(), "Jev: skip");
		const notes = buildMargin({ page: skipped.page, decisions: skipped.decisions, assessment: skipped.assessment, jevTriage: skipped.jevTriage });
		const marginNotes = [...notes.header, ...notes.blocks.flatMap((block) => block.notes)];
		assert.ok(!marginNotes.some((note) => note.title === "Excluded by a Hard Filter"));
		assert.ok(marginNotes.some((note) => note.title === "Location: not stated"));
	} finally { db.close(); }
});

test("closed and unscoreable Postings do not inflate Awaiting Jev", () => {
	const db = emptyViewDatabase();
	try {
		const insert = db.prepare(`INSERT INTO postings (key, source, board, title, url, discovered_at)
			VALUES (?, 'greenhouse', 'acme', ?, 'https://example.test/job', '2026-01-01')`);
		const assess = db.prepare(`INSERT INTO assessments (posting_key, listing_status) VALUES (?, ?)`);
		const filter = db.prepare(`INSERT INTO decisions (posting_key, stage, verdict, decided_at)
			VALUES (?, 'hard_filter', 'pass', '2026-01-02')`);
		for (const [key, listing] of [
			["closed-pass", "closed"], ["closed-old", "closed"], ["closed-current", "closed"],
			["no-text", "open"], ["protected", "unknown"], ["too-long", "open"],
			["waiting", "unknown"], ["for-you", "open"], ["application", "closed"],
		] as const) {
			insert.run(key, key);
			assess.run(key, listing);
			filter.run(key);
		}
		db.exec(`INSERT INTO jev_skip VALUES ('no-text', 'missing'), ('protected', 'protected'), ('too-long', 'too_long')`);
		const triage = db.prepare(`INSERT INTO jev_triage
			(posting_key, mode, state, decision, fit_probability, fit_score, exclusions, review_flags,
			 diagnostic_flags, qualifier_version, assessment_key, input_version, as_of_month)
			VALUES (?, 'shadow', ?, 'prioritize', 0.8, 0.8, '[]', '[]', '[]', 'jev-1', 'key', 'input', '2026-09')`);
		triage.run("closed-old", "stale");
		triage.run("closed-current", "current");
		triage.run("for-you", "current");
		db.exec("INSERT INTO application_states (posting_key, state) VALUES ('application', 'prepared')");
		const statuses = Object.fromEntries(readPostingEntries(db, query).map((entry) => [entry.posting.key, entry.status]));
		assert.deepEqual(statuses, {
			application: "applied", "closed-current": "closed", "closed-old": "closed", "closed-pass": "closed",
			"for-you": "queued", "no-text": "no-text", protected: "protected", "too-long": "too-long", waiting: "unscored",
		});
		for (const [status, count] of [["unscored", 1], ["queued", 1], ["needs-review", 0],
			["applied", 1], ["closed", 3], ["no-text", 1], ["protected", 1], ["too-long", 1]] as const) {
			assert.equal(countPostingEntries(db, { ...query, status }), count, status);
		}
		assert.deepEqual(
			Object.fromEntries(Object.entries(readFunnel(db)).filter(([key]) =>
				["unscored", "queued", "needsReview", "applied", "closed", "noText", "protected", "tooLong"].includes(key))),
			{ unscored: 1, queued: 1, needsReview: 0, applied: 1, closed: 3, noText: 1, tooLong: 1, protected: 1 },
		);
		assert.equal(statusLabel("no-text"), "No job text to read");
		assert.equal(statusLabel("protected"), "Reserved for evaluation");
		assert.equal(readPostingDetail(db, "closed-old")?.status, "closed");
	} finally { db.close(); }
});

test("an active promoted release without current results never routes from shadow", () => {
	const db = emptyViewDatabase();
	try {
		db.prepare("INSERT INTO postings (key, source, board, title, url, discovered_at) VALUES ('job', 'greenhouse', 'acme', 'Role', 'https://example.test/job', '2026-01-01')").run();
		db.prepare("INSERT INTO decisions (posting_key, stage, verdict, decided_at) VALUES ('job', 'hard_filter', 'pass', '2026-01-02')").run();
		db.prepare(`INSERT INTO jev_triage (posting_key, mode, state, decision, fit_probability, fit_score,
			exclusions, review_flags, diagnostic_flags, qualifier_version, assessment_key, input_version, as_of_month)
			VALUES ('job', 'shadow', 'current', 'prioritize', 0.8, 0.8, '[]', '[]', '[]', 'jev-current', 'ak', 'input', '2026-09')`).run();
		assert.equal(readPostingDetail(db, "job")?.status, "queued");
		db.prepare("UPDATE jev_selection SET mode = 'promoted'").run();
		assert.equal(readPostingDetail(db, "job")?.status, "unscored");
		assert.equal(readPostingDetail(db, "job")?.jev, null);
		assert.equal(readFunnel(db).unscored, 1);
	} finally { db.close(); }
});
