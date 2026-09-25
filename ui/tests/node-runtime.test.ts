/**
 * The seams that decide which Node runtime ends up inside a desktop bundle.
 *
 * `pnpm sidecar` used to copy this machine's own Node and could stage nothing else, so a
 * Windows installer needed a Windows build host for the Node half. Staging another platform's
 * runtime means fetching an executable and then shipping it to somebody, which puts three
 * questions in the build that were not there before: is this the artifact nodejs.org published,
 * is it for the target the file is *named* for, and is that name a target triple at all rather
 * than a path.
 *
 * The first is answered by a digest pinned in `node-runtime.ts` rather than fetched. A checksum
 * fetched from nodejs.org vouches for a binary fetched from nodejs.org, so whoever can answer as
 * that origin supplies both halves and they agree; the header check does not close it either,
 * because a substituted runtime would be a genuine executable of the right architecture. So the
 * six digests are asserted here as literals, independently of the constant they are checked
 * against — if the two ever disagree, one of them is wrong and the suite says so.
 *
 * The second is worth a test because it fails so late. A sidecar called
 * `node-x86_64-pc-windows-msvc.exe` that holds a Linux ELF has the right name, the right size
 * and a checksum that matches whatever was fetched, and it fails only on somebody's Windows
 * machine, as a dashboard with no API behind it — a missing runtime and a silent `None`, the
 * same shape as the bug PR #48 fixed. So the headers here are hand-built rather than read off
 * this machine: the assertions are about mismatches, and a mismatch cannot be produced by the
 * platform running the suite.
 *
 * The third is worth a test because it was wrong: only fields 0, 2 and 3 were read, so
 * `x86_64-/../../pwn2-windows-msvc` parsed, and staged 93 MB outside the one directory this
 * script owns.
 *
 * No test here touches the network, and none may: the point of the pin is that the build does
 * not ask anybody what the right answer is, and neither does the suite.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { test } from "node:test";

import { executableSuffix } from "../tools/platform.ts";
import * as runtime from "../tools/node-runtime.ts";
import {
	assertBinaryMatches,
	assertHostRuntimeUsable,
	describeFormat,
	nodeArtifact,
	parseTarget,
	pinnedDigest,
	pinnedDigestFor,
	readBinaryFormat,
	sidecarPath,
	NODE_DIGESTS,
	NODE_VERSION,
} from "../tools/node-runtime.ts";
import { UI_ROOT } from "../server/locations.ts";

/**
 * Every triple the mapping has an opinion about, and what that opinion is.
 *
 * A table rather than a handful of calls, because the failure being guarded is a triple quietly
 * resolving to its *neighbour* — the nearest plausible artifact — and that only shows up when
 * the refused cases are enumerated beside the accepted ones. Expected values are literals; none
 * of them is computed from the function under test.
 */
const ACCEPTED: readonly (readonly [string, string, string])[] = [
	["x86_64-pc-windows-msvc", "win32", "x64"],
	["aarch64-pc-windows-msvc", "win32", "arm64"],
	["x86_64-apple-darwin", "darwin", "x64"],
	["aarch64-apple-darwin", "darwin", "arm64"],
	["x86_64-unknown-linux-gnu", "linux", "x64"],
	["aarch64-unknown-linux-gnu", "linux", "arm64"],
];

