import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { createElement, createRef } from "react";
import { createServer } from "vite";
import { emptyViewDatabase } from "../fixtures/make-fixture.ts";
import { createApplicationRoutes } from "../server/applications/routes.ts";
import { pipelineWorkingDirectory, pythonInterpreter, systemContext } from "../server/locations.ts";
import { runEnvironment } from "../server/runs/environment.ts";
import { countPostingEntries, readPostingDetail, readPostingEntries } from "../server/queries.ts";
import { homeListOf } from "../src/lists.ts";
import { installDom } from "../tools/dom-harness.ts";

const key = "greenhouse:fixture:1";
const query = { status: null, rule: null, search: null, sort: "verified" as const, limit: 30 };
function hiddenView() {
	const db = emptyViewDatabase();
	db.prepare("INSERT INTO postings (key, source, board, title, url, discovered_at) VALUES (?, 'greenhouse', 'fixture', 'Lab', 'https://example.test/job', '2026-09-29')").run(key);
	db.prepare("INSERT INTO decisions (posting_key, stage, verdict, decided_at) VALUES (?, 'hard_filter', 'pass', '2026-09-29')").run(key);
	db.prepare("INSERT INTO keep_scores VALUES (?, 'input', 'adapter', 0.04, '2026-09-29')").run(key);
	return db;
}

// Synthetic Install only. Exercise the real approve/reject path without rebuilding View.
const adapter = `
import argparse, json, os, sys
from pathlib import Path
from unittest.mock import patch
from venator import applications
from venator.discover.store import append_observations
from venator.track.store import fold_states, load_events
home = Path(os.environ['VENATOR_HOME'])
profile = home / 'profiles' / 'candidate'
profile.mkdir(parents=True, exist_ok=True)
(profile / 'targeting.yaml').write_text('profile:\\n  name: candidate\\n  id: candidate-test\\n')
(profile / 'resume.yaml').write_text('name: Test Candidate\\n')
key = sys.argv[2]
postings = home / 'data' / 'postings'
if not postings.exists():
    append_observations(postings, [{'key': key, 'source': 'greenhouse', 'board': 'fixture', 'external_id': '1', 'title': 'Lab', 'url': 'https://example.test/job', 'discovered_at': '2026-09-29'}])
args = argparse.Namespace(action=sys.argv[1], key=key, profile='candidate', profile_dir=None)
with patch('venator.view.build.build_database') as build:
    result = applications.perform(args)
    build.assert_called_once()
events = load_events(home / 'data' / 'track')
result['state'] = fold_states(events, {})[key]['state']
result['event'] = events[-1]['event']
print(json.dumps(result))
`;

for (const [action, state, event, list] of [["save", "approved", "approve", "saved"], ["dismiss", "rejected", "reject", "dismissed"]] as const) {
	test(`a score-hidden Posting can ${action} through the route and adapter into ${list}`, async () => {
		const db = hiddenView();
		const home = mkdtempSync(join(tmpdir(), "venator-hidden-"));
		try {
			assert.equal(readPostingDetail(db, key)?.keepProbability, 0.04);
			assert.equal(homeListOf(readPostingEntries(db, query)[0]!), "filtered");
			const context = systemContext();
			const routes = createApplicationRoutes(async (argv) => {
				const result = JSON.parse(execFileSync(pythonInterpreter(context), ["-c", adapter, argv[0]!, argv[1]!], {
					cwd: pipelineWorkingDirectory(context), env: { ...runEnvironment(context), VENATOR_HOME: home }, encoding: "utf8",
				}));
				assert.equal(result.state, state);
				assert.equal(result.event, event);
				db.prepare("INSERT INTO application_states (posting_key, state) VALUES (?, ?)").run(key, state);
				return result;
			}, () => ["--profile", "candidate"]);
			const response = await routes.request("/", { method: "POST", headers: { "Content-Type": "application/json", "X-Venator-Application": "1", origin: "tauri://localhost" }, body: JSON.stringify({ key, action }) });
			assert.equal(response.status, 200, await response.text());
			assert.equal(readPostingDetail(db, key)?.status, "applied");
			assert.equal(countPostingEntries(db, { ...query, status: "hard-killed" }), 0);
			assert.equal(countPostingEntries(db, { ...query, status: "applied", application: list }), 1);
			assert.equal(homeListOf(readPostingEntries(db, query)[0]!), list);
		} finally { db.close(); rmSync(home, { recursive: true, force: true }); }
	});
}

