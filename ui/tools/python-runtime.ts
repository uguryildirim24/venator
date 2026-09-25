/**
 * What an official embeddable CPython for a given Rust target triple is called, what it is
 * supposed to hash to, what a staged tree has to turn out to hold, and what would make it wrong.
 *
 * The desktop bundle today carries the dashboard and nothing else. Every person who installs
 * Venator runs the whole pipeline on their own machine — Discover, Match, Tailor, Fill — so an Install with no Python finds no view database, is served an empty one, and
 * stays on its first-run screen forever. The machine this ships to has no `python`, no `uv`,
 * no `node` and no `git`, so the runtime has to arrive in the bundle.
 *
 * This is the same shape of problem `node-runtime.ts` solved for the API sidecar, and the same
 * three questions have to be answered before somebody else's runtime is shipped to somebody
 * else's machine: are these the bytes python.org published, are they for the target the tree is
 * *named* for, and is that name a target triple rather than a path. Two answers are borrowed
 * outright — `tripleFields` and `readBinaryFormat` are imported rather than copied — and the
 * pins below are the third.
 *
 * **The digests are pinned here and never fetched at staging time.** python.org publishes no per-release
 * `SHASUMS256.txt`; it publishes an MD5 on the download page and a detached OpenPGP signature
 * beside each artifact. Even a sha256 served from python.org would prove nothing on its own,
 * because whoever can answer as python.org serves the artifact and the checksum both and they
 * agree by construction. See `PYTHON_DIGESTS` for how each pin was established and how to
 * establish the next one.
 *
 * **A wheel set is checked by what it contains, not by what it says it is.** The wheel metadata
 * is not sufficient and it is worth being precise about why: `playwright` ships one build
 * re-tagged per platform, so `playwright-1.62.0-py3-none-win_amd64.whl` and its macOS sibling
 * both record `Tag: py3-none-any` inside `WHEEL` while carrying, respectively, `driver/node.exe`
 * and a Mach-O `driver/node`. A staging step that read the tags and stopped would install the
 * macOS wheel into a Windows bundle and report success. So `looksForeign` sweeps every staged
 * file's first bytes for an ELF or Mach-O image, and `isWindowsNative` sends everything Windows
 * would actually load through the same header read the sidecar gets.
 */

import { describeFormat, readBinaryFormat, tripleFields, type SidecarArch, type SidecarPlatform } from "./node-runtime.ts";
import { lstatSync, mkdirSync, readdirSync, readlinkSync, rmSync, writeFileSync } from "node:fs";
import { resolve, sep } from "node:path";

/** Where python.org publishes releases. Every artifact and signature is read from under it. */
export const PYTHON_DOWNLOADS = "https://www.python.org/ftp/python";

/**
 * The CPython staged into the bundle, pinned exactly.
 *
 * The series is 3.13 because that is what the project declares: `pyproject.toml` says
 * `requires-python = ">=3.13"` and `uv.lock` records the same, and the wheels resolved for the
 * bundle are `cp313`. The patch is the newest 3.13 python.org publishes an embeddable Windows
 * build for. `tests/python-runtime.test.ts` reads `requires-python` out of `pyproject.toml` and
 * fails if this version stops satisfying it, so raising the floor to 3.14 cannot leave this pin
 * quietly behind.
 *
 * Bumping it is a deliberate edit: every entry in `PYTHON_DIGESTS` is per release and has to
 * move with it.
 */
export const PYTHON_VERSION = "3.13.15";

/** The architectures python.org names an embeddable Windows build for. */
export type PythonArch = Extract<SidecarArch, "x64" | "arm64">;

/** A Rust target triple, resolved to what an official embeddable CPython is named by. */
export type PythonTarget = {
	readonly triple: string;
	readonly platform: SidecarPlatform;
	readonly arch: PythonArch;
	/** What python.org calls this architecture in an artifact name: `amd64`, `arm64`. */
	readonly flavour: string;
};

/** One official artifact: where to get it, what it must hash to, and how to check it again. */
export type PythonArtifact = {
	readonly url: string;
	/** The detached OpenPGP signature beside it, for regenerating the pin. Never fetched here. */
	readonly signatureUrl: string;
	/** The name it is cached under locally; a flat name, never a path. */
	readonly cacheName: string;
	/** The sha256 the download has to have, pinned in this file. Never fetched. */
	readonly digest: string;
	/** How many files the distribution holds, so a substitution that unpacks cleanly is caught. */
	readonly entryCount: number;
};

/**
 * The sha256 of every distribution this script can stage, for `PYTHON_VERSION`.
 * The Apple silicon pin is the digest published on the python-build-standalone GitHub release
 * 20260924 asset and checked against the downloaded archive on this Mac.
 *
 * Keyed by `<platform>-<arch>`, matching `node-runtime.ts`. A target with no entry is refused
 * rather than fetched: pinning means no digest ever arrives over the wire, so "unpinned" cannot
 * quietly become "look it up".
 *
 * **Establishing a pin.** Take it through the signature, never off the download page, and never
 * off a checksum served beside the artifact:
 *
 * ```
 * base=https://www.python.org/ftp/python/<version>
 * name=python-<version>-embed-<flavour>.zip
 * curl -O $base/$name -O $base/$name.asc
 * # Steve Dower signs the Windows artifacts; the fingerprint below is the one python.org
 * # documents for him, and it is the anchor — fetch the key from a keyserver, not from
 * # python.org, so the key and the artifact do not share an origin.
 * curl -o key.asc "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0xFC624643487034E5&options=mr"
 * gpg --import key.asc && gpg --verify $name.asc $name
 * # Good signature, primary key fingerprint 7ED1 0B65 31D7 C8E1 BC29  6021 FC62 4643 4870 34E5
 * sha256sum $name
 * ```
 *
 * The `x86_64` pin below was established exactly that way. `aarch64` is deliberately absent:
 * python.org does publish `embed-arm64`, but nothing here has fetched or verified it, and a
 * digest nobody checked is worse than no digest at all — the refusal is the honest state, and
 * `pinnedPythonDigest` is what says so.
 */
