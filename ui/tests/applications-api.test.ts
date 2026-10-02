import assert from "node:assert/strict";
import { test } from "node:test";
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Hono } from "hono";
import { applicationCommand, createApplicationRoutes } from "../server/applications/routes.ts";
import { applicationAction } from "../src/api.ts";
import { takePipelineLock } from "../server/pipeline-lock.ts";

const profile = () => ["--profile", "candidate"];
const headers = { "Content-Type": "application/json", "X-Venator-Application": "1", origin: "tauri://localhost" };

test("the real frontend Apply request reaches the mounted application route", async (context) => {
	let observed: readonly string[] = [];
	const server = new Hono();
	server.route("/api/applications", createApplicationRoutes(async (argv) => {
		observed = argv;
		return { prepared: true, file_hashes: { "resume.pdf": "resume-hash", "letter.pdf": "letter-hash" } };
	}, profile));
	context.mock.method(globalThis, "fetch", (input: string, init: RequestInit) => server.request(input, init));
	const result = await applicationAction("greenhouse:acme:1", "apply");
	assert.equal(result.prepared, true);
	assert.deepEqual(result.file_hashes, { "resume.pdf": "resume-hash", "letter.pdf": "letter-hash" });
	assert.deepEqual(observed, ["apply", "greenhouse:acme:1", "--profile", "candidate"]);
});

test("Posting contacts and outreach use the selected Profile and a matched contact id", async (context) => {
	const calls: string[][] = [];
	const server = new Hono();
	server.route("/api/applications", createApplicationRoutes(async (argv) => { calls.push([...argv]); return { contacts: [] }; }, profile));
	context.mock.method(globalThis, "fetch", (input: string, init: RequestInit) => server.request(input, init));
	const id = "a".repeat(32);
	assert.equal((await server.request("/api/applications/greenhouse%3Aexample%3A1/contacts")).status, 200);
	await applicationAction("greenhouse:example:1", "outreach", { id });
	await applicationAction("greenhouse:example:1", "outreach_undo", { id });
	assert.deepEqual(calls, [
		["contacts", "greenhouse:example:1", "--profile", "candidate"],
		["outreach", "greenhouse:example:1", "--profile", "candidate", "--contact", id],
		["outreach_undo", "greenhouse:example:1", "--profile", "candidate", "--contact", id],
	]);
});

test("application actions require the local action header and known action", async () => {
	const calls: readonly string[][] = [];
	const app = createApplicationRoutes(async () => { assert.fail("must not invoke a model"); }, profile);
	const external = await app.request("/", { method: "POST", body: JSON.stringify({ key: "p:1", action: "apply" }) });
	assert.equal(external.status, 403);
	const missing = await app.request("/", { method: "POST", headers, body: JSON.stringify({ key: "p:1", action: "unknown" }) });
	assert.equal(missing.status, 400);
	assert.equal(calls.length, 0);
});

test("Apply uses fixed argv, with no executable or profile path from the request", async () => {
	let observed: readonly string[] = [];
	const app = createApplicationRoutes(async (argv) => { observed = argv; return { prepared: true }; }, profile);
	const response = await app.request("/", { method: "POST", headers, body: JSON.stringify({
		key: "greenhouse:acme:1", action: "apply", profile: "../../other",
	}) });
	assert.equal(response.status, 200);
	assert.deepEqual(observed, ["apply", "greenhouse:acme:1", "--profile", "candidate"]);
});

