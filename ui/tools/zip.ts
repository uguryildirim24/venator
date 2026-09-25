/**
 * Enough of the zip format to unpack one published archive, and refuse every other outcome.
 *
 * `prepare-sidecar.ts` extracts a `.tar.gz` by shelling out to `tar`. The embeddable CPython
 * that `prepare-python.ts` stages is a `.zip`, and shelling out is not available for it: GNU
 * tar — the tar on every Linux build host — does not read zip at all, bsdtar does, and `unzip`
 * is not installed by default on macOS CI images or on Windows. A build step whose behaviour
 * depends on which `tar` answered is a build step that stages different trees on different
 * machines, which is the failure this whole path exists to rule out.
 *
 * So the archive is read here, and read strictly. Three properties matter and none of them are
 * things an external unpacker would have reported:
 *
 * - **Nothing partial survives.** Every entry's CRC-32 is checked against the value the central
 *   directory records for it, and a mismatch throws before the next entry is written. A tree
 *   that is missing half of `python313.zip` looks exactly like a complete one from the outside —
 *   right names, plausible sizes — and fails later as an import error on somebody's machine.
 * - **Nothing escapes.** An entry name is a path chosen by whoever built the archive. Absolute
 *   names, `..` segments, backslashes and drive letters are all refused by name before any
 *   directory is created, and the resolved path is then checked against the destination anyway.
 * - **Nothing is guessed.** An archive this reader does not fully understand — zip64, an
 *   encrypted entry, a symbolic link, a compression method other than store or deflate, or a
 *   central directory holding more headers than the end record declares — is a hard refusal
 *   rather than a partial unpack.
 *
 * The central directory is the authority for every entry, never the local header: the local
 * header's sizes and CRC are permitted to be zero when a data descriptor follows, so a reader
 * that trusts them can be handed an entry that verifies against nothing.
 */

import { crc32, inflateRawSync } from "node:zlib";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve, sep } from "node:path";

/** End of central directory record: "PK\5\6". */
const END_OF_CENTRAL_DIRECTORY = 0x0605_4b50;

/** Central directory file header: "PK\1\2". */
const CENTRAL_FILE_HEADER = 0x0201_4b50;

/** Local file header: "PK\3\4". */
const LOCAL_FILE_HEADER = 0x0403_4b50;

/** Stored, i.e. not compressed at all. */
const METHOD_STORE = 0;

/** Deflate, which is every other entry in practice. */
const METHOD_DEFLATE = 8;

/** Bit 0 of the general purpose flags: the entry is encrypted. */
const FLAG_ENCRYPTED = 0x0001;

/**
 * The largest an end-of-central-directory record can be: 22 fixed bytes plus a comment of up to
 * 65535. Scanning further back than this cannot find a real one.
 */
const MAX_END_RECORD = 22 + 0xffff;

/** A value a zip32 field uses to mean "the real value is in a zip64 record". */
const ZIP64_SENTINEL_16 = 0xffff;
const ZIP64_SENTINEL_32 = 0xffff_ffff;

/** The high byte of `version made by`, when the archive was written on a Unix host. */
const MADE_BY_UNIX = 3;

/** The `st_mode` bits that say what kind of file an entry is, and the one that says link. */
const UNIX_FILE_TYPE = 0xf000;
const UNIX_SYMLINK = 0xa000;

/** One entry, as the central directory describes it. */
export type ZipEntry = {
	/** The name recorded in the archive, exactly as stored, with `/` separators. */
	readonly name: string;
	/** `0` (stored) or `8` (deflate); anything else is refused before an entry is built. */
	readonly method: number;
	/** The CRC-32 the archive claims, which the extracted bytes have to reproduce. */
	readonly crc: number;
	readonly compressedSize: number;
	readonly uncompressedSize: number;
	/** Where this entry's local header starts, which is where its data is found. */
	readonly headerOffset: number;
	/** True when the name ends in `/`, which is how a zip records a directory. */
	readonly directory: boolean;
};

