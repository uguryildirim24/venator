/**
 * Every step that stages the Node sidecar, as functions something other than the command line
 * can call.
 *
 * These steps lived in `prepare-sidecar.ts`, which ends in a top-level `await main()`: importing
 * that file runs it, so nothing could reach the digest comparison the download route exists for.
 * That is the hole `python-staging.ts` was split out to close, measured rather than guessed at —
 * eleven mutations of checks no test could import, eleven survivors of a green suite.
 *
 * The gap this file was written for is narrower and worse. The route used to be chosen by
 * whether the requested triple happened to equal this machine's, so `--target
 * x86_64-pc-windows-msvc` on a Windows host was a no-op that copied `process.execPath`:
 * verification happened only when the build machine's platform differed from the target's, which
 * is never true of the two bundles this repository will actually ship — a `.dmg` from the
 * Owner's Mac and an installer from the `windows-latest` runner. The pinned digests were dead
 * code for both, and two builds of the same commit carried whatever Node each machine had.
 *
 * So the route follows what the caller *asked for* rather than what the host happens to be:
 * `sidecarRoute` takes `verified`, the packaging gate asks for it on every target including this
 * machine's own, and `tauri dev` does not — a dev loop is running this machine's Node already
 * and a 53 MB download buys it nothing.
 *
 * How the bytes are fetched is injected, for the reason `python-staging.ts`'s is: a test of "the
 * download did not match its pin" must not need nodejs.org. The default is the real one, so the
 * step being described is the step that runs.
 *
 * `node-runtime.ts` still holds everything that is a fact rather than a step — the pinned
 * version, the six digests, the artifact names, the target parsing, the header assertions.
 */

