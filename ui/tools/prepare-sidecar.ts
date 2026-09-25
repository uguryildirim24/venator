/**
 * Assembles what the packaged desktop app needs to run the read-only API on its own:
 *
 *   src-tauri/binaries/node-<target triple>   the Node runtime, as a Tauri sidecar
 *   src-tauri/resources/server.mjs            the API bundled to a single file
 *
 * A packaged app cannot rely on the machine's Node: GUI processes get a minimal PATH and this
 * machine's Node lives behind an fnm shim. Bundling the runtime costs disk and buys an app
 * that launches whether or not a shell was ever opened.
 *
 * **Two routes, chosen by what the caller asked for and never by what this machine happens to
 * be.** `--verified` stages the pinned Node from nodejs.org for whatever target is named, this
 * machine's own triple included; `desktop-stage.ts` passes it on every bundle, so the runtime
 * that ships is one whose bytes were checked. Without it — `tauri dev`, and a bare `pnpm
 * sidecar` — this machine's own Node is copied, which is what this script always did and is all
 * a dev loop needs, since it is running that Node already.
 *
 * The verified route fetches an executable that is then shipped to somebody else, so it is not
 * taken on trust: every download is checked against a sha256 pinned in `node-runtime.ts` — in
 * the repository, in git, reviewed once — rather than against a checksum fetched from the same
 * origin as the binary, which would agree with it by construction. Every staged runtime, the
 * copy included, then has its own header read back and matched against the target it is named
 * for, and the copy route additionally refuses a Node whose major is not the one the API bundle
 * is emitted for. Any failure removes the file it was writing and stops; nothing falls back to
 * copying, because a fallback would turn a verified bundle into an unverified one at exactly the
 * moment verification failed.
 *
 * `--target <rust triple>` (or `$VENATOR_SIDECAR_TARGET`) names a platform other than this one,
 * which the verified route can stage from any build host — so a Windows installer needs no
 * Windows machine for the Node half.
 *
 * **Every check the two routes make is in `sidecar-staging.ts`, not here**, and that is the
 * point of the split: this file ends in a top-level `await main()` and can never be imported, so
 * a check living in it is a check no test can reach. What is left here is reading two flags and
 * calling the steps in order.
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

import { build } from "vite";

import { UI_ROOT } from "../server/locations.ts";
import { stageAdapters } from "./adapter-staging.ts";
import { executableSuffix } from "./platform.ts";
import { PYTHON_RESOURCES, preparePythonResources } from "./python-runtime.ts";
import { sidecarPath } from "./node-runtime.ts";
import { assertRouteStageable, describeRoute, sidecarRoute, stageSidecar } from "./sidecar-staging.ts";

const BINARIES = resolve(UI_ROOT, "src-tauri/binaries");
const RESOURCES = resolve(UI_ROOT, "src-tauri/resources");

/**
 * Where downloaded runtimes are kept between builds. Each is ~60 MB compressed and a desktop
 * build reruns this script every time, so refetching per build is minutes of nothing. The
 * cache never shortens the check: a cached file is hashed on every hit exactly as a fresh
 * download is, because a cache that can vouch for itself is a cache that can ship a bad
 * binary. `node_modules/.cache/` is the conventional place for this and is already ignored.
 */
const CACHE = resolve(UI_ROOT, "node_modules/.cache/venator-sidecar");

function say(label: string, detail: string): void {
	process.stdout.write(`  ${label.padEnd(14)} ${detail}\n`);
}

/** Tauri looks for a sidecar named with the Rust host triple it is building for. */
function hostTriple(): string {
	const report = execFileSync("rustc", ["-vV"], { encoding: "utf8" });
	const line = report.split("\n").find((entry) => entry.startsWith("host: "));
	if (line === undefined) throw new Error("could not read the host triple from `rustc -vV`");
	return line.slice("host: ".length).trim();
}

