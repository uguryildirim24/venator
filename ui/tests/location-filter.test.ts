import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { availableLocations, locationChoices, matchingLocationKeys, readLocationSelection, saveLocationSelection } from "../server/location-filter.ts";
import { createLocationRoutes } from "../server/location-routes.ts";
import { countPostingEntryTotals, readFunnel, readPostingEntries, type PostingQuery } from "../server/queries.ts";

test("location choices join city and state spellings, Remote variants, and keep missing explicit", () => {
	assert.deepEqual(locationChoices("Boston, MA"), locationChoices("Boston, Massachusetts"));
	assert.deepEqual(locationChoices("Remote - US"), locationChoices("remote"));
	assert.deepEqual(locationChoices("Remote (US)"), locationChoices("remote"));
	assert.deepEqual(locationChoices(null), [{ key: "none", label: "No location" }]);
	assert.ok(locationChoices("Boston, MA").some(({ key }) => key === "state:MA"));
	assert.deepEqual(locationChoices("Boston, MA (Remote)"), [
		{ key: "remote", label: "Remote" }, { key: "state:MA", label: "Massachusetts" }, { key: "city:boston,MA", label: "Boston, MA", parent: "state:MA" },
	]);
	assert.deepEqual(locationChoices("Boston, MA; Cambridge, Massachusetts; Boston, MA").map(({ key }) => key),
		["state:MA", "city:boston,MA", "city:cambridge,MA"]);
});

test("source ATS variants collapse to one city and multi-place Postings count once in each place", () => {
	const variants = ["Boston-MA", "Boston, MA", "Boston, Massachusetts", "US-MA-Boston", "Boston MA 02115", "US - Boston, MA"];
	for (const value of variants) assert.ok(locationChoices(value).some(({ key }) => key === "city:boston,MA"), value);
	assert.deepEqual(locationChoices("India - Hyderabad").map(({ key }) => key), ["country:india", "city:hyderabad,india"]);
	assert.deepEqual(locationChoices("3 Locations", ["US - Boston, MA", "US - Cambridge, MA", "Boston-MA"]).map(({ key }) => key),
		["state:MA", "city:boston,MA", "city:cambridge,MA"]);
	assert.deepEqual(locationChoices("2 Locations"), [{ key: "several", label: "Several locations" }]);
	const listing = JSON.parse(readFileSync(new URL("../../tests/discover/fixtures/workday_listing.json", import.meta.url), "utf8"));
	assert.deepEqual(locationChoices("3 Locations", listing.jobPostings[1].additionalLocations).map(({ key }) => key),
		["state:MA", "city:cambridge,MA", "state:CA", "city:thousand oaks,CA"]);
	const db = emptyViewDatabase();
	try {
		const insert = db.prepare("INSERT INTO postings (key, source, board, title, location, location_places, url, discovered_at) VALUES (?, 'workday', 'acme', 'Job', ?, ?, 'https://example.test/job', '2026-01-01')");
		insert.run("one", "2 Locations", JSON.stringify(["US - Boston, MA", "US - Cambridge, MA"]));
		insert.run("two", "2 Locations", "[]");
		insert.run("three", "Remote - US", "[]");
		const choices = availableLocations(db);
		assert.equal(choices.find(({ key }) => key === "state:MA")?.count, 1);
		assert.equal(choices.find(({ key }) => key === "city:boston,MA")?.count, 1);
		assert.equal(choices.find(({ key }) => key === "city:cambridge,MA")?.count, 1);
		assert.equal(choices.find(({ key }) => key === "several")?.count, 1);
		assert.deepEqual(choices.map(({ count }) => count), choices.map(({ count }) => count).sort((a, b) => b - a));
		assert.equal(choices.find(({ key }) => key === "city:cambridge,MA")?.parent, "state:MA");
		assert.equal(matchingLocationKeys(db, ["city:cambridge,MA"]), '["one"]');
	} finally { db.close(); }
});

test("site suffixes, boilerplate, case and punctuation resolve to the same city", () => {
	for (const label of ["bOsToN, Ma (Main Campus)", "Boston MA", "Boston Job Posting Location", "Boston, MA University Campus", "Boston, MA (West Site)"]) {
		assert.ok(locationChoices(label).some(({ key }) => key === "city:boston,MA"), label);
	}
	assert.ok(locationChoices("Worcester, MA Memorial Campus").some(({ key }) => key === "city:worcester,MA"));
	assert.ok(locationChoices("Cambridge Posting Location").some(({ key }) => key === "city:cambridge,MA"));
	assert.ok(locationChoices("Boston, Ma (main Campus)").some(({ key }) => key === "city:boston,MA"));
	for (const path of ["US, Indianapolis IN", "USA - New Jersey - Rahway", "US - California - Thousand Oaks", "Princeton - NJ - US"]) {
		assert.equal(locationChoices(path).some(({ key }) => key === "other"), false, path);
	}
});

