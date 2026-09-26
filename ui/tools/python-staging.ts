/**
 * Every step that assembles the Python half of the desktop bundle, as functions that can be
 * called by something other than the command line.
 *
 * `prepare-python.ts` used to be all of this and a top-level `await main()` besides, which made
 * it a file no test could import even in principle — and it was the file holding the two claims
 * the whole staging step is *for*: that the archive is the bytes python.org published, and that
 * the dev group did not ship. A mutation run said so plainly: eleven mutations of those checks,
 * eleven survivors, a green suite each time. Deleting the sha256 comparison outright changed
 * nothing anybody could see.
 *
 * So the checks live here, where they are ordinary functions with ordinary arguments, and the
 * script above is argument parsing and orchestration. Two arguments are injected rather than
 * reached for, and both are injected for the same reason: a test of "the download did not match
 * its pin" must not need python.org, and a test of "the dev group leaked in" must not need `uv`
 * or a wheel index. The defaults are the real ones, so the script that matters is not the one
 * being described.
 *
 * `python-runtime.ts` still holds everything that is a fact rather than a step — the pins, the
 * names, the target parsing, the assertions over a wheel set. Nothing here duplicates it.
 */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
	closeSync,
	cpSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	openSync,
	readFileSync,
	readSync,
	readdirSync,
	renameSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { join, resolve } from "node:path";

import { extractZip, readZipEntries } from "./zip.ts";
import {
	HOST_ONLY_DIRECTORIES,
	PIPELINE_SOURCE,
	PYTHON_VERSION,
	REQUIRED_PIPELINE_FILES,
	SITE_PACKAGES,
	assertDistributionsResolved,
	assertNoDevDistributions,
	assertPythonBinaryMatches,
	assertWheelTags,
	devGroupNames,
	isForeignNative,
	isWindowsNative,
	looksForeign,
	looksWindowsNative,
	pathConfiguration,
	pathConfigurationName,
	pythonTag,
	requirementNames,
	stagedDistributions,
	type PythonTarget,
} from "./python-runtime.ts";

/** How a step says what it just did. The script prints these; a test collects them. */
export type Reporter = (label: string, detail: string) => void;

/** How the bytes of an artifact are obtained. Injected so no test of the pin needs python.org. */
export type FetchBytes = (url: string) => Promise<Uint8Array>;

/** How a build tool is run. Injected so no test of the wheel set needs `uv` or an index. */
export type RunCommand = (argv: readonly string[], cwd: string) => string;

/** Enough of a file for any executable header. */
export const HEADER_BYTES = 4096;

export function megabytes(bytes: number): string {
	return `${Math.round(bytes / 1_000_000)} MB`;
}

export function sha256(path: string): string {
	return createHash("sha256").update(readFileSync(path)).digest("hex");
}

/** The first bytes of a file, without reading the rest of it. */
export function head(path: string, length: number): Uint8Array {
	const buffer = new Uint8Array(length);
	const handle = openSync(path, "r");
	try {
		const read = readSync(handle, buffer, 0, length, 0);
		return buffer.subarray(0, read);
	} finally {
		closeSync(handle);
	}
}

/** Every file under `root`, as paths relative to it, with `/` separators. */
export function walk(root: string, prefix = ""): readonly string[] {
	const found: string[] = [];
	for (const entry of readdirSync(join(root, prefix), { withFileTypes: true })) {
		const child = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
		if (entry.isDirectory()) found.push(...walk(root, child));
		else found.push(child);
	}
	return found;
}

/** Precompiled caches from an archive or wheel cost bundle space and must not be shipped. */
export function removeBytecode(root: string): void {
	function visit(directory: string): void {
		for (const entry of readdirSync(directory, { withFileTypes: true })) {
			const path = join(directory, entry.name);
			if (entry.isDirectory()) {
				if (entry.name === "__pycache__") rmSync(path, { recursive: true });
				else visit(path);
			} else if (entry.name.endsWith(".pyc")) {
				rmSync(path);
			}
		}
	}
	visit(root);
}

/** The bytes at `url`, or an explanation of what a build host needs to reach it. */
export async function downloadBytes(url: string): Promise<Uint8Array> {
	const response = await fetch(url).catch((cause: Error) => {
		throw new Error(
			`could not reach ${url}: ${cause.message}. Staging the Python runtime needs network ` +
				"access to python.org and to the package index.",
			{ cause },
		);
	});
	if (!response.ok) {
		throw new Error(
			`python.org answered ${response.status} for ${url}. Either the pinned Python version ` +
				"does not publish an embeddable build for this target, or the target triple is not " +
				"one it names.",
		);
	}
	return new Uint8Array(await response.arrayBuffer());
}

