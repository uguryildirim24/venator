/**
 * The seams that decide which Python ends up inside a desktop bundle, and what may ride along.
 *
 * `pnpm sidecar` gives the bundle a Node runtime and the read-only API — a view over a database
 * somebody else's machine built. `pnpm python` is what makes the bundle able to build that
 * database itself, which is the difference between an app that opens onto the fixture forever
 * and an Install that runs its Owner's pipeline. It fetches an interpreter and a wheel set and
 * ships them to somebody, which puts the same three questions in the build that `pnpm sidecar`
 * had to answer: are these the bytes python.org published, are they for the target the tree is
 * *named* for, and is that name a target triple at all rather than a path.
 *
 * The first is answered by a digest pinned in `python-runtime.ts` and asserted here again as a
 * literal, independently of the constant it is checked against — if the two disagree, one of
 * them is wrong and the suite says so. It is never fetched: a checksum served by whoever served
 * the archive agrees with it by construction, so the pin was established once through the
 * release's detached OpenPGP signature and lives in git.
 *
 * The second is worth testing because of how late it fails. A tree called
 * `python/x86_64-pc-windows-msvc/` holding a Linux ELF has the right name, the right size and a
 * checksum matching whatever was fetched, and it fails only on somebody's Windows machine as a
 * pipeline that will not start. The headers here are hand-built rather than read off this
 * machine: the assertions are about mismatches, and a mismatch cannot be produced by the
 * platform running the suite.
 *
 * The third is `parsePythonTarget`, and it is the same escape `parseTarget` was fixed for.
 *
 * There is a fourth question the sidecar never had to ask, and it has its own tests below: a
 * wheel set can be wrong in ways no header check on one file would notice — a dev dependency
 * riding along, or `playwright`'s macOS build, which records the same `Tag: py3-none-any` as its
 * Windows build and differs only in whether its bundled driver is a Mach-O image.
 *
 * No test here touches the network, and none may.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
	existsSync,
	lstatSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	readdirSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { test } from "node:test";

import {
	BUNDLER_TRIPLE,
	HOST_ONLY_DIRECTORIES,
	NOTICE_NAME,
	PIPELINE_SOURCE,
	PYTHON_RESOURCES,
	PYTHON_DIGESTS,
	PYTHON_DOWNLOADS,
	PYTHON_VERSION,
	REQUIRED_PIPELINE_FILES,
	RUNTIME_EVIDENCE,
	SITE_PACKAGES,
	assertDistributionsResolved,
	assertNoDevDistributions,
	assertPythonBinaryMatches,
	assertWheelTags,
	containedResource,
	devGroupNames,
	digestKey,
	isForeignNative,
	isWindowsNative,
	looksForeign,
	missingRuntimeParts,
	missingRuntimeRefusal,
	normalizeDistribution,
	parsePythonTarget,
	pathConfiguration,
	pathConfigurationName,
	pinnedEntryCount,
	pinnedPythonDigest,
	preparePythonResources,
	pythonResourcesNotice,
	pythonResourcesRoot,
	stalePaths,
	pythonAbiNumber,
	pythonArtifact,
	pythonStagingRoot,
	pythonTag,
	requirementNames,
	resolveTargetTriple,
	stageablePythonTarget,
	stagedDistributions,
	staleStagings,
	unpinnedRuntimeNotice,
} from "../tools/python-runtime.ts";
import type { TripleClaim } from "../tools/python-runtime.ts";
import { VERIFIED_FLAG } from "../tools/node-runtime.ts";
import { CHECKOUT_ROOT, UI_ROOT } from "../server/locations.ts";

/** The message of the error `run` throws, so a test can assert on several parts of it. */
function failureOf(run: () => void): string {
	try {
		run();
	} catch (caught) {
		return caught instanceof Error ? caught.message : String(caught);
	}
	assert.fail("expected a refusal, and nothing was thrown");
}

/**
 * Every triple the mapping has an opinion about, and what that opinion is.
 *
 * A table rather than a handful of calls, because the failure being guarded is a triple quietly
 * resolving to its neighbour — the nearest plausible artifact — and that only shows up when the
 * refused cases sit beside the accepted ones. Expected values are literals; none is computed
 * from the function under test.
 */
const ACCEPTED: readonly (readonly [string, string, string])[] = [
	["x86_64-pc-windows-msvc", "x64", "amd64"],
	["aarch64-pc-windows-msvc", "arm64", "arm64"],
];

const REFUSED: readonly (readonly [string, RegExp])[] = [
	// Every non-Windows platform: python.org publishes an embeddable distribution for Windows
	// alone, and there is no nearest match to fall back to.
	["x86_64-apple-darwin", /operating system "darwin"/],
	["x86_64-unknown-linux-gnu", /operating system "linux"/],
	["aarch64-unknown-linux-gnu", /operating system "linux"/],
	["x86_64-unknown-linux-musl", /operating system "linux"/],
	["x86_64-unknown-freebsd", /operating system "freebsd"/],
	["aarch64-linux-android", /operating system "android"/],
	["aarch64-apple-ios", /operating system "ios"/],
	// Windows, but not an architecture python.org names an embeddable build for. i686 is the
	// dangerous one — python.org really does publish `embed-win32` — and staging it under an
	// x86_64 triple would be a genuine, correctly-signed interpreter that cannot load a single
	// `win_amd64` wheel beside it.
	["i686-pc-windows-msvc", /architecture "i686"/],
	["thumbv7a-pc-windows-msvc", /architecture "thumbv7a"/],
	["riscv64gc-pc-windows-msvc", /architecture "riscv64gc"/],
	// Shape: none of these is a target triple, and each is a path or most of one.
	["x86_64-/../../pwn2-windows-msvc", /not a target triple/],
	["x86_64-pc-windows-msvc/../../escape", /not a target triple/],
	["x86_64-pc-windows-msvc-", /not a target triple/],
	["x86_64-pc-windows-msvc-extra", /not a target triple/],
	["x86_64--windows-msvc", /not a target triple/],
	["-pc-windows-msvc", /not a target triple/],
	["x86_64-pc", /not a target triple/],
	["x86_64", /not a target triple/],
	["../../../etc/passwd", /not a target triple/],
	["x86_64-pc-windows-msvc ", /not a target triple/],
	["x86_64 pc windows msvc", /not a target triple/],
	["", /cannot stage a Python runtime for ""/],
];

test("every triple python.org publishes an embeddable build for resolves to exactly that build", () => {
	for (const [triple, arch, flavour] of ACCEPTED) {
		assert.deepEqual(parsePythonTarget(triple), { triple, platform: "win32", arch, flavour }, triple);
	}
	assert.equal(ACCEPTED.length, 2, "one platform, two architectures, and that is the whole matrix");
});

