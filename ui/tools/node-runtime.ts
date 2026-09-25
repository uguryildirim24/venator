/**
 * What an official Node build for a given Rust target triple is called, what it is supposed to
 * hash to, and what a staged binary actually turns out to be.
 *
 * `prepare-sidecar.ts` staged the sidecar by copying `process.execPath`, so the only runtime it
 * could ever produce was one for the machine it ran on. Cross-staging means fetching somebody
 * else's runtime and shipping it, and three things that were free before now have to be
 * established: the bytes must be the ones nodejs.org published (checked against a digest pinned
 * *here*, in the repository), they must be for the target the file is *named* for, and the name
 * must be a target triple rather than a path.
 *
 * The digests are pinned rather than fetched on purpose. A checksum read from
 * `https://nodejs.org/dist/v<version>/SHASUMS256.txt` and the artifact it vouches for come from
 * the same origin, so anything that can answer as nodejs.org serves both halves and they agree
 * by construction — and a substituted runtime would be a genuine executable of the right
 * architecture, so `readBinaryFormat` below does not close it either. An in-repository digest is
 * a different kind of claim: it was reviewed once, it is in git, and changing it is a diff
 * somebody has to approve. See `NODE_DIGESTS` for how to regenerate them.
 *
 * `readBinaryFormat` covers the other half. A sidecar called
 * `node-x86_64-pc-windows-msvc.exe` that holds a Linux ELF passes every size, name and checksum
 * test anyone would think to run, and then Tauri bundles it and the packaged app opens onto a
 * dashboard with no API behind it — a missing runtime and a silent `None`, the same shape as the
 * bug PR #48 fixed. Nothing here may fail quietly.
 */

import { resolve, sep } from "node:path";

/** Where nodejs.org publishes releases. Every artifact and checksum is read from under it. */
export const NODE_DIST = "https://nodejs.org/dist";

/**
 * The Node release staged as the sidecar on the download route, pinned exactly.
 *
 * This is what every *bundle* carries: `desktop-stage.ts` asks for the verified route on every
 * target, so two builds of the same commit ship the same runtime whether or not either was a
 * cross build. The copy route — `tauri dev`, and a bare `pnpm sidecar` — still stages this
 * machine's own `process.execPath`, and what it guarantees is the major:
 * `assertHostRuntimeUsable` refuses a host Node whose major is not this one, because the API
 * bundled beside it is emitted for this major.
 *
 * The major is 24 because that is what the repo already targets everywhere it says so:
 * `.github/workflows/ci.yml` installs `node-version: "24"` for all three TypeScript jobs, and
 * the API bundle in `prepare-sidecar.ts` is emitted with vite's `target: "node24"`. There is
 * no `engines` field in `ui/package.json` and no `.nvmrc` anywhere in the repo, so those two
 * are the whole of the evidence. Bumping this is a deliberate edit: the digests in
 * `NODE_DIGESTS` are per release and every one of them has to move with it.
 */
export const NODE_VERSION = "24.20.0";

/** The platforms nodejs.org publishes a build for that Venator is packaged onto. */
export type SidecarPlatform = "win32" | "darwin" | "linux";

/** The architectures nodejs.org publishes a build for. */
export type SidecarArch = "x64" | "arm64";

/** A Rust target triple, resolved to the coordinates an official Node build is named by. */
export type SidecarTarget = {
	readonly triple: string;
	readonly platform: SidecarPlatform;
	readonly arch: SidecarArch;
};

/** One official Node artifact: where to get it, what it must hash to, and what to take out. */
export type NodeArtifact = {
	/** The URL to fetch. */
	readonly url: string;
	/** The name this artifact goes by in that release's `SHASUMS256.txt`, for messages. */
	readonly checksumName: string;
	/** The name it is cached under locally; a flat name, never a path. */
	readonly cacheName: string;
	/** The sha256 the download has to have, pinned in this file. Never fetched. */
	readonly digest: string;
	/**
	 * The path of the runtime inside the archive, or `null` when the download *is* the
	 * runtime. Windows releases publish a bare `node.exe`, which is why that case exists.
	 */
	readonly member: string | null;
};

