/**
 * What a packaged app carries beside its bundled server, and whether anything checks it.
 *
 * Three routes spawn a Python adapter *by path*, as a sibling of their own module. In a
 * checkout that is the source tree. In a packaged app the whole server is one bundled
 * `resources/server.mjs`, so the sibling is `resources/….py` — and until `adapter-staging.ts`
 * nothing put one there, and `tauri.conf.json` named none of them. On a packaged Install those
 * three spawns failed at launch: `boards/resolve` reported that reading a careers page was not
 * part of the Install, and the run surface could describe no run.
 *
 * The load-back is what turned that from two quiet degradations into a stopper: it refuses a
 * write it cannot verify, so an unstaged `verify_profile.py` is a packaged app that cannot
 * write a Profile at all.
 *
 * These cases are the three ways the arrangement can silently stop being true: an adapter added
 * to the server and not to the list, an adapter in the list that `tauri.conf.json` does not
 * name, and two adapters that would land on the same name.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { test } from "node:test";

import { UI_ROOT } from "../server/locations.ts";
import { PIPELINE_ADAPTERS, stageAdapters, stagedResourceNames } from "../tools/adapter-staging.ts";

/** Every `.py` file under `server/`, found rather than listed. */
function adaptersOnDisk(directory: string, prefix: string): readonly string[] {
	const found: string[] = [];
	for (const entry of readdirSync(directory, { withFileTypes: true })) {
		const path = join(directory, entry.name);
		if (entry.isDirectory()) found.push(...adaptersOnDisk(path, `${prefix}${entry.name}/`));
		else if (entry.name.endsWith(".py")) found.push(`${prefix}${entry.name}`);
	}
	return found;
}

test("every Python adapter under server/ is one the bundle stages", () => {
	// Found by walking, compared against the written list. A fourth adapter added beside the
	// server and not added here would ship in a checkout and be absent from every package.
	const onDisk = adaptersOnDisk(join(UI_ROOT, "server"), "server/");
	assert.deepEqual([...onDisk].sort(), [...PIPELINE_ADAPTERS].sort());
});

test("every adapter the bundle stages is one tauri.conf.json packages", () => {
	// Staging a file into `resources/` does nothing on its own: Tauri packages what its
	// `resources` list names, and nothing else. Two lists, and this is what keeps them equal.
	const configuration = readFileSync(join(UI_ROOT, "src-tauri", "tauri.conf.json"), "utf8");
	const packaged: readonly string[] = JSON.parse(configuration).bundle.resources;
	for (const resource of stagedResourceNames()) {
		assert.ok(packaged.includes(resource), `tauri.conf.json does not package ${resource}`);
	}
});

test("no two adapters would land on the same name beside the server", () => {
	// They are staged flat, because that is where the bundled server looks for them. Two files
	// called `plan.py` in different directories would overwrite one another in silence.
	const names = stagedResourceNames();
	assert.equal(new Set(names).size, names.length, `two adapters share a name: ${names.join(", ")}`);
});

test("staging copies each adapter's real bytes beside the server", () => {
	const resources = mkdtempSync(join(tmpdir(), "venator-adapters-"));
	try {
		const staged = stageAdapters(UI_ROOT, resources);
		assert.equal(staged.length, PIPELINE_ADAPTERS.length);
		for (const adapter of PIPELINE_ADAPTERS) {
			const landed = join(resources, basename(adapter));
			assert.ok(statSync(landed).isFile(), adapter);
			assert.equal(readFileSync(landed, "utf8"), readFileSync(join(UI_ROOT, adapter), "utf8"), adapter);
		}
	} finally {
		rmSync(resources, { recursive: true, force: true });
	}
});

test("a bundle that would ship without an adapter is refused rather than built", () => {
	const resources = mkdtempSync(join(tmpdir(), "venator-adapters-"));
	try {
		// A `ui` root with no `server/` in it stands for the staging having half-happened. The
		// alternative is a package that installs, opens, and cannot write a Profile.
		assert.throws(() => stageAdapters(resources, resources), /would ship without/u);
		assert.deepEqual(readdirSync(resources), [], "nothing was staged from a refused run");
	} finally {
		rmSync(resources, { recursive: true, force: true });
	}
});