const REFUSED: readonly (readonly [string, RegExp])[] = [
	// musl is the dangerous one: the glibc build is the right architecture and the right size,
	// and dies at exec on the machine it was staged for.
	["x86_64-unknown-linux-musl", /glibc Linux builds, not "musl"/],
	["aarch64-unknown-linux-musl", /glibc Linux builds, not "musl"/],
	["x86_64-unknown-linux-gnux32", /glibc Linux builds, not "gnux32"/],
	["armv7-unknown-linux-gnueabihf", /architecture "armv7"/],
	["riscv64gc-unknown-linux-gnu", /architecture "riscv64gc"/],
	["i686-pc-windows-msvc", /architecture "i686"/],
	["i686-unknown-linux-gnu", /architecture "i686"/],
	["powerpc64le-unknown-linux-gnu", /architecture "powerpc64le"/],
	["s390x-unknown-linux-gnu", /architecture "s390x"/],
	["wasm32-unknown-unknown", /architecture "wasm32"/],
	["aarch64-apple-ios", /operating system "ios"/],
	["x86_64-apple-ios", /operating system "ios"/],
	["aarch64-linux-android", /operating system "android"/],
	["x86_64-unknown-freebsd", /operating system "freebsd"/],
	["x86_64-unknown-netbsd", /operating system "netbsd"/],
	["x86_64-unknown-illumos", /operating system "illumos"/],
	["aarch64-unknown-none", /operating system "none"/],
	// Shape: none of these is a target triple, and each of them used to get past `parseTarget`
	// or reach a field it never read.
	["x86_64-/../../pwn2-windows-msvc", /not a target triple/],
	["x86_64-pc-windows-msvc-", /not a target triple/],
	["x86_64-pc-windows-msvc-extra", /not a target triple/],
	["x86_64--windows-msvc", /not a target triple/],
	["-pc-windows-msvc", /not a target triple/],
	["x86_64-pc", /not a target triple/],
	["x86_64", /not a target triple/],
	["nonsense", /cannot stage a Node sidecar for "nonsense"/],
	["", /cannot stage a Node sidecar for ""/],
	["../../../etc/passwd", /not a target triple/],
	["x86_64-pc-windows-msvc/../../escape", /not a target triple/],
	["x86_64-pc-windows-msvc\u0000", /not a target triple/],
	["x86_64 pc windows msvc", /not a target triple/],
];

test("every triple nodejs.org publishes a build for resolves to exactly that build", () => {
	for (const [triple, platform, arch] of ACCEPTED) {
		assert.deepEqual(parseTarget(triple), { triple, platform, arch }, triple);
	}
	assert.equal(ACCEPTED.length, 6, "six is the whole matrix: three platforms, two architectures");
});

test("a triple with no official build is refused by name, never matched to its neighbour", () => {
	for (const [triple, because] of REFUSED) {
		assert.throws(() => parseTarget(triple), because, `expected ${JSON.stringify(triple)} refused`);
	}
});

test("a triple that is really a path is refused before it can become a filename", () => {
	// The one the review drove all the way through: `parseTarget` read fields 0, 2 and 3 only, so
	// this parsed as Windows/x64, and `resolve(BINARIES, \`node-${triple}.exe\`)` landed 93 MB in
	// `src-tauri/` — outside the gitignored `src-tauri/binaries/` — and the script exited 0.
	assert.throws(() => parseTarget("x86_64-/../../pwn2-windows-msvc"), /not a target triple/);

	// And the containment rule itself, stated where loosening the parser cannot quietly undo it.
	// Asserted against the real directory and by its parts rather than as one joined string: a
	// separator written into an expected value is a test that only proves what the platform
	// running it happens to spell, and this one is about a path escaping a directory on every
	// platform. `sidecarPath` is pure path arithmetic and touches no filesystem, so the directory
	// needs only to be named, not to exist.
	const binaries = resolve(UI_ROOT, "src-tauri/binaries");

	const staged = sidecarPath(binaries, "x86_64-pc-windows-msvc", ".exe");
	assert.equal(dirname(staged), binaries, "a real triple stages inside the binaries directory");
	assert.equal(basename(staged), "node-x86_64-pc-windows-msvc.exe");

	// These are the values that genuinely leave the directory: the `node-` filename prefix eats
	// one `..` on its own, which is exactly the sort of near-miss that makes "it looked fine when
	// I tried it" the wrong test.
	const escaped = failureOf(() => sidecarPath(binaries, "x86_64-/../../pwn2-windows-msvc", ".exe"));
	assert.match(escaped, /which is outside/);
	assert.ok(escaped.includes(binaries), "names the one directory this script owns");
	assert.ok(escaped.includes("pwn2-windows-msvc.exe"), "names the file it refused to write");
	assert.ok(escaped.includes("Nothing was written"));
	assert.throws(() => sidecarPath(binaries, "a/../../../etc", ""), /which is outside/);

	// A sibling directory whose name merely starts with `binaries` is outside it, and this is the
	// case that makes the separator in the containment check load-bearing: `binaries-evil/pwn.exe`
	// is a prefix match on `binaries` and a different directory entirely.
	const sibling = failureOf(() =>
		sidecarPath(binaries, `x/../../${basename(binaries)}-evil/pwn`, ".exe"),
	);
	assert.match(sibling, /which is outside/);
	assert.ok(sibling.includes(`${basename(binaries)}-evil`), "the sibling is named, not swallowed");

	// A path that stays inside is still not a triple, and `parseTarget` is what refuses it.
	const inside = sidecarPath(binaries, "../../escape", "");
	assert.equal(dirname(inside), binaries);
	assert.equal(basename(inside), "escape");
	assert.throws(() => parseTarget("../../escape"), /not a target triple/);
});

