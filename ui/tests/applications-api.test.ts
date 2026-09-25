import assert from "node:assert/strict";
import { test } from "node:test";
import { Hono } from "hono";
import { createApplicationRoutes } from "../server/applications/routes.ts";
import { applicationAction } from "../src/api.ts";

const profile = () => ["--profile", "candidate"];
const headers = { "Content-Type": "application/json", "X-Venator-Application": "1", origin: "tauri://localhost" };

test("the real frontend preparation request reaches the mounted application route", async (context) => {
	let observed: readonly string[] = [];
	const server = new Hono();
	server.route("/api/applications", createApplicationRoutes(async (argv) => {
		observed = argv;
		return { prepared: true };
	}, profile));
	context.mock.method(globalThis, "fetch", (input: string, init: RequestInit) => server.request(input, init));
	const result = await applicationAction("greenhouse:acme:1", "prepare", { provider: "codex" });
	assert.equal(result.prepared, true);
	assert.deepEqual(observed, ["prepare", "greenhouse:acme:1", "--profile", "candidate", "--provider", "codex"]);
});

test("application actions require the local action header and an explicit preparation provider", async () => {
	const calls: readonly string[][] = [];
	const app = createApplicationRoutes(async () => { assert.fail("must not invoke a model"); }, profile);
	const external = await app.request("/", { method: "POST", body: JSON.stringify({ key: "p:1", action: "prepare" }) });
	assert.equal(external.status, 403);
	const missing = await app.request("/", { method: "POST", headers, body: JSON.stringify({ key: "p:1", action: "prepare" }) });
	assert.equal(missing.status, 400);
	assert.equal(calls.length, 0);
});

test("preparation uses fixed argv, with no executable or profile path from the request", async () => {
	let observed: readonly string[] = [];
	const app = createApplicationRoutes(async (argv) => { observed = argv; return { prepared: true }; }, profile);
	const response = await app.request("/", { method: "POST", headers, body: JSON.stringify({
		key: "greenhouse:acme:1", action: "prepare", provider: "claude", coverLetter: true, profile: "../../other",
	}) });
	assert.equal(response.status, 200);
	assert.deepEqual(observed, ["prepare", "greenhouse:acme:1", "--profile", "candidate", "--provider", "claude", "--cover-letter"]);
});

test("two store-writing application operations cannot overlap and failure releases the guard", async () => {
	let release: () => void = () => {};
	const blocked = new Promise<void>((resolve) => { release = resolve; });
	let started: () => void = () => {};
	const ready = new Promise<void>((resolve) => { started = resolve; });
	let calls = 0;
	const app = createApplicationRoutes(async () => { calls += 1; started(); await blocked; throw new Error("provider unavailable"); }, profile);
	const init = { method: "POST", headers, body: JSON.stringify({ key: "p:1", action: "save" }) };
	const first = app.request("/", init);
	await ready;
	assert.equal((await app.request("/", init)).status, 409);
	release();
	assert.equal((await first).status, 400);
	assert.equal((await app.request("/", init)).status, 400);
	assert.equal(calls, 2);
});

test("document reads are allowlisted and responses are not cached", async () => {
	const app = createApplicationRoutes(async () => ({ base64: Buffer.from("resume text").toString("base64") }), profile);
	assert.equal((await app.request("/p%3A1/files/secrets.txt")).status, 404);
	const file = await app.request("/p%3A1/files/resume.txt");
	assert.equal(file.status, 200);
	assert.equal(file.headers.get("cache-control"), "no-store");
	assert.equal(await file.text(), "resume text");
});
