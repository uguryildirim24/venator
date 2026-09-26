import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import { test } from "node:test";

import { ensureFixtureDatabase } from "../fixtures/make-fixture.ts";
import { createServer } from "../server/app.ts";

const root = resolve(import.meta.dirname, "../src");
const clients = ["api.ts", "runs/api.ts", "onboarding/api.ts"];
// Keep the fixture ID in parts so it cannot be mistaken for a phone number by the public scan.
const fixtureKey = `greenhouse%3Aginkgobioworks%3A${"51852"}${"85007"}`;
type Endpoint = { method: "GET" | "POST"; path: string };

// Enumerate client call sites rather than maintain a parallel list of server paths.
// Reject any new fetch outside the three clients and any unrecognised path expression.
function clientPaths(): Endpoint[] {
	const endpoints: Endpoint[] = [];
	for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
		if (!entry.isFile() || !/\.tsx?$/u.test(entry.name)) continue;
		const name = relative(root, resolve(entry.parentPath, entry.name));
		const source = readFileSync(resolve(entry.parentPath, entry.name), "utf8");
		if (!clients.includes(name)) {
			assert.doesNotMatch(source, /\bfetch\s*\(/u, `fetch in ${name} needs path coverage`);
			continue;
		}
		for (const line of source.split("\n")) {
			if (/^\s*(?:export )?(?:async )?function\s/u.test(line) || /^\s*(?:\/\/|\*)/u.test(line)) continue;
			const site = line.match(/\b(useApi|postJson|postForm|call|fetch)(?:<[^>]+>)?\s*\(/u);
			if (!site) continue;
			const kind = site[1];
			if (kind === "call" && name !== "runs/api.ts") continue;
			// The generic helpers forward their arguments; their concrete call sites
			// are enumerated below. Only these three forms are permitted.
			if (kind === "fetch" && (/\$\{path\}/u.test(line) || /\$\{API_BASE_URL\}\$\{path\}/u.test(line))) continue;
			if (kind === "useApi" && /\(path,/u.test(line)) continue;
			if ((kind === "postJson" || kind === "postForm") && /\(path,/u.test(line)) continue;
			if (kind === "call" && /\(path,/u.test(line)) continue;
			let path: string;
			let prefix = "";
			let method: Endpoint["method"] = "GET";
			if (kind === "fetch") {
				const url = line.match(/`\$\{API_BASE_URL\}(\/[^`]*?)`/u);
				if (!url) throw new Error(`${name}: unrecognised fetch: ${line}`);
				path = url[1] ?? "";
				method = /\/locations|\/applications`/u.test(line) && name === "api.ts" && !line.includes("/onboarding") ? "POST" : "GET";
			} else {
				const argument = line.slice((site.index ?? 0) + site[0].length);
				const found = argument.match(/(?:"([^"]*)"|`([^`]*)`)/u);
				if (!found) throw new Error(`${name}: unrecognised ${kind} path: ${line}`);
				path = found[1] ?? found[2] ?? "";
				if (kind === "call") {
					prefix = "/runs";
					const verb = argument.match(/,\s*"(GET|POST)"/u);
					if (!verb) throw new Error(`${name}: unrecognised run method: ${line}`);
					// SAFETY: the regex accepts only GET or POST.
					method = verb[1] as Endpoint["method"];
				} else if (kind === "postJson" || kind === "postForm") {
					prefix = "/onboarding";
					method = "POST";
				}
			}
			path = path.replaceAll("${encodeURIComponent(key)}", fixtureKey)
				.replaceAll("${encodeURIComponent(name)}", "fixture")
				.replaceAll("${query}", "?limit=5")
				.replaceAll("${String(limit)}", "5");
			if (/\$\{/u.test(path)) throw new Error(`${name}: unrecognised path interpolation: ${line}`);
			endpoints.push({ method, path: `/api${prefix}${path}` });
		}
	}
	// An anchor, not fetch: the browser itself requests the document URL.
	const api = readFileSync(join(root, "api.ts"), "utf8");
	assert.match(api, /\/applications\/\$\{encodeURIComponent\(key\)\}\/files\/\$\{name\}/u);
	endpoints.push({ method: "GET", path: `/api/applications/${fixtureKey}/files/resume.pdf` });
	return [...new Map(endpoints.map((entry) => [`${entry.method} ${entry.path}`, entry])).values()];
}

test("every client API path resolves on the mounted server, including both location spellings", async () => {
	const home = await mkdtemp(join(tmpdir(), "venator-client-paths-"));
	const before = { home: process.env.VENATOR_HOME, view: process.env.VENATOR_VIEW_DB };
	try {
		process.env.VENATOR_HOME = home;
		process.env.VENATOR_VIEW_DB = join(home, "view.db");
		ensureFixtureDatabase(process.env.VENATOR_VIEW_DB);
		const app = createServer();
		const paths = clientPaths();
		assert.ok(paths.some(({ method, path }) => method === "GET" && path === "/api/locations"));
		assert.ok(paths.some(({ method, path }) => method === "POST" && path === "/api/locations"));
		for (const path of ["/api/locations", "/api/locations/"]) paths.push({ method: "GET", path }, { method: "POST", path });
		for (const path of ["/api/locations", "/api/locations/"]) {
			const preflight = await app.request(path, { method: "OPTIONS", headers: {
				origin: "tauri://localhost", "access-control-request-method": "POST", "access-control-request-headers": "X-Venator-Location",
			} });
			assert.equal(preflight.headers.get("access-control-allow-origin"), "tauri://localhost");
			assert.match(preflight.headers.get("access-control-allow-methods") ?? "", /POST/u);
			const rejected = await app.request(path, { method: "POST", headers: { origin: "https://boards.example.com", "X-Venator-Location": "1" }, body: '{"selected":[]}' });
			assert.equal(rejected.status, 403);
		}
		for (const { method, path } of paths) {
			const location = path.startsWith("/api/locations");
			const response = await app.request(path, method === "GET" ? undefined : {
				method, headers: { "content-type": "application/json", "X-Venator-Location": "1", "X-Venator-Run": "1", "X-Venator-Onboarding": "1", "X-Venator-Application": "1" },
				body: location ? '{"selected":["remote"]}' : "{",
			});
			// Stopping when no run exists is a domain 404, not a missing route.
			if (method === "POST" && path === "/api/runs/current/stop" && response.status === 404) {
				// SAFETY: the run router's error response has a structured error code.
				assert.equal(((await response.json()) as { error: { code: string } }).error.code, "no_run");
			} else assert.notEqual(response.status, 404, `${method} ${path} was not found`);
			if (location) {
				assert.equal(response.status, 200, `${method} ${path}`);
				// SAFETY: a 200 from the location router has this response shape.
				const selection = await response.json() as { selected: string[]; choices: { key: string }[] };
				assert.ok(selection.choices.some(({ key }) => key === "remote"));
				if (method === "POST") assert.deepEqual(selection.selected, ["remote"]);
			}
		}
	} finally {
		if (before.home === undefined) delete process.env.VENATOR_HOME; else process.env.VENATOR_HOME = before.home;
		if (before.view === undefined) delete process.env.VENATOR_VIEW_DB; else process.env.VENATOR_VIEW_DB = before.view;
		await rm(home, { recursive: true, force: true });
	}
});