/**
 * The sha256 of every artifact `pnpm sidecar` can download, written out again independently of
 * the map the script checks against.
 *
 * These were confirmed against `https://nodejs.org/dist/v24.20.0/SHASUMS256.txt.asc` — the
 * *signed* file — whose signature verifies under 5BE8A3F6C8A5C01D106C0AD820B1A390B168D356, a
 * fingerprint listed in `keys.list` in github.com/nodejs/release-keys, and each artifact was
 * downloaded and hashed to the value below. Duplicating them here is the point: a version bump
 * that moves `NODE_DIGESTS` and forgets this table fails the suite instead of shipping.
 */
const PUBLISHED: readonly (readonly [string, string])[] = [
	["win32-x64", "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5"],
	["win32-arm64", "92949e7764e56e305cb84ea3d575912e822c79e85599362e8d408b04b9ffd326"],
	["darwin-x64", "9e5b2644cf107befb6aefca676b96d3296bc10138096f022ed378d6233ed81f4"],
	["darwin-arm64", "40e5607e5ecb3db9192723776da2d75d966260fc74a7a9e731c1bd67dda96bc8"],
	["linux-x64", "855d581f8a4eb1a8117e3426de25fe02770592febcfb31369aee1ffbfee9e8ec"],
	["linux-arm64", "3515603e2487879a39bc75716f1a2affd027500c64ba50e845cf72cb33219013"],
];

test("the digest every download is checked against is the one this repository pins", () => {
	assert.equal(NODE_VERSION, "24.20.0", "the digests below are for this release and no other");
	for (const [key, digest] of PUBLISHED) {
		assert.equal(NODE_DIGESTS.get(key), digest, key);
	}
	assert.equal(NODE_DIGESTS.size, 6, "every producible artifact is pinned, and nothing else is");
});

test("every target that parses has a pinned digest, so none can reach the network unchecked", () => {
	for (const [triple] of ACCEPTED) {
		assert.match(pinnedDigest(parseTarget(triple)), /^[0-9a-f]{64}$/, triple);
	}
});

test("a target with no pinned digest refuses, rather than falling back to fetching one", () => {
	// The whole value of pinning: "nobody pinned this" must never become "ask nodejs.org". The
	// digest table is total over SidecarPlatform × SidecarArch, so no well-typed target can reach
	// this today; the lookup is exposed by key so the refusal is reachable anyway. The key below
	// is the state this repository could be in one commit from now — a platform added to the
	// union, a triple mapped onto it, and nobody having pinned what its runtime should hash to.
	assert.throws(() => pinnedDigestFor("sunos-x64"), /no sha256 is pinned/);
	assert.throws(() => pinnedDigestFor("linux-riscv64"), /none was made/);
	assert.throws(() => pinnedDigestFor("win32-x64 "), /never fetched at build time on purpose/);
	assert.throws(() => pinnedDigestFor(""), /no sha256 is pinned/);
});

test("nothing reads a checksum off the network any more", () => {
	// The checksum and the binary used to come from the same origin, so whoever could answer as
	// nodejs.org served both halves and they agreed by construction. The helpers that did it are
	// gone; this fails if either comes back.
	assert.equal("checksumsUrl" in runtime, false, "checksumsUrl is gone and must stay gone");
	assert.equal("digestFor" in runtime, false, "digestFor is gone and must stay gone");
	const script = readFileSync(`${UI_ROOT}/tools/prepare-sidecar.ts`, "utf8");
	assert.equal(script.includes("SHASUMS"), false, "the script names no checksum file at all");
	assert.equal(
		script.includes("VENATOR_SIDECAR_NODE_VERSION"),
		false,
		"the version override is gone: it had no digest to be checked against, and it was " +
			"interpolated raw into both the download URL and the cache path",
	);
});