/**
 * The archive, in the cache, proven to hash to the sha256 this repository pins for it.
 *
 * Returns only on a match. A mismatch — cached or freshly downloaded — deletes the file and
 * throws, so nothing downstream can ever see a payload that failed. The download lands on a
 * `.part-<pid>` name and is renamed only after it has passed, so a run that is interrupted
 * mid-download leaves nothing a later run could mistake for a cache hit.
 *
 * The cached file is hashed on every hit exactly as a fresh download is: a cache that can vouch
 * for itself is a cache that can ship a bad runtime.
 */
export async function verifiedDownload(
	cache: string,
	url: string,
	cacheName: string,
	digest: string,
	say: Reporter,
	fetchBytes: FetchBytes = downloadBytes,
): Promise<string> {
	mkdirSync(cache, { recursive: true });
	const path = resolve(cache, cacheName);

	if (existsSync(path)) {
		if (sha256(path) === digest) {
			say("checksum ok", `cached, sha256 ${digest}, as pinned (${megabytes(statSync(path).size)})`);
			return path;
		}
		say("cache rejected", `${path} failed its checksum; refetching`);
		rmSync(path);
	}

	say("downloading", url);
	const partial = `${path}.part-${process.pid}`;
	writeFileSync(partial, await fetchBytes(url));
	const actual = sha256(partial);
	if (actual !== digest) {
		rmSync(partial);
		throw new Error(
			`checksum mismatch for ${cacheName}: this repository pins ${digest}, the download ` +
				`hashed to ${actual}. Nothing was staged. Retry once; if it keeps failing, the bytes ` +
				`arriving here are not the ones python.org published for Python ${PYTHON_VERSION} ` +
				"and must not be bundled — or PYTHON_DIGESTS is stale, which is a diff to make " +
				"deliberately, through the release signature, and not by pasting in whatever the " +
				"download hashed to.",
		);
	}
	renameSync(partial, path);
	say("checksum ok", `sha256 ${digest}, as pinned (${megabytes(statSync(path).size)})`);
	return path;
}