test("every other triple is refused by name, and none of them is approximated", () => {
	for (const [triple, because] of REFUSED) {
		assert.throws(() => parsePythonTarget(triple), because, triple);
	}
	// The refusal says what is and is not possible, because the person reading it is packaging a
	// desktop app and has just been told a platform is unavailable.
	assert.equal(parsePythonTarget("aarch64-apple-darwin").platform, "darwin");
});

/**
 * The sha256 of the archive `pnpm python` downloads, written out again independently of the map
 * the script checks against.
 *
 * Established once, through the signature and not through the download page:
 *
 * ```
 * curl -O https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip{,.asc}
 * curl -o key.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xFC624643487034E5&options=mr"
 * gpg --import key.asc
 * gpg --verify python-3.13.15-embed-amd64.zip.asc python-3.13.15-embed-amd64.zip
 * ```
 *
 * which reports a good signature from "Steve Dower (Python Release Signing)", primary key
 * fingerprint 7ED1 0B65 31D7 C8E1 BC29 6021 FC62 4643 4870 34E5 — the fingerprint python.org
 * documents for the Windows release artifacts, fetched from a keyserver rather than from
 * python.org so the key and the archive do not share an origin. `sha256sum` of the verified file
 * is the value below. Duplicating it here is the point: a version bump that moves
 * `PYTHON_DIGESTS` and forgets this table fails the suite instead of shipping.
 */
const DIGESTS: readonly (readonly [string, string])[] = [
	["win32-x64", "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"],
	["darwin-arm64", "a18e1d1b6067d39cf7b2b605fdb78ad6b8a3aed221c44ef934d399dccf355453"],
];

test("the pinned digest is the one that was verified, and it is pinned rather than fetched", () => {
	for (const [key, digest] of DIGESTS) {
		assert.equal(PYTHON_DIGESTS.get(key), digest, key);
		assert.equal(pinnedPythonDigest(key), digest, key);
	}
	assert.equal(PYTHON_DIGESTS.size, DIGESTS.length, "a pin added without a line here is a half-done bump");
	assert.equal(PYTHON_VERSION, "3.13.15", "the digests above are for this release and no other");
});

test("a target with no pinned digest refuses instead of looking one up", () => {
	// python.org does publish `embed-arm64`, and nothing here has ever fetched or verified it. A
	// digest nobody checked is worse than no digest, so the refusal is the honest state — and
	// falling back to a checksum from python.org would hand the decision to whoever is answering
	// as python.org, which is the whole thing the pin takes away.
	const refusal = failureOf(() => pinnedPythonDigest("win32-arm64"));
	assert.match(refusal, /no sha256 is pinned in python-runtime\.ts for "win32-arm64"/);
	assert.match(refusal, /none was made/, "says plainly that no download happened");
	assert.match(refusal, /OpenPGP signature/, "says where a real digest comes from");
	assert.match(refusal, /agrees with it by construction/, "says why it is not fetched");
	assert.throws(() => pythonArtifact(parsePythonTarget("aarch64-pc-windows-msvc")), /no sha256 is pinned/);
	assert.throws(() => pinnedEntryCount("win32-arm64"), /no file count is pinned/);
});

test("the archive's file count is pinned too, so a clean unpack of the wrong thing is caught", () => {
	// 34 is what `python-3.13.15-embed-amd64.zip` holds: python.exe, pythonw.exe, python3.dll,
	// python313.dll, python313.zip, python313._pth, python.cat, LICENSE.txt, seventeen .pyd
	// extension modules and six support DLLs. The digest settles the bytes; this settles that
	// the bytes are the distribution they are filed as.
	assert.equal(pinnedEntryCount("win32-x64"), 34);
});

test("the artifact is named and located the way python.org names and locates it", () => {
	const target = parsePythonTarget("x86_64-pc-windows-msvc");
	const artifact = pythonArtifact(target);
	assert.equal(artifact.cacheName, "python-3.13.15-embed-amd64.zip");
	assert.equal(artifact.url, "https://www.python.org/ftp/python/3.13.15/python-3.13.15-embed-amd64.zip");
	assert.equal(artifact.signatureUrl, `${artifact.url}.asc`);
	assert.equal(artifact.digest, "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf");
	assert.equal(artifact.entryCount, 34);
	assert.equal(PYTHON_DOWNLOADS, "https://www.python.org/ftp/python");
	assert.equal(digestKey(target), "win32-x64");
	// A flat name, never a path: it becomes a filename in the download cache.
	assert.equal(artifact.cacheName.includes("/"), false);
	assert.equal(artifact.cacheName.includes("\\"), false);
});

test("the interpreter's own names follow the version, and garbage is refused", () => {
	assert.equal(pythonAbiNumber("3.13.15"), "313");
	assert.equal(pythonAbiNumber("3.14.0"), "314");
	assert.equal(pythonTag("3.13.15"), "cp313");
	assert.equal(pathConfigurationName("3.13.15"), "python313._pth");
	assert.equal(pathConfigurationName("3.14.2"), "python314._pth");
	// The defaults follow PYTHON_VERSION, which is 3.13.15 above.
	assert.equal(pythonTag(), "cp313");
	assert.equal(pathConfigurationName(), "python313._pth");
	assert.throws(() => pythonAbiNumber("3"), /not a Python version/);
	assert.throws(() => pythonAbiNumber("../../etc"), /not a Python version/);
	assert.throws(() => pythonAbiNumber(""), /not a Python version/);
});

test("the staged version satisfies what the project declares it needs", () => {
	// Read from pyproject.toml rather than written down, because the failure being guarded is the
	// two drifting apart: raising the floor to 3.14 while this file still pins a 3.13 would ship
	// an interpreter the pipeline does not run on, and every wheel would still be cp313.
	const pyproject = readFileSync(resolve(CHECKOUT_ROOT, "pyproject.toml"), "utf8");
	const declared = /requires-python\s*=\s*"([^"]+)"/u.exec(pyproject);
	assert.notEqual(declared, null, "pyproject.toml declares requires-python");
	const floor = /^>=\s*(\d+)\.(\d+)$/u.exec(declared?.[1] ?? "");
	assert.notEqual(floor, null, `requires-python is a >= floor, not ${declared?.[1] ?? ""}`);
	const staged = PYTHON_VERSION.split(".");
	assert.equal(Number(staged[0]), Number(floor?.[1] ?? 0), "the same major");
	assert.ok(
		Number(staged[1]) >= Number(floor?.[2] ?? 0),
		`Python ${PYTHON_VERSION} is staged and pyproject.toml requires ${declared?.[1] ?? ""}`,
	);
	// uv.lock agrees, and it is what the wheels are exported from.
	assert.match(readFileSync(resolve(CHECKOUT_ROOT, "uv.lock"), "utf8"), /requires-python = ">=3\.13"/u);
});