/**
 * The sha256 of every artifact this script can stage, for `NODE_VERSION` exactly.
 *
 * Keyed by `<platform>-<arch>`, which is the whole of what decides an artifact once the version
 * is fixed. A target with no entry here is refused rather than fetched: the point of pinning is
 * that no digest ever arrives over the wire, so "unpinned" cannot mean "look it up".
 *
 * **Regenerating these on a version bump.** They are the same values nodejs.org publishes in
 * that release's `SHASUMS256.txt`, but take them through the signature rather than off the plain
 * file, because the plain file is same-origin with the artifacts and proves nothing on its own:
 *
 * ```
 * curl -O https://nodejs.org/dist/v<version>/SHASUMS256.txt.asc
 * gpg --verify SHASUMS256.txt.asc          # key must be one of the fingerprints in
 *                                          # github.com/nodejs/release-keys keys.list
 * ```
 *
 * Then copy the six lines for `win-{x64,arm64}/node.exe` and
 * `node-v<version>-{darwin,linux}-{x64,arm64}.tar.gz` in here. `ui/tests/node-runtime.test.ts`
 * pins all six again, so a half-done bump fails the suite rather than the build.
 */
export type DigestKey = `${SidecarPlatform}-${SidecarArch}`;

/**
 * Typed as a total record on purpose: adding a platform or an architecture to the unions above
 * without pinning its digest is a compile error here, rather than something discovered at the
 * moment a build would otherwise have had to fetch one.
 */
const PINNED = {
	"win32-x64": "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5",
	"win32-arm64": "92949e7764e56e305cb84ea3d575912e822c79e85599362e8d408b04b9ffd326",
	"darwin-x64": "9e5b2644cf107befb6aefca676b96d3296bc10138096f022ed378d6233ed81f4",
	"darwin-arm64": "40e5607e5ecb3db9192723776da2d75d966260fc74a7a9e731c1bd67dda96bc8",
	"linux-x64": "855d581f8a4eb1a8117e3426de25fe02770592febcfb31369aee1ffbfee9e8ec",
	"linux-arm64": "3515603e2487879a39bc75716f1a2affd027500c64ba50e845cf72cb33219013",
} satisfies Record<DigestKey, string>;

/**
 * The same table, looked up by a plain string.
 *
 * Deliberately not keyed by `DigestKey`: the refusal below has to be reachable — and therefore
 * testable — with a key nobody pinned, which a total record typed by the unions can never
 * produce. `PINNED` keeps the compile-time guarantee; this keeps the runtime one.
 */
export const NODE_DIGESTS: ReadonlyMap<string, string> = new Map(Object.entries(PINNED));

/**
 * The pinned sha256 for `target`, or a refusal.
 *
 * The refusal is the important half. Falling back to the published `SHASUMS256.txt` for a target
 * nobody pinned would hand the decision straight back to whoever is answering as nodejs.org,
 * which is the whole thing the pin exists to take away.
 *
 * `PINNED` is total over the two unions, so this cannot fire today. It is kept as a runtime
 * refusal because the property that matters is "an unpinned target does not reach the network",
 * and that has to hold however the map is edited later, not only while the types happen to
 * enforce it.
 */
export function pinnedDigest(target: SidecarTarget): string {
	return pinnedDigestFor(`${target.platform}-${target.arch}`);
}

/** `pinnedDigest` by key, which is the form the refusal can actually be exercised in. */
export function pinnedDigestFor(key: string): string {
	const digest = NODE_DIGESTS.get(key);
	if (digest === undefined) {
		throw new Error(
			`no sha256 is pinned in node-runtime.ts for "${key}" at Node ${NODE_VERSION}, so there ` +
				"is nothing to check a download against and none was made. Add the digest to " +
				"NODE_DIGESTS from that release's signed SHASUMS256.txt — this is never fetched at " +
				"build time on purpose, because a checksum served by whoever served the binary " +
				"agrees with it by construction.",
		);
	}
	return digest;
}

