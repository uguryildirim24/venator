/**
 * Jev diagnostics decode only stale and unavailable rows in verification order.
 * No Jev number orders a row or leaves the server.
 */

import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";

import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { decodeJevTriage } from "../server/jev.ts";
import { countJevTriageEntries, readJevTriageEntries } from "../server/queries.ts";

test("an empty view with jev_triage has no Early review rows", () => {
	const database = emptyViewDatabase();
	try {
		assert.equal(countJevTriageEntries(database, { decision: null, search: null, limit: 50, offset: 0 }), 0);
		assert.deepEqual(readJevTriageEntries(database, { decision: null, search: null, limit: 50, offset: 0 }), []);
	} finally {
		database.close();
	}
});

test("an older view without jev_triage still decodes as an empty Early review list", () => {
	const database = new DatabaseSync(":memory:");
	try {
		database.exec(`
			CREATE TABLE postings (key TEXT PRIMARY KEY, source TEXT, board TEXT, company TEXT,
			  title TEXT, location TEXT, url TEXT, posted_at TEXT, discovered_at TEXT, description_html TEXT);
			CREATE TABLE decisions (id INTEGER PRIMARY KEY, posting_key TEXT, stage TEXT,
			  verdict TEXT, rule TEXT, score INTEGER, reason TEXT, filters_version TEXT, decided_at TEXT);
			CREATE TABLE application_states (posting_key TEXT PRIMARY KEY, state TEXT, detail TEXT, since TEXT);
			CREATE TABLE assessments (posting_key TEXT PRIMARY KEY, status TEXT, summary TEXT,
			  evidence TEXT, conflicts TEXT, unknowns TEXT, listing_status TEXT, last_verified_at TEXT,
			  description_kind TEXT, apply_url TEXT, opportunity_type TEXT);
		`);
		database.exec(
			`INSERT INTO postings (key, source, board, title, url, discovered_at)
			 VALUES ('greenhouse:acme:1', 'greenhouse', 'acme', 'Role', 'https://example.test/job', '2026-01-01')`,
		);
		assert.equal(countJevTriageEntries(database, { decision: null, search: null, limit: 50, offset: 0 }), 0);
		assert.deepEqual(readJevTriageEntries(database, { decision: null, search: null, limit: 50, offset: 0 }), []);
	} finally {
		database.close();
	}
});

test("current decisions belong in the main lists, not diagnostics", () => {
	const database = emptyViewDatabase();
	try {
		const insertPosting = database.prepare(
			`INSERT INTO postings (key, source, board, company, title, location, url, posted_at, discovered_at)
			 VALUES (?, 'greenhouse', 'acme', 'Acme', ?, 'Boston', 'https://example.test/job', NULL, ?)`,
		);
		insertPosting.run("greenhouse:acme:exclude", "Excluded role", "2026-01-04");
		insertPosting.run("greenhouse:acme:review", "Review role", "2026-01-03");
		insertPosting.run("greenhouse:acme:priority", "Priority role", "2026-01-02");
		insertPosting.run("greenhouse:acme:none", "Fallback role", "2026-01-01");
		database.exec(`
			INSERT INTO jev_triage (
			  posting_key, mode, state, decision, fit_probability, fit_score, primary_rule,
			  exclusions, review_flags, diagnostic_flags, qualifier_version, assessment_key,
			  input_version, policy_hash, model_id, as_of_month, decided_at, reason
			) VALUES
			('greenhouse:acme:exclude', 'promoted', 'current', 'exclude', 0.1, 0.1, 'policy_exclude',
			 '[]', '[]', '[]', 'jev-1', 'ak-e', 'in-1', 'ph', NULL, '2026-09', '2026-09-01', NULL),
			('greenhouse:acme:review', 'promoted', 'current', 'review', 0.5, 0.5, 'experience_gap',
			 '[]', '[]', '[]', 'jev-1', 'ak-r', 'in-1', 'ph', NULL, '2026-09', '2026-09-01', NULL),
			('greenhouse:acme:priority', 'promoted', 'current', 'prioritize', 0.9, 0.8, 'student_program',
			 '[]', '[]', '[]', 'jev-1', 'ak-p', 'in-1', 'ph', NULL, '2026-09', '2026-09-01', NULL),
			('greenhouse:acme:none', 'promoted', 'unavailable', 'unassessed', NULL, NULL, NULL,
			 '[]', '[]', '[]', NULL, NULL, NULL, NULL, NULL, '2026-09', NULL, 'Jev assessment is unavailable');
		`);
		database.exec("UPDATE jev_selection SET mode = 'promoted'");
		const entries = readJevTriageEntries(database, {
			decision: null,
			search: null,
			limit: 50,
			offset: 0,
		});
		assert.deepEqual(
			entries.map((entry) => entry.triage.decision),
			["unassessed"],
		);
	} finally {
		database.close();
	}
});