test("score-hidden passes get buttons and s/x; Hard Filter kills get neither and no Apply", async () => {
	const db = hiddenView();
	const actions: string[] = [];
	let failAction: string | null = null;
	const settle = installDom("http://localhost", undefined, (input, init) => {
		const url = new URL(String(input), "http://localhost");
		if (url.pathname === "/api/locations") return Response.json({ selected: [], choices: [] });
		if (url.pathname === "/api/applications" && init?.method === "POST") {
			const action: string = JSON.parse(String(init.body)).action;
			actions.push(action);
			if (action === failAction) return Response.json({ error: `Fixture ${action} failed.` }, { status: 409 });
			return Response.json({});
		}
		if (url.pathname.startsWith("/api/applications/")) return Response.json({ prepared: false });
		if (url.pathname === "/api/postings") return Response.json({ entries: readPostingEntries(db, query), total: 1, newCount: 0 });
		if (url.pathname.startsWith("/api/postings/")) return Response.json(readPostingDetail(db, key));
		assert.fail(`Unexpected request ${url}`);
	});
	const server = await createServer({ root: fileURLToPath(new URL("..", import.meta.url)), server: { middlewareMode: true }, appType: "custom", logLevel: "silent" });
	const { createRoot } = await import("react-dom/client");
	const container = document.createElement("div");
	document.body.append(container);
	const root = createRoot(container);
	try {
		const { Workspace } = await server.ssrLoadModule("/src/views/workspace.tsx");
		const render = async (reloadToken: number) => {
			root.render(createElement(Workspace, { route: reloadToken === 2 ? { name: "posting", key, from: "filtered" } : { name: "queue", list: "filtered", search: "" }, reloadToken, onReload: () => {}, funnel: null, searchField: createRef(), sidebarHidden: false, onShowSidebar: () => {} }));
			const expected = reloadToken === 0 ? 'button[aria-label="Save"]' : '.note-title';
			for (let attempt = 0; attempt < 50; attempt++) {
				await settle();
				if (container.querySelector(expected) && (reloadToken === 0 || container.textContent?.includes("Excluded by a Hard Filter"))) return;
			}
			assert.fail(`Workspace did not load: ${container.innerHTML}`);
		};
		const press = async (letter: string) => {
			window.dispatchEvent(new window.KeyboardEvent("keydown", { key: letter }));
			await settle();
		};
		await render(0);
		assert.ok(container.querySelector('button[aria-label="Save"]'), container.innerHTML);
		assert.ok(container.querySelector('button[aria-label="Dismiss"]'));
		assert.ok(container.querySelector(".margin-application"));
		assert.ok(container.textContent?.includes("Hidden: low keep score · 4%"));
		assert.ok(!container.textContent?.includes("Everything this rule excluded"));
		assert.ok(!container.textContent?.includes("Excluded by a Hard Filter"));
		assert.equal(container.querySelector(".capsule"), null);
		await press("s");
		await press("x");
		assert.deepEqual(actions, ["save", "dismiss"]);
		container.querySelector<HTMLButtonElement>('button[aria-label="Save"]')?.click();
		await settle();
		container.querySelector<HTMLButtonElement>('button[aria-label="Dismiss"]')?.click();
		await settle();
		assert.deepEqual(actions, ["save", "dismiss", "save", "dismiss"]);
		// Hidden Postings still report a failed triage action, without offering Apply.
		for (const [action, letter] of [["save", "s"], ["dismiss", "x"]] as const) {
			failAction = action;
			await press(letter);
			assert.ok(container.querySelector('[role="alert"]')?.textContent?.includes(`Fixture ${action} failed.`), container.innerHTML);
			assert.equal(container.querySelector(".capsule"), null);
			assert.ok(!container.textContent?.includes("Retry Apply"));
		}
		failAction = null;
		db.exec("UPDATE decisions SET verdict = 'kill', rule = 'location', reason = 'Outside New England'");
		await render(1);
		assert.equal(container.querySelector('button[aria-label="Save"]'), null);
		assert.equal(container.querySelector('button[aria-label="Dismiss"]'), null);
		assert.equal(container.querySelector(".margin-application"), null);
		assert.equal(container.querySelector(".capsule"), null);
		assert.ok(container.textContent?.includes("Excluded by a Hard Filter"));
		assert.ok(container.textContent?.includes("Everything this rule excluded"));
		assert.ok(!container.textContent?.includes("Hidden: low keep score"));
		await press("s");
		await press("x");
		assert.deepEqual(actions, ["save", "dismiss", "save", "dismiss", "save", "dismiss"]);
		// A grouped list head can be a pass while the opened requisition is a kill.
		db.exec("UPDATE postings SET company = 'Fixture', location = 'Boston'");
		db.prepare("INSERT INTO postings (key, source, board, company, location, title, url, discovered_at) VALUES ('greenhouse:fixture:2', 'greenhouse', 'fixture', 'Fixture', 'Boston', 'Lab', 'https://example.test/job2', '2026-09-29')").run();
		db.exec("INSERT INTO decisions (posting_key, stage, verdict, decided_at) VALUES ('greenhouse:fixture:2', 'hard_filter', 'pass', '2026-09-29')");
		db.exec("INSERT INTO keep_scores VALUES ('greenhouse:fixture:2', 'input', 'adapter', 0.06, '2026-09-29')");
		assert.equal(readPostingEntries(db, query)[0]?.hardFilter?.verdict, "pass");
		await render(2);
		await press("s");
		await press("x");
		assert.equal(container.querySelector('button[aria-label="Save"]'), null);
		assert.deepEqual(actions, ["save", "dismiss", "save", "dismiss", "save", "dismiss"]);
	} finally { root.unmount(); container.remove(); await server.close(); db.close(); }
});
