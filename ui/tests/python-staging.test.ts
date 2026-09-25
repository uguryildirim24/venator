/**
 * The steps that assemble the Python half of the bundle, exercised as functions.
 *
 * These checks lived in `prepare-python.ts` and could not be reached from a test: that file ends
 * in a top-level `await main()`, so importing it runs it. The consequence was measured rather
 * than guessed at — eleven mutations of the checks in it, eleven survivors of a green suite,
 * including the sha256 comparison the download is checked against and the `--no-dev` that keeps
 * `pytest` and `pdfplumber` out of an Owner's installer. Those two are the whole safety claim of
 * the staging step.
 *
 * So the steps moved into `python-staging.ts` and the tests are here. Two of them take an
 * injected argument, and both injections exist so that a test of a refusal does not need the
 * thing being refused: the download takes how to fetch bytes, so no test reaches python.org, and
 * the wheel staging takes how to run a command, so no test needs `uv` or an index. Everything
 * else is real — a real zip built byte by byte below, real files on disk, real headers read back.
 *
 * No test here touches the network, and none may.
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { crc32 } from "node:zlib";

import { CHECKOUT_ROOT } from "../server/locations.ts";
import {
	PYTHON_VERSION,
	pathConfiguration,
	pathConfigurationName,
	parsePythonTarget,
	type PythonTarget,
} from "../tools/python-runtime.ts";
import {
	assertStagedForTarget,
	stageInterpreter,
	stagePipeline,
	stageWheels,
	verifiedDownload,
	walk,
	wheelExportArgv,
	wheelInstallArgv,
	writePathConfiguration,
	type Reporter,
	type RunCommand,
} from "../tools/python-staging.ts";

const TARGET: PythonTarget = parsePythonTarget("x86_64-pc-windows-msvc");

/** What a step reported, so a test can assert on what it said as well as on what it did. */
type Recorder = {
	readonly lines: readonly string[];
	readonly say: Reporter;
};

/** The lines a step reported, collected. */
function recorder(): Recorder {
	const lines: string[] = [];
	return { lines, say: (label, detail) => lines.push(`${label}: ${detail}`) };
}

/** The message of the error `run` throws, so a test can assert on several parts of it. */
async function failureOf<T>(run: () => T | Promise<T>): Promise<string> {
	try {
		await run();
	} catch (caught) {
		return caught instanceof Error ? caught.message : String(caught);
	}
	assert.fail("expected a refusal, and nothing was thrown");
}

function scratch(name: string): string {
	return mkdtempSync(join(tmpdir(), `venator-${name}-`));
}

/**
 * A stored-only zip holding `files`, written out by hand.
 *
 * Deliberately not built with the reader under test's own notion of the format, and deliberately
 * store-only: what these tests need from an archive is that it is real enough to unpack, and
 * `zip.test.ts` is where the format itself is exercised.
 */
function zip(files: readonly (readonly [string, Uint8Array])[]): Uint8Array {
	const locals: Uint8Array[] = [];
	const centrals: Uint8Array[] = [];
	let offset = 0;
	for (const [name, data] of files) {
		const encoded = new TextEncoder().encode(name);
		const local = new Uint8Array(30 + encoded.length + data.length);
		const localView = new DataView(local.buffer);
		localView.setUint32(0, 0x0403_4b50, true);
		localView.setUint16(4, 20, true);
		localView.setUint32(14, crc32(data), true);
		localView.setUint32(18, data.length, true);
		localView.setUint32(22, data.length, true);
		localView.setUint16(26, encoded.length, true);
		local.set(encoded, 30);
		local.set(data, 30 + encoded.length);
		locals.push(local);

		const central = new Uint8Array(46 + encoded.length);
		const centralView = new DataView(central.buffer);
		centralView.setUint32(0, 0x0201_4b50, true);
		centralView.setUint16(4, 20, true);
		centralView.setUint16(6, 20, true);
		centralView.setUint32(16, crc32(data), true);
		centralView.setUint32(20, data.length, true);
		centralView.setUint32(24, data.length, true);
		centralView.setUint16(28, encoded.length, true);
		centralView.setUint32(42, offset, true);
		central.set(encoded, 46);
		centrals.push(central);
		offset += local.length;
	}
	const directory = Buffer.concat(centrals);
	const end = new Uint8Array(22);
	const endView = new DataView(end.buffer);
	endView.setUint32(0, 0x0605_4b50, true);
	endView.setUint16(8, files.length, true);
	endView.setUint16(10, files.length, true);
	endView.setUint32(12, directory.length, true);
	endView.setUint32(16, offset, true);
	return new Uint8Array(Buffer.concat([...locals, directory, end]));
}