const ARCHITECTURES = new Map<string, SidecarArch>([
	["x86_64", "x64"],
	["aarch64", "arm64"],
]);

/**
 * One field of a target triple. Letters, digits and underscores, which is every triple `rustc
 * --print target-list` names that nodejs.org publishes a build for.
 *
 * Narrow on purpose, and the narrowness is the point: the triple becomes part of a filename, so
 * a field holding `/` or `..` is a path rather than a target. `--target
 * 'x86_64-/../../pwn2-windows-msvc'` used to parse — only fields 0, 2 and 3 were ever read — and
 * staged 93 MB into `src-tauri/`, outside the gitignored `src-tauri/binaries/`, and exited 0.
 */
const TRIPLE_FIELD = /^[A-Za-z0-9_]+$/;

/**
 * `triple` split into its fields, or `null` when it is not a target triple at all.
 *
 * Shared with `python-runtime.ts`, which stages a different runtime for the same triples and
 * needs the same answer to the same question. Duplicating this was the alternative and it is
 * the wrong one: the check is what stops a `--target` from being a path, and a second copy is a
 * second thing to remember to tighten.
 */
export function tripleFields(triple: string): readonly string[] | null {
	const parts = triple.split("-");
	if (parts.length < 3 || parts.length > 4 || !parts.every((part) => TRIPLE_FIELD.test(part))) {
		return null;
	}
	return parts;
}

function unsupported(triple: string, because: string): Error {
	return new Error(
		`cannot stage a Node sidecar for "${triple}": ${because}. ` +
			"nodejs.org publishes official builds for x86_64 and aarch64 on Windows (msvc), " +
			"macOS and glibc Linux only. Pass one of those target triples, or build the " +
			"sidecar on the machine it is for.",
	);
}

/**
 * A Rust target triple → the Node build that runs on it.
 *
 * Strict on purpose. Every triple this does not name is a loud refusal rather than a nearest
 * match, because the nearest match is exactly the wrong-but-plausible artifact: a musl target
 * handed the glibc build produces a sidecar that is the right architecture, the right name and
 * the right size, and dies at exec on the machine it was staged for.
 */
export function parseTarget(triple: string): SidecarTarget {
	// Shape first, before any field is read and long before any of it reaches a path. A Rust
	// triple is <arch>-<vendor>-<os> or <arch>-<vendor>-<os>-<env>; anything else — a bare word, a
	// trailing empty field, an extra segment, a field with a separator in it — is not one, and a
	// build script guessing at what somebody meant is how it ends up writing outside its own
	// output directory.
	const parts = tripleFields(triple);
	if (parts === null) {
		throw unsupported(
			triple,
			"that is not a target triple — it has to be three or four fields of letters, digits " +
				"and underscores joined by dashes, like x86_64-pc-windows-msvc",
		);
	}
	const architecture = parts[0] ?? "";
	const arch = ARCHITECTURES.get(architecture);
	if (arch === undefined) {
		throw unsupported(triple, `no official Node build for the architecture "${architecture}"`);
	}
	const operatingSystem = parts[2] ?? "";
	const environment = parts[3] ?? "";
	if (operatingSystem === "windows") return { triple, platform: "win32", arch };
	if (operatingSystem === "darwin") return { triple, platform: "darwin", arch };
	if (operatingSystem === "linux") {
		if (environment !== "" && environment !== "gnu") {
			throw unsupported(triple, `nodejs.org publishes glibc Linux builds, not "${environment}"`);
		}
		return { triple, platform: "linux", arch };
	}
	throw unsupported(triple, `no official Node build for the operating system "${operatingSystem}"`);
}

