import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { countPostingEntries, readFunnel, readPostingDetail, readPostingEntries } from "../server/queries.ts";

const query = { status: null, rule: null, search: null, sort: "verified" as const, limit: 30 };

test("only current keep scores route passes at the exact cutoffs; résumé evidence does not", () => {
	const db = emptyViewDatabase();
	try {
		const cases = [
			["high", 1, "queued"], ["half", 0.5, "queued"],
			["below-half", 0.49999, "needs-review"], ["hidden-cut", 0.1, "needs-review"],
			["below-hidden", 0.099999, "hard-killed"], ["zero", 0, "hard-killed"],
			["waiting", null, "unscored"], ["closed", 0.8, "closed"],
			["killed", 0.9, "hard-killed"], ["applied", 0, "applied"],
		] as const;
		for (const [key, probability] of cases) {
			db.prepare("INSERT INTO postings (key, source, board, title, url, discovered_at) VALUES (?, 'greenhouse', 'acme', 'Role', 'https://example.test/job', '2026-01-01')").run(key);
			db.prepare("INSERT INTO decisions (posting_key, stage, verdict, score, decided_at) VALUES (?, 'llm_score', 'queue', 99, '2026-01-01')").run(key);
			db.prepare("INSERT INTO decisions (posting_key, stage, verdict, decided_at) VALUES (?, 'hard_filter', ?, '2026-01-02')").run(key, key === "killed" ? "kill" : "pass");
			db.prepare("INSERT INTO assessments (posting_key,status,summary,evidence,conflicts,unknowns,listing_status,last_verified_at,description_kind) VALUES (?, 'not_suitable', 'Evidence conflict', '[]', '[]', '[]', ?, '2026-01-01', 'full')").run(key, key === "closed" ? "closed" : "open");
			db.prepare("INSERT INTO keep_scores VALUES (?, 'current-input', 'current-adapter', ?, '2026-09-29')").run(key, probability);

		}
		db.exec("INSERT INTO application_states (posting_key,state) VALUES ('applied','prepared')");
		const statuses = Object.fromEntries(readPostingEntries(db, query).map((entry) => [entry.posting.key, entry.status]));
		assert.deepEqual(statuses, Object.fromEntries(cases.map(([key, , status]) => [key, status])));
		assert.deepEqual(readPostingEntries(db, { ...query, status: "queued" }).map((entry) => entry.posting.key), ["high", "half"]);
		assert.deepEqual(readPostingEntries(db, { ...query, status: "needs-review" }).map((entry) => entry.posting.key), ["below-half", "hidden-cut"]);
		assert.equal(readFunnel(db).unscored, 1);
		assert.equal(countPostingEntries(db, { ...query, status: "hard-killed" }), 3);
		assert.equal(readPostingDetail(db, "half")?.status, "queued");
		db.exec("UPDATE keep_scores SET probability = NULL WHERE posting_key = 'half'");
		assert.equal(readPostingDetail(db, "half")?.status, "unscored");
	} finally { db.close(); }
});
