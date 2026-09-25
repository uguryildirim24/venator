/**
 * `pnpm snapshot` — writes each route to a standalone HTML file with the app's stylesheet
 * inlined, so the dashboard can be looked at (and handed to someone) without running it.
 * Snapshots are static: no scripts, no API calls, no interaction.
 *
 * It renders whatever database the server would normally open — the pipeline view when one
 * exists, and the sample view otherwise, which it asks for explicitly — and picks the Postings
 * to feature by asking the API, so the snapshots stay meaningful as the real data changes.
 */

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import type { PostingsResponse } from "../shared/contracts.ts";
import { UI_ROOT } from "../server/locations.ts";
import { startApi } from "./api-process.ts";
import { startRenderer } from "./dom-harness.ts";

const SNAPSHOT_PORT = 5172;
const OUTPUT_DIRECTORY = resolve(UI_ROOT, "snapshots");

type Snapshot = {
	readonly file: string;
	readonly hash: string;
	readonly title: string;
};

function page(title: string, styles: string, markup: string): string {
	return [
		"<!doctype html>",
		'<html lang="en"><head><meta charset="utf-8" />',
		`<title>venator — ${title}</title>`,
		`<style>${styles}</style>`,
		"</head><body>",
		markup,
		"</body></html>",
	].join("\n");
}

/** Prefers a queued Match, then any killed Posting, so the detail snapshot always shows a decision. */
async function featuredKey(origin: string): Promise<string | null> {
	for (const path of ["/api/postings?status=queued&sort=verified&limit=1", "/api/postings?status=hard-killed&limit=1", "/api/postings?limit=1"]) {
		const response = await fetch(`${origin}${path}`).catch(() => null);
		if (response === null || !response.ok) continue;
		const body: PostingsResponse = await response.json();
		const first = body.entries[0];
		if (first !== undefined) return first.posting.key;
	}
	return null;
}

// Sample data is asked for rather than fallen into: the server serves the fixture only to a
// run that asks (`server/db.ts`), and a snapshot of an Install with nothing in it is not what
// anybody wants these for. A real `build/venator.db` still wins — asking only decides what
// happens when there is none.
const api = await startApi({ databasePath: null, port: SNAPSHOT_PORT, sampleData: true });
const styles = readFileSync(resolve(UI_ROOT, "src/styles.css"), "utf8");
mkdirSync(OUTPUT_DIRECTORY, { recursive: true });

const key = await featuredKey(api.origin);
const snapshots: readonly Snapshot[] = [
	{ file: "queue.html", hash: "#/queue", title: "Review queue" },
	{ file: "inspector.html", hash: "#/inspector", title: "Filter inspector" },
	{
		file: "inspector-education-fit.html",
		hash: "#/inspector?status=hard-killed&rule=education_fit",
		title: "Filter inspector — education fit",
	},
	...(key === null
		? []
		: [
				{
					file: "detail.html",
					hash: `#/postings/${encodeURIComponent(key)}`,
					title: "Posting detail",
				},
			]),
];

const renderer = await startRenderer(api.origin);
try {
	for (const snapshot of snapshots) {
		const markup = await renderer.render(snapshot.hash);
		const path = resolve(OUTPUT_DIRECTORY, snapshot.file);
		writeFileSync(path, page(snapshot.title, styles, markup), "utf8");
		process.stdout.write(`  wrote ${path}\n`);
	}
} finally {
	await renderer.close();
	api.stop();
}