/** Run `uv`, or explain that the build host needs it. */
export function runUv(argv: readonly string[], cwd: string): string {
	try {
		return execFileSync("uv", [...argv], { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
	} catch (cause) {
		const because = cause instanceof Error ? cause.message : String(cause);
		throw new Error(
			`\`uv ${argv.join(" ")}\` failed: ${because}. Staging the wheels needs uv on the build ` +
				"host — it is the one tool that can resolve and download a wheel set for a platform " +
				"it is not running on. Install it from https://docs.astral.sh/uv/ and run `pnpm " +
				"python` again. Nothing was staged.",
			{ cause },
		);
	}
}

/** Unpack the embeddable distribution into `destination` and prove it is the whole of it. */
export function stageInterpreter(
	archive: string,
	destination: string,
	target: PythonTarget,
	entryCount: number,
	say: Reporter,
): void {
	const bytes = new Uint8Array(readFileSync(archive));
	const entries = readZipEntries(bytes);
	if (entries.length !== entryCount) {
		throw new Error(
			`the archive for ${target.triple} holds ${entries.length} entries where this repository ` +
				`records ${entryCount} for Python ${PYTHON_VERSION}. Nothing was staged. A digest ` +
				"settles the bytes; this settles that the bytes are the distribution they are filed " +
				"as, which a substitution that unpacks cleanly would otherwise pass.",
		);
	}
	const written = extractZip(bytes, destination);
	say("interpreter", `${written} files unpacked, each verified against its recorded CRC-32`);

	// The interpreter's own account of itself, read back. A genuine CPython for the wrong
	// architecture is what every other check here lets through.
	const interpreter = resolve(destination, "python.exe");
	if (!existsSync(interpreter)) {
		throw new Error(
			`the archive for ${target.triple} unpacked without a python.exe in it. Nothing was ` +
				"staged. An embeddable distribution is the interpreter and its DLLs in one flat " +
				"directory; a tree without it is not one.",
		);
	}
	assertPythonBinaryMatches(head(interpreter, HEADER_BYTES), target, "python.exe");
	say("header ok", `python.exe is a Windows PE executable for ${target.arch}, matching ${target.triple}`);
}

/**
 * The `uv export` that produces the wheel set, as argv.
 *
 * Built as data rather than written at the call site so that `--no-dev` is a thing a test can
 * assert on. It is one of the two flags this whole step exists to keep: dropped, the resolved
 * set and the staged set agree with each other and both carry `pytest` and `pdfplumber` into an
 * Owner's installer. `assertNoDevDistributions` is the other half and neither is redundant.
 *
 * From `uv.lock`, not from a fresh resolve: the lock is what `uv sync` installs and what the
 * test suite runs against, so the bundle ships the versions that were tested rather than
 * whatever the index holds on the day of the build.
 */
export function wheelExportArgv(requirements: string): readonly string[] {
	return ["export", "--no-dev", "--no-emit-project", "--format", "requirements-txt", "--quiet", "-o", requirements];
}

/**
 * The `uv pip install` that puts the wheel set in the staged tree, as argv.
 *
 * `--only-binary :all:` refuses a source distribution rather than building one here — a wheel
 * built here would be built for this machine, and the whole point of naming a target is that it
 * is not this machine. `--require-hashes` means the index does not get to vouch for itself; the
 * hashes travelled with `uv.lock`. `--no-deps` because the exported set is already transitively
 * closed and pinned, and resolving again would let the index add to it.
 */
export function wheelInstallArgv(
	site: string,
	triple: string,
	series: string,
	requirements: string,
): readonly string[] {
	return [
		"pip",
		"install",
		"--target",
		site,
		"--python-platform",
		triple,
		"--python-version",
		series,
		"--only-binary",
		":all:",
		"--require-hashes",
		"--no-deps",
		"--quiet",
		"-r",
		requirements,
	];
}

/** Resolve and install the runtime wheels for `target` into the staged tree. */
export function stageWheels(
	destination: string,
	target: PythonTarget,
	checkout: string,
	cache: string,
	say: Reporter,
	run: RunCommand = runUv,
): void {
	mkdirSync(cache, { recursive: true });
	const scratch = mkdtempSync(join(cache, "export-"));
	const requirements = join(scratch, "runtime.txt");
	const series = PYTHON_VERSION.split(".").slice(0, 2).join(".");
	try {
		run(wheelExportArgv(requirements), checkout);
		const exported = readFileSync(requirements, "utf8");
		const resolved = requirementNames(exported);
		say("resolved", `${resolved.length} runtime distributions from uv.lock: ${resolved.join(", ")}`);

		const site = resolve(destination, ...SITE_PACKAGES.split("/"));
		run(wheelInstallArgv(site, target.triple, series, requirements), checkout);

		// Cross-installing writes the host's console-script shims and headers; neither belongs in
		// a tree named for another platform.
		for (const directory of HOST_ONLY_DIRECTORIES) {
			const path = resolve(site, directory);
			if (existsSync(path)) {
				rmSync(path, { recursive: true });
				say("pruned", `${SITE_PACKAGES}/${directory}/, which uv writes in this host's shape`);
			}
		}

		const entries = readdirSync(site);
		const staged = stagedDistributions(entries);
		assertDistributionsResolved(staged, resolved);
		assertNoDevDistributions(staged, devGroupNames(readFileSync(resolve(checkout, "pyproject.toml"), "utf8")));

		const tag = pythonTag();
		const platform = target.arch === "x64" ? "win_amd64" : "win_arm64";
		for (const entry of entries) {
			if (!entry.endsWith(".dist-info")) continue;
			const wheel = resolve(site, entry, "WHEEL");
			if (!existsSync(wheel)) continue;
			const tags = readFileSync(wheel, "utf8")
				.split("\n")
				.filter((line) => line.startsWith("Tag:"))
				.map((line) => line.slice("Tag:".length).trim());
			assertWheelTags(entry, tags, tag, platform);
		}
		say("wheels", `${staged.length} distributions installed, every tag ${tag}-*-${platform} or pure Python`);
	} finally {
		rmSync(scratch, { recursive: true, force: true });
	}
}

/** Copy `src/venator` into the staged tree, without the host's compiled caches. */
export function stagePipeline(destination: string, checkout: string, say: Reporter): void {
	const source = resolve(checkout, "src", "venator");
	if (!existsSync(source)) {
		throw new Error(
			`there is no ${source} to stage. Nothing was staged. This script assembles a bundle out ` +
				"of a checkout and has to be run from one.",
		);
	}
	const staged = resolve(destination, PIPELINE_SOURCE, "venator");
	// `__pycache__` holds this machine's .pyc files, compiled by whatever Python is on this host
	// and stamped with its magic number. CPython ignores a stale cache, so shipping them is
	// weight rather than breakage — but it is this host's weight in a tree named for another
	// target, which is the thing this script exists not to do.
	cpSync(source, staged, {
		recursive: true,
		filter: (from) => !from.split(/[\\/]/u).includes("__pycache__"),
	});
	for (const required of REQUIRED_PIPELINE_FILES) {
		if (existsSync(resolve(destination, PIPELINE_SOURCE, ...required.split("/")))) continue;
		throw new Error(
			`the staged pipeline is missing ${required}. Nothing was staged. That file is read at ` +
				"runtime and is not a dependency, so a tree without it imports cleanly and fails " +
				"later, on the Owner's machine.",
		);
	}
	const files = walk(staged);
	say("pipeline", `src/venator staged, ${files.length} files, including ${REQUIRED_PIPELINE_FILES.join(" and ")}`);
}

/**
 * Replace the distribution's `._pth`, so the interpreter can see the wheels and the pipeline.
 *
 * The `._pth` the distribution ships names only the stdlib zip, so a tree staged without this
 * step has every wheel next to an interpreter that cannot import one of them.
 *
 * Replaced, never added. The name is derived from the pinned version, and CPython looks for
 * `<executable>._pth` and then `<dll>._pth` — so if the derived name and the one the archive
 * actually ships ever drifted apart, writing this would leave *two*, the stale one would win,
 * and the tree would look complete while importing nothing. That the file is already there is
 * what says the version pin and the archive still agree.
 */
export function writePathConfiguration(destination: string, target: PythonTarget, say: Reporter): void {
	const configuration = pathConfigurationName();
	const path = resolve(destination, configuration);
	if (!existsSync(path)) {
		throw new Error(
			`the archive for ${target.triple} does not ship a ${configuration}, which is the ` +
				`file Python ${PYTHON_VERSION} reads its sys.path from. Nothing was staged. The ` +
				"pinned version and the archive filed under it have drifted apart — writing this " +
				"file anyway would leave the distribution's own beside it, and the interpreter " +
				"would read that one.",
		);
	}
	writeFileSync(path, pathConfiguration(), "utf8");
	say("sys.path", `${configuration} rewritten to add ${SITE_PACKAGES} and ${PIPELINE_SOURCE}`);
}

/**
 * Read back every staged file and refuse anything built for a platform this is not for.
 *
 * The last check and the one the others cannot do. Wheel metadata is not enough — `playwright`
 * records `Tag: py3-none-any` in its Windows and its macOS build alike, and they differ only in
 * whether `playwright/driver/node.exe` is a PE image or `playwright/driver/node` is a Mach-O
 * one — so every file's first bytes are looked at, whatever it is called.
 *
 * And whatever it is called cuts both ways, which is why the architecture check is on the bytes
 * rather than on the extension: `playwright`'s driver has no extension, so a *Windows* PE for
 * the wrong architecture planted at `playwright/driver/node` was read by nothing at all. Every
 * file that begins `MZ` goes through the header read, and the extensions Windows loads natively
 * stay in the list so that a `.dll` that is somehow not a PE is still an answer rather than a
 * skip.
 */
export function assertStagedForTarget(root: string, target: PythonTarget): number {
	let checked = 0;
	for (const path of walk(root)) {
		const absolute = resolve(root, ...path.split("/"));
		if (isForeignNative(path)) {
			throw new Error(
				`${path} is native code for a platform that is not Windows, and the runtime is being ` +
					`staged for ${target.triple}. Nothing was staged. A wheel resolved for the build ` +
					"host rather than for the target is how this happens.",
			);
		}
		const bytes = head(absolute, HEADER_BYTES);
		if (looksForeign(bytes)) {
			throw new Error(
				`${path} is an executable image for a platform that is not Windows, and the runtime ` +
					`is being staged for ${target.triple}. Nothing was staged.\n\n` +
					"    Nothing about this file's name says so. `playwright` publishes one build\n" +
					"    re-tagged per platform and records the same `Tag: py3-none-any` in all of\n" +
					"    them, so its macOS wheel installs into a Windows tree without a complaint\n" +
					"    from anything that reads metadata. Resolve the wheels with `--python-platform\n" +
					`    ${target.triple}\`.\n`,
			);
		}
		if (isWindowsNative(path) || looksWindowsNative(bytes)) {
			assertPythonBinaryMatches(bytes, target, path);
			checked += 1;
		}
	}
	return checked;
}