/**
 * True when the central directory records this entry as a symbolic link.
 *
 * A zip stores a link as an ordinary member whose *contents* are the target path, with the type
 * carried in the Unix mode bits of the external attributes — which only mean anything when the
 * archive says it was written on a Unix host. A reader that ignores them writes a regular file
 * holding a path, which is a tree that looks complete and is not: `python.exe` as a 12-byte text
 * file passes every count and every CRC. Refusing is the honest answer for this reader, which
 * unpacks one official Windows distribution and has no business creating links at all.
 */
export function isSymlinkEntry(madeBy: number, externalAttributes: number): boolean {
	if (madeBy >> 8 !== MADE_BY_UNIX) return false;
	return ((externalAttributes >>> 16) & UNIX_FILE_TYPE) === UNIX_SYMLINK;
}

function malformed(because: string, cause?: Error): Error {
	const message =
		`this is not an archive that can be unpacked safely: ${because}. Nothing was extracted. ` +
		"The download passed its sha256, so reaching here means the pinned digest names " +
		"something that is not the archive it is filed as.";
	return cause === undefined ? new Error(message) : new Error(message, { cause });
}

function u16(view: DataView, offset: number): number {
	if (offset + 2 > view.byteLength) throw malformed("it ends in the middle of a header");
	return view.getUint16(offset, true);
}

function u32(view: DataView, offset: number): number {
	if (offset + 4 > view.byteLength) throw malformed("it ends in the middle of a header");
	return view.getUint32(offset, true);
}

/** Where the end-of-central-directory record starts, searched backwards from the last byte. */
function endRecordOffset(view: DataView): number {
	const earliest = Math.max(0, view.byteLength - MAX_END_RECORD);
	for (let offset = view.byteLength - 22; offset >= earliest; offset -= 1) {
		if (view.getUint32(offset, true) === END_OF_CENTRAL_DIRECTORY) return offset;
	}
	throw malformed("it has no end-of-central-directory record, so it is not a zip at all");
}

/**
 * Every entry the archive's central directory names.
 *
 * The count is returned as a list rather than a map because the count itself is a check: the
 * caller knows how many files an official distribution holds, and an archive that unpacks
 * cleanly but holds nine entries where thirty-four were expected is a substitution, not a
 * corruption, and no per-entry check would catch it.
 */