export const MAC_RELEASE = "20260924";
const PINNED: ReadonlyMap<string, string> = new Map([
	["win32-x64", "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"],
	// python-build-standalone install_only, aarch64-apple-darwin, 3.13.15+20260924.
	["darwin-arm64", "a18e1d1b6067d39cf7b2b605fdb78ad6b8a3aed221c44ef934d399dccf355453"],
]);

/** The same table, read-only, so a test can assert on it without reaching through a function. */
export const PYTHON_DIGESTS: ReadonlyMap<string, string> = PINNED;

/**
 * How many files each pinned distribution holds.
 *
 * Not a substitute for the digest — the digest already settles the bytes — but the check that
 * catches a *complete-looking* unpack. A tree that comes out of extraction with nine of
 * thirty-four members is the failure this whole file is written against: every name that is
 * there is right, every file that is there verifies, and Python does not start.
 */
const ENTRY_COUNTS: ReadonlyMap<string, number> = new Map([["win32-x64", 34]]);

function unsupported(triple: string, because: string): Error {
	return new Error(
		`cannot stage a Python runtime for "${triple}": ${because}. Supported pinned targets ` +
			"are x86_64-pc-windows-msvc and aarch64-apple-darwin.",
	);
}

/**
 * A Rust target triple → the embeddable CPython that runs on it.
 *
 * Strict, and refusing rather than approximating, for the reason `parseTarget` gives: the
 * nearest match is exactly the wrong-but-plausible artifact. `platform` is carried on the result
 * even though it can only be `win32` today because every check downstream is written against the
 * target rather than against an assumption, and the day a second platform is pinned that has to
 * already be true.
 */
export function parsePythonTarget(triple: string): PythonTarget {
	const parts = tripleFields(triple);
	if (parts === null) {
		throw unsupported(
			triple,
			"that is not a target triple — it has to be three or four fields of letters, digits " +
				"and underscores joined by dashes, like x86_64-pc-windows-msvc",
		);
	}
	const operatingSystem = parts[2] ?? "";
	if (parts[1] === "apple" && operatingSystem === "darwin" && parts[0] === "aarch64") {
		return { triple, platform: "darwin", arch: "arm64", flavour: "aarch64" };
	}
	if (operatingSystem !== "windows") {
		throw unsupported(triple, `there is no pinned build for the operating system "${operatingSystem}"`);
	}
	const architecture = parts[0] ?? "";
	if (architecture === "x86_64") return { triple, platform: "win32", arch: "x64", flavour: "amd64" };
	if (architecture === "aarch64") return { triple, platform: "win32", arch: "arm64", flavour: "arm64" };
	throw unsupported(triple, `there is no embeddable build for the architecture "${architecture}"`);
}

/**
 * The pinned sha256 for `key`, or a refusal.
 *
 * The refusal is the point of the function. Falling back to whatever python.org serves for a
 * target nobody pinned would hand the decision back to whoever is answering as python.org,
 * which is the whole thing the pin takes away.
 */
export function pinnedPythonDigest(key: string): string {
	const digest = PINNED.get(key);
	if (digest === undefined) {
		throw new Error(
			`no sha256 is pinned in python-runtime.ts for "${key}" at Python ${PYTHON_VERSION}, so ` +
				"there is nothing to check a download against and none was made. Add the digest to " +
				"PYTHON_DIGESTS after verifying that release's detached OpenPGP signature — it is " +
				"never fetched at build time on purpose, because a checksum served by whoever served " +
				"the archive agrees with it by construction.",
		);
	}
	return digest;
}

/** How many files the pinned distribution for `key` holds; refuses alongside the digest. */
export function pinnedEntryCount(key: string): number {
	const count = ENTRY_COUNTS.get(key);
	if (count === undefined) {
		throw new Error(
			`no file count is pinned in python-runtime.ts for "${key}", so an archive that unpacked ` +
				"cleanly but held the wrong distribution could not be told apart from the right one. " +
				"Add it to ENTRY_COUNTS beside the digest.",
		);
	}
	return count;
}

/** The key `PYTHON_DIGESTS` and `ENTRY_COUNTS` file a target under. */
export function digestKey(target: PythonTarget): string {
	return `${target.platform}-${target.arch}`;
}