test("the path configuration is what makes the staged wheels importable at all", () => {
	// Not cosmetic. An embeddable distribution ships a `._pth` naming only the stdlib zip, with
	// `import site` commented out, so a tree staged without this step has 129 MB of wheels next
	// to an interpreter that cannot import one of them — and looks complete from the outside.
	// Written out as one literal because every part of it matters: the order, the backslashes and
	// the CRLF are what the file is read as on Windows.
	assert.equal(
		pathConfiguration("3.13.15"),
		"python313.zip\r\n.\r\nLib\\site-packages\r\nsrc\r\nimport site\r\n",
	);
	assert.equal(pathConfiguration("3.14.0").startsWith("python314.zip\r\n"), true);
	assert.equal(SITE_PACKAGES, "Lib/site-packages");
	assert.equal(PIPELINE_SOURCE, "src");
});

test("a runtime is staged inside the directory this script owns, and nowhere else", () => {
	// Asserted against the real directory and by its parts rather than as one joined string: a
	// separator in an expected value only proves what the platform running the suite happens to
	// spell, and this rule is about a path escaping a directory on every platform.
	// `pythonStagingRoot` is pure path arithmetic and touches no filesystem.
	const resources = resolve(UI_ROOT, "src-tauri/resources");

	const staged = pythonStagingRoot(resources, "x86_64-pc-windows-msvc");
	assert.deepEqual(relative(resources, staged).split(sep), ["python", "x86_64-pc-windows-msvc"]);

	const escaped = failureOf(() => pythonStagingRoot(resources, "x86_64-/../../pwn2-windows-msvc"));
	assert.match(escaped, /which is outside/);
	assert.ok(escaped.includes("Nothing was written"));
	assert.throws(() => pythonStagingRoot(resources, "../../escape"), /which is outside/);
	assert.throws(() => pythonStagingRoot(resources, "a/../../../etc"), /which is outside/);

	// A sibling whose name merely starts with the staging directory is outside it, and this is
	// the case that makes the separator in the containment check load-bearing: `python-evil` is a
	// prefix match on `python` and a different directory entirely.
	const sibling = failureOf(() => pythonStagingRoot(resources, "../python-evil/x"));
	assert.match(sibling, /which is outside/);
	assert.ok(sibling.includes("python-evil"), "the sibling is named, not swallowed");
	assert.equal(dirname(dirname(staged)), resources);
	assert.equal(basename(dirname(staged)), "python");
});

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