export function readZipEntries(archive: Uint8Array): readonly ZipEntry[] {
	const view = new DataView(archive.buffer, archive.byteOffset, archive.byteLength);
	const end = endRecordOffset(view);
	const count = u16(view, end + 10);
	const directoryOffset = u32(view, end + 16);
	if (count === ZIP64_SENTINEL_16 || directoryOffset === ZIP64_SENTINEL_32) {
		throw malformed(
			"it is a zip64 archive, which this reader does not implement — an official CPython " +
				"embeddable distribution is around 11 MB and never needs zip64",
		);
	}

	const entries: ZipEntry[] = [];
	let cursor = directoryOffset;
	for (let index = 0; index < count; index += 1) {
		if (u32(view, cursor) !== CENTRAL_FILE_HEADER) {
			throw malformed(`its central directory entry ${index} does not start with a file header`);
		}
		const madeBy = u16(view, cursor + 4);
		const flags = u16(view, cursor + 8);
		const method = u16(view, cursor + 10);
		const crc = u32(view, cursor + 16);
		const compressedSize = u32(view, cursor + 20);
		const uncompressedSize = u32(view, cursor + 24);
		const nameLength = u16(view, cursor + 28);
		const extraLength = u16(view, cursor + 30);
		const commentLength = u16(view, cursor + 32);
		const externalAttributes = u32(view, cursor + 38);
		const headerOffset = u32(view, cursor + 42);
		const nameStart = cursor + 46;
		if (nameStart + nameLength > archive.byteLength) {
			throw malformed(`its central directory entry ${index} names a file it has no room for`);
		}
		const name = Buffer.from(archive.subarray(nameStart, nameStart + nameLength)).toString("utf8");
		if ((flags & FLAG_ENCRYPTED) !== 0) {
			throw malformed(`"${name}" is encrypted, and an official distribution never is`);
		}
		if (method !== METHOD_STORE && method !== METHOD_DEFLATE) {
			throw malformed(`"${name}" uses compression method ${method}, not store or deflate`);
		}
		if (isSymlinkEntry(madeBy, externalAttributes)) {
			throw malformed(
				`"${name}" is a symbolic link, and this reader writes files. Flattening one produces ` +
					"a regular file whose contents are the path it pointed at — a tree that passes " +
					"every count and every CRC while holding a `python.exe` that is a line of text",
			);
		}
		// The offset field carries the sentinel for the same reason the sizes do, and it is the
		// one that decides where the data is read from: an unread zip64 record here means every
		// entry is unpacked from whatever happens to lie at 0xffffffff.
		if (
			compressedSize === ZIP64_SENTINEL_32 ||
			uncompressedSize === ZIP64_SENTINEL_32 ||
			headerOffset === ZIP64_SENTINEL_32
		) {
			throw malformed(`"${name}" carries its real size or offset in a zip64 record, which is not read here`);
		}
		entries.push({
			name,
			method,
			crc,
			compressedSize,
			uncompressedSize,
			headerOffset,
			directory: name.endsWith("/"),
		});
		cursor = nameStart + nameLength + extraLength + commentLength;
	}

	// The end record's count is what the loop above trusted, and trusting it is only safe if it
	// is also the number of headers the archive actually holds. An archive declaring one entry
	// while carrying two unpacks the first and ignores the second without a word — which is a
	// member smuggled past the pinned entry count, past the CRC of everything that *was* read,
	// and into a tree nobody counted it into.
	if (cursor + 4 <= archive.byteLength && u32(view, cursor) === CENTRAL_FILE_HEADER) {
		throw malformed(`its central directory holds more headers than the ${count} its end record declares`);
	}
	return entries;
}

/**
 * The entry's bytes, proven to reproduce the CRC-32 the central directory recorded.
 *
 * This is the check that a partially-written or substituted member cannot pass. It is done per
 * entry rather than once over the archive because the archive's own digest is already checked
 * before anything is opened — what is left to establish is that each member came out of the
 * decompressor whole, which the archive digest says nothing about.
 */
export function readZipEntryData(archive: Uint8Array, entry: ZipEntry): Uint8Array {
	const view = new DataView(archive.buffer, archive.byteOffset, archive.byteLength);
	if (u32(view, entry.headerOffset) !== LOCAL_FILE_HEADER) {
		throw malformed(`"${entry.name}" does not start with a local file header`);
	}
	// The local header repeats the name and the sizes, and both are allowed to disagree with the
	// central directory when a data descriptor follows. Only its two length fields are read, and
	// only to find where the data begins.
	const nameLength = u16(view, entry.headerOffset + 26);
	const extraLength = u16(view, entry.headerOffset + 28);
	const start = entry.headerOffset + 30 + nameLength + extraLength;
	const finish = start + entry.compressedSize;
	if (finish > archive.byteLength) {
		throw malformed(`"${entry.name}" runs past the end of the archive`);
	}
	const stored = archive.subarray(start, finish);
	const data = entry.method === METHOD_STORE ? stored : new Uint8Array(inflateRawSync(stored));
	if (data.byteLength !== entry.uncompressedSize) {
		throw malformed(
			`"${entry.name}" unpacked to ${data.byteLength} bytes where the archive records ` +
				`${entry.uncompressedSize}`,
		);
	}
	const actual = crc32(data);
	if (actual !== entry.crc) {
		throw malformed(
			`"${entry.name}" failed its CRC-32: the archive records ${entry.crc.toString(16)}, the ` +
				`bytes hash to ${actual.toString(16)}`,
		);
	}
	return data;
}