/**
 * The official artifact carrying the Node runtime for `target`, at `NODE_VERSION`.
 *
 * The version is not a parameter. It used to be, fed from `$VENATOR_SIDECAR_NODE_VERSION`, which
 * was interpolated raw into both the URL and the cache filename: a value of
 * `1/../../dist/v24.20.0` normalised to a real nodejs.org path and wrote its cache entry outside
 * the cache directory. With the digests pinned per release there is nothing an override could be
 * checked against anyway, so the version is the constant above and changing it is an edit here.
 */
export function nodeArtifact(target: SidecarTarget): NodeArtifact {
	const version = NODE_VERSION;
	const digest = pinnedDigest(target);
	if (target.platform === "win32") {
		// Windows releases publish the runtime on its own, already suffixed. No archive means
		// no extraction, and no half-written extraction to notice afterwards.
		const checksumName = `win-${target.arch}/node.exe`;
		return {
			url: `${NODE_DIST}/v${version}/${checksumName}`,
			checksumName,
			cacheName: `node-v${version}-win-${target.arch}.exe`,
			digest,
			member: null,
		};
	}
	const system = target.platform === "darwin" ? "darwin" : "linux";
	// .tar.gz rather than the smaller .tar.xz: gzip is what every tar can read unaided, and a
	// build host that has to install xz first is a failure mode bought for a few megabytes.
	const stem = `node-v${version}-${system}-${target.arch}`;
	const cacheName = `${stem}.tar.gz`;
	return {
		url: `${NODE_DIST}/v${version}/${cacheName}`,
		checksumName: cacheName,
		cacheName,
		digest,
		member: `${stem}/bin/node`,
	};
}

/** What a binary's own header says it is: one platform, and every architecture it holds. */
export type BinaryFormat = {
	readonly platform: SidecarPlatform;
	readonly arches: readonly SidecarArch[];
};

const ELF_MACHINES = new Map<number, SidecarArch>([
	[0x3e, "x64"],
	[0xb7, "arm64"],
]);

const PE_MACHINES = new Map<number, SidecarArch>([
	[0x8664, "x64"],
	[0xaa64, "arm64"],
]);

const MACH_CPUS = new Map<number, SidecarArch>([
	[0x0100_0007, "x64"],
	[0x0100_000c, "arm64"],
]);

function truncated(): Error {
	return new Error("the staged runtime is too short to carry an executable header at all");
}

function u16(view: DataView, offset: number, little: boolean): number {
	if (offset + 2 > view.byteLength) throw truncated();
	return view.getUint16(offset, little);
}

function u32(view: DataView, offset: number, little: boolean): number {
	if (offset + 4 > view.byteLength) throw truncated();
	return view.getUint32(offset, little);
}

function named(machines: Map<number, SidecarArch>, code: number): readonly SidecarArch[] {
	const arch = machines.get(code);
	return arch === undefined ? [] : [arch];
}

/**
 * Read a binary's own header to find out what it is.
 *
 * This is the guard against the failure the cross-staging path could otherwise introduce
 * silently: a runtime for the wrong platform or the wrong architecture, sitting under a file
 * name that says otherwise. The header is the binary's own account of itself, so it cannot be
 * satisfied by naming a file correctly. `arches` is a list because a macOS universal binary
 * genuinely holds several.
 *
 * `head` needs only the first few kilobytes of the file.
 */