test("joined places split, but ambiguous cities and unnamed campuses are not guessed", () => {
	const keys = locationChoices("Boston, MA | Springfield, IL; Seattle, WA / Boston Job Posting Location").map(({ key }) => key);
	assert.deepEqual(keys, ["state:MA", "city:boston,MA", "state:IL", "city:springfield,IL", "state:WA", "city:seattle,WA"]);
	assert.ok(locationChoices("Springfield").some(({ key }) => key === "other"));
	assert.ok(locationChoices("Springfield, MA").some(({ key }) => key === "city:springfield,MA"));
	assert.ok(locationChoices("University Medical Campus").some(({ key }) => key === "other"));
});

test("standalone Remote variants have no bogus children, including sourced lists", () => {
	for (const label of ["Remote, North Carolina, USA", "United States - Remote", "USA-NC-Remote", "Remote Position (USA)", "Remote-Friendly (Travel Required)", "US: USA Remote", "Remote_United States", "(Remote US)", "Option to work remote in Canada"]) {
		assert.deepEqual(locationChoices(label), [{ key: "remote", label: "Remote" }], label);
	}
	assert.deepEqual(locationChoices("2 Locations", ["Remote, USA", "Boston, MA"]).map(({ key }) => key), ["remote", "state:MA", "city:boston,MA"]);
	assert.deepEqual(locationChoices("Boston, MA or Remote").map(({ key }) => key), ["remote", "state:MA", "city:boston,MA"]);
	assert.deepEqual(locationChoices("Spain - Remote Location-Madrid"), [{ key: "remote", label: "Remote" }]);
});

test("a cross-origin simple POST cannot change the Install's location choice", async () => {
	const routes = createLocationRoutes();
	for (const headers of [{ "content-type": "text/plain", origin: "https://boards.example.com" }, { "content-type": "text/plain" }]) {
		const response = await routes.request("/", { method: "POST", headers, body: '{"selected":[]}' });
		assert.equal(response.status, 403);
	}
	const forged = await routes.request("/", { method: "POST", headers: { origin: "https://boards.example.com", "X-Venator-Location": "1" }, body: '{"selected":[]}' });
	assert.equal(forged.status, 403);
	const preflight = await routes.request("/", { method: "OPTIONS", headers: { origin: "tauri://localhost", "access-control-request-method": "POST", "access-control-request-headers": "X-Venator-Location" } });
	assert.match(preflight.headers.get("access-control-allow-headers") ?? "", /X-Venator-Location/);
});

test("location filter narrows list, pagination and counts without changing the view", () => {
	const db = emptyViewDatabase();
	try {
		const insert = db.prepare("INSERT INTO postings (key, source, board, title, location, url, discovered_at) VALUES (?, 'greenhouse', 'acme', ?, ?, 'https://example.test/job', '2026-01-01')");
		insert.run("one", "One", "Boston, MA");
		insert.run("two", "Two", "Boston, Massachusetts");
		insert.run("three", "Three", "Springfield, Massachusetts");
		insert.run("four", "Four", "Remote - US");
		insert.run("five", "Five", null);
		insert.run("six", "Six", "Cambridge, MA; Remote - US");
		const choices = availableLocations(db);
		assert.equal(choices.find(({ key }) => key === "city:boston,MA")?.count, 2);
		const base: PostingQuery = { search: null, status: null, rule: null, sort: "verified", limit: 1 };
		const city = matchingLocationKeys(db, ["city:boston,MA"]);
		assert.equal(countPostingEntryTotals(db, { ...base, locationKeys: city }).total, 2);
		assert.equal(readPostingEntries(db, { ...base, locationKeys: city }).length, 1);
		assert.equal(readPostingEntries(db, { ...base, locationKeys: city, offset: 1 }).length, 1);
		assert.equal(readFunnel(db, city).discovered, 2);
		assert.equal(countPostingEntryTotals(db, { ...base, locationKeys: matchingLocationKeys(db, ["state:MA"]) }).total, 4);
		assert.equal(countPostingEntryTotals(db, { ...base, locationKeys: matchingLocationKeys(db, ["remote", "none"]) }).total, 3);
		assert.equal(countPostingEntryTotals(db, base).total, 6);
	} finally { db.close(); }
});

test("choice stays inside one Install and survives a read from disk", async () => {
	const home = await mkdtemp(join(tmpdir(), "venator-locations-"));
	const context = { platform: "linux" as const, home, workingDirectory: home, checkoutRoot: null, environment: (name: string) => name === "VENATOR_HOME" ? home : undefined };
	try {
		await saveLocationSelection(["remote", "none"], context);
		assert.deepEqual(readLocationSelection(context), ["remote", "none"]);
		assert.deepEqual(JSON.parse(await readFile(join(home, "location-filter.json"), "utf8")), ["remote", "none"]);
	} finally { await rm(home, { recursive: true, force: true }); }
});
