/**
 * Assembles the Python half of the packaged desktop app:
 *
 *   src-tauri/resources/python/<target triple>/python.exe, *.pyd, *.dll   embeddable CPython
 *   src-tauri/resources/python/<target triple>/Lib/site-packages/         the runtime wheels
 *   src-tauri/resources/python/<target triple>/src/venator/               the pipeline itself
 *   src-tauri/resources/python/<target triple>/python313._pth             what ties them together
 *
 * `prepare-sidecar.ts` gives the bundle a Node runtime and the read-only API. That is the
 * dashboard, and the dashboard is a view over a database somebody else's machine built. Every
 * Install runs the whole pipeline itself — Discover, Match, Tailor, Fill — so on a
 * machine with nothing installed the app opens, finds no view database, and stays on its
 * first-run screen forever. The laptop this ships to answers "not recognized" to `python`,
 * `uv`, `node`, `git` and `claude` alike, so the interpreter and its dependencies have to
 * arrive in the bundle.
 *
 * This stages them and stops there. The Rust host passes the bundled interpreter to the API.
 * Playwright's Chromium (the Fill stage browser) is not part of the first-run bundle.
 *
 * Everything fetched here is checked the way `prepare-sidecar.ts` checks the Node runtime,
 * because it is the same act: shipping somebody else's binary to somebody else's machine. The
 * archive is checked against a sha256 pinned in `python-runtime.ts` — in the repository, in
 * git, established once through the release signature — never against a checksum fetched
 * beside it. What comes out of the archive then has its own headers read back and matched
 * against the target the tree is named for, because a genuine, correctly-checksummed CPython
 * for the wrong architecture passes every other check there is. The wheels get the same
 * treatment from the other end: the set is compared against what `uv.lock` resolves, the dev
 * group is refused by name, and every staged file's first bytes are swept for an image built
 * for a platform this bundle is not for.
 *
 * **Every one of those checks is in `python-staging.ts`, not here**, and that is the point of
 * the split: this file has a top-level `await main()` and can never be imported, so a check
 * living in it is a check no test can reach. It did hold them once, and a mutation run over the
 * lot of them — the sha256 comparison deleted, `--no-dev` dropped from the export — came back
 * green eleven times out of eleven. What is left here is reading a flag and calling the steps
 * in order.
 *
 * Nothing is written into place until all of it has passed. The tree is assembled in a scratch
 * directory beside its destination and moved there as the last act, so a failure leaves the
 * previous staging untouched rather than half-replaced.
 */

import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, renameSync, rmSync, statSync } from "node:fs";
import { relative, resolve } from "node:path";
import { parseArgs } from "node:util";

import { CHECKOUT_ROOT, UI_ROOT } from "../server/locations.ts";
import {
	PYTHON_VERSION,
	parsePythonTarget,
	preparePythonResources,
	pythonArtifact,
	pythonStagingRoot,
} from "./python-runtime.ts";
import {
	assertStagedForTarget,
	megabytes,
	stageInterpreter,
	stagePipeline,
	stageWheels,
	runUv,
	wheelExportArgv,
	verifiedDownload,
	walk,
	writePathConfiguration,
} from "./python-staging.ts";

const RESOURCES = resolve(UI_ROOT, "src-tauri/resources");

/**
 * Where downloaded archives are kept between builds, beside the sidecar's own cache and for the
 * same reason. A cached file is hashed on every hit exactly as a fresh download is: a cache that
 * can vouch for itself is a cache that can ship a bad runtime.
 */
const CACHE = resolve(UI_ROOT, "node_modules/.cache/venator-python");

function stageMacInterpreter(archive: string, destination: string): void {
	// The checked install_only archive has a single python/ prefix. Keep that prefix: its
	// relative libpython links and rpaths must remain intact when the bundle is relocated.
	execFileSync("tar", ["-xzf", archive, "-C", destination], { stdio: "inherit" });
	if (!statSync(resolve(destination, "python/bin/python3.13")).isFile()) {
		throw new Error("the pinned macOS archive contains no python/bin/python3.13");
	}
}

function stageMacWheels(destination: string, checkout: string, cache: string): void {
	const requirements = resolve(cache, `mac-requirements-${process.pid}.txt`);
	try {
		runUv(wheelExportArgv(requirements), checkout);
		// --python installs wheels into this interpreter, not the build host's environment.
		// Hashes come from uv.lock; --no-deps forbids a second resolution.
		const python = resolve(destination, "python/bin/python3.13");
		runUv(["pip", "install", "--python", python,
			"--require-hashes", "--no-deps", "--only-binary", ":all:", "-r", requirements], checkout);
		// The project itself is local; --no-deps prevents a second index resolution.
		runUv(["pip", "install", "--python", python, "--no-deps", checkout], checkout);
	} finally {
		rmSync(requirements, { force: true });
	}
}

function say(label: string, detail: string): void {
	process.stdout.write(`  ${label.padEnd(14)} ${detail}\n`);
}