import { execFileSync } from "node:child_process";
import {
	chmodSync,
	copyFileSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	renameSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { resolve } from "node:path";

import {
	NODE_VERSION,
	assertBinaryMatches,
	assertHostRuntimeUsable,
	describeFormat,
	nodeArtifact,
	parseTarget,
	type NodeArtifact,
	type SidecarOrigin,
	type SidecarTarget,
} from "./node-runtime.ts";
// The three file helpers and the two injection points are the same ones either staging step
// needs, and they are already written there. A second copy is a second thing to keep right.
import { HEADER_BYTES, head, megabytes, sha256, type FetchBytes, type Reporter } from "./python-staging.ts";

/**
 * How a sidecar is being staged, decided once and carried to every step that has to know.
 *
 * `origin` is the whole of the decision — the pinned runtime downloaded from nodejs.org and
 * checked against a digest in this repository, or this machine's own Node copied — and `target`
 * is non-null on exactly the first of those, because it is what an artifact is named by.
 */
export type StagingRoute = {
	readonly triple: string;
	/** Whether the triple is this build machine's own. Reported; it no longer decides anything. */
	readonly isHost: boolean;
	readonly origin: SidecarOrigin;
	/** What to download and what to check it against, or `null` on the copy route. */
	readonly target: SidecarTarget | null;
};

/**
 * Which route a request takes, decided by intent rather than by coincidence.
 *
 * `verified` forces the download for **every** target, this machine's own included. That is the
 * defect this replaced: `isHost ? null : parseTarget(triple)` meant a Windows target on a
 * Windows host, or a macOS target on the Owner's Mac, took the copy route and shipped an
 * unverified runtime, and the only builds that were ever checked were the ones nobody makes.
 *
 * `parseTarget` throws for a triple nothing can be staged for — and, since it checks the whole
 * triple's shape, for anything that is not a triple at all. That is what a garbage or
 * path-shaped `--target` gets: refused here, before anything is fetched or written.
 */
export function sidecarRoute(requested: string, host: string, verified: boolean): StagingRoute {
	const triple = requested === "" ? host : requested;
	const isHost = triple === host;
	if (verified || !isHost) return { triple, isHost, origin: "download", target: parseTarget(triple) };
	return { triple, isHost, origin: "host", target: null };
}

/**
 * Which Node version the staged runtime is announced as: the pinned one on the download route,
 * and whatever Node is running the build on the copy route.
 *
 * A function taking the host version rather than an expression reading `process.versions.node`
 * inline, because on a build host that happens to be running the pinned version the two answers
 * are the same string — so the machine most likely to run this is the one machine a test could
 * not tell a collapsed `NODE_VERSION` apart on. Passed in, one test says it out loud.
 */
export function stagedNodeVersion(origin: SidecarOrigin, hostVersion: string): string {
	return origin === "download" ? NODE_VERSION : hostVersion;
}

/** How a route reads in the log, so the line says which of the two checks a build got. */
export function describeRoute(route: StagingRoute): string {
	return route.origin === "download"
		? `pinned Node ${NODE_VERSION}, downloaded and checksummed`
		: "this machine's own Node, copied unverified";
}

/**
 * The target a staged runtime's header is checked against: always the one its *name* claims,
 * never what this machine happens to be.
 *
 * `null` for a triple no official Node build is named by — a musl Linux, say, which nobody can
 * reach on the download route and which only the copy route can produce. There is nothing to
 * check the header against then, and the line the caller prints says so rather than leaving a
 * silence to be read as a check that passed.
 */
export function nameTarget(triple: string): SidecarTarget | null {
	try {
		return parseTarget(triple);
	} catch {
		return null;
	}
}

/**
 * What has to hold before anything is written, given the route.
 *
 * The copy route stages whatever Node is running the build and the API bundled beside it is
 * emitted for `NODE_VERSION`'s major, so the two have to agree. The download route stages that
 * exact version and has nothing to ask this machine — guarding it with this would refuse a
 * perfectly good verified bundle because of the Node that happened to launch the build.
 */
export function assertRouteStageable(route: StagingRoute, hostVersion: string): void {
	if (route.origin === "host") assertHostRuntimeUsable(hostVersion);
}

/** The bytes at `url`, or an explanation of what a build host needs to reach it. */
export async function fetchNodeBytes(url: string): Promise<Uint8Array> {
	const response = await fetch(url).catch((cause: Error) => {
		throw new Error(
			`could not reach ${url}: ${cause.message}. Staging a verified runtime needs network ` +
				"access to nodejs.org — every bundle downloads the pinned Node rather than copying " +
				"this machine's. `pnpm sidecar` without --verified copies instead and needs no network.",
			{ cause },
		);
	});
	if (!response.ok) {
		throw new Error(
			`nodejs.org answered ${response.status} for ${url}. Either the pinned Node version ` +
				"does not publish a build for this target, or the target triple is not one it " +
				"names.",
		);
	}
	return new Uint8Array(await response.arrayBuffer());
}

/**
 * The artifact, in the cache, proven to hash to the sha256 this repository pins for it.
 *
 * Returns only on a match. A mismatch — cached or freshly downloaded — deletes the file and
 * throws, so nothing downstream can ever see a payload that failed. Nothing here reads a
 * checksum off the network: the expected digest travelled with the checkout.
 *
 * The cached file is hashed on every hit exactly as a fresh download is, because a cache that
 * can vouch for itself is a cache that can ship a bad binary.
 */
export async function verifiedDownload(
	cache: string,
	artifact: NodeArtifact,
	say: Reporter,
	fetchBytes: FetchBytes = fetchNodeBytes,
): Promise<string> {
	const digest = artifact.digest;
	mkdirSync(cache, { recursive: true });
	const path = resolve(cache, artifact.cacheName);

	if (existsSync(path)) {
		if (sha256(path) === digest) {
			say("checksum ok", `cached, sha256 ${digest}, as pinned (${megabytes(statSync(path).size)})`);
			return path;
		}
		say("cache rejected", `${path} failed its checksum; refetching`);
		rmSync(path);
	}

	say("downloading", artifact.url);
	const partial = `${path}.part-${process.pid}`;
	writeFileSync(partial, await fetchBytes(artifact.url));
	const actual = sha256(partial);
	if (actual !== digest) {
		rmSync(partial);
		throw new Error(
			`checksum mismatch for ${artifact.checksumName}: this repository pins ${digest}, the ` +
				`download hashed to ${actual}. Nothing was staged. Retry once; if it keeps failing, ` +
				"the bytes arriving here are not the ones nodejs.org published for Node " +
				`${NODE_VERSION} and must not be bundled — or NODE_DIGESTS is stale, which is a diff ` +
				"to make deliberately and not by pasting in whatever the download hashed to.",
		);
	}
	renameSync(partial, path);
	say("checksum ok", `sha256 ${digest}, as pinned (${megabytes(statSync(path).size)})`);
	return path;
}

/** Pull the one runtime out of a Node tarball, into `destination`. */
export function extractMember(archive: string, member: string, destination: string, scratchRoot: string): void {
	mkdirSync(scratchRoot, { recursive: true });
	const scratch = mkdtempSync(`${scratchRoot}/extract-`);
	try {
		try {
			execFileSync("tar", ["-xzf", archive, "-C", scratch, member], { stdio: "pipe" });
		} catch (cause) {
			throw new Error(
				`could not extract ${member} from ${archive}. This needs a \`tar\` that reads ` +
					"gzip, which macOS, Linux and Windows 10 and later all ship.",
				{ cause },
			);
		}
		const extracted = resolve(scratch, member);
		if (!existsSync(extracted) || statSync(extracted).size === 0) {
			throw new Error(
				`\`tar\` reported success but ${member} is missing or empty. Nothing was staged.`,
			);
		}
		copyFileSync(extracted, destination);
	} finally {
		rmSync(scratch, { recursive: true, force: true });
	}
}

/**
 * Put the runtime `route` names at `sidecar`, and prove it is what it is named for.
 *
 * There is no fallback here and there must never be one: a download that cannot be reached, or
 * bytes that do not hash to the pin, is a refusal. Copying this machine's Node instead would
 * turn every verified bundle into an unverified one at exactly the moment verification failed,
 * which is the one moment it was for.
 */
export async function stageSidecar(
	route: StagingRoute,
	sidecar: string,
	cache: string,
	say: Reporter,
	fetchBytes: FetchBytes = fetchNodeBytes,
	// What to download and what to check it against, injected for the reason `fetchBytes` is.
	// `null` means "the artifact this route names", which is what every production call site
	// gets and is the real step. Reading it off `route` in here instead is what made everything
	// past the digest comparison unreachable: a test can inject the bytes but not the pin they
	// are compared against, so no test could get past the mismatch to the extraction, the header
	// read-back, or the removal of a sidecar that fails it — three safety checks with no
	// coverage at all.
	artifact: NodeArtifact | null = null,
): Promise<void> {
	const target = route.target;
	if (target === null) {
		// The loud case, and the only one that can still ship a runtime nobody vouched for. It is
		// reachable from `tauri dev` and from a bare `pnpm sidecar`, never from a bundle.
		say("unverified", `this machine's own Node ${process.versions.node}, copied — provenance not checked`);
		copyFileSync(process.execPath, sidecar);
	} else {
		const wanted = artifact ?? nodeArtifact(target);
		const archive = await verifiedDownload(cache, wanted, say, fetchBytes);
		// Windows publishes the runtime bare and every other platform publishes it inside a
		// tarball, so `member` is what says which of those arrived — not the platform, which
		// would be the same fact read from a second place.
		if (wanted.member === null) copyFileSync(archive, sidecar);
		else extractMember(archive, wanted.member, sidecar, cache);
		if (target.platform !== "win32") chmodSync(sidecar, 0o755);
	}

	// Both outcomes print. The absence of a line used to be the only sign that the header had not
	// been checked, and a check nobody can see in the log is a check nobody knows did not run.
	const checkable = target ?? nameTarget(route.triple);
	if (checkable === null) {
		say("header skipped", `no official Node build names ${route.triple}; not checked`);
	} else {
		try {
			const format = assertBinaryMatches(head(sidecar, HEADER_BYTES), checkable, route.origin);
			say("header ok", `${describeFormat(format)}, matching ${route.triple}`);
		} catch (cause) {
			// Never leave behind a runtime that failed its own header check: the next build would
			// find the file, stage nothing, and bundle it.
			rmSync(sidecar, { force: true });
			throw cause;
		}
	}
	const version = stagedNodeVersion(route.origin, process.versions.node);
	say("node runtime", `${sidecar} (${megabytes(statSync(sidecar).size)}, Node ${version})`);
}
