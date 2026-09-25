/**
 * Which runtime a desktop bundle actually carries, and whether anything checked it.
 *
 * The route used to be chosen by whether the requested triple happened to equal the Rust host
 * triple: `crossTarget = isHost ? null : parseTarget(triple)`, and a null target meant "copy
 * `process.execPath`". So `--target x86_64-pc-windows-msvc` on a Windows host was a no-op, and
 * verification happened only when the build machine's platform differed from the target's —
 * which is true of no bundle this repository will ship. A `.dmg` is built on the Owner's Mac for
 * the Owner's Mac; a Windows installer is built on the `windows-latest` runner for Windows. Both
 * copied the build machine's own Node, unverified as to provenance, and the six digests pinned
 * in `node-runtime.ts` were dead code for every bundle anybody would ever install.
 *
 * The two guards that did run on that route establish that the major is 24 and that the header
 * is the right platform and architecture. Neither says the bytes came from nodejs.org.
 *
 * So the route follows what the caller asked for, and this file is about that decision and the
 * checks behind it. The steps are reachable because they live in `sidecar-staging.ts` rather
 * than in `prepare-sidecar.ts`, which ends in a top-level `await main()` and runs on import —
 * the same reason `python-staging.ts` exists, where eleven mutations of unimportable checks
 * survived a green suite eleven times.
 *
 * How the bytes are fetched is injected everywhere below. **No test here touches the network,
 * and none may**: the whole point of a pinned digest is that the build does not ask anybody what
 * the right answer is, and neither does the suite. The script itself is asserted by its source
 * rather than spawned, because spawning it either downloads 53 MB or stages 124 MB of Node into
 * the checkout, and every step it calls is exercised directly here.
 */

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { gzipSync } from "node:zlib";

import { UI_ROOT } from "../server/locations.ts";
import {
	NODE_VERSION,
	VERIFIED_FLAG,
	nodeArtifact,
	parseTarget,
	pinnedDigest,
	type NodeArtifact,
} from "../tools/node-runtime.ts";
import type { Reporter } from "../tools/python-staging.ts";
import {
	assertRouteStageable,
	describeRoute,
	nameTarget,
	sidecarRoute,
	stageSidecar,
	stagedNodeVersion,
	verifiedDownload,
} from "../tools/sidecar-staging.ts";

/** A directory of this test's own. Everything staged below is written into one. */
function scratch(): string {
	return mkdtempSync(join(tmpdir(), "venator-sidecar-"));
}

/** What a step reported, so a test can assert on what it said as well as on what it did. */
type Recorder = {
	readonly lines: readonly string[];
	readonly say: Reporter;
};

function recorder(): Recorder {
	const lines: string[] = [];
	return { lines, say: (label, detail) => lines.push(`${label}: ${detail}`) };
}

/** The message of the refusal `run` throws, so a test can assert on several parts of it. */
async function failureOf<T>(run: () => T | Promise<T>): Promise<string> {
	try {
		await run();
	} catch (caught) {
		return caught instanceof Error ? caught.message : String(caught);
	}
	assert.fail("expected a refusal, and nothing was thrown");
}

/** Every triple an official Node build is named by. The whole matrix, and nothing else. */
const TRIPLES: readonly string[] = [
	"x86_64-pc-windows-msvc",
	"aarch64-pc-windows-msvc",
	"x86_64-apple-darwin",
	"aarch64-apple-darwin",
	"x86_64-unknown-linux-gnu",
	"aarch64-unknown-linux-gnu",
];

/**
 * The triple of the machine running this suite.
 *
 * Derived rather than written down, because the case the defect lived in is "the target equals
 * the host" and a hard-coded triple would only be that case on one platform. On the Owner's Mac
 * this is `aarch64-apple-darwin`, on CI's Linux runner `x86_64-unknown-linux-gnu`, and the
 * assertions below are the same either way.
 */
function thisMachine(): string {
	const triple = TRIPLES.find((candidate) => {
		const target = parseTarget(candidate);
		return target.platform === process.platform && target.arch === process.arch;
	});
	if (triple === undefined) {
		assert.fail(`no official Node build names ${process.platform}-${process.arch}`);
	}
	return triple;
}

/**
 * A triple that is *not* this machine's, derived for the same reason as the one above.
 *
 * Writing a literal here is how the first version of this file went red on the Windows runner:
 * `x86_64-pc-windows-msvc` is a cross target on the Linux runner and the Owner's Mac, and is
 * the host on `windows-latest`. A test that means "not this machine" has to say so.
 */