test("a Windows sidecar is the bare node.exe nodejs.org publishes, so nothing is extracted", () => {
	const artifact = nodeArtifact(parseTarget("x86_64-pc-windows-msvc"));
	assert.equal(artifact.url, "https://nodejs.org/dist/v24.20.0/win-x64/node.exe");
	assert.equal(artifact.checksumName, "win-x64/node.exe");
	assert.equal(artifact.member, null);
	assert.equal(artifact.cacheName, "node-v24.20.0-win-x64.exe");
	assert.equal(artifact.digest, "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5");
});

test("a macOS or Linux sidecar comes out of the release tarball", () => {
	const mac = nodeArtifact(parseTarget("aarch64-apple-darwin"));
	assert.equal(mac.url, "https://nodejs.org/dist/v24.20.0/node-v24.20.0-darwin-arm64.tar.gz");
	assert.equal(mac.member, "node-v24.20.0-darwin-arm64/bin/node");
	assert.equal(mac.digest, "40e5607e5ecb3db9192723776da2d75d966260fc74a7a9e731c1bd67dda96bc8");
	const linux = nodeArtifact(parseTarget("x86_64-unknown-linux-gnu"));
	assert.equal(linux.checksumName, "node-v24.20.0-linux-x64.tar.gz");
	assert.equal(linux.member, "node-v24.20.0-linux-x64/bin/node");
	assert.equal(linux.digest, "855d581f8a4eb1a8117e3426de25fe02770592febcfb31369aee1ffbfee9e8ec");
});

test("a cache name is a flat filename, never a path", () => {
	// `$VENATOR_SIDECAR_NODE_VERSION` went straight into this string, so a value of
	// `1/../../dist/v24.20.0` wrote its cache entry outside the cache directory — and normalised
	// to a real nodejs.org URL on the way, so the download itself looked ordinary.
	for (const [triple] of ACCEPTED) {
		const { cacheName } = nodeArtifact(parseTarget(triple));
		assert.equal(cacheName.includes("/"), false, cacheName);
		assert.equal(cacheName.includes("\\"), false, cacheName);
		assert.equal(cacheName.includes(".."), false, cacheName);
		assert.equal(cacheName.startsWith("node-v24.20.0-"), true, cacheName);
	}
});

test("the suffix follows the target, not the machine staging it", () => {
	// This is the whole of the cross-staging naming rule: on this host `executableSuffix()`
	// with no argument is "", and a Windows sidecar still has to be `.exe`.
	assert.equal(executableSuffix(parseTarget("x86_64-pc-windows-msvc").platform), ".exe");
	assert.equal(executableSuffix(parseTarget("aarch64-pc-windows-msvc").platform), ".exe");
	assert.equal(executableSuffix(parseTarget("aarch64-apple-darwin").platform), "");
	assert.equal(executableSuffix(parseTarget("x86_64-unknown-linux-gnu").platform), "");
});

test("the pinned Node major is the one the repo targets everywhere else", () => {
	assert.match(NODE_VERSION, /^24\./);
});

test("the host path refuses a Node that is not the major the API is built for", () => {
	// The cross path stages `NODE_VERSION`; the host path used to stage whatever Node was running
	// the script, unremarked. A developer on Node 22 got a bundle whose API had been emitted by
	// vite for `node24` and whose `server/db.ts` opens the view with `node:sqlite`.
	assert.throws(
		() => assertHostRuntimeUsable("22.22.2", "24.20.0"),
		/this machine's Node is 22\.22\.2, and the sidecar has to be a Node 24 build/,
	);
	assert.throws(() => assertHostRuntimeUsable("20.11.0", "24.20.0"), /Node 20\.11\.0/);
	assert.throws(() => assertHostRuntimeUsable("26.0.0", "24.20.0"), /has to be a Node 24 build/);
});

test("the host path accepts any patch of the right major, and says so with both versions", () => {
	assertHostRuntimeUsable("24.20.0", "24.20.0");
	assertHostRuntimeUsable("24.0.0", "24.20.0");
	assertHostRuntimeUsable("24.99.1", "24.20.0");
	// The default second argument is the pin, so the check the script actually runs is this one.
	assertHostRuntimeUsable(NODE_VERSION);
});