/** Stage the sidecar, then bundle the API and the fallback view beside it. */
async function main(): Promise<void> {
	const { values } = parseArgs({
		options: { target: { type: "string" }, verified: { type: "boolean" } },
		allowPositionals: false,
	});
	const requested = values.target ?? process.env["VENATOR_SIDECAR_TARGET"] ?? "";
	const host = hostTriple();

	// Refuses a triple nothing can be staged for, and — since it checks the whole triple's shape
	// — anything that is not a triple at all, before a byte is fetched or written.
	const route = sidecarRoute(requested, host, values.verified === true);

	// The copy route stages whatever Node is running this script, and the API bundled beside it
	// is emitted for `NODE_VERSION`'s major. Refuse before anything is written rather than
	// package a runtime the API was not built against.
	assertRouteStageable(route, process.versions.node);

	mkdirSync(BINARIES, { recursive: true });
	mkdirSync(RESOURCES, { recursive: true });

	// `bundle.resources` names `resources/python/**/*`, and Tauri's build script refuses a
	// resource glob that matches nothing — it fails `cargo build` before the host is compiled.
	// This step runs on every desktop build and the Python staging step does not, so the
	// directory is made real here, and anything staged for another target is taken out of it:
	// the glob ships whatever is under it, so a Windows runtime left over from a cross-stage
	// would otherwise ride into a macOS bundle.
	for (const removed of preparePythonResources(RESOURCES, route.triple)) {
		say(
			"python cleared",
			`${PYTHON_RESOURCES}/${removed} was staged for another target and would have shipped in ` +
				`this one; restage with \`pnpm python --target ${removed}\``,
		);
	}

	// The suffix follows the target, never the machine this runs on: a Windows sidecar staged
	// from Linux still has to be `.exe`, because that is the name Tauri looks for and the only
	// name `CreateProcess` can start. On this machine's own triple the two are the same thing.
	const suffix = route.target === null ? executableSuffix() : executableSuffix(route.target.platform);
	const sidecar = sidecarPath(BINARIES, route.triple, suffix);
	const machine = route.isHost ? "this machine" : `cross, staged from ${host}`;
	say("target", `${route.triple} (${machine}; ${describeRoute(route)})`);

	await stageSidecar(route, sidecar, CACHE, say);

	await build({
		root: UI_ROOT,
		logLevel: "warn",
		configFile: false,
		build: {
			ssr: resolve(UI_ROOT, "server/main.ts"),
			outDir: "src-tauri/resources",
			emptyOutDir: false,
			target: "node24",
			rollupOptions: { output: { entryFileNames: "server.mjs" } },
		},
		// Bundle Hono and friends into the one file; only Node's own modules stay external.
		ssr: { noExternal: true, target: "node" },
	});
	say("api bundle", resolve(RESOURCES, "server.mjs"));

	// The three Python adapters the bundled server spawns *by path*, beside it. Without this a
	// packaged app cannot read a careers page, cannot describe a run, and — since the load-back
	// refuses a write it cannot verify — cannot write a Profile at all. `adapter-staging.ts`
	// says why at length; `tauri.conf.json` names each staged file.
	for (const path of stageAdapters(UI_ROOT, RESOURCES)) say("adapter", path);

	// The sample view is deliberately not staged. It used to ship as the app's fallback view, so
	// an Install that had never run the pipeline opened onto nineteen Postings from real
	// employers that were not this person's. A shipped Install now carries none of that: with no
	// view of its own it is served an empty one and shows the screen written for a first run
	// (`server/db.ts`, `src/views/first-run.tsx`). Sample data is a development tool and stays
	// in `ui/fixtures/`, where `$VENATOR_SAMPLE_DATA` asks for it.
	//
	// A copy an earlier build left here is taken out rather than left lying: this directory is
	// gitignored and assembled, `tauri.conf.json` no longer declares the file so nothing would
	// ship it, and a stale one sitting in a staging tree is exactly the thing somebody re-adds
	// to the resource list one day to find out what it is.
	const staleSampleView = resolve(RESOURCES, "venator.fixture.db");
	if (existsSync(staleSampleView)) {
		rmSync(staleSampleView);
		say("removed", `${staleSampleView} — the sample view is not shipped`);
	}
}

// Every failure in here is a refusal to stage something, and every one of them has already
// said what it was doing and what to do about it. Print that sentence rather than a stack
// trace, and exit non-zero so `pnpm sidecar`, `tauri dev` and `tauri build` all stop instead
// of going on to bundle whatever happens to be sitting in `binaries/`.
try {
	await main();
} catch (caught) {
	const failure = caught instanceof Error ? caught : new Error(String(caught));
	const because = failure.cause instanceof Error ? `\n    caused by: ${failure.cause.message}` : "";
	process.stderr.write(`\nprepare-sidecar: ${failure.message}${because}\n\n`);
	process.exitCode = 1;
}