export function readBinaryFormat(head: Uint8Array): BinaryFormat {
	const view = new DataView(head.buffer, head.byteOffset, head.byteLength);
	if (head.byteLength < 4) throw truncated();
	const magic = u32(view, 0, false);

	// ELF: 7f 'E' 'L' 'F', endianness at byte 5, e_machine at 0x12.
	if (magic === 0x7f45_4c46) {
		const little = head[5] === 1;
		return { platform: "linux", arches: named(ELF_MACHINES, u16(view, 0x12, little)) };
	}

	// Mach-O, 64-bit and thin: magic then cputype, both little-endian.
	if (u32(view, 0, true) === 0xfeed_facf) {
		return { platform: "darwin", arches: named(MACH_CPUS, u32(view, 4, true)) };
	}

	// Mach-O universal: a big-endian count of 20-byte entries, each starting with a cputype.
	if (magic === 0xcafe_babe) {
		const count = u32(view, 4, false);
		const arches: SidecarArch[] = [];
		for (let index = 0; index < count; index += 1) {
			const arch = MACH_CPUS.get(u32(view, 8 + index * 20, false));
			if (arch !== undefined) arches.push(arch);
		}
		return { platform: "darwin", arches };
	}

	// PE: 'M' 'Z', then a file offset at 0x3c pointing at "PE\0\0" and the machine word.
	if (head[0] === 0x4d && head[1] === 0x5a) {
		const header = u32(view, 0x3c, true);
		if (u32(view, header, false) !== 0x5045_0000) {
			throw new Error("the staged runtime starts like a Windows executable but has no PE header");
		}
		return { platform: "win32", arches: named(PE_MACHINES, u16(view, header + 4, true)) };
	}

	throw new Error(
		"the staged runtime is not an ELF, Mach-O or PE executable at all — it is most likely " +
			"an error page or an HTML redirect that was written to disk instead of a binary",
	);
}

/** Prose for a `BinaryFormat`, for the one message that has to say what was staged instead. */
export function describeFormat(format: BinaryFormat): string {
	const systems = new Map<SidecarPlatform, string>([
		["win32", "a Windows PE executable"],
		["darwin", "a macOS Mach-O executable"],
		["linux", "a Linux ELF executable"],
	]);
	const arches = format.arches.length === 0 ? "an architecture this script does not name" : format.arches.join(" + ");
	return `${systems.get(format.platform) ?? format.platform} for ${arches}`;
}

/**
 * Where a staged runtime came from. The two failures look identical on disk and mean entirely
 * different things, so they do not share a message.
 */
export type SidecarOrigin = "host" | "download";

/**
 * The flag that asks for the download route on **every** target, this machine's own included.
 *
 * Named here rather than in either script because two of them have to agree on it, and only one
 * of them can import it: `desktop-stage.ts` passes this constant, while `prepare-sidecar.ts`
 * re-spells the flag as a `parseArgs` option key, which cannot be a constant. So the agreement is
 * held by three things rather than by the import — this constant is asserted to be `--verified`,
 * the option key is asserted to be declared, and `parseArgs` refuses an option it was not given
 * — and a flag one of them spelt differently would be a loud refusal rather than a flag that
 * silently does nothing, which is exactly the failure this flag exists to end.
 */
export const VERIFIED_FLAG = "--verified";

/**
 * Refuse a staged runtime that is not for the target it is named for.
 *
 * Called on every sidecar, including the host copy of `process.execPath`. On the host path this
 * is a real check rather than a formality on exactly one machine shape — an Apple Silicon Mac
 * running an x64 Node, usually under Rosetta — and that is the person these messages are written
 * for. Their toolchain has worked for months and they will read a hard failure from a build
 * script as the build script being broken, so the message says what was found, why it matters,
 * and what to do, and does not imply they did anything wrong.
 *
 * Returns the format it accepted, so the caller can say in the log what it just proved rather
 * than parsing the same bytes a second time to find out.
 */