/** Tauri names a bundle for the Rust host triple, so that is what a target defaults to. */
function hostTriple(): string {
	const report = execFileSync("rustc", ["-vV"], { encoding: "utf8" });
	const line = report.split("\n").find((entry) => entry.startsWith("host: "));
	if (line === undefined) throw new Error("could not read the host triple from `rustc -vV`");
	return line.slice("host: ".length).trim();
}

async function main(): Promise<void> {
	const { values } = parseArgs({ options: { target: { type: "string" } }, allowPositionals: false });
	const requested = values.target ?? process.env["VENATOR_PYTHON_TARGET"] ?? "";
	const triple = requested === "" ? hostTriple() : requested;

	// Refuses a triple nothing can be staged for, and — since it checks the whole triple's shape
	// — anything that is not a triple at all, before a byte is fetched or written.
	const target = parsePythonTarget(triple);
	const artifact = pythonArtifact(target);
	say("target", `${triple} (Python ${PYTHON_VERSION}, ${target.platform === "darwin" ? "install_only" : "embeddable"} ${target.flavour})`);

	const root = pythonStagingRoot(RESOURCES, triple);
	mkdirSync(RESOURCES, { recursive: true });
	mkdirSync(CACHE, { recursive: true });

	// Shared with `prepare-sidecar.ts`, which does this on every desktop build whether or not
	// anything is staged: the directory has to exist and never be empty, because Tauri's build
	// script refuses a resource glob that matches no files. It also takes out anything staged
	// for another target, since the glob ships whatever is under it.
	for (const removed of preparePythonResources(RESOURCES, triple)) {
		say("cleared", `python/${removed} was staged for another target and would have shipped in this one`);
	}

	const archive = await verifiedDownload(CACHE, artifact.url, artifact.cacheName, artifact.digest, say);

	// Assembled beside the destination and moved in as the last act. A failure part way through
	// then leaves the previous staging exactly as it was, rather than a tree that is half of two.
	const scratch = `${root}.staging-${process.pid}`;
	rmSync(scratch, { recursive: true, force: true });
	mkdirSync(scratch, { recursive: true });
	try {
		if (target.platform === "darwin") {
			stageMacInterpreter(archive, scratch);
			stageMacWheels(scratch, CHECKOUT_ROOT, CACHE);
			// The API sidecar ships a signed Node 24 beside the host executable. Playwright's
			// driver package requires Node >=20 and uses PLAYWRIGHT_NODEJS_PATH at runtime;
			// its private Node and pip (needed only by uv while assembling wheels) must not ship.
			const site = resolve(scratch, "python/lib/python3.13/site-packages");
			rmSync(resolve(site, "playwright/driver/node"));
			rmSync(resolve(site, "pip"), { recursive: true });
			for (const entry of readdirSync(site)) {
				if (/^pip-[^/]+\.dist-info$/.test(entry)) {
					rmSync(resolve(site, entry), { recursive: true });
				}
			}
			const interpreter = resolve(scratch, "python/bin/python3.13");
			const result = execFileSync(interpreter, ["-c", "import venator, playwright, pdfplumber, yaml; print(venator.__file__)"], {
				encoding: "utf8", env: { ...process.env, PYTHONNOUSERSITE: "1" },
			});
			say("import ok", result.trim());
		} else {
			stageInterpreter(archive, scratch, target, artifact.entryCount, say);
			stageWheels(scratch, target, CHECKOUT_ROOT, CACHE, say);
			stagePipeline(scratch, CHECKOUT_ROOT, say);
			writePathConfiguration(scratch, target, say);
			const checked = assertStagedForTarget(scratch, target);
			say("headers ok", `${checked} native files read back, all Windows PE for ${target.arch}`);
		}

		const files = walk(scratch);
		const bytes = files.reduce((total, path) => total + statSync(resolve(scratch, ...path.split("/"))).size, 0);
		rmSync(root, { recursive: true, force: true });
		renameSync(scratch, root);
		say("staged", `${relative(UI_ROOT, root)} — ${files.length} files, ${megabytes(bytes)}`);
	} catch (cause) {
		// Never leave a tree that failed its own checks where a later build could bundle it.
		rmSync(scratch, { recursive: true, force: true });
		throw cause;
	}
}

// Every failure in here is a refusal to stage something, and every one has already said what it
// was doing and what to do about it. Print that sentence rather than a stack trace, and exit
// non-zero so `pnpm python` and anything that calls it stop instead of going on to bundle
// whatever happens to be sitting in `resources/`.
try {
	await main();
} catch (caught) {
	const failure = caught instanceof Error ? caught : new Error(String(caught));
	const because = failure.cause instanceof Error ? `\n    caused by: ${failure.cause.message}` : "";
	process.stderr.write(`\nprepare-python: ${failure.message}${because}\n\n`);
	process.exitCode = 1;
}