/** The official artifact carrying the embeddable CPython for `target`, at `PYTHON_VERSION`. */
export function pythonArtifact(target: PythonTarget): PythonArtifact {
	const key = digestKey(target);
	if (target.platform === "darwin") {
		const cacheName = `cpython-${PYTHON_VERSION}+${MAC_RELEASE}-${target.flavour}-apple-darwin-install_only.tar.gz`;
		const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${MAC_RELEASE}/${cacheName.replace("+", "%2B")}`;
		return { url, signatureUrl: "", cacheName, digest: pinnedPythonDigest(key), entryCount: 0 };
	}
	const cacheName = `python-${PYTHON_VERSION}-embed-${target.flavour}.zip`;
	const url = `${PYTHON_DOWNLOADS}/${PYTHON_VERSION}/${cacheName}`;
	return { url, signatureUrl: `${url}.asc`, cacheName, digest: pinnedPythonDigest(key), entryCount: pinnedEntryCount(key) };
}

/** `3.13.15` → `313`, which is what CPython names its DLL, its stdlib zip and its `._pth` for. */
export function pythonAbiNumber(version: string = PYTHON_VERSION): string {
	const parts = version.split(".");
	const major = parts[0] ?? "";
	const minor = parts[1] ?? "";
	if (!/^\d+$/u.test(major) || !/^\d+$/u.test(minor)) {
		throw new Error(`"${version}" is not a Python version this can name a runtime for`);
	}
	return `${major}${minor}`;
}

/** `3.13.15` → `cp313`, the interpreter tag every compiled wheel in the bundle has to carry. */
export function pythonTag(version: string = PYTHON_VERSION): string {
	return `cp${pythonAbiNumber(version)}`;
}

/** `3.13.15` → `python313._pth`, the file that decides the embedded interpreter's `sys.path`. */
export function pathConfigurationName(version: string = PYTHON_VERSION): string {
	return `python${pythonAbiNumber(version)}._pth`;
}

/** Where the wheels go inside the staged tree, as a `/`-joined path relative to its root. */
export const SITE_PACKAGES = "Lib/site-packages";

/** Where `src/venator` goes inside the staged tree, as a `/`-joined path relative to its root. */
export const PIPELINE_SOURCE = "src";

/**
 * The `._pth` the staged runtime ships with, replacing the one in the distribution.
 *
 * This is not cosmetic and it is the difference between a tree that works and one that looks
 * complete. An embeddable distribution ships a `._pth` naming only the stdlib zip and its own
 * directory, with `import site` commented out; a runtime staged with that file has 129 MB of
 * wheels next to it and cannot import one of them. Python reads this file instead of consulting
 * the environment at all — no `PYTHONPATH`, no registry, no user site — which is exactly what an
 * Install wants: the bundle's interpreter cannot be redirected by whatever else is on the
 * machine.
 *
 * Backslashes and CRLF because this file is read on Windows and that is what the distribution
 * ships; Python accepts either separator, so the choice is about matching the artifact rather
 * than about correctness. `import site` is uncommented so that `.pth` files inside
 * `site-packages` are processed — nothing in the current wheel set installs one, and a set that
 * later does should not fail mysteriously.
 */
export function pathConfiguration(version: string = PYTHON_VERSION): string {
	const lines = [
		`python${pythonAbiNumber(version)}.zip`,
		".",
		SITE_PACKAGES.replaceAll("/", "\\"),
		PIPELINE_SOURCE.replaceAll("/", "\\"),
		"import site",
	];
	return `${lines.join("\r\n")}\r\n`;
}

/** The one directory under `resources/` that every staged Python runtime lives in. */
export const PYTHON_RESOURCES = "python";

/** The file that keeps that directory from ever being empty. See `pythonResourcesNotice`. */
export const NOTICE_NAME = "README.txt";

/**
 * What goes in `resources/python/README.txt`, and why the file exists at all.
 *
 * `bundle.resources` names `resources/python/**\/*`, and Tauri's build script refuses a resource
 * glob that matches nothing — `glob pattern resources/python/**\/* path not found or didn't
 * match any files`, which fails `cargo build` before a line of the host is compiled. That is not
 * a hypothetical: it failed the `windows — tauri host compiles` job, which stages the sidecar
 * and never runs `pnpm python`, and it would fail the Owner's Mac the same way.
 *
 * The fix is not to drop the resource — the runtime has to ship — but to make the directory
 * real whether or not anything was staged into it. `prepare-sidecar.ts` writes this file on
 * every desktop build, so the glob always resolves, and a bundle built without the staging step
 * carries this one file and says so rather than failing to build.
 */
export function pythonResourcesNotice(): string {
	return [
		"This directory holds the Python runtime the Venator pipeline runs on, staged per target",
		"triple by `pnpm python --target <triple>` (ui/tools/prepare-python.ts).",
		"",
		"`pnpm sidecar` creates it whether or not anything has been staged, because",
		"tauri.conf.json declares `resources/python/**/*` and Tauri's bundler refuses a resource",
		"glob that matches no files at all. A bundle built without the staging step therefore",
		"carries this file and nothing else, which is the honest state of it: the dashboard is",
		"there, the pipeline is not.",
		"",
		"Everything here is generated and gitignored. Delete it at will.",
		"",
	].join("\n");
}

/**
 * The staged runtimes under `resources/python/` that are not for `triple`.
 *
 * A bundle is built for one target, and `bundle.resources` is a glob rather than a triple — so
 * everything under `resources/python/` ships, whichever target it was staged for. Without this,
 * staging a Windows runtime on the Owner's Mac and then building a `.dmg` produces a macOS
 * bundle carrying 154 MB of Windows binaries: a plausible-looking artifact, built without a
 * warning, wrong in a way nothing downstream would notice.
 *
 * `prepare-sidecar.ts` runs first on every desktop build and knows the target, so it is where
 * this is enforced. Removing a staged tree costs one `pnpm python --target <triple>` to get
 * back, and the caller says out loud what it removed.
 */
export function staleStagings(entries: readonly string[], triple: string): readonly string[] {
	return entries.filter((entry) => entry !== triple && entry !== NOTICE_NAME).sort();
}

/**
 * `name` resolved under `directory`, or a refusal — the containment rule on its own.
 *
 * Every path this file writes to or deletes goes through here, and the two callers below are why
 * it is one function rather than a rule each of them states for itself: the function that stages
 * carried the check and the function that *deletes* did not, which is the more dangerous half of
 * the pair to leave uncovered.
 *
 * The trailing separator is the whole of it. Without it `resources/python/x86_64` is a prefix of
 * `resources/python/x86_64-evil`, and a sibling directory passes as containment.
 *
 * This is path arithmetic and nothing more: `resolve` does not resolve symbolic links, so a
 * containment check on its own says where a path *spells*, never where it *leads*. Whoever hands
 * a directory to this has to have established that the directory is the directory —
 * `pythonResourcesRoot` is where that is done.
 */
export function containedResource(directory: string, name: string, what: string, because: string): string {
	const root = resolve(directory);
	const path = resolve(root, name);
	if (path.startsWith(root + sep)) return path;
	throw new Error(
		`${what} resolves to ${path}, which is outside ${root}. Nothing was written and nothing ` +
			`was removed. ${because}`,
	);
}

/**
 * `resources/python/`, proven to be a real directory inside `resources/` rather than a link out.
 *
 * `preparePythonResources` clears this directory with a recursive delete, and a recursive delete
 * is the one operation here that a containment check cannot make safe on its own.
 * `node:path.resolve` is textual — it does not resolve symbolic links — so when
 * `resources/python` is itself a link, `readdirSync` enumerates the *target's* children and
 * every `resolve(root, entry)` built from them passes containment while naming a file outside
 * `resources/`, outside the bundle and outside the checkout. That is not a hypothetical shape:
 * this runs on every `pnpm sidecar`, which `beforeDevCommand` and `beforeBuildCommand` both run.
 *
 * So the link is refused rather than followed, and refused before anything is created. Only the
 * last component is asked about: the ancestors are the caller's own — a checkout under a
 * symlinked home, or macOS's `/tmp`, where every path resolves somewhere other than where it is
 * spelled — and a build host is not this script's to disapprove of. Asked as "is this entry a
 * link", not as "does this path resolve to itself", because the second question answers *no* on
 * an ordinary macOS temporary directory and on Windows whenever the canonical casing of a path
 * differs from the one that was passed in.
 */
export function pythonResourcesRoot(resources: string): string {
	const directory = resolve(resources);
	const root = resolve(directory, PYTHON_RESOURCES);
	const entry = lstatSync(root, { throwIfNoEntry: false });
	if (entry === undefined || !entry.isSymbolicLink()) return root;
	throw new Error(
		`the staged runtimes have to live in ${root}, and ${root} is a link to ` +
			`${readlinkSync(root)}. Nothing was written and nothing was removed. This directory is ` +
			"emptied of runtimes staged for another target with a recursive delete, and a path is " +
			`resolved textually — so every entry of ${readlinkSync(root)} would be deleted, from ` +
			`outside ${directory}. Remove the link, or point the build at the directory it really ` +
			"means to write into.",
	);
}

/**
 * Where each of `stale` is, as a path this script is allowed to delete — or a refusal.
 *
 * Split out of `preparePythonResources` for the reason `zip.ts` splits `containedPath` out of
 * `entryPath`: the branch that matters has to be reachable, and therefore testable, by
 * something. These names come from `readdirSync`, which never answers `..` or an absolute path,
 * so the containment check cannot fire through the caller — and a check nobody can make fire is
 * a check nobody knows still works. It is stated here, where loosening what may be enumerated
 * cannot quietly undo it, and exercised directly.
 */
export function stalePaths(root: string, stale: readonly string[]): readonly string[] {
	return stale.map((entry) =>
		containedResource(
			root,
			entry,
			`the staged runtime "${entry}"`,
			"Only what is inside the directory this script owns may be removed by it, and a " +
				"recursive delete of anything else is somebody's work gone with an exit code of 0.",
		),
	);
}

/**
 * Make `resources/python/` hold exactly what a bundle for `triple` may carry, and return it.
 *
 * Two jobs, both of which have to happen before the bundler looks: the directory exists and is
 * never empty, and nothing staged for another target is sitting in it. Returns the directories
 * it removed so the caller can report them rather than deleting somebody's work in silence.
 *
 * The order is load-bearing. The directory is proven to be itself before a byte is written and
 * before anything is removed, so a refusal leaves the tree exactly as it was found. An entry
 * *inside* it that is a link is unlinked rather than followed, which is what `rmSync` does with
 * one and is the behaviour wanted: the link is the thing that was staged there.
 */
export function preparePythonResources(resources: string, triple: string): readonly string[] {
	const root = pythonResourcesRoot(resources);
	mkdirSync(root, { recursive: true });
	writeFileSync(resolve(root, NOTICE_NAME), pythonResourcesNotice(), "utf8");
	const stale = staleStagings(readdirSync(root), triple);
	for (const path of stalePaths(root, stale)) rmSync(path, { recursive: true, force: true });
	return stale;
}

/**
 * Where the runtime for `triple` is staged, refusing any path that leaves `resources`.
 *
 * `parsePythonTarget` already rejects a triple that could escape, so this can only fire if that
 * validation is loosened later. It is here because "the build script writes inside its own
 * output directory" is the property that matters.
 */
export function pythonStagingRoot(resources: string, triple: string): string {
	return containedResource(
		resolve(resources, PYTHON_RESOURCES),
		triple,
		`a Python runtime for "${triple}"`,
		"A target triple becomes part of the staging path, so a triple carrying a path separator " +
			"escapes the one directory this script owns.",
	);
}

/**
 * The target a bundle for `triple` can carry a Python runtime for, or `null`.
 *
 * Two different absences, deliberately answered the same way, because both mean "no runtime goes
 * in this bundle": a target with no pinned archive (Intel macOS or Linux), and a Windows triple whose
 * digest nobody has verified, which today is `aarch64`. Neither is a failure and neither may be
 * papered over; `unpinnedRuntimeNotice` is what a build says about it.
 */
export function stageablePythonTarget(triple: string): PythonTarget | null {
	let target: PythonTarget;
	try {
		target = parsePythonTarget(triple);
	} catch {
		return null;
	}
	return PINNED.has(digestKey(target)) ? target : null;
}

/**
 * What a staged runtime has to have in it for a bundle to be carrying one, as relative paths.
 *
 * Not a file count and not a size: four files, each of which is a different way for the staging
 * to have half-happened. The interpreter is the runtime; `site-packages` is the wheels the
 * pipeline imports; `src/venator/__init__.py` is the pipeline itself; and the `._pth` is what
 * lets the interpreter see the other two — an embeddable CPython with the distribution's own
 * `._pth` has 129 MB of wheels beside it and cannot import one of them.
 */
export function runtimeEvidence(target: PythonTarget): readonly string[] {
	return target.platform === "darwin"
		? ["python/bin/python3.13", "python/lib/python3.13/site-packages", "python/lib/python3.13/site-packages/venator/__init__.py"]
		: RUNTIME_EVIDENCE;
}

export const RUNTIME_EVIDENCE: readonly string[] = [
	"python.exe",
	SITE_PACKAGES,
	`${PIPELINE_SOURCE}/venator/__init__.py`,
	pathConfigurationName(),
];

/** Which of `RUNTIME_EVIDENCE` is not there, asked of whoever knows what is on disk. */
export function missingRuntimeParts(present: (path: string) => boolean, target?: PythonTarget): readonly string[] {
	return (target ? runtimeEvidence(target) : RUNTIME_EVIDENCE).filter((path) => !present(path));
}

/**
 * The refusal for a bundle that would ship without the Python runtime it can carry.
 *
 * The `README.txt` that keeps Tauri's resource glob resolving means this failure is silent by
 * construction: a bundle built without ever running `pnpm python` succeeds and ships a `python/`
 * directory holding one text file. The installer is the right size for a dashboard, the app
 * opens, it finds no view database, it falls back to the sample, and it stays there — which is
 * the exact outcome staging a runtime exists to prevent, arrived at without a single warning.
 */
export function missingRuntimeRefusal(triple: string, missing: readonly string[], optOut: string): string {
	return (
		`the desktop bundle for ${triple} would ship without a Python runtime: ${missing.join(", ")} ` +
		`${missing.length === 1 ? "is" : "are"} not staged under ${PYTHON_RESOURCES}/${triple}/. ` +
		"Nothing was bundled.\n\n" +
		`    Stage it with \`pnpm python --target ${triple}\` and build again. That step needs\n` +
		"    the network, `uv` on the build host, and about 154 MB of disk.\n\n" +
		"    This cannot be left to be noticed later. `resources/python/` always holds at least a\n" +
		"    README.txt — Tauri refuses a resource glob that matches nothing — so a bundle built\n" +
		"    without the staging step is not a build failure. It is an installer that opens onto\n" +
		"    the sample corpus on a machine with no Python and stays there.\n\n" +
		`    A bundle deliberately without one is ${optOut}=1, which says so on the way past.\n`
	);
}

/**
 * What a build says when the target it is bundling for can carry no Python runtime at all.
 *
 * Used for targets without a pinned runtime, such as Intel macOS and Linux. Apple silicon
 * bundles carry python-build-standalone; Windows x86-64 carries python.org's embedded build.
 *
 * It is said at build time, every time, because the alternative is a bundle whose difference
 * from a complete one is invisible from the outside. The purge is named in the same breath for
 * the same reason: `pnpm sidecar` removes runtimes staged for another target, so building a
 * `.dmg` on a Mac that has staged a Windows runtime takes that runtime out of `resources/`, and
 * a Windows bundle made afterwards has to stage it again.
 */
export function unpinnedRuntimeNotice(triple: string): string {
	return (
		`this bundle carries no Python runtime: nothing is pinned for ${triple}. python.org ` +
		"publishes an embeddable distribution for Windows only; python-build-standalone is pinned for Apple silicon. A bundle for this target is " +
		"the dashboard and the read-only API over whatever view database the machine already " +
		"has. On a machine that has never run the pipeline, the app opens onto the sample " +
		`corpus. \`pnpm sidecar\` has also just cleared any runtime staged for another target out ` +
		`of ${PYTHON_RESOURCES}/, so a Windows bundle made after this one stages its runtime again.`
	);
}

/**
 * One answer to "which target is this bundle for", and where that answer came from.
 *
 * The source is spelled the way an operator sets it, because the only two places a claim is
 * used — the line a build prints and the refusal below — are addressed to whoever has to
 * reconcile two of them.
 */
export interface TripleClaim {
	/** `TAURI_ENV_TARGET_TRIPLE`, `--target`, `$VENATOR_DESKTOP_TARGET`. */
	readonly source: string;
	/** The triple that source names, or `""` when it named none. */
	readonly triple: string;
}

/** The variable `tauri build` sets for every command it runs, naming what it is packaging for. */
export const BUNDLER_TRIPLE = "TAURI_ENV_TARGET_TRIPLE";

/**
 * The refusal for a build given two different answers about which target it is for.
 *
 * A refusal rather than a precedence, because precedence cannot catch this. `tauri build` sets
 * `TAURI_ENV_TARGET_TRIPLE` and then runs `beforeBuildCommand` with no arguments, so whichever
 * of the two a resolution order picks, the loser is discarded in silence — and the losing case
 * has a shape: the staging steps are told one triple, the bundler packages for the other, and
 * what comes out is an installer of the right size and the right name carrying another
 * platform's binaries. Nothing downstream compares them back, and nothing on the finished
 * `.dmg` says which one it was built from.
 *
 * So the operator is told that their two answers disagree instead of one being chosen for them.
 */
export function conflictingTripleRefusal(bundler: string, other: TripleClaim): string {
	return (
		`two different answers about which target this bundle is for: ${BUNDLER_TRIPLE} says ` +
		`${bundler} and ${other.source} says ${other.triple}. Nothing was staged.\n\n` +
		`    ${BUNDLER_TRIPLE} is set by \`tauri build\` itself, and it is the triple the bundler\n` +
		"    is packaging for rather than a preference to be overridden. Taking the other one\n" +
		`    would stage ${other.triple} binaries into a bundle named ${bundler}: the right\n` +
		"    size, the right name, and a pipeline that cannot start on the machine it installs\n" +
		"    on.\n\n" +
		`    Set ${other.source} to ${bundler}, or drop it and let the bundler's triple stand.\n`
	);
}

/**
 * Which target to stage for, given every source that named one — or `null` for "ask the host".
 *
 * `bundler` is `TAURI_ENV_TARGET_TRIPLE` and `fallbacks` are the sources a person sets by hand,
 * in precedence order. Two rules, and the first is why this function exists:
 *
 * 1. **When the bundler named a target, no other source may name a different one.** Any
 *    disagreement is refused, naming both values and where each came from. Agreement passes,
 *    and a bundler triple nobody contradicted is simply followed.
 * 2. **When it named none**, somebody is running the staging step by hand and the fallbacks
 *    decide, in order, with the Rust host triple — `null` here — behind them all.
 */
export function resolveTargetTriple(bundler: string, fallbacks: readonly TripleClaim[]): TripleClaim | null {
	if (bundler !== "") {
		for (const claim of fallbacks) {
			if (claim.triple !== "" && claim.triple !== bundler) {
				throw new Error(conflictingTripleRefusal(bundler, claim));
			}
		}
		return { source: BUNDLER_TRIPLE, triple: bundler };
	}
	return fallbacks.find((claim) => claim.triple !== "") ?? null;
}

/** Extensions Windows loads as native code, and which therefore have to be PE for the target. */
const WINDOWS_NATIVE = [".pyd", ".dll", ".exe"];

/** Extensions that are native code for a platform that is not Windows, whatever is inside. */
const FOREIGN_NATIVE = [".so", ".dylib"];

function hasExtension(name: string, extensions: readonly string[]): boolean {
	const lowered = name.toLowerCase();
	return extensions.some((extension) => lowered.endsWith(extension));
}

/** True when Windows would load this file as native code, so its header has to be read. */
export function isWindowsNative(name: string): boolean {
	return hasExtension(name, WINDOWS_NATIVE);
}

/** True when this name is native code for another platform, refused without reading it. */
export function isForeignNative(name: string): boolean {
	return hasExtension(name, FOREIGN_NATIVE);
}

/** ELF, and the four Mach-O magics: 32- and 64-bit thin images, and both universal orderings. */
const FOREIGN_MAGICS = [0x7f45_4c46, 0xfeed_face, 0xfeed_facf, 0xcefa_edfe, 0xcffa_edfe, 0xcafe_babe, 0xbeba_feca];

/**
 * True when these bytes begin an executable image for a platform that is not Windows.
 *
 * This is the check the wheel tags cannot do. `playwright` ships one build re-tagged per
 * platform and records `Tag: py3-none-any` in every one of them, so the macOS wheel and the
 * Windows wheel are indistinguishable by metadata and differ only in whether
 * `playwright/driver/node` is a Mach-O image or `playwright/driver/node.exe` is a PE one. The
 * driver has no extension on macOS and Linux, so nothing but its first four bytes gives it away.
 */
export function looksForeign(head: Uint8Array): boolean {
	if (head.byteLength < 4) return false;
	const view = new DataView(head.buffer, head.byteOffset, head.byteLength);
	const magic = view.getUint32(0, false);
	return FOREIGN_MAGICS.includes(magic);
}

/**
 * True when these bytes begin something Windows would load as an executable image.
 *
 * The companion to `isWindowsNative`, and the reason there are two: the extensions are what a
 * *name* says, and `playwright`'s bundled driver has no extension at all. A Windows PE for the
 * wrong architecture planted at `playwright/driver/node` is not foreign — `looksForeign` is
 * about ELF and Mach-O — and it is not named like anything Windows loads, so with the check
 * gated on the extension alone its header was read by nothing. `MZ` is two bytes and settles it:
 * `readBinaryFormat` reads the rest and says which architecture.
 */
export function looksWindowsNative(head: Uint8Array): boolean {
	return head.byteLength >= 2 && head[0] === 0x4d && head[1] === 0x5a;
}

/**
 * Refuse a staged binary that is not for the target the tree is named for.
 *
 * The same job `assertBinaryMatches` does for the sidecar, with the message written for the
 * person who hits it: somebody whose build has worked for months, reading a hard failure from a
 * script they did not know existed.
 */
export function assertPythonBinaryMatches(head: Uint8Array, target: PythonTarget, what: string): void {
	const format = readBinaryFormat(head);
	if (format.platform === target.platform && format.arches.includes(target.arch)) return;
	throw new Error(
		`${what} in the staged Python runtime is ${describeFormat(format)}, and the runtime is ` +
			`being staged for ${target.triple}, which needs ${target.arch}. Nothing was staged.\n\n` +
			"    A runtime for the wrong architecture passes every check anyone thinks to run: the\n" +
			"    download matched its pinned sha256, the archive unpacked, the file has the name it\n" +
			"    should have and roughly the size it should have. It fails on the machine it was\n" +
			"    packaged for, at import, as a pipeline that will not start — and by then it is in\n" +
			"    somebody's installer.\n\n" +
			"    Check that the digest pinned in python-runtime.ts and the target it is filed under\n" +
			`    have not drifted apart, and that the wheels were resolved with \`--python-platform\n` +
			`    ${target.triple}\`.\n`,
	);
}

/** PEP 503: the one spelling of a distribution name that two sources can be compared on. */
export function normalizeDistribution(name: string): string {
	return name.toLowerCase().replaceAll(/[-_.]+/gu, "-");
}

/**
 * The distributions a `uv export` requirements file pins, normalized.
 *
 * Deliberately narrow. The file is generated by `uv export --no-dev` from `uv.lock` moments
 * earlier, so this is not a requirements parser; it reads the one shape uv emits — `name==version
 * \` at the start of a line — and ignores hash continuations and comments. Anything else in the
 * file is not something this should be guessing about.
 */
export function requirementNames(exported: string): readonly string[] {
	const names: string[] = [];
	for (const line of exported.split("\n")) {
		if (line.startsWith(" ") || line.startsWith("\t") || line.startsWith("#")) continue;
		const match = /^([A-Za-z0-9][A-Za-z0-9._-]*)==/u.exec(line.trim());
		if (match !== null) names.push(normalizeDistribution(match[1] ?? ""));
	}
	return names.sort();
}

/**
 * The distributions actually present in a staged `site-packages`, read off its `.dist-info`
 * directories and normalized.
 */
export function stagedDistributions(entries: readonly string[]): readonly string[] {
	const names: string[] = [];
	for (const entry of entries) {
		if (!entry.endsWith(".dist-info")) continue;
		const stem = entry.slice(0, -".dist-info".length);
		const separator = stem.lastIndexOf("-");
		if (separator <= 0) continue;
		names.push(normalizeDistribution(stem.slice(0, separator)));
	}
	return names.sort();
}

/**
 * The names `pyproject.toml` declares in its `dev` dependency group.
 *
 * Read from the file rather than written down here, because the point of the check that uses it
 * is that the dev group did not ship, and a copy of the group in this file could agree with
 * itself while disagreeing with the project. Version specifiers, extras and environment markers
 * are stripped; the name is all this needs.
 */
export function devGroupNames(pyproject: string): readonly string[] {
	const table = pyproject.indexOf("[dependency-groups]");
	if (table < 0) return [];
	const opened = /\bdev\s*=\s*\[/u.exec(pyproject.slice(table));
	if (opened === null || opened.index === undefined) return [];

	// Scanned rather than matched with one expression, because a requirement may carry extras —
	// `pytest[xdist]>=9` — and the `]` closing those is not the `]` closing the array. A regex
	// that stops at the first one reads the group as empty, which is the direction that fails
	// open: an empty dev group makes `assertNoDevDistributions` a check that can never fire.
	const names: string[] = [];
	let cursor = table + opened.index + opened[0].length;
	while (cursor < pyproject.length) {
		const character = pyproject[cursor] ?? "";
		if (character === "]") break;
		if (character !== '"' && character !== "'") {
			cursor += 1;
			continue;
		}
		const close = pyproject.indexOf(character, cursor + 1);
		if (close < 0) break;
		const name = /^[A-Za-z0-9][A-Za-z0-9._-]*/u.exec(pyproject.slice(cursor + 1, close));
		if (name !== null) names.push(normalizeDistribution(name[0]));
		cursor = close + 1;
	}
	return names.sort();
}

/**
 * Refuse a staged wheel set that is not exactly the one that was resolved.
 *
 * Both directions matter and they fail differently. Something extra is a dependency that was
 * never resolved for this target — the dev group leaking in is the case worth naming, because
 * `pdfplumber` drags in `cryptography` and `cffi` and every one of those is a compiled wheel
 * that would then have to be right for Windows too. Something missing is a wheel that did not
 * install, which surfaces on the Owner's machine as an import error and nowhere earlier.
 */
export function assertDistributionsResolved(staged: readonly string[], resolved: readonly string[]): void {
	const wanted = new Set(resolved);
	const have = new Set(staged);
	const extra = staged.filter((name) => !wanted.has(name));
	const missing = resolved.filter((name) => !have.has(name));
	if (extra.length === 0 && missing.length === 0) return;
	const parts: string[] = [];
	if (extra.length > 0) parts.push(`staged but not resolved: ${extra.join(", ")}`);
	if (missing.length > 0) parts.push(`resolved but not staged: ${missing.join(", ")}`);
	throw new Error(
		`the staged wheel set is not the set that was resolved — ${parts.join("; ")}. Nothing was ` +
			"staged.\n\n" +
			"    The resolved set comes from `uv export --no-dev` over uv.lock, which is the lock\n" +
			"    the test suite runs against, so this is the bundle disagreeing with the project.\n" +
			"    An extra distribution is usually the dev group arriving through a `--no-deps` that\n" +
			"    was dropped; a missing one is a wheel that failed to install into --target.\n",
	);
}

/**
 * Refuse a staged wheel set that contains anything from the dev group.
 *
 * `assertDistributionsResolved` already covers this when the export was produced correctly.
 * This is not the same check twice: it holds even if the export itself was wrong, which is the
 * failure that would otherwise be invisible — a `--no-dev` dropped from the export command
 * makes the resolved set and the staged set agree with each other and both be wrong. `pytest`
 * and `pdfplumber` in an Owner's installer is 40 MB of a chain that reads PDFs, shipped because
 * one flag went missing.
 */
export function assertNoDevDistributions(staged: readonly string[], dev: readonly string[]): void {
	const declared = new Set(dev);
	const shipped = staged.filter((name) => declared.has(name));
	if (shipped.length === 0) return;
	throw new Error(
		`the staged wheel set contains the dev group: ${shipped.join(", ")}. Nothing was staged.\n\n` +
			"    Only the runtime dependencies belong in a bundle. The dev group is what the test\n" +
			"    suite needs — pytest, and pdfplumber with cryptography and cffi behind it — and\n" +
			"    none of it runs on an Owner's machine. Export the requirements with `--no-dev`.\n",
	);
}

/**
 * Refuse a wheel whose recorded tags are for another interpreter or another platform.
 *
 * This runs *and is not trusted*. It catches the honest half — a `cp312` wheel, or a `manylinux`
 * one — and it is worth having because those fail as an unhelpful import error much later. It
 * does not catch `playwright`, which records `py3-none-any` in every one of its per-platform
 * builds; `looksForeign` is what covers that, and the two are not redundant.
 */
export function assertWheelTags(distribution: string, tags: readonly string[], tag: string, platform: string): void {
	const acceptable = tags.filter((entry) => {
		const fields = entry.split("-");
		const interpreter = fields[0] ?? "";
		const wheelPlatform = fields[2] ?? "";
		const pure = interpreter.startsWith("py") && wheelPlatform === "any";
		return pure || (interpreter === tag && wheelPlatform === platform);
	});
	if (acceptable.length > 0) return;
	throw new Error(
		`${distribution} is tagged ${tags.join(", ")}, and this bundle needs ${tag}-*-${platform} ` +
			"or a pure-Python wheel. Nothing was staged.\n\n" +
			"    A wheel for another interpreter or another platform installs without complaint and\n" +
			"    fails at import on the machine it was packaged for. Resolve the wheels with\n" +
			`    \`--python-platform\` and \`--python-version\` naming the target, and refuse a source\n` +
			"    distribution with `--only-binary :all:` rather than building one here.\n",
	);
}

/**
 * Directories `uv pip install --target` writes that belong to the *host*, not to the target.
 *
 * Cross-installing produces these in the host's shape: `bin/` holds console-script shims, and
 * staging from Linux writes POSIX `#!` scripts named `httpx`, `playwright` and `normalizer`
 * into a tree bound for Windows, where the equivalent would be `Scripts\*.exe`. `include/` is
 * `greenlet.h`, which exists to compile against greenlet and is never read at runtime.
 *
 * Neither would be executed on the Owner's machine, so neither breaks anything — but a Windows
 * bundle carrying a Linux shell script is a build that quietly produced a tree shaped like the
 * machine that made it, and the whole point of naming a target is that it did not. The pipeline
 * is run as `python -m venator.<stage>`, so no console script is wanted in any case.
 */
export const HOST_ONLY_DIRECTORIES: readonly string[] = ["bin", "include"];

/**
 * Files the pipeline reads at runtime that no dependency provides and no `.py` glob would catch.
 *
 * `qualify-output-2.json` is not documentation: `venator.qualify.versions` reads it at import to
 * derive the schema version, so a staged tree with the `.py` files and not this one fails on the
 * first import of the qualification modules. Every non-`.py` file under `src/venator/` is named
 * here; `tests/python-runtime.test.ts` walks the tree so a new one fails loudly.
 */
export const REQUIRED_PIPELINE_FILES: readonly string[] = [
	"venator/__init__.py",
	"venator/qualify/schemas/qualify-output-2.json",
];