test("the version refusal is written for somebody whose toolchain has worked for months", () => {
	// This message is the entire user interface of a failure that will happen to a real person,
	// and their first reading will be that the build script is broken rather than their setup.
	const message = failureOf(() => assertHostRuntimeUsable("22.22.2", "24.20.0"));
	assert.match(message, /22\.22\.2/, "names what was found");
	assert.match(message, /Node 24/, "names what was expected");
	assert.match(message, /Why this matters/, "says why a working setup is suddenly refused");
	assert.match(message, /target: "node24"/, "names the concrete reason");
	assert.match(message, /node:sqlite/, "and the other one");
	assert.match(message, /Nothing is wrong with this checkout/, "does not blame the reader");
	assert.match(message, /fnm use 24/, "says exactly what to do");
	assert.match(message, /nvm use 24/, "for the other version manager too");
	assert.match(message, /Nothing was written/, "says what the build did not do");
});

/** The message a refusal was built with, so a test can assert on the prose a person reads. */
function failureOf(act: () => void): string {
	try {
		act();
	} catch (caught) {
		return caught instanceof Error ? caught.message : String(caught);
	}
	throw new assert.AssertionError({ message: "expected a refusal, and nothing was thrown" });
}

/** An ELF header: 64-bit, little-endian, `machine` at 0x12. */
function elf(machine: number): Uint8Array {
	const bytes = new Uint8Array(64);
	bytes.set([0x7f, 0x45, 0x4c, 0x46, 2, 1], 0);
	new DataView(bytes.buffer).setUint16(0x12, machine, true);
	return bytes;
}

/** A PE header: "MZ", the COFF offset at 0x3c, then "PE\0\0" and the machine word. */
function pe(machine: number): Uint8Array {
	const bytes = new Uint8Array(256);
	bytes.set([0x4d, 0x5a], 0);
	const view = new DataView(bytes.buffer);
	view.setUint32(0x3c, 0x80, true);
	bytes.set([0x50, 0x45, 0, 0], 0x80);
	view.setUint16(0x84, machine, true);
	return bytes;
}

/** A thin 64-bit Mach-O header: little-endian magic, then the cpu type. */
function machO(cpu: number): Uint8Array {
	const bytes = new Uint8Array(32);
	const view = new DataView(bytes.buffer);
	view.setUint32(0, 0xfeed_facf, true);
	view.setUint32(4, cpu, true);
	return bytes;
}

/** A universal Mach-O header: a big-endian count of 20-byte entries, each a cpu type first. */
function universal(cpus: readonly number[]): Uint8Array {
	const bytes = new Uint8Array(8 + cpus.length * 20);
	const view = new DataView(bytes.buffer);
	view.setUint32(0, 0xcafe_babe, false);
	view.setUint32(4, cpus.length, false);
	cpus.forEach((cpu, index) => view.setUint32(8 + index * 20, cpu, false));
	return bytes;
}

test("a binary's own header says what it is, whatever it was named", () => {
	assert.deepEqual(readBinaryFormat(elf(0x3e)), { platform: "linux", arches: ["x64"] });
	assert.deepEqual(readBinaryFormat(elf(0xb7)), { platform: "linux", arches: ["arm64"] });
	assert.deepEqual(readBinaryFormat(pe(0x8664)), { platform: "win32", arches: ["x64"] });
	assert.deepEqual(readBinaryFormat(pe(0xaa64)), { platform: "win32", arches: ["arm64"] });
	assert.deepEqual(readBinaryFormat(machO(0x0100_000c)), { platform: "darwin", arches: ["arm64"] });
	assert.deepEqual(readBinaryFormat(universal([0x0100_0007, 0x0100_000c])), {
		platform: "darwin",
		arches: ["x64", "arm64"],
	});
});

test("this machine's own Node is a binary the header reader recognises", () => {
	// The synthesised headers above prove the parsing; this proves the parsing is of the thing
	// actually staged on the host path, which is a copy of exactly this file.
	const format = readBinaryFormat(readFileSync(process.execPath).subarray(0, 4096));
	assert.ok(format.arches.length > 0, `${describeFormat(format)} named no architecture`);
});