export function assertBinaryMatches(
	head: Uint8Array,
	target: SidecarTarget,
	origin: SidecarOrigin,
): BinaryFormat {
	const format = readBinaryFormat(head);
	if (format.platform === target.platform && format.arches.includes(target.arch)) return format;
	const found = describeFormat(format);
	if (origin === "host") {
		throw new Error(
			`this machine's own Node is ${found}, and the sidecar is being staged for ` +
				`${target.triple}, which needs ${target.arch}. Nothing was written.\n\n` +
				"    This is a mismatch between two things on this machine — the Node that is on PATH\n" +
				"    and the target `rustc -vV` reports — and not a failure of Tauri, of the bundler,\n" +
				"    or of this checkout. The sidecar is a copy of the Node running this script, and it\n" +
				"    is the runtime that gets packaged and shipped, so it has to be built for the\n" +
				"    machine the app will be installed on.\n\n" +
				"    On an Apple Silicon Mac this is nearly always an x64 Node inherited from an older\n" +
				"    machine or installed from an x64 shell, running under Rosetta. Check it with\n" +
				"    `node -p process.arch`: it will say x64 where it should say arm64. Install a Node\n" +
				"    built for this machine — `fnm install " +
				`${NODE_VERSION.split(".")[0] ?? ""} --arch arm64\`, or the arm64 installer\n` +
				"    from nodejs.org — and run `pnpm sidecar` again.\n",
		);
	}
	throw new Error(
		`the runtime downloaded for ${target.triple} is ${found}, which will not run on that ` +
			"target. The sidecar was not written and the download was discarded.\n\n" +
			"    This one should not be reachable: the bytes matched the sha256 pinned for this\n" +
			"    target in node-runtime.ts, and an artifact with that digest is the official build\n" +
			"    for it. Reaching here means the pinned digest and the target it is filed under have\n" +
			"    drifted apart — check NODE_DIGESTS against the signed SHASUMS256.txt for Node " +
			`${NODE_VERSION} before staging anything.\n`,
	);
}

/**
 * Refuse to stage this machine's Node when it is not the major the API is built for.
 *
 * The cross path stages `NODE_VERSION`; the host path stages whatever Node happens to be running
 * this script, and nothing compared the two. A developer on Node 22 got a bundle whose API had
 * been emitted by vite for `node24` and whose `server/db.ts` opens the view with `node:sqlite` —
 * an app that may simply not start, first noticed by whoever installed it.
 *
 * Fails closed, which does newly stop a build that used to finish. That is the intent: the
 * alternative is quietly bundling a runtime the API was not built against.
 */
export function assertHostRuntimeUsable(hostVersion: string, pinned: string = NODE_VERSION): void {
	const found = hostVersion.split(".")[0] ?? "";
	const wanted = pinned.split(".")[0] ?? "";
	if (found === wanted) return;
	throw new Error(
		`this machine's Node is ${hostVersion}, and the sidecar has to be a Node ${wanted} build. ` +
			"Nothing was written.\n\n" +
			"    Why this matters: the sidecar is the runtime that goes inside the desktop bundle and\n" +
			`    runs the API on somebody else's machine, and the API bundled beside it is emitted for\n` +
			`    Node ${wanted} — vite's \`target: "node${wanted}"\`, and \`server/db.ts\` opens the view with\n` +
			"    `node:sqlite`. Staging this machine's Node instead would package an app whose own API\n" +
			"    may not start, and the first sign of it would be a window opening onto a dashboard\n" +
			"    with no data behind it.\n\n" +
			`    Nothing is wrong with this checkout or with Node ${hostVersion}; every other script\n` +
			"    here runs on it, and this is the first one that has to care which one it is. Run the\n" +
			`    build under Node ${wanted}:\n\n` +
			`        fnm use ${wanted}      # or nvm use ${wanted}, or install Node ${wanted} from nodejs.org\n` +
			"        pnpm sidecar\n\n" +
			`    \`node --version\` should say v${wanted} before you rerun.\n`,
	);
}

/**
 * Where the sidecar for `triple` is written, refusing any path that leaves `binaries`.
 *
 * `parseTarget` already rejects a triple that could escape, so this can only fire if that
 * validation is ever loosened. It is here because "the build script writes inside its own output
 * directory" is the property that actually matters, and a property worth having is worth stating
 * where it cannot be edited away by accident.
 */
export function sidecarPath(binaries: string, triple: string, suffix: string): string {
	const directory = resolve(binaries);
	const path = resolve(directory, `node-${triple}${suffix}`);
	if (!path.startsWith(directory + sep)) {
		throw new Error(
			`a sidecar for "${triple}" would be written to ${path}, which is outside ${directory}. ` +
				"Nothing was written. A target triple becomes part of the sidecar's filename, so a " +
				"triple carrying a path separator escapes the one directory this script owns.",
		);
	}
	return path;
}