test("stale rows remain in verification order, never score order", () => {
	const database = emptyViewDatabase();
	try {
		const insertPosting = database.prepare(
			`INSERT INTO postings (key, source, board, company, title, location, url, posted_at, discovered_at)
			 VALUES (?, 'greenhouse', 'acme', 'Acme', ?, 'Boston', 'https://example.test/job', NULL, ?)`,
		);
		insertPosting.run("greenhouse:acme:stale-high", "Stale high", "2026-01-03");
		insertPosting.run("greenhouse:acme:stale-low", "Stale low", "2026-01-01");
		insertPosting.run("greenhouse:acme:current", "Current", "2026-01-04");
		database.exec(`
			INSERT INTO jev_triage (
			  posting_key, mode, state, decision, fit_probability, fit_score, primary_rule,
			  exclusions, review_flags, diagnostic_flags, qualifier_version, assessment_key,
			  input_version, policy_hash, model_id, as_of_month, decided_at, reason
			) VALUES
			('greenhouse:acme:stale-high', 'promoted', 'stale', 'prioritize', 0.99, 0.99, 'student_program',
			 '[]', '[]', '[]', 'jev-0', NULL, 'in-0', 'ph', NULL, '2026-09', '2026-08-01', NULL),
			('greenhouse:acme:stale-low', 'promoted', 'stale', 'prioritize', 0.2, 0.2, 'student_program',
			 '[]', '[]', '[]', 'jev-0', NULL, 'in-0', 'ph', NULL, '2026-09', '2026-08-01', NULL),
			('greenhouse:acme:current', 'promoted', 'current', 'prioritize', 0.7, 0.7, 'student_program',
			 '[]', '[]', '[]', 'jev-1', 'ak-c', 'in-1', 'ph', NULL, '2026-09', '2026-09-01', NULL);
		`);
		database.exec("UPDATE jev_selection SET mode = 'promoted'");
		const entries = readJevTriageEntries(database, {
			decision: "prioritize",
			search: null,
			limit: 50,
			offset: 0,
		});
		assert.deepEqual(
			entries.map((entry) => entry.posting.key),
			// Neither stale row is verified, so the more recently discovered one leads; its higher
			// historical score is not why.
			["greenhouse:acme:stale-high", "greenhouse:acme:stale-low"],
		);
	} finally {
		database.close();
	}
});

test("diagnostics omits current shadow rows before promotion", () => {
	const database = emptyViewDatabase();
	try {
		database.exec(`
			INSERT INTO postings (key, source, board, company, title, location, url, discovered_at)
			VALUES ('greenhouse:acme:shadow', 'greenhouse', 'acme', 'Acme', 'Shadow role', 'Boston', 'https://example.test/job', '2026-01-01');
			INSERT INTO jev_triage (
			  posting_key, mode, state, decision, fit_probability, fit_score, primary_rule,
			  exclusions, review_flags, diagnostic_flags, qualifier_version, assessment_key,
			  input_version, policy_hash, model_id, as_of_month, decided_at, reason
			) VALUES
			('greenhouse:acme:shadow', 'promoted', 'unavailable', 'unassessed', NULL, NULL, NULL,
			 '[]', '[]', '[]', NULL, NULL, NULL, NULL, NULL, '2026-09', NULL, 'Not promoted'),
			('greenhouse:acme:shadow', 'shadow', 'current', 'prioritize', 0.8, 0.9, NULL,
			 '[]', '[]', '[]', 'jev-1', 'ak', 'in-1', 'ph', NULL, '2026-09', '2026-09-01', NULL);
		`);
		const entries = readJevTriageEntries(database, { decision: null, search: null, limit: 50, offset: 0 });
		assert.equal(entries.length, 0);
	} finally {
		database.close();
	}
});

test("an R1 exclusion object decodes by its rule name", () => {
	const triage = decodeJevTriage({
		posting_key: "fixture:jev-lab-intern",
		mode: "promoted",
		state: "current",
		decision: "exclude",
		fit_probability: 0.85,
		fit_score: 0.8875,
		primary_rule: "restricted_role",
		exclusions: JSON.stringify([
			{
				rule: "restricted_role",
				basis: "model_assisted_policy",
				question_ids: ["citizenship_or_clearance"],
				probabilities: [["citizenship_or_clearance", 0.95]],
				guard_code: "fired",
				policy_value: "exclude",
				policy_exclusion: true,
			},
		]),
		review_flags: "[]",
		diagnostic_flags: "[]",
		qualifier_version: "jev:7f6cf2d9302311874743e3147870ffb16cf30a886ea1c077e40159573f0c82b3",
		assessment_key: "ak",
		input_version: "in",
		policy_hash: null,
		model_id: "jev-1.13.0",
		as_of_month: "2026-09",
		decided_at: "2026-09-10T12:02:00+00:00",
		reason: "Excluded by your policy.",
	});
	assert.deepEqual(triage.exclusions, ["restricted_role"]);
	assert.equal(triage.primaryRule, "restricted_role");
	assert.equal(Object.hasOwn(triage, "fitProbability"), false);
	assert.equal(Object.hasOwn(triage, "fitScore"), false);
	assert.equal(JSON.stringify(triage).includes("0.8"), false);
});

test("a malformed exclusions list is refused rather than rendered", () => {
	assert.throws(() =>
		decodeJevTriage({
			posting_key: "greenhouse:acme:1",
			mode: "promoted",
			state: "current",
			decision: "review",
			fit_probability: 0.5,
			fit_score: 0.4,
			primary_rule: "experience_gap",
			exclusions: "{",
			review_flags: "[]",
			diagnostic_flags: "[]",
			qualifier_version: "jev-1",
			assessment_key: "ak",
			input_version: "in",
			policy_hash: null,
			model_id: null,
			as_of_month: "2026-09",
			decided_at: null,
			reason: null,
		}),
	);
});