test("a runtime for the wrong platform is refused under a correct-looking name", () => {
	// The exact artifact this whole path exists to prevent: staged from Linux, named for
	// Windows, and an ELF inside.
	assert.throws(
		() => assertBinaryMatches(elf(0x3e), parseTarget("x86_64-pc-windows-msvc"), "download"),
		/downloaded for x86_64-pc-windows-msvc is a Linux ELF executable for x64/,
	);
	assert.throws(
		() => assertBinaryMatches(pe(0x8664), parseTarget("x86_64-apple-darwin"), "download"),
		/is a Windows PE executable/,
	);
});

test("a runtime for the wrong architecture is refused too, platform notwithstanding", () => {
	assert.throws(
		() => assertBinaryMatches(pe(0xaa64), parseTarget("x86_64-pc-windows-msvc"), "download"),
		/for arm64, which will not run on that target/,
	);
	assert.throws(
		() => assertBinaryMatches(machO(0x0100_0007), parseTarget("aarch64-apple-darwin"), "host"),
		/for x64/,
	);
});

test("the architecture refusal on a Mac reads as a toolchain mismatch, not a bundler failure", () => {
	// An x64 Node under Rosetta on an Apple Silicon Mac is the one machine shape where the host
	// header check can genuinely fire, and the person it fires at has been building this project
	// happily for months. If they read it as Tauri being broken they file the wrong bug and lose
	// an afternoon.
	const message = failureOf(() =>
		assertBinaryMatches(machO(0x0100_0007), parseTarget("aarch64-apple-darwin"), "host"),
	);
	assert.match(message, /this machine's own Node is a macOS Mach-O executable for x64/);
	assert.match(message, /needs arm64/, "names what was expected");
	assert.match(message, /not a failure of Tauri, of the bundler/, "rules out the wrong bug");
	assert.match(message, /Rosetta/, "names the actual cause on the machine this happens on");
	assert.match(message, /process\.arch/, "gives them something to check");
	assert.match(message, /--arch arm64/, "says exactly what to do");
	assert.match(message, /arm64 installer/, "and names the option for somebody not using fnm");
	assert.match(message, /Nothing was written/);
	// A download mismatch is a different situation and does not borrow this explanation.
	const downloaded = failureOf(() =>
		assertBinaryMatches(machO(0x0100_0007), parseTarget("aarch64-apple-darwin"), "download"),
	);
	assert.equal(downloaded.includes("Rosetta"), false);
	assert.match(downloaded, /pinned/, "a bad download is about the pin, not about this machine");
});

test("a matching runtime passes, including a universal binary that holds the architecture", () => {
	assert.deepEqual(assertBinaryMatches(pe(0x8664), parseTarget("x86_64-pc-windows-msvc"), "download"), {
		platform: "win32",
		arches: ["x64"],
	});
	assertBinaryMatches(elf(0xb7), parseTarget("aarch64-unknown-linux-gnu"), "download");
	assertBinaryMatches(universal([0x0100_0007, 0x0100_000c]), parseTarget("aarch64-apple-darwin"), "host");
});

test("something that is not an executable at all is refused rather than staged", () => {
	// An error page or a redirect written to disk is a plausible download outcome and would
	// otherwise be bundled as the runtime.
	assert.throws(
		() => readBinaryFormat(new TextEncoder().encode("<!doctype html><title>404</title>")),
		/not an ELF, Mach-O or PE executable/,
	);
	assert.throws(() => readBinaryFormat(new Uint8Array([0x4d, 0x5a])), /too short/);
});

test("the desktop bundle names a Windows installer target and keeps the macOS ones", () => {
	// `cargo tauri build` on Windows without this produces a loose .exe and no installer —
	// nothing anyone can be handed. nsis is Tauri's own default Windows installer; msi needs the
	// WiX toolset. Neither is toolchain-free: Tauri's bundler downloads NSIS and
	// `nsis_tauri_utils.dll` from GitHub the first time it bundles one. CI does run the bundler
	// — `windows — the installer a person installs` on `windows-latest`, on pushes to `main` and
	// on `workflow_dispatch` but not on a pull request — so a `tauri build` with `nsis` has
	// completed. This test still asserts the configuration alone, not that any bundle was
	// produced.
	const config = readFileSync(`${UI_ROOT}/src-tauri/tauri.conf.json`, "utf8");
	const targets = JSON.parse(config).bundle.targets;
	assert.deepEqual(targets, ["app", "dmg", "nsis"]);
});
