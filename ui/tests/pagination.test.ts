import assert from "node:assert/strict";
import { test } from "node:test";
import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { countPostingEntries, readPostingEntries, type PostingQuery } from "../server/queries.ts";
import { createApiRoutes } from "../server/routes.ts";
import { inspectorHash, parseRoute, postingHash, postingReturnHash, queueHash } from "../src/router.ts";

test("all jobs remain reachable across pages, including tied sort values past 1000", () => {
	const db = emptyViewDatabase();
	try {
		const insert = db.prepare("INSERT INTO postings (key, source, board, title, url, discovered_at) VALUES (?, 'greenhouse', 'acme', ?, 'https://example.test/job', '2026-01-01')");
		for (let index = 1072; index >= 0; index -= 1) insert.run(`job:${String(index).padStart(4, "0")}`, index % 2 === 0 ? "Lab assistant" : "Data assistant");
		for (const sort of ["verified", "discovered", "title"] as const) {
		const query: PostingQuery = { search: null, status: null, rule: null, sort, limit: 50 };
			assert.equal(countPostingEntries(db, query), 1073);
			const keys: string[] = [];
			for (let offset = 0; offset < 1073; offset += 50) {
				const entries = readPostingEntries(db, { ...query, offset });
				assert.equal(entries.length, Math.min(50, 1073 - offset));
				keys.push(...entries.map((entry) => entry.posting.key));
			}
			assert.equal(new Set(keys).size, 1073);
			assert.deepEqual(readPostingEntries(db, { ...query, offset: 1073 }), []);
			assert.deepEqual(readPostingEntries(db, query), readPostingEntries(db, { ...query, offset: 0 }));
			const filtered = { ...query, search: "Lab", offset: 500 };
			assert.equal(countPostingEntries(db, filtered), 537);
			assert.equal(readPostingEntries(db, filtered).length, 37);
			assert.ok(readPostingEntries(db, filtered).every((entry) => entry.posting.title === "Lab assistant"));
		}
	} finally { db.close(); }
});

test("paging bounds reject malformed, negative, fractional and unsafe numbers before reading a database", async () => {
	const app = createApiRoutes();
	for (const offset of ["-1", "1.5", "1x", "1e3", "9007199254740992", "0;DROP TABLE postings"]) {
		const response = await app.request(`/postings?offset=${encodeURIComponent(offset)}`);
		assert.equal(response.status, 400, offset);
	}
	for (const limit of ["0", "-1", "1.5", "50x", "9007199254740992"]) {
		assert.equal((await app.request(`/postings?limit=${limit}`)).status, 400);
	}
});

test("an unknown status is refused, not read as an empty filter", async () => {
	const app = createApiRoutes();
	for (const status of ["killed", "Queued", "needs_review"]) {
		const response = await app.request(`/postings?status=${status}`);
		assert.equal(response.status, 400, status);
	}
});

test("list links preserve page and filters through focus, while changed filters reset paging", () => {
	const filters = { status: "hard-killed" as const, rule: "location", search: "lab & data" };
	for (const returnTo of [queueHash("needs-review", "lab & data", 23), inspectorHash(filters, 13)]) {
		const route = parseRoute(postingHash("greenhouse:acme:1", "needs-review", returnTo));
		assert.equal(route.name, "posting");
		if (route.name !== "posting") assert.fail("expected focus");
		assert.equal(postingReturnHash(route.from, route.returnTo), returnTo);
	}
	assert.equal(parseRoute(queueHash("needs-review", "changed")).name, "queue");
	assert.ok(!queueHash("needs-review", "changed").includes("page="));
	assert.ok(!inspectorHash({ ...filters, search: "changed" }).includes("page="));
	for (const page of ["-1", "2.5", "abc", "9007199254740992"]) {
		const route = parseRoute(`#/queue?page=${page}`);
		assert.equal(route.name === "queue" ? route.page : -1, 0);
	}
});