/** An ELF header claiming `machine`: the shape a wheel resolved for the build host has. */
function elf(machine: number): Uint8Array {
	const head = new Uint8Array(0x40);
	head.set([0x7f, 0x45, 0x4c, 0x46, 2, 1], 0);
	new DataView(head.buffer).setUint16(0x12, machine, true);
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

test("a runtime for the wrong platform or architecture is refused, however it is named", () => {
	const target = parsePythonTarget("x86_64-pc-windows-msvc");
	assert.match(
		failureOf(() => assertPythonBinaryMatches(elf(0x3e), target, "python.exe")),
		/python\.exe in the staged Python runtime is a Linux ELF executable for x64/,
	);
	assert.match(
		failureOf(() => assertPythonBinaryMatches(machO(0x0100_0007), target, "playwright/driver/node.exe")),
		/is a macOS Mach-O executable for x64/,
	);
	// Right platform, wrong architecture — the case a platform check on its own lets through.
	assert.match(
		failureOf(() => assertPythonBinaryMatches(pe(0xaa64), target, "python.exe")),
		/is a Windows PE executable for arm64/,
	);
	assert.throws(
		() => assertPythonBinaryMatches(pe(0x8664), parsePythonTarget("aarch64-pc-windows-msvc"), "python.exe"),
		/needs arm64/,
	);
	// And the one that must pass.
	assertPythonBinaryMatches(pe(0x8664), target, "python.exe");
});

test("the refusal explains why a wrong-architecture runtime got this far", () => {
	// The person reading it has a build that has worked for months and a download that passed its
	// checksum. If they read this as the bundler being broken they file the wrong bug.
	const message = failureOf(() =>
		assertPythonBinaryMatches(elf(0x3e), parsePythonTarget("x86_64-pc-windows-msvc"), "python.exe"),
	);
	assert.match(message, /passes every check anyone thinks to run/);
	assert.match(message, /matched its pinned sha256/, "names why the earlier checks were silent");
	assert.match(message, /Nothing was staged/);
	assert.match(message, /--python-platform/, "says what to look at");
});

test("an image built for another platform is caught by its first bytes, whatever it is called", () => {
	// This is the check wheel metadata cannot do. `playwright` ships one build re-tagged per
	// platform and records `Tag: py3-none-any` in every one of them, so its macOS wheel installs
	// into a Windows tree without a complaint from anything that reads metadata — and its driver
	// is called `node`, with no extension to give it away.
	assert.equal(looksForeign(elf(0x3e)), true, "a Linux ELF");
	assert.equal(looksForeign(elf(0xb7)), true, "an arm64 Linux ELF");
	assert.equal(looksForeign(machO(0x0100_0007)), true, "a thin 64-bit Mach-O");
	assert.equal(looksForeign(new Uint8Array([0xca, 0xfe, 0xba, 0xbe])), true, "a universal Mach-O");
	assert.equal(looksForeign(new Uint8Array([0xbe, 0xba, 0xfe, 0xca])), true, "and the other byte order");
	assert.equal(looksForeign(pe(0x8664)), false, "a Windows PE is what belongs here");
	assert.equal(looksForeign(new TextEncoder().encode("import sys\n")), false, "a .py file is not an image");
	assert.equal(looksForeign(new Uint8Array([0x7f, 0x45])), false, "too short to say, and not a refusal");
	assert.equal(looksForeign(new Uint8Array()), false);
});

test("native code is recognised by name in both directions", () => {
	for (const name of ["python.exe", "_ssl.pyd", "libcrypto-3.dll", "PIL/_imaging.cp313-win_amd64.pyd", "A.DLL"]) {
		assert.equal(isWindowsNative(name), true, name);
		assert.equal(isForeignNative(name), false, name);
	}
	for (const name of ["_yaml.cpython-313-x86_64-linux-gnu.so", "PIL/_imaging.dylib"]) {
		assert.equal(isForeignNative(name), true, name);
		assert.equal(isWindowsNative(name), false, name);
	}
	for (const name of ["venator/qualify/schemas/qualify-output-2.json", "python313.zip", "playwright/driver/node", "LICENSE.txt"]) {
		assert.equal(isWindowsNative(name), false, name);
		assert.equal(isForeignNative(name), false, name);
	}
});

test("the resolved wheel set is read out of what uv exported, and nothing else", () => {
	// The shape `uv export --no-dev --format requirements-txt` emits: a pinned line per
	// distribution, hash continuations indented under it, comments and a marker preamble.
	const exported = [
		"# This file was autogenerated by uv via the following command:",
		"#    uv export --no-dev --no-emit-project --format requirements-txt",
		"anyio==4.14.2 \\",
		"    --hash=sha256:9f505dda5ac9f0c8309b5e8bd445a8c2bf7246f3ce950121e45ea15bc41d1494",
		"    # via httpx",
		"PyYAML==6.0.3 \\",
		"    --hash=sha256:0000000000000000000000000000000000000000000000000000000000000000",
		"typing_extensions==4.16.0",
		// Indented, and therefore a continuation of the line above rather than a requirement of
		// its own. Only a line that starts at column zero starts one — which is the rule that
		// keeps this from being a requirements parser, and the rule that decides whether the dev
		// group can arrive through the resolved set.
		"    pytest==9.1.1",
		"",
	].join("\n");
	assert.deepEqual(requirementNames(exported), ["anyio", "pyyaml", "typing-extensions"]);
	assert.deepEqual(requirementNames(""), []);
	assert.deepEqual(requirementNames("    --hash=sha256:abc\n\t--hash=sha256:def\n"), []);
});

test("the staged wheel set is read out of the directory names uv actually writes", () => {
	assert.deepEqual(
		stagedDistributions([
			"anyio",
			"anyio-4.14.2.dist-info",
			"typing_extensions-4.16.0.dist-info",
			"typing_extensions.py",
			"charset_normalizer-3.5.1.dist-info",
			"PIL",
			"yaml",
		]),
		["anyio", "charset-normalizer", "typing-extensions"],
	);
	assert.deepEqual(stagedDistributions([]), []);
	assert.equal(normalizeDistribution("Typing_Extensions"), "typing-extensions");
	assert.equal(normalizeDistribution("charset.normalizer"), "charset-normalizer");
});

test("a staged set that is not the resolved set is refused, in both directions", () => {
	assertDistributionsResolved(["anyio", "httpx"], ["anyio", "httpx"]);
	const extra = failureOf(() => assertDistributionsResolved(["anyio", "httpx", "pytest"], ["anyio", "httpx"]));
	assert.match(extra, /staged but not resolved: pytest/);
	const missing = failureOf(() => assertDistributionsResolved(["anyio"], ["anyio", "reportlab"]));
	assert.match(missing, /resolved but not staged: reportlab/);
	assert.equal(missing.includes("staged but not resolved"), false, "one direction at a time");
	assert.match(extra, /Nothing was staged/);
});

test("the dev group is refused by name, independently of how the set was resolved", () => {
	// Not the same check as the one above. If `--no-dev` were dropped from the export, the
	// resolved set and the staged set would agree with each other and both be wrong, and pytest
	// and pdfplumber — with cryptography and cffi behind it — would ship in somebody's installer.
	const dev = ["pdfplumber", "pytest"];
	assertNoDevDistributions(["anyio", "httpx", "playwright"], dev);
	const refusal = failureOf(() => assertNoDevDistributions(["anyio", "pdfplumber", "pytest"], dev));
	assert.match(refusal, /contains the dev group: pdfplumber, pytest/);
	assert.match(refusal, /--no-dev/, "says how it gets fixed");
});

test("the dev group is read from the project, not written down beside it", () => {
	// A copy of the group in the build script could agree with itself while disagreeing with the
	// project, which is exactly the drift this check exists to catch.
	assert.deepEqual(
		devGroupNames('[dependency-groups]\ndev = [\n    "pdfplumber>=0.11.10",\n    "pytest>=9.1.1",\n]\n'),
		["pdfplumber", "pytest"],
	);
	// Extras are the case a single regular expression gets wrong: the `]` closing `[xdist]` is
	// not the `]` closing the array, and stopping at it reads the group as empty — which makes
	// the check that uses it one that can never fire.
	assert.deepEqual(
		devGroupNames('[dependency-groups]\ndev = ["pytest[xdist]>=9.1.1", "pdfplumber>=0.11"]'),
		["pdfplumber", "pytest"],
	);
	assert.deepEqual(devGroupNames("[dependency-groups]\ndev = [\"Ruff[extra]==1.0\"]"), ["ruff"]);
	assert.deepEqual(devGroupNames('[project]\ndependencies = ["httpx"]\n'), [], "no dev group is not an error");
	// And against the real file, so a dev dependency added later is covered without an edit here.
	const declared = devGroupNames(readFileSync(resolve(CHECKOUT_ROOT, "pyproject.toml"), "utf8"));
	assert.deepEqual(declared, ["pytest"]);
});

test("a wheel tagged for another interpreter or another platform is refused", () => {
	assertWheelTags("anyio-4.14.2.dist-info", ["py3-none-any"], "cp313", "win_amd64");
	assertWheelTags("pyyaml-6.0.3.dist-info", ["cp313-cp313-win_amd64"], "cp313", "win_amd64");
	assertWheelTags("x", ["cp313-cp313-win_amd64", "cp313-cp313-win32"], "cp313", "win_amd64");
	assert.match(
		failureOf(() => assertWheelTags("pyyaml", ["cp313-cp313-manylinux_2_17_x86_64"], "cp313", "win_amd64")),
		/tagged cp313-cp313-manylinux_2_17_x86_64/,
	);
	assert.match(
		failureOf(() => assertWheelTags("pyyaml", ["cp312-cp312-win_amd64"], "cp313", "win_amd64")),
		/needs cp313-\*-win_amd64/,
	);
	assert.match(
		failureOf(() => assertWheelTags("pyyaml", ["cp313-cp313-win_arm64"], "cp313", "win_amd64")),
		/needs cp313-\*-win_amd64/,
	);
	assert.throws(() => assertWheelTags("pyyaml", [], "cp313", "win_amd64"), /Nothing was staged/);
});

test("every file the pipeline reads beyond its own .py files is named", () => {
	// A data file under src/venator/ that staging does not require can go missing from a
	// bundle and still import until the first read on the Owner's machine. So the list is
	// compared with the tree rather than trusted.
	const root = resolve(CHECKOUT_ROOT, "src");
	const found: string[] = [];
	const visit = (directory: string): void => {
		for (const entry of readdirSync(directory, { withFileTypes: true })) {
			const path = join(directory, entry.name);
			if (entry.isDirectory()) {
				if (entry.name !== "__pycache__") visit(path);
			} else if (!entry.name.endsWith(".py")) {
				found.push(relative(root, path).split(sep).join("/"));
			}
		}
	};
	visit(resolve(root, "venator"));
	assert.deepEqual(found.sort(), REQUIRED_PIPELINE_FILES.filter((file) => !file.endsWith(".py")));
	assert.deepEqual(HOST_ONLY_DIRECTORIES, ["bin", "include"]);
});

test("the desktop bundle declares the staged runtime, or it does not ship", () => {
	// Staging into `src-tauri/resources/` does nothing on its own — Tauri copies what
	// `bundle.resources` names and ignores the rest, so an undeclared tree is 154 MB assembled on
	// the build host and absent from the installer.
	const config = readFileSync(resolve(UI_ROOT, "src-tauri/tauri.conf.json"), "utf8");
	const resources = JSON.parse(config).bundle.resources;
	// The sample view is not among them, and that is a property rather than an omission: a
	// shipped Install must not carry nineteen Postings from employers its Owner never searched
	// (`server/db.ts`, `src-tauri/src/lib.rs`).
	//
	// The four `.py` files are the adapters the bundled server spawns by *path*, as siblings
	// of itself. They were absent from this list until the load-back was added, and on a
	// packaged Install that meant `boards/resolve` and the run plan failed at launch;
	// `tools/adapter-staging.ts` and `tests/adapter-staging.test.ts` say the rest.
	assert.deepEqual(resources, [
		"resources/server.mjs",
		"resources/parse_resume.py",
		"resources/resolve_board.py",
		"resources/verify_profile.py",
		"resources/plan.py",
		"resources/python/**/*",
	]);
	// The glob and the directory the staging writes into have to be the same directory, or the
	// bundle declares one thing and the build stages another.
	assert.ok(resources.includes(`resources/${PYTHON_RESOURCES}/**/*`));

	// And the step is reachable the way every other build step here is.
	const scripts = JSON.parse(readFileSync(resolve(UI_ROOT, "package.json"), "utf8")).scripts;
	assert.equal(scripts.python, "node tools/prepare-python.ts");
});

test("the resources directory is never empty, because an empty glob fails the build", () => {
	// This is not a tidiness rule. Tauri's build script refuses a resource glob that matches
	// nothing — `glob pattern resources/python/**/* path not found or didn't match any files` —
	// and it fails `cargo build` before a line of the host is compiled. It failed the
	// `windows — tauri host compiles` job, which stages the sidecar and never runs `pnpm python`.
	assert.equal(PYTHON_RESOURCES, "python");
	assert.equal(NOTICE_NAME, "README.txt");
	const notice = pythonResourcesNotice();
	assert.match(notice, /pnpm python --target/, "says how the directory gets filled");
	assert.match(notice, /refuses a resource\s+glob that matches no files/u, "says why it is here at all");
	assert.match(notice, /generated and gitignored/, "says it is not somebody's file to keep");
	assert.equal(notice.endsWith("\n"), true);

	// `prepare-sidecar.ts` is what runs on every desktop build, so it is what has to do this.
	const sidecar = readFileSync(resolve(UI_ROOT, "tools/prepare-sidecar.ts"), "utf8");
	assert.match(sidecar, /preparePythonResources\(RESOURCES, route\.triple\)/u);
});

test("a runtime staged for another target is taken out rather than shipped in this one", () => {
	// `bundle.resources` is a glob, not a triple: everything under `resources/python/` ships,
	// whichever target it was staged for. Staging a Windows runtime on the Owner's Mac and then
	// building a .dmg would otherwise produce a macOS bundle carrying 154 MB of Windows
	// binaries — built without a warning and wrong in a way nothing downstream would notice.
	const entries = ["README.txt", "aarch64-apple-darwin", "x86_64-pc-windows-msvc"];
	assert.deepEqual(staleStagings(entries, "x86_64-pc-windows-msvc"), ["aarch64-apple-darwin"]);
	assert.deepEqual(staleStagings(entries, "aarch64-apple-darwin"), ["x86_64-pc-windows-msvc"]);
	// A target with nothing staged for it clears both, and the notice is never one of them.
	assert.deepEqual(staleStagings(entries, "x86_64-unknown-linux-gnu"), [
		"aarch64-apple-darwin",
		"x86_64-pc-windows-msvc",
	]);
	assert.deepEqual(staleStagings(["README.txt"], "x86_64-pc-windows-msvc"), []);
	assert.deepEqual(staleStagings([], "x86_64-pc-windows-msvc"), []);
});

test("preparing the resources directory makes it real, keeps this target, and clears the rest", () => {
	const resources = mkdtempSync(join(tmpdir(), "venator-resources-"));
	try {
		// Nothing there at all: the directory and its notice are created, and there is something
		// for the glob to match.
		assert.deepEqual(preparePythonResources(resources, "x86_64-pc-windows-msvc"), []);
		const root = resolve(resources, "python");
		assert.deepEqual(readdirSync(root), ["README.txt"]);
		assert.equal(readFileSync(resolve(root, "README.txt"), "utf8"), pythonResourcesNotice());

		// A staged tree for this target survives; one for another target does not.
		mkdirSync(resolve(root, "x86_64-pc-windows-msvc"), { recursive: true });
		writeFileSync(resolve(root, "x86_64-pc-windows-msvc", "python.exe"), "MZ");
		mkdirSync(resolve(root, "aarch64-apple-darwin"), { recursive: true });
		writeFileSync(resolve(root, "aarch64-apple-darwin", "python"), "elf");
		assert.deepEqual(preparePythonResources(resources, "x86_64-pc-windows-msvc"), ["aarch64-apple-darwin"]);
		assert.deepEqual(readdirSync(root).sort(), ["README.txt", "x86_64-pc-windows-msvc"]);
		assert.equal(existsSync(resolve(root, "x86_64-pc-windows-msvc", "python.exe")), true);

		// Running it again changes nothing and reports nothing.
		assert.deepEqual(preparePythonResources(resources, "x86_64-pc-windows-msvc"), []);
		assert.deepEqual(readdirSync(root).sort(), ["README.txt", "x86_64-pc-windows-msvc"]);
	} finally {
		rmSync(resources, { recursive: true, force: true });
	}
});

test("the containment rule is one function, and the sibling that merely shares a prefix is out", () => {
	// Stated once and used by both the function that stages and the function that deletes. The
	// deleting one is why it is shared at all: it enumerated a directory and removed every entry
	// with no containment check of any kind, which is the half of the pair that cannot be undone.
	const root = resolve(tmpdir(), "venator-contained");
	assert.deepEqual(relative(root, containedResource(root, "x86_64-pc-windows-msvc", "a", "b")).split(sep), [
		"x86_64-pc-windows-msvc",
	]);

	// The trailing separator, load-bearing: `venator-contained-evil` is a prefix match on
	// `venator-contained` and a different directory entirely.
	const sibling = failureOf(() => containedResource(root, `../${basename(root)}-evil/pwn`, "a runtime", "Because."));
	assert.match(sibling, /which is outside/);
	assert.ok(sibling.includes(`${basename(root)}-evil`), "the sibling is named, not swallowed");
	assert.ok(sibling.includes("Nothing was written and nothing was removed"));
	assert.ok(sibling.includes("a runtime resolves to"), "the caller says what it was resolving");
	assert.ok(sibling.includes("Because."), "and why that directory is the only one it may touch");
	assert.match(failureOf(() => containedResource(root, "../../etc", "a", "b")), /which is outside/);
	assert.match(failureOf(() => containedResource(root, "a/../../etc", "a", "b")), /which is outside/);

	// And the rule as the deletion reaches it. `readdirSync` never answers `..` or an absolute
	// path, so this cannot fire through `preparePythonResources` — which is exactly why it is a
	// function of its own rather than a line inside the loop that deletes.
	assert.deepEqual(stalePaths(root, ["aarch64-apple-darwin", "..evil"]), [
		resolve(root, "aarch64-apple-darwin"),
		resolve(root, "..evil"),
	]);
	const escape = failureOf(() => stalePaths(root, ["aarch64-apple-darwin", "../../etc"]));
	assert.match(escape, /which is outside/);
	assert.ok(escape.includes("the staged runtime"), "says what it was about to delete");
	assert.ok(escape.includes("recursive delete"), "and what that would have cost");
	assert.deepEqual(stalePaths(root, []), []);
});

test("a symlinked resources/python is refused rather than emptied through", () => {
	// The defect this is written against, reproduced exactly: `resolve` is textual and does not
	// follow links, so with `resources/python` symlinked at a sibling directory, `readdirSync`
	// enumerated that directory's children and the recursive delete removed them — outside
	// `resources/`, outside the checkout, silently, with an exit code of 0. This runs on every
	// `pnpm sidecar`, and `beforeDevCommand` and `beforeBuildCommand` both run that.
	const sandbox = mkdtempSync(join(tmpdir(), "venator-resources-link-"));
	try {
		const resources = resolve(sandbox, "resources");
		mkdirSync(resources, { recursive: true });
		const precious = resolve(sandbox, "precious");
		mkdirSync(resolve(precious, "venator"), { recursive: true });
		writeFileSync(resolve(precious, "keepme.txt"), "not the build's file to delete");
		writeFileSync(resolve(precious, "venator", "notes.md"), "nor this one");
		symlinkSync(precious, resolve(resources, "python"), "dir");

		const failure = failureOf(() => preparePythonResources(resources, "x86_64-pc-windows-msvc"));
		assert.ok(failure.includes(resolve(resources, "python")), "names the path it required");
		assert.ok(failure.includes(precious), "names the path it got");
		assert.match(failure, /is a link to/);
		assert.ok(failure.includes("Nothing was written and nothing was removed"));

		// And it means it: the refusal is before the notice is written and before anything is
		// removed, so the directory on the other end of the link is exactly as it was found.
		assert.deepEqual(readdirSync(precious).sort(), ["keepme.txt", "venator"]);
		assert.deepEqual(readdirSync(resolve(precious, "venator")), ["notes.md"]);
		assert.equal(existsSync(resolve(precious, "README.txt")), false, "not even the notice");

		// A link to somewhere that does not exist is the same refusal, in this reader's own words.
		// It is why the check comes before the directory is created rather than after: `mkdir -p`
		// over a dangling link fails with a bare `EEXIST` out of the middle of a build step.
		rmSync(resolve(resources, "python"));
		symlinkSync(resolve(sandbox, "nowhere"), resolve(resources, "python"), "dir");
		const dangling = failureOf(() => preparePythonResources(resources, "x86_64-pc-windows-msvc"));
		assert.match(dangling, /is a link to/);
		assert.ok(dangling.includes("Nothing was written and nothing was removed"));

		// `pythonResourcesRoot` is the check on its own, and it answers plainly when there is no
		// link: the same directory back.
		rmSync(resolve(resources, "python"));
		mkdirSync(resolve(resources, "python"));
		assert.equal(pythonResourcesRoot(resources), resolve(resources, "python"));
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});

test("a link inside the resources directory is unlinked, and an oddly-named directory is a name", () => {
	// The two narrower cases, which were already sound and have to stay that way. A stale entry
	// that is itself a link is the thing that was staged there, so removing it is removing a
	// link — never the tree it points at. And a directory called `..evil` is an ordinary basename
	// that `resolve` keeps whole; only a literal `..` walks anywhere.
	const sandbox = mkdtempSync(join(tmpdir(), "venator-resources-entry-"));
	try {
		const resources = resolve(sandbox, "resources");
		const keep = resolve(sandbox, "keep");
		mkdirSync(keep, { recursive: true });
		writeFileSync(resolve(keep, "keepme.txt"), "pointed at, never followed");

		preparePythonResources(resources, "x86_64-pc-windows-msvc");
		const root = resolve(resources, "python");
		symlinkSync(keep, resolve(root, "aarch64-apple-darwin"), "dir");
		mkdirSync(resolve(root, "..evil"), { recursive: true });
		writeFileSync(resolve(root, "..evil", "planted"), "staged for nothing at all");

		assert.deepEqual(preparePythonResources(resources, "x86_64-pc-windows-msvc"), [
			"..evil",
			"aarch64-apple-darwin",
		]);
		assert.deepEqual(readdirSync(root), ["README.txt"], "both entries gone from the directory");
		assert.deepEqual(readdirSync(keep), ["keepme.txt"], "and the link's target is untouched");
		assert.equal(existsSync(resolve(sandbox, "evil")), false, "`..evil` never resolved as `..`");
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});

test("an ancestor that is a link is the build host's business, not this script's", () => {
	// Only the last component has to be real. A checkout under a symlinked home — or macOS, where
	// `/tmp` is a link to `/private/tmp` and every one of these tests runs under it — is an
	// ordinary build host, and refusing it would make this check something people route around.
	const sandbox = mkdtempSync(join(tmpdir(), "venator-resources-ancestor-"));
	try {
		const real = resolve(sandbox, "real");
		mkdirSync(real, { recursive: true });
		const through = resolve(sandbox, "through");
		symlinkSync(real, through, "dir");

		assert.deepEqual(preparePythonResources(through, "x86_64-pc-windows-msvc"), []);
		assert.deepEqual(readdirSync(resolve(real, "python")), ["README.txt"]);
		assert.equal(lstatSync(resolve(real, "python")).isSymbolicLink(), false);
	} finally {
		rmSync(sandbox, { recursive: true, force: true });
	}
});

test("a target either has a runtime pinned for it or is one that carries none", () => {
	// Two different absences, and a bundle treats them the same way because both mean the same
	// thing: no runtime goes in this one. Neither is a failure and neither may be papered over.
	assert.equal(stageablePythonTarget("x86_64-pc-windows-msvc")?.arch, "x64");
	assert.equal(stageablePythonTarget("aarch64-apple-darwin")?.platform, "darwin");
	assert.equal(stageablePythonTarget("x86_64-unknown-linux-gnu"), null);
	assert.equal(stageablePythonTarget("aarch64-pc-windows-msvc"), null, "a build exists; nothing has verified it");
	assert.equal(stageablePythonTarget("not a triple"), null);
	assert.equal(stageablePythonTarget(""), null);
});

test("a bundle that would ship without the runtime it can carry is refused, by name", () => {
	// The failure is silent by construction: `resources/python/` always holds at least a
	// README.txt, because Tauri refuses a resource glob that matches nothing — so a bundle built
	// without ever running `pnpm python` is not a build failure. It is an installer that opens
	// onto the sample corpus on a machine with no Python and stays there.
	assert.deepEqual(RUNTIME_EVIDENCE, [
		"python.exe",
		SITE_PACKAGES,
		`${PIPELINE_SOURCE}/venator/__init__.py`,
		pathConfigurationName(),
	]);

	// Four files, each a different way for the staging to have half-happened — the interpreter,
	// the wheels it imports, the pipeline itself, and the `._pth` that lets it see the other two.
	assert.deepEqual(missingRuntimeParts(() => true), []);
	assert.deepEqual(missingRuntimeParts(() => false), RUNTIME_EVIDENCE);
	assert.deepEqual(
		missingRuntimeParts((path) => path !== SITE_PACKAGES && path !== pathConfigurationName()),
		[SITE_PACKAGES, pathConfigurationName()],
	);

	const refusal = missingRuntimeRefusal("x86_64-pc-windows-msvc", [SITE_PACKAGES, "python.exe"], "VENATOR_OPT_OUT");
	assert.ok(refusal.includes("Lib/site-packages, python.exe"), "names what is missing");
	assert.ok(refusal.includes("Nothing was bundled"));
	assert.match(refusal, /pnpm python --target x86_64-pc-windows-msvc/, "says how to fix it");
	assert.match(refusal, /VENATOR_OPT_OUT=1/, "and how to mean it deliberately");
	assert.match(missingRuntimeRefusal("x86_64-pc-windows-msvc", ["python.exe"], "X"), /python\.exe is not staged/u);
});

test("a target with no runtime to carry says so at build time, every time", () => {
	// The Owner's Mac, and not a state to be fixed by staging harder: there is no macOS
	// embeddable distribution to pin, so a .dmg carries the dashboard and no pipeline. Said out
	// loud at build time, because the difference from a complete bundle is invisible afterwards.
	const notice = unpinnedRuntimeNotice("x86_64-apple-darwin");
	assert.ok(notice.includes("carries no Python runtime"));
	assert.ok(notice.includes("x86_64-apple-darwin"));
	assert.match(notice, /embeddable distribution for Windows only/);
	assert.match(notice, /sample\s+corpus/u, "says what the Install gets instead");
	// And that the sidecar step has just taken any other target's runtime out from under it.
	assert.match(notice, /cleared any runtime staged for another target/);
});

test("the packaging path stages for the target it is bundling for, and asserts what it staged", () => {
	// `beforeBuildCommand` used to run `pnpm sidecar` with no target at all — this machine's own
	// Node into a bundle named for another platform — and nothing anywhere asserted that the
	// Python runtime was there. Both are one step now, and `tauri build` is what runs it.
	const config = JSON.parse(readFileSync(resolve(UI_ROOT, "src-tauri/tauri.conf.json"), "utf8"));
	assert.equal(config.build.beforeBuildCommand, "pnpm desktop:stage && pnpm build:desktop");

	const scripts = JSON.parse(readFileSync(resolve(UI_ROOT, "package.json"), "utf8")).scripts;
	assert.equal(scripts["desktop:stage"], "node tools/desktop-stage.ts");
	assert.equal(scripts.sidecar, "node tools/prepare-sidecar.ts");
	assert.equal(scripts.python, "node tools/prepare-python.ts");
	assert.equal(scripts["desktop:build"], "tauri build", "still what packages the app");

	// The gate starts those two scripts by file rather than through `pnpm`, because on Windows
	// `pnpm` is a `.cmd` that `execFileSync` cannot start without a shell. Same programs, so the
	// two spellings are asserted to be the same files.
	const gate = readFileSync(resolve(UI_ROOT, "tools/desktop-stage.ts"), "utf8");
	for (const script of [scripts.sidecar, scripts.python]) {
		const file = script.replace("node ", "");
		assert.ok(gate.includes(`resolve(UI_ROOT, "${file}")`), `${file} is what the gate runs`);
	}
	assert.match(gate, /\[script, "--target", triple, \.\.\.flags\]/u, "and the resolved target is forwarded to both");

	// And the sidecar step is asked for a verified runtime, which is what makes the Node inside a
	// bundle one whose bytes were checked rather than whatever Node the build machine happened to
	// have. `tests/sidecar-staging.test.ts` covers the route that flag selects.
	assert.equal(VERIFIED_FLAG, "--verified");
	assert.match(gate, /step\(SIDECAR, triple, VERIFIED_FLAG\)/u, "on every bundle, host target included");
	assert.match(gate, /step\(PYTHON, triple\)/u, "and the Python step, which has no such route, is not");
});

test("the desktop build command is one a Windows shell can run", () => {
	// The step between staging and the bundler, and the one that made a Windows installer
	// impossible to produce at all. `beforeBuildCommand` ends in `pnpm build:desktop`, and that
	// script used to be `VITE_API_BASE=... vite build` — a POSIX assignment prefix. pnpm runs a
	// script through cmd.exe on Windows, which has no such grammar: it looks for a program named
	// `VITE_API_BASE=http://127.0.0.1:5170/api`, does not find one, and takes `tauri build` down
	// with it before a single byte is bundled. The value is a Vite mode file now, which both
	// hosts read the same way.
	//
	// Asserted rather than left to the packaging job, because the packaging job runs on pushes to
	// main and this suite runs on every pull request: the cheap check is the one that should
	// catch a reintroduced prefix.
	const scripts = JSON.parse(readFileSync(resolve(UI_ROOT, "package.json"), "utf8")).scripts;
	const desktop = scripts["build:desktop"];
	assert.equal(desktop, "vite build --mode desktop");
	assert.doesNotMatch(desktop, /^\s*[A-Z_][A-Z0-9_]*=/u, "no POSIX assignment prefix; cmd.exe cannot parse one");

	// And the mode file carries the origin the prefix used to carry. A webview page is served
	// from `tauri://localhost`, where the default relative `/api` in `src/api.ts` resolves against
	// the app protocol and reaches nothing at all.
	const mode = readFileSync(resolve(UI_ROOT, ".env.desktop"), "utf8");
	assert.match(mode, /^VITE_API_BASE=http:\/\/127\.0\.0\.1:5170\/api$/mu);
});

test("the triple the bundler is packaging for is followed, and never quietly outranked", () => {
	// The resolution order shipped documented one way and implemented another, and nothing here
	// noticed: `tauri build` sets TAURI_ENV_TARGET_TRIPLE and then runs `beforeBuildCommand` with
	// no arguments, so a $VENATOR_DESKTOP_TARGET left over in a shell beat the triple actually
	// being packaged for and the staging steps were handed the loser. Both steps take `--target`
	// and neither compares it back, so what came out was 150 MB of Windows binaries inside a
	// macOS .dmg, announced as "bundling for x86_64-pc-windows-msvc" and exiting 0.
	const bundler = "aarch64-apple-darwin";
	const other = "x86_64-pc-windows-msvc";
	const none: readonly TripleClaim[] = [
		{ source: "--target", triple: "" },
		{ source: "$VENATOR_DESKTOP_TARGET", triple: "" },
	];

	// Set, and contradicted by nothing: followed, and it says that is where the answer came from.
	assert.deepEqual(resolveTargetTriple(bundler, none), { source: BUNDLER_TRIPLE, triple: bundler });

	// Set, and agreed with: still followed, still attributed to the bundler rather than the echo.
	assert.deepEqual(
		resolveTargetTriple(bundler, [
			{ source: "--target", triple: bundler },
			{ source: "$VENATOR_DESKTOP_TARGET", triple: bundler },
		]),
		{ source: BUNDLER_TRIPLE, triple: bundler },
	);

	// Set, and contradicted: refused from either source, rather than one of them winning. A
	// precedence cannot catch this — whichever way it ran, the loser would go without a word.
	for (const source of ["--target", "$VENATOR_DESKTOP_TARGET"]) {
		const claims = none.map((claim) => (claim.source === source ? { source, triple: other } : claim));
		assert.throws(
			() => resolveTargetTriple(bundler, claims),
			(caught: Error) => {
				assert.ok(caught.message.includes(bundler), "names what the bundler is packaging for");
				assert.ok(caught.message.includes(other), "and the triple that disagreed");
				assert.ok(caught.message.includes(BUNDLER_TRIPLE), "and where the first came from");
				assert.ok(caught.message.includes(source), "and where the second came from");
				assert.match(caught.message, /Nothing was staged/u);
				return true;
			},
			`${source} disagreeing with ${BUNDLER_TRIPLE} is refused`,
		);
	}

	// Absent — somebody running `pnpm desktop:stage` by hand — and the fallbacks decide in order.
	assert.deepEqual(
		resolveTargetTriple("", [
			{ source: "--target", triple: other },
			{ source: "$VENATOR_DESKTOP_TARGET", triple: bundler },
		]),
		{ source: "--target", triple: other },
		"an explicit --target is the first fallback",
	);
	assert.deepEqual(
		resolveTargetTriple("", [
			{ source: "--target", triple: "" },
			{ source: "$VENATOR_DESKTOP_TARGET", triple: bundler },
		]),
		{ source: "$VENATOR_DESKTOP_TARGET", triple: bundler },
		"then the environment",
	);
	assert.equal(resolveTargetTriple("", none), null, "and then, and only then, the Rust host triple");

	// With no bundler triple there is nothing to disagree with, so two hand-set sources are a
	// precedence rather than a refusal. That is the case where a person is watching the output.
	assert.deepEqual(
		resolveTargetTriple("", [
			{ source: "--target", triple: other },
			{ source: "$VENATOR_DESKTOP_TARGET", triple: other },
		]),
		{ source: "--target", triple: other },
	);
});

test("the gate reads those sources, in that order, and a disagreement stops `tauri build`", () => {
	// The resolver above is only half of it: a correct resolver called with the arguments in the
	// wrong order reproduces the whole defect. So this asserts the wiring, and then runs the gate
	// for real. `tauri build` stops only if the refusal reaches the exit code, and the refusal is
	// worth nothing if the build carries on and packages what is sitting in `resources/`.
	const gate = readFileSync(resolve(UI_ROOT, "tools/desktop-stage.ts"), "utf8");
	const call = /resolveTargetTriple\(process\.env\[BUNDLER_TRIPLE\] \?\? "", \[\s*\{ source: "--target"[^]*?\$VENATOR_DESKTOP_TARGET"/u;
	assert.match(gate, call, "the bundler's triple is the first argument, and the two fallbacks follow in order");

	// A conflict is refused before a single step is spawned, so this stages nothing and touches
	// no tree — which is itself the property being asserted.
	//
	// The two triples are musl ones deliberately. This spawns the real gate, so the run that
	// happens when the refusal *stops* firing is a real staging run against this checkout, and a
	// mutation run of that shape has already cleared 150 MB of locally staged Windows runtime out
	// of `src-tauri/resources/python/` — gitignored, nothing tracked lost, but a developer's
	// afternoon. Neither `parseTarget` nor `parsePythonTarget` can stage a musl target, so a gate
	// that failed to refuse gets as far as the sidecar step and is turned away there, before
	// anything is written, fetched or cleared. The refusal under test compares two strings and
	// does not care which strings they are, so nothing about the case is weakened by choosing
	// ones that fail safe afterwards.
	const run = spawnSync(process.execPath, [resolve(UI_ROOT, "tools/desktop-stage.ts")], {
		cwd: UI_ROOT,
		encoding: "utf8",
		env: {
			...process.env,
			TAURI_ENV_TARGET_TRIPLE: "x86_64-unknown-linux-musl",
			VENATOR_DESKTOP_TARGET: "aarch64-unknown-linux-musl",
		},
	});
	assert.equal(run.status, 1, "a refusal exits non-zero, or `tauri build` packages the wrong tree anyway");
	assert.match(run.stderr, /two different answers about which target this bundle is for/u);
	assert.match(run.stderr, /x86_64-unknown-linux-musl/u);
	assert.match(run.stderr, /aarch64-unknown-linux-musl/u);
	assert.doesNotMatch(run.stdout, /bundling for/u, "and says so instead of announcing a winner");
});
