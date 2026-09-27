import assert from "node:assert/strict";
import { test } from "node:test";
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Hono } from "hono";
import { applicationCommand, createApplicationRoutes } from "../server/applications/routes.ts";
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

test("status limits simultaneous children to two", async () => {
	let calls = 0;
	let release: () => void = () => {};
	const blocked = new Promise<void>((resolve) => { release = resolve; });
	const app = createApplicationRoutes(async () => { calls++; await blocked; return { prepared: false }; }, profile);
	const pending = ["/a", "/b", "/c"].map((key) => app.request(key));
	await new Promise((resolve) => setTimeout(resolve, 10));
	assert.equal(calls, 2);
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
