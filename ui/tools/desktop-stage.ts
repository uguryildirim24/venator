/**
 * Everything the desktop bundle needs on disk before Tauri looks, with the target resolved once.
 *
 * `tauri build` runs this through `beforeBuildCommand`, and it exists because of what a bundle
 * used to be able to be: `resources/python/` always holds at least a `README.txt` — Tauri
 * refuses a resource glob that matches no files — so a bundle built without ever running `pnpm
 * python` succeeded and shipped a `python/` directory holding one line of text. Nothing failed,
 * nothing warned, and the installer that came out opens onto the sample corpus on a machine with
 * no Python and stays there forever. That is the whole failure the staging step exists to
 * prevent, reached by not running it.
 *
 * The assertion does not belong in `prepare-sidecar.ts`, which CI runs on every pull request and
 * which legitimately does not want 154 MB. It belongs here, on the path that produces an
 * installer somebody installs.
 *
 * Three things happen, in this order and for a reason:
 *
 * 1. **The target is resolved once**, and everything downstream is told what it is. Tauri sets
 *    `TAURI_ENV_TARGET_TRIPLE` for the commands it runs, so a `tauri build --target <triple>` is
 *    followed rather than guessed at — and any other source that names a *different* triple is
 *    refused rather than ranked, because whichever of the two a precedence picked, the loser
 *    would be discarded in silence while the bundler went on packaging for its own. With no
 *    bundler triple somebody is running this by hand, and `--target`, `$VENATOR_DESKTOP_TARGET`
 *    and the Rust host triple decide, in that order.
 * 2. **The sidecar is staged for that target, verified.** `beforeBuildCommand` used to run `pnpm
 *    sidecar` with no target at all, which on a cross build stages this machine's own Node into a
 *    bundle named for another platform. Passing the target fixed that half; the other half is
 *    that the sidecar step decided whether to *check* a runtime by whether the target equalled
 *    the Rust host triple, so a same-platform bundle — the only kind either of this repository's
 *    two build machines produces — copied the build machine's own Node and checked nothing about
 *    where it came from. This asks for the verified route explicitly, so what ships is the pinned
 *    Node, downloaded and matched against a digest in this repository, on every bundle. That step
 *    also makes `resources/python/` real and clears out any runtime staged for a *different*
 *    target, so it has to run before the runtime is looked for rather than after.
 * 3. **The Python runtime is staged, or its absence is stated.** A target with a pinned
 *    embeddable CPython gets one — staged if it is not already there — and a bundle that would
 *    ship without it is refused. A target with none says so, out loud, every time.
 *
 * Apple silicon bundles carry the pinned python-build-standalone interpreter and locked wheels.
 * A target without a pin is reported explicitly.
 */

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { parseArgs } from "node:util";

import { UI_ROOT } from "../server/locations.ts";
import { NODE_VERSION, VERIFIED_FLAG } from "./node-runtime.ts";
import {
	BUNDLER_TRIPLE,
	PYTHON_RESOURCES,
	missingRuntimeParts,
	missingRuntimeRefusal,
	pythonStagingRoot,
	resolveTargetTriple,
	stageablePythonTarget,
	unpinnedRuntimeNotice,
} from "./python-runtime.ts";

const RESOURCES = resolve(UI_ROOT, "src-tauri/resources");

/**
 * The one way to say "this bundle is deliberately without a Python runtime".
 *
 * Explicit, per build, and named in the refusal. A default that quietly allowed it would make
 * the check decorative — the whole failure being guarded is a bundle that ships incomplete
 * without anybody deciding it should.
 */
const OPT_OUT = "VENATOR_BUNDLE_WITHOUT_PYTHON";

/**
 * The scripts this runs, as files rather than as `pnpm <name>`.
 *
 * Same programs `pnpm sidecar` and `pnpm python` name — `tests/python-runtime.test.ts` asserts
 * that they are the same files, so the two cannot drift — started through this Node rather than
 * through the package manager. On Windows `pnpm` is a `.cmd`, which `execFileSync` cannot start
 * without a shell, and a build step that needs a shell is a build step that needs a *particular*
 * shell.
 */
const SIDECAR = resolve(UI_ROOT, "tools/prepare-sidecar.ts");
const PYTHON = resolve(UI_ROOT, "tools/prepare-python.ts");