/**
 * Where `name` may be written under `destination`, or a refusal.
 *
 * An archive entry's name is chosen by whoever built the archive, so it is checked twice and
 * both checks matter. The first is by name, because `..` and a leading `/` say what was
 * intended and can be reported plainly. The second is by resolved path, because the first is a
 * blocklist and the second is the property actually wanted. The trailing separator is not a
 * detail: without it `/out/python-x86_64` is a prefix of `/out/python-x86_64-evil`, and a
 * sibling directory passes as containment.
 */
export function entryPath(destination: string, name: string): string {
	const root = resolve(destination);
	if (name === "" || name.startsWith("/") || name.startsWith("\\")) {
		throw malformed(`"${name}" is an absolute path, and an archive may not choose one`);
	}
	if (/^[A-Za-z]:/u.test(name)) {
		throw malformed(`"${name}" names a Windows drive, and an archive may not choose one`);
	}
	if (name.includes("\\")) {
		throw malformed(`"${name}" contains a backslash, which is not a zip path separator`);
	}
	if (name.split("/").includes("..")) {
		throw malformed(`"${name}" walks out of the directory it is being unpacked into`);
	}
	return containedPath(root, name);
}

/**
 * `name` resolved under `root`, or a refusal — the containment rule on its own.
 *
 * Split out of `entryPath` rather than inlined into it for the reason `node-runtime.ts` splits
 * `pinnedDigestFor` out of `pinnedDigest`: the branch that matters has to be reachable, and
 * therefore testable, by something. `entryPath` refuses `..` by name first, so its own call here
 * can no longer fail — which would make the containment check a check nobody can prove runs.
 * The property is worth stating where loosening the name rules cannot quietly undo it, and worth
 * exercising directly.
 *
 * The trailing separator is the whole of it. Without it `/out/python-x86_64` is a prefix of
 * `/out/python-x86_64-evil`, and a sibling directory passes as containment.
 */
export function containedPath(root: string, name: string): string {
	const directory = resolve(root);
	const path = resolve(directory, name);
	if (!path.startsWith(directory + sep)) {
		throw malformed(`"${name}" resolves to ${path}, which is outside ${directory}`);
	}
	return path;
}

/**
 * Unpack every file in `archive` under `destination`, returning how many were written.
 *
 * Directories are created from the files that need them rather than from the archive's own
 * directory entries, so an archive that omits them — or names one after the file inside it —
 * still unpacks. Nothing here is incremental: the caller unpacks into a scratch directory and
 * moves it into place, so a throw part way through leaves a partial tree that is never seen.
 */
export function extractZip(archive: Uint8Array, destination: string): number {
	const entries = readZipEntries(archive);
	let written = 0;
	for (const entry of entries) {
		if (entry.directory) {
			place(entry.name, () => mkdirSync(entryPath(destination, entry.name.slice(0, -1)), { recursive: true }));
			continue;
		}
		const path = entryPath(destination, entry.name);
		const data = readZipEntryData(archive, entry);
		place(entry.name, () => {
			mkdirSync(dirname(path), { recursive: true });
			writeFileSync(path, data);
		});
		written += 1;
	}
	return written;
}

/**
 * Run one write, and say what an archive did rather than what a syscall answered.
 *
 * An archive that names `a` as a file and `a/b` as a file under it, or `a/` as a directory and
 * `a` as a file, is a malformed archive — and until this was here it surfaced as a bare
 * `EEXIST: file already exists, mkdir '/tmp/.../a'` or `EISDIR: illegal operation on a
 * directory`, an errno from the middle of a build step with no mention of the archive it came
 * from. The errno is kept as the cause, because it is the useful half for whoever has to look.
 */
function place(name: string, write: () => void): void {
	try {
		write();
	} catch (caught) {
		throw malformed(
			`"${name}" cannot be written where the archive puts it — the same path is claimed twice, ` +
				"once as a file and once as a directory",
			caught instanceof Error ? caught : new Error(String(caught)),
		);
	}
}