function anotherMachine(host: string): string {
	const triple = TRIPLES.find((candidate) => candidate !== host);
	if (triple === undefined) assert.fail("the matrix holds six triples, so one of them is not this machine's");
	return triple;
}

test("a bundle's runtime is downloaded and checked even when the target is this machine's own", async () => {
	// The whole defect, as one case: the target equals the host, and the runtime is still fetched
	// from nodejs.org and still checked against the digest this repository pins. Before, this
	// combination copied `process.execPath` and checked nothing about where it came from.
	const host = thisMachine();
	const route = sidecarRoute("", host, true);
	assert.equal(route.isHost, true, "this is the case that used to skip the check");
	assert.equal(route.origin, "download");
	assert.equal(route.target?.triple, host, "and it is staged for the target it is named for");

	const directory = scratch();
	try {
		const sidecar = join(directory, "node");
		const asked: string[] = [];
		const failure = await failureOf(async () =>
			stageSidecar(route, sidecar, join(directory, "cache"), () => {}, async (url) => {
				asked.push(url);
				return Promise.resolve(new TextEncoder().encode("not what nodejs.org published"));
			}),
		);

		// Refused on the digest, before the bytes could become a sidecar — and refused against the
		// value pinned for *this machine's* target, which is one of the two the pins were dead
		// code for.
		assert.match(failure, /checksum mismatch/u);
		assert.ok(failure.includes(pinnedDigest(parseTarget(host))), "against the pin for this target");
		assert.match(failure, /Nothing was staged/u);
		assert.deepEqual(asked, [nodeArtifact(parseTarget(host)).url], "one fetch, from nodejs.org");
		assert.equal(existsSync(sidecar), false, "and no runtime was left where a build could bundle it");
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

test("a verification that fails is a refusal, and never a quiet fall back to copying", async () => {
	// The tempting repair, and the one that would undo the whole change: catch the download and
	// copy this machine's Node instead. That turns a verified bundle into an unverified one at
	// exactly the moment verification failed, which is the one moment it was for.
	const host = thisMachine();
	const directory = scratch();
	try {
		const sidecar = join(directory, "node");
		const said = recorder();
		const failure = await failureOf(async () =>
			stageSidecar(sidecarRoute("", host, true), sidecar, join(directory, "cache"), said.say, () => {
				throw new Error("nodejs.org is unreachable from this build host");
			}),
		);
		assert.match(failure, /unreachable/u, "the reason it could not be verified is what comes out");
		assert.equal(existsSync(sidecar), false, "and nothing at all was staged");
		assert.equal(
			said.lines.some((line) => line.startsWith("unverified: ")),
			false,
			"this machine has a Node a fallback could have copied, and none of it was copied",
		);
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

test("the dev loop still copies this machine's own Node, and asks nobody for it", async () => {
	// `tauri dev` runs the API on the Node the developer is already running, so forcing a 53 MB
	// download into a dev loop is a tax for nothing. This is the one route that can still produce
	// an unchecked runtime, and it is reachable only from `beforeDevCommand` and a bare `pnpm
	// sidecar` — so it says out loud what it did.
	const host = thisMachine();
	const route = sidecarRoute("", host, false);
	assert.equal(route.origin, "host");
	assert.equal(route.target, null, "nothing is downloaded, so there is nothing to check it against");

	const directory = scratch();
	try {
		const sidecar = join(directory, "node");
		const cache = join(directory, "cache");
		const said = recorder();
		await stageSidecar(route, sidecar, cache, said.say, () => {
			assert.fail("the copy route must not fetch anything");
		});

		assert.equal(statSync(sidecar).size, statSync(process.execPath).size, "this machine's own Node");
		assert.equal(existsSync(cache), false, "and no download cache was ever opened");
		assert.ok(
			said.lines.some((line) => line.startsWith("unverified: ")),
			`the copied runtime is announced as unverified, in ${JSON.stringify(said.lines)}`,
		);
		assert.ok(said.lines.some((line) => line.startsWith("header ok: ")), "its header is still read back");
		assert.ok(
			said.lines.every((line) => !line.startsWith("checksum ok: ")),
			"and nothing claims a checksum was compared",
		);
		// And the line names the version that was actually staged rather than the pinned one. On a
		// build host already running the pin these are the same string and this says nothing; the
		// unit test of `stagedNodeVersion` is what tells them apart on every machine.
		assert.ok(
			said.lines.some((line) => line.startsWith("node runtime: ") && line.endsWith(`Node ${process.versions.node})`)),
			`this machine's own version, not the pin, in ${JSON.stringify(said.lines)}`,
		);
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

test("a target that is not this machine's is downloaded whether or not a bundle asked", async () => {
	// The route the script could already take, unchanged: a cross target has no local Node to
	// copy, so `--verified` is not what puts it on the download route and dropping the flag does
	// not take it off.
	//
	// Every triple is tried as the build host rather than only this machine's. The decision is a
	// function of two triples and a flag and does not touch the filesystem, so a suite that only
	// ever asks about its own platform is checking one column of it — which is how this file
	// first went red on `windows-latest`, where the triple written here as "a cross target" is
	// the host.
	for (const host of TRIPLES) {
		const other = anotherMachine(host);
		for (const verified of [true, false]) {
			const route = sidecarRoute(other, host, verified);
			assert.equal(route.origin, "download", `${other} from ${host} with verified=${verified}`);
			assert.equal(route.isHost, false);
			assert.equal(route.target?.triple, other);
		}
		// And the host's own triple is on the download route when a bundle asked, on the copy
		// route when nothing did — on every one of the six, not only on the platform running this.
		// All four cells, because three of them left `isHost` decided by something other than the
		// triple: `requested === ""` in place of `triple === host` passes the other three and
		// puts the host's own triple, named explicitly with no flag, on the download route.
		assert.equal(sidecarRoute("", host, true).origin, "download", host);
		assert.equal(sidecarRoute(host, host, true).origin, "download", `${host}, named explicitly`);
		assert.equal(sidecarRoute("", host, false).origin, "host", host);
		assert.equal(sidecarRoute(host, host, false).origin, "host", `${host}, named explicitly`);
		assert.equal(sidecarRoute(host, host, false).isHost, true, `${host} is this machine's own`);
		assert.equal(sidecarRoute(host, host, false).target, null, "so there is nothing to download");
	}

	// And the log says which of the two a build got, rather than leaving it to be inferred from
	// which lines happen to be absent.
	const host = thisMachine();
	assert.match(describeRoute(sidecarRoute("", host, true)), /downloaded and checksummed/u);
	assert.ok(describeRoute(sidecarRoute("", host, true)).includes(NODE_VERSION), "naming the pinned version");
	assert.match(describeRoute(sidecarRoute("", host, false)), /copied unverified/u);
});

test("a triple that is not one is refused before anything is fetched or written", () => {
	// `sidecarRoute` is where a garbage or path-shaped `--target` stops, on either route, because
	// the triple becomes part of a filename and part of a URL.
	const host = thisMachine();
	for (const wanted of ["../../../etc/passwd", "x86_64-/../../pwn2-windows-msvc", "nonsense", "x86_64-pc"]) {
		assert.throws(() => sidecarRoute(wanted, host, true), /cannot stage a Node sidecar/u, wanted);
		assert.throws(() => sidecarRoute(wanted, host, false), /cannot stage a Node sidecar/u, wanted);
	}
	// A triple nodejs.org publishes no build for is refused on the verified route by name.
	assert.throws(() => sidecarRoute("x86_64-unknown-linux-musl", host, true), /glibc Linux builds/u);
});

test("a host nodejs.org names no build for copies, and says its header was not checked", async () => {
	// A musl Linux, say: there is no artifact to download and nothing to check the header
	// against. The copy route is all that is left, and the missing check is stated rather than
	// left as a silence in the log.
	const musl = "x86_64-unknown-linux-musl";
	assert.equal(nameTarget(musl), null, "no official build names it");
	assert.equal(nameTarget("x86_64-pc-windows-msvc")?.platform, "win32", "and one that does, resolves");

	const route = sidecarRoute("", musl, false);
	assert.equal(route.origin, "host");
	const directory = scratch();
	try {
		const said = recorder();
		await stageSidecar(route, join(directory, "node"), join(directory, "cache"), said.say, () => {
			assert.fail("there is nothing to fetch for a target with no official build");
		});
		assert.ok(said.lines.some((line) => line.startsWith("header skipped: ")), "the skipped check is named");
		assert.ok(said.lines.some((line) => line.includes(musl)), "and so is the triple it was skipped for");
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

/** An artifact whose digest a test controls, so the comparison can be exercised both ways. */
function artifactFor(bytes: Uint8Array): NodeArtifact {
	return {
		url: "https://nodejs.org/dist/v24.20.0/win-x64/node.exe",
		checksumName: "win-x64/node.exe",
		cacheName: "node-v24.20.0-win-x64.exe",
		digest: createHash("sha256").update(bytes).digest("hex"),
		member: null,
	};
}

test("the digest is compared on every hit, cached or fresh, and a mismatch leaves nothing", async () => {
	// The comparison the pin exists for. Deleted, everything downstream still works: the bytes
	// are written, renamed into the cache under their real name, and staged as the runtime.
	const published = new TextEncoder().encode("the bytes nodejs.org published");
	const artifact = artifactFor(published);
	const directory = scratch();
	try {
		const cache = join(directory, "cache");

		const fresh = recorder();
		const path = await verifiedDownload(cache, artifact, fresh.say, async () => Promise.resolve(published));
		assert.deepEqual(new Uint8Array(readFileSync(path)), published);
		assert.ok(fresh.lines.some((line) => line.startsWith("checksum ok: sha256")));

		// A hit is hashed again rather than trusted for its name.
		const hit = recorder();
		const again = await verifiedDownload(cache, artifact, hit.say, () => {
			assert.fail("a cache hit that matches its pin must not fetch anything");
		});
		assert.equal(again, path);
		assert.ok(hit.lines.some((line) => line.startsWith("checksum ok: cached")));

		// A cache entry that no longer matches is rejected and refetched, not reused.
		writeFileSync(path, "tampered with after it was cached");
		const stale = recorder();
		await verifiedDownload(cache, artifact, stale.say, async () => Promise.resolve(published));
		assert.ok(stale.lines.some((line) => line.startsWith("cache rejected: ")));
		assert.deepEqual(new Uint8Array(readFileSync(path)), published, "and the pinned bytes are what is left");

		// And bytes that are not the pinned ones reach nothing: not the cache under their real
		// name, and not a `.part` file a later run could mistake for a hit.
		const empty = join(directory, "empty-cache");
		const failure = await failureOf(async () =>
			verifiedDownload(empty, artifact, () => {}, async () =>
				Promise.resolve(new TextEncoder().encode("substituted")),
			),
		);
		assert.match(failure, /checksum mismatch for win-x64\/node\.exe/u);
		assert.ok(failure.includes(artifact.digest), "names what this repository pins");
		assert.match(failure, /NODE_DIGESTS is stale/u, "and what a deliberate change would look like");
		assert.deepEqual(readdirSync(empty), [], "nothing a later run could pick up");
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

/** An ELF header: 64-bit, little-endian, `machine` at 0x12. What a Linux Node starts with. */
function elf(machine: number): Uint8Array {
	const bytes = new Uint8Array(64);
	bytes.set([0x7f, 0x45, 0x4c, 0x46, 2, 1], 0);
	new DataView(bytes.buffer).setUint16(0x12, machine, true);
	return bytes;
}

/** A PE header: "MZ", the COFF offset at 0x3c, then "PE\0\0" and the machine word. */
function pe(machine: number): Uint8Array {
	const bytes = new Uint8Array(0x100);
	bytes.set([0x4d, 0x5a], 0);
	const view = new DataView(bytes.buffer);
	view.setUint32(0x3c, 0x80, true);
	view.setUint32(0x80, 0x5045_0000, false);
	view.setUint16(0x84, machine, true);
	return bytes;
}

/**
 * A gzipped tar carrying one member, assembled field by field rather than by asking `tar` for
 * one — the same rule the zip suite follows, and for the same reason: the archive has to be
 * exactly what nodejs.org's shape is, on a Windows runner whose `tar` is bsdtar and a Linux one
 * whose `tar` is GNU, and "whatever the local tool wrote" is two different archives.
 */
function tarball(member: string, contents: Uint8Array): Uint8Array {
	const header = Buffer.alloc(512);
	header.write(member, 0, "ascii");
	header.write("0000755\0", 100, "ascii");
	header.write("0000000\0", 108, "ascii");
	header.write("0000000\0", 116, "ascii");
	header.write(`${contents.length.toString(8).padStart(11, "0")}\0`, 124, "ascii");
	header.write("00000000000\0", 136, "ascii");
	// The checksum is summed with its own field read as eight spaces, which is the one part of
	// the format that is not simply a number written down.
	header.write("        ", 148, "ascii");
	header.write("0", 156, "ascii");
	header.write("ustar\0", 257, "ascii");
	header.write("00", 263, "ascii");
	let sum = 0;
	for (let index = 0; index < header.length; index += 1) sum += header[index] ?? 0;
	header.write(`${sum.toString(8).padStart(6, "0")}\0 `, 148, "ascii");

	const padding = Buffer.alloc((512 - (contents.length % 512)) % 512);
	// Two zero blocks end a tar; without them `tar` reports a truncated archive.
	const blocks = [header, Buffer.from(contents), padding, Buffer.alloc(1024)];
	return new Uint8Array(gzipSync(Buffer.concat(blocks)));
}

/** `artifactFor`, for a target whose runtime nodejs.org publishes inside a tarball. */
function tarballArtifactFor(bytes: Uint8Array, member: string): NodeArtifact {
	return {
		url: `https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz`,
		checksumName: `node-v${NODE_VERSION}-linux-x64.tar.gz`,
		cacheName: `node-v${NODE_VERSION}-linux-x64.tar.gz`,
		digest: createHash("sha256").update(bytes).digest("hex"),
		member,
	};
}

test("what a verified download becomes is read back, and is the runtime rather than the archive", async () => {
	// Everything past the digest comparison. It was unreachable until `stageSidecar` took the
	// artifact as an argument: it read `nodeArtifact(route.target)` itself, so a test could inject
	// the bytes but never the pin they are checked against, and every test stopped at the
	// mismatch. Three checks lived past that point with no coverage at all — the extraction, the
	// header read-back, and the removal of a sidecar that fails it.
	const host = thisMachine();
	const directory = scratch();
	try {
		// Windows publishes the runtime bare, so the download *is* the sidecar.
		const bare = pe(0x8664);
		const windows = join(directory, "node.exe");
		const said = recorder();
		await stageSidecar(
			sidecarRoute("x86_64-pc-windows-msvc", host, true),
			windows,
			join(directory, "cache"),
			said.say,
			async () => Promise.resolve(bare),
			artifactFor(bare),
		);
		assert.deepEqual(new Uint8Array(readFileSync(windows)), bare, "the downloaded bytes, staged");
		assert.ok(
			said.lines.some((line) => line === "header ok: a Windows PE executable for x64, matching x86_64-pc-windows-msvc"),
			`the header is read back on the download route too, in ${JSON.stringify(said.lines)}`,
		);
		assert.ok(
			said.lines.some((line) => line.startsWith("node runtime: ") && line.includes(NODE_VERSION)),
			"and the line names the pinned version, which is what was staged",
		);

		// Every other platform publishes it inside a tarball, and the member is what has to land —
		// not the archive under the sidecar's name, which would pass its digest and be unrunnable.
		const member = `node-v${NODE_VERSION}-linux-x64/bin/node`;
		const runtime = elf(0x3e);
		const archive = tarball(member, runtime);
		const linux = join(directory, "node");
		const extracted = recorder();
		await stageSidecar(
			sidecarRoute("x86_64-unknown-linux-gnu", host, true),
			linux,
			join(directory, "cache"),
			extracted.say,
			async () => Promise.resolve(archive),
			tarballArtifactFor(archive, member),
		);
		assert.deepEqual(new Uint8Array(readFileSync(linux)), runtime, "the member, not the tarball");
		assert.ok(
			extracted.lines.some((line) => line.startsWith("header ok: a Linux ELF executable for x64")),
			`and it is read back as what it is, in ${JSON.stringify(extracted.lines)}`,
		);
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

test("a staged runtime that fails its header check is deleted, not left for the next build", async () => {
	// The digest says the bytes are the ones pinned; it does not say the target they are filed
	// under is the target they run on. If the pin and the triple ever drift apart, what is on
	// disk at this point is a runnable binary of the wrong shape under the right name — and a
	// build that merely threw would leave it there for the next one to find, stage nothing, and
	// bundle it.
	const host = thisMachine();
	const directory = scratch();
	try {
		const sidecar = join(directory, "node.exe");
		const wrong = elf(0x3e);
		const said = recorder();
		const failure = await failureOf(async () =>
			stageSidecar(
				sidecarRoute("x86_64-pc-windows-msvc", host, true),
				sidecar,
				join(directory, "cache"),
				said.say,
				async () => Promise.resolve(wrong),
				artifactFor(wrong),
			),
		);
		assert.match(failure, /is a Linux ELF executable for x64, which will not run on that target/u);
		assert.match(failure, /NODE_DIGESTS against the signed SHASUMS256\.txt/u, "and what to check");
		assert.equal(existsSync(sidecar), false, "and nothing is left where the next build would bundle it");
		assert.ok(
			said.lines.every((line) => !line.startsWith("header ok: ")),
			"nothing claims the header matched",
		);
		assert.ok(
			said.lines.some((line) => line.startsWith("checksum ok: ")),
			"and the digest did pass, which is the whole point of the case",
		);
	} finally {
		rmSync(directory, { recursive: true, force: true });
	}
});

test("the version a staged runtime is announced as is the route's, not always the pinned one", () => {
	// Both answers are the same string on a build host running the pinned version, which is the
	// machine most likely to run this — so the host version is an argument rather than something
	// read inside, and this is where the two are told apart.
	assert.equal(stagedNodeVersion("download", "22.22.2"), NODE_VERSION, "the pin is what was staged");
	assert.equal(stagedNodeVersion("host", "22.22.2"), "22.22.2", "and the copy route staged this machine's");
	assert.notEqual(NODE_VERSION, "22.22.2", "which are different strings, or this test says nothing");
});

test("the Node-major check guards the copy route, and never the verified one", () => {
	// It is there because the copy route stages whatever Node is running the build and the API
	// beside it is emitted for `node24`. The verified route stages an exact pinned version, so
	// asking it about this machine's Node would refuse a perfectly good bundle over the Node that
	// happened to launch the build.
	assert.throws(
		() => assertRouteStageable(sidecarRoute("", thisMachine(), false), "22.22.2"),
		/this machine's Node is 22\.22\.2, and the sidecar has to be a Node 24 build/u,
	);
	// Held for every build host, for the reason given above: the answer depends on the route and
	// not on which platform the suite happens to be running on.
	for (const host of TRIPLES) {
		assert.throws(() => assertRouteStageable(sidecarRoute("", host, false), "22.22.2"), /Node 24 build/u, host);
		assertRouteStageable(sidecarRoute("", host, true), "22.22.2");
		assertRouteStageable(sidecarRoute("", host, false), NODE_VERSION);
		// A cross target is on the download route with or without the flag, so this machine's Node
		// is not what is being staged and is not asked about either.
		assertRouteStageable(sidecarRoute(anotherMachine(host), host, false), "22.22.2");
	}
});

test("the script asks for the route the caller named, and `tauri dev` still gets the copy", () => {
	// Asserted by source: spawning the real script either downloads 53 MB or stages 124 MB into
	// this checkout, and every step it calls is exercised above. What is left to go wrong here is
	// the wiring — a flag parsed and then not passed is exactly the shape of the defect this
	// change fixes.
	const script = readFileSync(resolve(UI_ROOT, "tools/prepare-sidecar.ts"), "utf8");
	assert.match(script, /verified: \{ type: "boolean" \}/u, "the flag is declared, so parseArgs accepts it");
	assert.match(
		script,
		/sidecarRoute\(requested, host, values\.verified === true\)/u,
		"and what was parsed is what decides the route",
	);
	assert.match(script, /assertRouteStageable\(route, process\.versions\.node\)/u);
	assert.equal(
		script.includes("parseTarget"),
		false,
		"and the script makes no route decision of its own: `isHost ? null : parseTarget(triple)` " +
			"is the line this change removed, and it belongs to nothing that reads a flag",
	);

	// `beforeDevCommand` stays on the copy route: a dev loop is running this machine's Node
	// already, and 53 MB per `tauri dev` buys nothing.
	const config = JSON.parse(readFileSync(resolve(UI_ROOT, "src-tauri/tauri.conf.json"), "utf8"));
	assert.equal(config.build.beforeDevCommand, "pnpm sidecar && pnpm dev");
	assert.equal(config.build.beforeDevCommand.includes(VERIFIED_FLAG), false);
	assert.equal(config.build.beforeBuildCommand, "pnpm desktop:stage && pnpm build:desktop");

	// And the packaging gate names what it staged at its own top level, rather than leaving it to
	// a line that scrolls past inside a subprocess.
	const gate = readFileSync(resolve(UI_ROOT, "tools/desktop-stage.ts"), "utf8");
	assert.match(
		gate,
		/say\(\s*"sidecar",\s*`Node \$\{NODE_VERSION\} for \$\{triple\}, downloaded and checked/u,
		"the bundle's runtime is named in the gate's own summary",
	);
});