function say(label: string, detail: string): void {
	process.stdout.write(`  ${label.padEnd(14)} ${detail}\n`);
}

/** Tauri names a bundle for the Rust host triple, so that is what a target falls back to. */
function hostTriple(): string {
	const report = execFileSync("rustc", ["-vV"], { encoding: "utf8" });
	const line = report.split("\n").find((entry) => entry.startsWith("host: "));
	if (line === undefined) throw new Error("could not read the host triple from `rustc -vV`");
	return line.slice("host: ".length).trim();
}

/** Run one of this repository's own build steps, with its output going where this one's goes. */
function step(script: string, triple: string, ...flags: readonly string[]): void {
	execFileSync(process.execPath, [script, "--target", triple, ...flags], { cwd: UI_ROOT, stdio: "inherit" });
}

function main(): void {
	const { values } = parseArgs({ options: { target: { type: "string" } }, allowPositionals: false });
	// Refused before anything is staged, so a build whose two answers disagree leaves the tree
	// exactly as it found it rather than half-staged for the target that happened to win.
	const claim = resolveTargetTriple(process.env[BUNDLER_TRIPLE] ?? "", [
		{ source: "--target", triple: values.target ?? "" },
		{ source: "$VENATOR_DESKTOP_TARGET", triple: process.env["VENATOR_DESKTOP_TARGET"] ?? "" },
	]);
	const triple = claim?.triple ?? hostTriple();
	say("bundling for", `${triple} (${claim?.source ?? "this machine's Rust host triple"})`);

	// The Node runtime, the API bundle and the fallback view — and, on the way, `resources/python/`
	// made real and emptied of anything staged for another target.
	//
	// `VERIFIED_FLAG` is what makes the runtime in this bundle one whose bytes were checked. The
	// sidecar step used to decide that by whether the target happened to equal the Rust host
	// triple, so the only bundles ever verified were cross builds — and neither bundle this
	// repository ships is one: a `.dmg` is built on the Owner's Mac for the Owner's Mac, and a
	// Windows installer on a `windows-latest` runner for Windows. Both took the copy route and
	// shipped the build machine's own Node, unverified as to provenance, with the six pinned
	// digests dead code beside them.
	step(SIDECAR, triple, VERIFIED_FLAG);

	// Said here, at the top level, rather than left to a line that scrolls past inside a
	// subprocess: which runtime a bundle carries is a property of the bundle, and a build that
	// verified one should say so where whoever ran it will read it. The copy route is reachable
	// only from `tauri dev` and from a bare `pnpm sidecar`, never from here.
	say(
		"sidecar",
		`Node ${NODE_VERSION} for ${triple}, downloaded and checked against the sha256 pinned in node-runtime.ts`,
	);

	const target = stageablePythonTarget(triple);
	if (target === null) {
		say("no runtime", unpinnedRuntimeNotice(triple));
		return;
	}

	const root = pythonStagingRoot(RESOURCES, triple);
	const absent = (): readonly string[] =>
		missingRuntimeParts((path) => existsSync(resolve(root, ...path.split("/"))), target);
	if (absent().length > 0) {
		say("staging", `no Python runtime under ${PYTHON_RESOURCES}/${triple}/ yet; staging one now`);
		step(PYTHON, triple);
	}

	const missing = absent();
	if (missing.length === 0) {
		say("runtime ok", `${PYTHON_RESOURCES}/${triple}/ carries the interpreter, the wheels and the pipeline`);
		return;
	}
	if (process.env[OPT_OUT] === "1") {
		say("no runtime", `${OPT_OUT}=1, so this bundle ships without one: ${missing.join(", ")} missing`);
		return;
	}
	throw new Error(missingRuntimeRefusal(triple, missing, OPT_OUT));
}

// Every failure in here is a refusal to bundle something, and every one has already said what it
// was doing and what to do about it. Print that sentence rather than a stack trace, and exit
// non-zero so `tauri build` stops instead of packaging whatever is sitting in `resources/`.
try {
	main();
} catch (caught) {
	const failure = caught instanceof Error ? caught : new Error(String(caught));
	process.stderr.write(`\ndesktop-stage: ${failure.message}\n\n`);
	process.exitCode = 1;
}