/** A PE header claiming `machine`, which is all `readBinaryFormat` reads out of one. */
function pe(machine: number): Uint8Array {
	const head = new Uint8Array(0x100);
	head[0] = 0x4d;
	head[1] = 0x5a;
	const view = new DataView(head.buffer);
	view.setUint32(0x3c, 0x80, true);
	view.setUint32(0x80, 0x5045_0000, false);
	view.setUint16(0x84, machine, true);
	return head;
}

/** A thin 64-bit Mach-O header claiming `cpu`: what `playwright`'s macOS driver is. */
function machO(cpu: number): Uint8Array {
	const head = new Uint8Array(0x20);
	const view = new DataView(head.buffer);
	view.setUint32(0, 0xfeed_facf, true);
	view.setUint32(4, cpu, true);
	return head;
}

function text(value: string): Uint8Array {
	return new TextEncoder().encode(value);
}

/** An archive shaped like the embeddable distribution: an interpreter, a DLL and its `._pth`. */
function distribution(interpreter: Uint8Array = pe(0x8664)): Uint8Array {
	return zip([
		["python.exe", interpreter],
		["python313.dll", pe(0x8664)],
		[pathConfigurationName(), text("python313.zip\r\n.\r\n")],
	]);
}

function archiveAt(directory: string, bytes: Uint8Array): string {
	const path = resolve(directory, "python-embed.zip");
	writeFileSync(path, bytes);
	return path;
}

test("a cached archive is hashed on every hit, and one that fails its pin is refetched", async () => {
	const cache = scratch("cache");
	try {
		const good = text("the bytes python.org published");
		const digest = "3d2e9e4b83c9e4b0f4c04e02fca38f16e0d9a1c76e0a19d8e4c8a5a0e7b4a2d1";
		const real = await import("node:crypto").then((crypto) =>
			crypto.createHash("sha256").update(good).digest("hex"),
		);
		assert.notEqual(real, digest, "the two digests in this test are different values");

		// A cache that can vouch for itself is a cache that can ship a bad runtime: the file is
		// there under the right name, and it is not the file that was pinned.
		writeFileSync(resolve(cache, "python-embed.zip"), text("something else entirely"));
		const first = recorder();
		const url = "https://example.invalid/x.zip";
		const path = await verifiedDownload(cache, url, "python-embed.zip", real, first.say, async () =>
			Promise.resolve(good),
		);
		assert.ok(
			first.lines.some((line) => line.startsWith("cache rejected")),
			"the stale cache entry is named as rejected, not silently reused",
		);
		assert.deepEqual(new Uint8Array(readFileSync(path)), good, "and what comes back is the pinned bytes");

		// The second call is a hit, and it hashes the file again rather than trusting its name.
		const second = recorder();
		const again = await verifiedDownload(cache, url, "python-embed.zip", real, second.say, () => {
			assert.fail("a cache hit that matches its pin must not fetch anything");
		});
		assert.equal(again, path);
		assert.ok(second.lines.some((line) => line.startsWith("checksum ok: cached")));
	} finally {
		rmSync(cache, { recursive: true, force: true });
	}
});