test("edit transports only bounded reviewed passages and a version", async () => {
	let observed: readonly string[] = [];
	const app = createApplicationRoutes(async (argv) => { observed = argv; return { prepared: true, version: "next" }; }, profile);
	const edits = [{ draft_id: "letter:0", text: "My own words." }];
	const response = await app.request("/", { method: "POST", headers, body: JSON.stringify({ action: "edit", key: "greenhouse:acme:1", version: "abc123", edits }) });
	assert.equal(response.status, 200);
	assert.deepEqual(observed, ["edit", "greenhouse:acme:1", "--profile", "candidate", "--version", "abc123", "--edits", JSON.stringify(edits)]);
	assert.equal((await app.request("/", { method: "POST", headers, body: JSON.stringify({ action: "edit", key: "greenhouse:acme:1", version: "abc123", edits: [{ draft_id: "letter:0", text: "a\nb" }] }) })).status, 400);
	assert.equal((await app.request("/", { method: "POST", headers, body: JSON.stringify({ action: "edit", key: "greenhouse:acme:1", version: "abc123", edits: [{ draft_id: "letter:0", text: "a".repeat(1801) }] }) })).status, 400);
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

test("loop lock makes application actions answer 409; a dead pid is taken over", async () => {
	const home = mkdtempSync(join(tmpdir(), "venator-pipeline-"));
	const previous = process.env.VENATOR_HOME;
	process.env.VENATOR_HOME = home;
	try {
		const release = takePipelineLock();
		assert.ok(release);
		const app = createApplicationRoutes(async () => { assert.fail("busy lock must not run action"); }, profile);
		const init = { method: "POST", headers, body: JSON.stringify({ key: "p:1", action: "save" }) };
		assert.equal((await app.request("/", init)).status, 409);
		release();
		const path = join(home, "build", ".pipeline.lock");
		writeFileSync(path, JSON.stringify({ pid: 99999999, at: 1 }));
		const recovered = takePipelineLock();
		assert.ok(recovered);
		recovered();
	} finally {
		if (previous === undefined) delete process.env.VENATOR_HOME;
		else process.env.VENATOR_HOME = previous;
		rmSync(home, { recursive: true, force: true });
	}
});

test("status single-flights by Profile and key, without caching a subsequent read", async () => {
	let calls = 0;
	let release: () => void = () => {};
	const blocked = new Promise<void>((resolve) => { release = resolve; });
	const app = createApplicationRoutes(async () => { calls++; await blocked; return { prepared: false }; }, profile);
	const first = app.request("/p%3A1");
	const second = app.request("/p%3A1");
	await new Promise((resolve) => setTimeout(resolve, 10));
	assert.equal(calls, 1);
	release();
	assert.equal((await first).status, 200);
	assert.equal((await second).status, 200);
	assert.equal((await app.request("/p%3A1")).status, 200);
	assert.equal(calls, 2);
});

test("aborting the last reader cancels its status child; other readers keep theirs", async () => {
	let stopped = 0;
	let ready: () => void = () => {};
	const started = new Promise<void>((resolve) => { ready = resolve; });
	const app = createApplicationRoutes((_argv, options) => new Promise((resolve) => {
		options?.signal?.addEventListener("abort", () => { stopped++; resolve({ prepared: false }); });
		ready();
	}), profile);
	const first = new AbortController();
	const second = new AbortController();
	const one = app.request("/p%3A1", { signal: first.signal });
	await started;
	const two = app.request("/p%3A1", { signal: second.signal });
	await new Promise((resolve) => setTimeout(resolve, 10));
	first.abort();
	await one;
	assert.equal(stopped, 0);
	second.abort();
	await two;
	assert.equal(stopped, 1);
});

test("an aborted status kills the actual child process group", { skip: process.platform === "win32" }, async () => {
	const directory = mkdtempSync(join(tmpdir(), "venator-status-abort-"));
	const script = join(directory, "interpreter");
	const pidFile = join(directory, "pid");
	writeFileSync(script, `#!/bin/sh\necho $$ > '${pidFile}'\nexec sleep 30\n`);
	chmodSync(script, 0o755);
	const previous = process.env.VENATOR_PYTHON;
	process.env.VENATOR_PYTHON = script;
	const controller = new AbortController();
	try {
		const result = applicationCommand(["status", "synthetic"], { signal: controller.signal, timeoutMs: 2_000 });
		for (let attempt = 0; attempt < 100 && !existsSync(pidFile); attempt++) {
			await new Promise((resolve) => setTimeout(resolve, 10));
		}
		assert.equal(existsSync(pidFile), true);
		const pid = Number(readFileSync(pidFile, "utf8"));
		controller.abort();
		await assert.rejects(result);
		assert.throws(() => process.kill(-pid, 0), { code: "ESRCH" });
	} finally {
		controller.abort();
		if (previous === undefined) delete process.env.VENATOR_PYTHON;
		else process.env.VENATOR_PYTHON = previous;
		rmSync(directory, { recursive: true, force: true });
	}
});

test("a confirmation status read holds the same busy flag as application writes", async () => {
	let release: () => void = () => {};
	const blocked = new Promise<void>((resolve) => { release = resolve; });
	let ready: () => void = () => {};
	const started = new Promise<void>((resolve) => { ready = resolve; });
	const app = createApplicationRoutes(async (argv) => {
		if (argv[0] === "status") { ready(); await blocked; }
		return { prepared: false };
	}, profile);
	const status = app.request("/greenhouse%3Afixture%3A123");
	await started;
	const action = { method: "POST", headers, body: JSON.stringify({ action: "applied", key: "greenhouse:fixture:123" }) };
	assert.equal((await app.request("/", action)).status, 409);
	release();
	assert.equal((await status).status, 200);
	assert.equal((await app.request("/", action)).status, 200);
});

test("status serializes children because a status read can record Applied", async () => {
	let calls = 0;
	let release: () => void = () => {};
	const blocked = new Promise<void>((resolve) => { release = resolve; });
	const app = createApplicationRoutes(async () => { calls++; await blocked; return { prepared: false }; }, profile);
	const pending = ["/a", "/b", "/c"].map((key) => app.request(key));
	await new Promise((resolve) => setTimeout(resolve, 10));
	assert.equal(calls, 1);
	release();
	await Promise.all(pending);
	assert.equal(calls, 3);
});

test("document reads are allowlisted and responses are not cached", async () => {
	const app = createApplicationRoutes(async () => ({ base64: Buffer.from("resume text").toString("base64") }), profile);
	assert.equal((await app.request("/p%3A1/files/secrets.txt")).status, 404);
	const file = await app.request("/p%3A1/files/resume.txt");
	assert.equal(file.status, 200);
	assert.equal(file.headers.get("cache-control"), "no-store");
	assert.equal(await file.text(), "resume text");
});
