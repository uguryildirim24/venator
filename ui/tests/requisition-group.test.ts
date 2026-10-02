import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { countPostingEntryTotals, countPostingEntries, readPostingDetail, readPostingEntries } from "../server/queries.ts";

test("distinct requisitions share one row, highest keep probability first, each detail reachable", () => {
	const db = emptyViewDatabase();
	try {
		db.prepare("INSERT INTO runs (at, status, stage) VALUES (?, 'ok', 'discover')").run("2026-09-28T00:00:00Z");
		db.prepare("INSERT INTO runs (at, status, stage) VALUES (?, 'ok', 'discover')").run("2026-09-29T00:00:00Z");
		for (let n = 0; n < 7; n += 1) {
			const key = `abbott-${n}`;
			db.prepare("INSERT INTO postings (key, source, board, company, title, location, url, discovered_at) VALUES (?, 'workday', 'abbott', 'Abbott', ?, 'Westbrook, ME', ?, ?)")
				.run(key, n === 1 ? " sample processing associate i - 3rd shift " : "Sample Processing Associate I - 3rd Shift", `https://example.test/${key}`, n === 3 ? "2026-09-28T12:00:00Z" : "2026-01-01");
			db.prepare("INSERT INTO decisions (posting_key, stage, verdict, decided_at) VALUES (?, 'hard_filter', 'pass', '2026-01-02')").run(key);
			db.prepare("INSERT INTO keep_scores VALUES (?, 'current-input', 'current-adapter', ?, '2026-09-29', 0)").run(key, n === 5 ? 0.95 : 0.7);
		}
		const query = { status: "queued" as const, rule: null, search: null, sort: "verified" as const, limit: 2 };
		assert.equal(countPostingEntries(db, query), 1);
		assert.deepEqual(countPostingEntryTotals(db, query), { total: 1, newCount: 1 });
		const [row] = readPostingEntries(db, query);
		assert.equal(row?.posting.key, "abbott-5");
		assert.equal(row?.isNew, true);
		assert.equal(row?.groupKeys?.length, 7);
		assert.equal(readPostingEntries(db, { ...query, offset: 1 }).length, 0);
		for (const key of row?.groupKeys ?? []) assert.equal(readPostingDetail(db, key)?.posting.key, key);
	} finally { db.close(); }
});