test("a download that does not match the pin is refused, and leaves nothing behind", async () => {
	// The check the whole pin exists for. Deleting this comparison used to change nothing any
	// test could see: the bytes were written, renamed into the cache, and staged.
	const cache = scratch("cache");
	try {
		const digest = "0".repeat(64);
		const failure = await failureOf(async () =>
			verifiedDownload(cache, "https://example.invalid/x.zip", "python-embed.zip", digest, () => {}, async () =>
				Promise.resolve(text("not what was pinned")),
			),
		);
		assert.match(failure, /checksum mismatch for python-embed\.zip/);
		assert.ok(failure.includes(digest), "names what this repository pins");
		assert.ok(failure.includes("Nothing was staged"));
		assert.match(failure, /PYTHON_DIGESTS is stale/, "and says what a deliberate change looks like");

		// Not into the cache under its real name, and not left as a partial either — a `.part`
		// file a later run could mistake for a hit is the thing the rename is for.
		assert.deepEqual(readdirSync(cache), [], "nothing a later run could pick up");
	} finally {
		rmSync(cache, { recursive: true, force: true });
	}
});

test("an archive that is not the pinned distribution is refused by its file count", async () => {
	// The digest settles the bytes; this settles that the bytes are the distribution they are
	// filed as. A tree that comes out of extraction with nine of thirty-four members has the
	// right name for every file that is there, and Python does not start.
	const sandbox = scratch("interpreter");
	try {
		const archive = archiveAt(sandbox, distribution());
		const destination = resolve(sandbox, "staged");
		const failure = await failureOf(() => stageInterpreter(archive, destination, TARGET, 34, recorder().say));
		assert.match(failure, /holds 3 entries where this repository records 34/);
		assert.ok(failure.includes(PYTHON_VERSION));
		assert.ok(failure.includes("Nothing was staged"));
		assert.equal(existsSync(destination), false, "and nothing was unpacked");
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});

test("the staged interpreter has its own header read back, whatever the archive was called", async () => {
	const sandbox = scratch("interpreter");
	try {
		// A genuine, correctly-counted archive whose python.exe is a PE for another architecture:
		// it unpacks, every CRC passes, every name is right, and it fails on the machine it was
		// packaged for.
		const wrong = archiveAt(sandbox, distribution(pe(0xaa64)));
		const failure = await failureOf(() =>
			stageInterpreter(wrong, resolve(sandbox, "wrong"), TARGET, 3, recorder().say),
		);
		assert.match(failure, /python\.exe in the staged Python runtime is a Windows PE executable for arm64/);
		assert.ok(failure.includes("x86_64-pc-windows-msvc"));

		// An archive with no interpreter in it at all is not an embeddable distribution.
		const headless = archiveAt(scratch("interpreter"), zip([["python313.dll", pe(0x8664)]]));
		assert.match(
			await failureOf(() => stageInterpreter(headless, resolve(sandbox, "headless"), TARGET, 1, recorder().say)),
			/unpacked without a python\.exe in it/,
		);

		// And the one that is right passes, unpacks every member, and says what it checked.
		const right = archiveAt(scratch("interpreter"), distribution());
		const report = recorder();
		const destination = resolve(sandbox, "right");
		stageInterpreter(right, destination, TARGET, 3, report.say);
		assert.deepEqual(readdirSync(destination).sort(), ["python.exe", "python313._pth", "python313.dll"].sort());
		assert.ok(report.lines.some((line) => line.startsWith("interpreter: 3 files unpacked")));
		assert.ok(report.lines.some((line) => line.includes("python.exe is a Windows PE executable for x64")));
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});

test("the wheel set is exported without the dev group, and installed for the target", () => {
	// Both flags are load-bearing and both are argv, so both are assertable. `--no-dev` is the
	// one that keeps `pytest` and `pdfplumber` — and `cryptography` and `cffi` behind them — out
	// of an Owner's installer; dropped, the resolved set and the staged set agree and both are
	// wrong, which is the failure no comparison between them can see.
	const exported = wheelExportArgv("/tmp/runtime.txt");
	assert.ok(exported.includes("--no-dev"), "the dev group is excluded at the source");
	assert.deepEqual(exported.slice(0, 2), ["export", "--no-dev"]);
	assert.deepEqual(exported.slice(-2), ["-o", "/tmp/runtime.txt"]);
	assert.ok(exported.includes("--no-emit-project"));

	const install = wheelInstallArgv("/tmp/site", "x86_64-pc-windows-msvc", "3.13", "/tmp/runtime.txt");
	assert.deepEqual(install.slice(0, 2), ["pip", "install"]);
	for (const [flag, value] of [
		["--target", "/tmp/site"],
		["--python-platform", "x86_64-pc-windows-msvc"],
		["--python-version", "3.13"],
		["--only-binary", ":all:"],
		["-r", "/tmp/runtime.txt"],
	] as const) {
		assert.equal(install[install.indexOf(flag) + 1], value, `${flag} names ${value}`);
	}
	// A wheel is never built here, the index never vouches for itself, and the exported set is
	// already closed: resolving again would let the index add to it.
	assert.ok(install.includes("--require-hashes"));
	assert.ok(install.includes("--no-deps"));
});

/**
 * A `uv` that installs `distributions` into the target instead of resolving anything.
 *
 * The export writes exactly the names it is told to, so a test can put the staged set and the
 * resolved set into any relation it likes — including agreeing with each other and both being
 * wrong, which is the state a missing `--no-dev` produces and the state only
 * `assertNoDevDistributions` can see.
 */
function fakeUv(
	distributions: readonly (readonly [string, string])[],
	tag = "cp313-cp313-win_amd64",
	installed: readonly (readonly [string, string])[] = distributions,
): RunCommand {
	return (argv) => {
		if (argv[0] === "export") {
			const output = argv[argv.indexOf("-o") + 1] ?? "";
			writeFileSync(output, `${distributions.map(([name, version]) => `${name}==${version}`).join("\n")}\n`);
			return "";
		}
		const site = argv[argv.indexOf("--target") + 1] ?? "";
		for (const [name, version] of installed) {
			const info = resolve(site, `${name}-${version}.dist-info`);
			mkdirSync(info, { recursive: true });
			writeFileSync(resolve(info, "WHEEL"), `Wheel-Version: 1.0\nTag: ${tag}\n`);
		}
		// Cross-installing writes the host's console-script shims and its headers: POSIX `#!`
		// scripts named `playwright` and `normalizer`, and `greenlet.h`, in a tree bound for
		// Windows. Neither would run on the Owner's machine; both are this host's shape.
		mkdirSync(resolve(site, "bin"), { recursive: true });
		writeFileSync(resolve(site, "bin", "playwright"), "#!/usr/bin/env python\n");
		return "";
	};
}

test("a wheel set carrying the dev group is refused even when it is the set that was resolved", async () => {
	const sandbox = scratch("wheels");
	try {
		const cache = resolve(sandbox, "cache");
		// The export and the install agree: `pytest` was resolved and `pytest` was staged, so the
		// comparison between the two sets passes. `pyproject.toml`'s own dev group is what says
		// this is wrong, and it is read from the checkout rather than copied into the build.
		const failure = await failureOf(() =>
			stageWheels(
				resolve(sandbox, "staged"),
				TARGET,
				CHECKOUT_ROOT,
				cache,
				recorder().say,
				fakeUv([
					["pyyaml", "6.0.3"],
					["pytest", "9.1.1"],
				]),
			),
		);
		assert.match(failure, /the staged wheel set contains the dev group: pytest/);
		assert.ok(failure.includes("Export the requirements with `--no-dev`"));

		// The set that was resolved and nothing else passes, and says what it installed.
		const report = recorder();
		stageWheels(resolve(sandbox, "clean"), TARGET, CHECKOUT_ROOT, cache, report.say, fakeUv([["pyyaml", "6.0.3"]]));
		assert.ok(
			report.lines.includes("wheels: 1 distributions installed, every tag cp313-*-win_amd64 or pure Python"),
		);

		// What uv wrote in this host's shape is taken back out, and said out loud.
		assert.deepEqual(
			readdirSync(resolve(sandbox, "clean", "Lib", "site-packages")).sort(),
			["pyyaml-6.0.3.dist-info"],
			"no bin/ in a tree named for another platform",
		);
		assert.ok(report.lines.some((line) => line.startsWith("pruned: Lib/site-packages/bin/")));

		// A set that is not the set that was resolved fails in both directions, and neither is
		// the dev-group check: something extra was never resolved for this target, and something
		// missing is a wheel that did not install and surfaces on the Owner's machine as an
		// import error.
		const extra = await failureOf(() =>
			stageWheels(
				resolve(sandbox, "extra"),
				TARGET,
				CHECKOUT_ROOT,
				cache,
				recorder().say,
				fakeUv([["pyyaml", "6.0.3"]], "cp313-cp313-win_amd64", [
					["pyyaml", "6.0.3"],
					["cryptography", "46.0.4"],
				]),
			),
		);
		assert.match(extra, /staged but not resolved: cryptography/);
		assert.match(
			await failureOf(() =>
				stageWheels(
					resolve(sandbox, "missing"),
					TARGET,
					CHECKOUT_ROOT,
					cache,
					recorder().say,
					fakeUv(
						[
							["pyyaml", "6.0.3"],
							["certifi", "2026.4.16"],
						],
						"cp313-cp313-win_amd64",
						[["pyyaml", "6.0.3"]],
					),
				),
			),
			/resolved but not staged: certifi/,
		);

		// And a wheel tagged for another interpreter is refused on its recorded tags.
		assert.match(
			await failureOf(() =>
				stageWheels(
					resolve(sandbox, "tagged"),
					TARGET,
					CHECKOUT_ROOT,
					cache,
					recorder().say,
					fakeUv([["pyyaml", "6.0.3"]], "cp312-cp312-manylinux1_x86_64"),
				),
			),
			/is tagged cp312-cp312-manylinux1_x86_64, and this bundle needs cp313-\*-win_amd64/,
		);
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});

test("the pipeline is staged with the files it reads at runtime, and without this host's caches", async () => {
	const sandbox = scratch("pipeline");
	try {
		// A checkout shaped like the real one, so the assertion is about what is copied rather
		// than about the machine the suite runs on.
		const checkout = resolve(sandbox, "checkout");
		const source = resolve(checkout, "src", "venator");
		mkdirSync(resolve(source, "qualify", "schemas"), { recursive: true });
		mkdirSync(resolve(source, "__pycache__"), { recursive: true });
		writeFileSync(resolve(source, "__init__.py"), "");
		writeFileSync(resolve(source, "qualify", "schemas", "qualify-output-2.json"), "{}\n");
		writeFileSync(resolve(source, "__pycache__", "__init__.cpython-313.pyc"), "this host's bytes");

		const destination = resolve(sandbox, "staged");
		const report = recorder();
		stagePipeline(destination, checkout, report.say);
		assert.deepEqual([...walk(resolve(destination, "src"))].sort(), [
			"venator/__init__.py",
			"venator/qualify/schemas/qualify-output-2.json",
		]);
		assert.ok(report.lines.some((line) => line.includes("qualify-output-2.json")));

		// The schema is read when `venator.qualify.versions` is imported and is not a dependency:
		// a tree without it fails on the Owner's machine.
		rmSync(resolve(source, "qualify", "schemas", "qualify-output-2.json"));
		const failure = await failureOf(() => stagePipeline(resolve(sandbox, "second"), checkout, recorder().say));
		assert.match(failure, /the staged pipeline is missing venator\/qualify\/schemas\/qualify-output-2\.json/);

		assert.match(
			await failureOf(() => stagePipeline(resolve(sandbox, "third"), resolve(sandbox, "empty"), recorder().say)),
			/there is no .* to stage/,
		);
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});

test("the sys.path file is replaced, never added, and never written where there was none", async () => {
	const sandbox = scratch("pth");
	try {
		const destination = resolve(sandbox, "staged");
		mkdirSync(destination, { recursive: true });
		// CPython reads `<executable>._pth` and then `<dll>._pth`, so writing a derived name that
		// the archive does not ship leaves two, and the stale one wins: a tree that looks complete
		// and imports nothing.
		const failure = await failureOf(() => writePathConfiguration(destination, TARGET, recorder().say));
		assert.match(failure, /does not ship a python313\._pth/);
		assert.ok(failure.includes("the interpreter"));
		assert.deepEqual(readdirSync(destination), [], "and nothing was written beside it");

		writeFileSync(resolve(destination, pathConfigurationName()), "python313.zip\r\n.\r\n");
		writePathConfiguration(destination, TARGET, recorder().say);
		assert.equal(readFileSync(resolve(destination, pathConfigurationName()), "utf8"), pathConfiguration());
		assert.deepEqual(readdirSync(destination), [pathConfigurationName()], "replaced, never added");
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});

test("every staged file's first bytes are read, including the ones with no extension", async () => {
	const sandbox = scratch("sweep");
	const write = (root: string, path: string, bytes: Uint8Array): void => {
		const target = resolve(root, ...path.split("/"));
		mkdirSync(dirname(target), { recursive: true });
		writeFileSync(target, bytes);
	};
	try {
		// The tree as it should be: an interpreter, a compiled extension, and files that are not
		// executables at all.
		const good = resolve(sandbox, "good");
		write(good, "python.exe", pe(0x8664));
		write(good, "Lib/site-packages/yaml/_yaml.cp313-win_amd64.pyd", pe(0x8664));
		write(good, "Lib/site-packages/yaml/__init__.py", text("import sys\n"));
		write(good, "python313._pth", text("python313.zip\r\n"));
		assert.equal(assertStagedForTarget(good, TARGET), 2, "both native files were read back");

		// `playwright` ships one build re-tagged per platform and records `Tag: py3-none-any` in
		// every one of them; the macOS driver has no extension, so nothing but its first bytes
		// says what it is.
		const foreign = resolve(sandbox, "foreign");
		write(foreign, "Lib/site-packages/playwright/driver/node", machO(0x0100_000c));
		assert.match(
			await failureOf(() => assertStagedForTarget(foreign, TARGET)),
			/playwright\/driver\/node is an executable image for a platform that is not Windows/,
		);

		// And the case the extension gate let through: a *Windows* PE for the wrong architecture,
		// at the same extensionless path. It is not foreign, it is not named like anything
		// Windows loads, and until the check followed the bytes rather than the name its header
		// was read by nothing at all.
		const wrong = resolve(sandbox, "wrong");
		write(wrong, "Lib/site-packages/playwright/driver/node", pe(0x014c));
		const failure = await failureOf(() => assertStagedForTarget(wrong, TARGET));
		assert.match(failure, /playwright\/driver\/node in the staged Python runtime is a Windows PE executable/);
		assert.ok(failure.includes("which needs x64"));

		// The extensions stay in the rule beside the bytes: a `.pyd` that is somehow not a PE at
		// all is an answer rather than a file nothing looked at.
		const hollow = resolve(sandbox, "hollow");
		write(hollow, "Lib/site-packages/yaml/_yaml.cp313-win_amd64.pyd", text("<!doctype html>\n"));
		assert.match(
			await failureOf(() => assertStagedForTarget(hollow, TARGET)),
			/not an ELF, Mach-O or PE executable at all/,
		);

		// A name that says it outright is still refused without reading anything.
		const named = resolve(sandbox, "named");
		write(named, "Lib/site-packages/greenlet/_greenlet.so", text("not even an ELF"));
		assert.match(
			await failureOf(() => assertStagedForTarget(named, TARGET)),
			/_greenlet\.so is native code for a platform that is not Windows/,
		);
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});
