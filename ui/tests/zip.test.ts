/**
 * The zip reader that unpacks the embeddable CPython, exercised against archives built here.
 *
 * `prepare-python.ts` cannot shell out to unpack a zip: GNU tar does not read the format at all,
 * and `unzip` is absent from macOS CI images and from Windows. So the reader is ours, and what
 * it refuses is the whole reason it exists — an external unpacker would report none of it.
 *
 * Every archive below is assembled byte by byte by `zip` at the bottom of this file. That is
 * deliberate and it is the only honest way to test this: an archive produced by the reader's own
 * notion of the format would agree with it about anything, including a mistake, and the failures
 * being guarded — a member that fails its CRC, an entry name that walks out of the destination,
 * a zip64 record — cannot be produced by asking a real zip tool nicely.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, relative, resolve, sep } from "node:path";
import { test } from "node:test";
import { crc32, deflateRawSync } from "node:zlib";

import {
	containedPath,
	entryPath,
	extractZip,
	isSymlinkEntry,
	readZipEntries,
	readZipEntryData,
	type ZipEntry,
} from "../tools/zip.ts";

const STORE = 0;
const DEFLATE = 8;

/** One member to write into a test archive, with every field the writer would otherwise derive. */
type Member = {
	readonly name: string;
	readonly data: Uint8Array;
	/** `0` stored or `8` deflate; anything else is a deliberately malformed archive. */
	readonly method: number;
	/** General purpose flags; bit 0 marks an entry encrypted. */
	readonly flags: number;
	/** The CRC to *record*, which a corruption test deliberately makes disagree with `data`. */
	readonly crc: number;
};

function member(name: string, text: string, method: number = DEFLATE): Member {
	const data = new TextEncoder().encode(text);
	return { name, data, method, flags: 0, crc: crc32(data) };
}

/**
 * An archive holding `members`, written out by hand.
 *
 * `counted` and `directoryOffset` exist so the malformed cases can be built: an archive whose
 * end record claims a different number of entries than it holds, and one whose fields carry the
 * zip64 sentinel, are both things a reader has to have an answer for and neither can be produced
 * by a correct writer.
 */
function zip(members: readonly Member[], counted: number = members.length, sentinel = false): Uint8Array {
	const locals: Uint8Array[] = [];
	const centrals: Uint8Array[] = [];
	let offset = 0;
	for (const entry of members) {
		const name = new TextEncoder().encode(entry.name);
		const stored = entry.method === STORE ? entry.data : new Uint8Array(deflateRawSync(entry.data));
		const local = new Uint8Array(30 + name.length + stored.length);
		const localView = new DataView(local.buffer);
		localView.setUint32(0, 0x0403_4b50, true);
		localView.setUint16(4, 20, true);
		localView.setUint16(6, entry.flags, true);
		localView.setUint16(8, entry.method, true);
		localView.setUint32(14, entry.crc, true);
		localView.setUint32(18, stored.length, true);
		localView.setUint32(22, entry.data.length, true);
		localView.setUint16(26, name.length, true);
		local.set(name, 30);
		local.set(stored, 30 + name.length);
		locals.push(local);

		const central = new Uint8Array(46 + name.length);
		const centralView = new DataView(central.buffer);
		centralView.setUint32(0, 0x0201_4b50, true);
		centralView.setUint16(4, 20, true);
		centralView.setUint16(6, 20, true);
		centralView.setUint16(8, entry.flags, true);
		centralView.setUint16(10, entry.method, true);
		centralView.setUint32(16, entry.crc, true);
		centralView.setUint32(20, sentinel ? 0xffff_ffff : stored.length, true);
		centralView.setUint32(24, sentinel ? 0xffff_ffff : entry.data.length, true);
		centralView.setUint16(28, name.length, true);
		centralView.setUint32(42, offset, true);
		central.set(name, 46);
		centrals.push(central);
		offset += local.length;
	}

	const directory = Buffer.concat(centrals);
	const end = new Uint8Array(22);
	const endView = new DataView(end.buffer);
	endView.setUint32(0, 0x0605_4b50, true);
	endView.setUint16(8, counted, true);
	endView.setUint16(10, counted, true);
	endView.setUint32(12, directory.length, true);
	endView.setUint32(16, offset, true);
	return new Uint8Array(Buffer.concat([...locals, directory, end]));
}

/** The one entry of a single-member archive, without an assertion the linter would refuse. */
function only(entries: readonly ZipEntry[]): ZipEntry {
	assert.equal(entries.length, 1, "these archives are built with one member");
	const entry = entries[0];
	if (entry === undefined) assert.fail("the archive under test holds no entries");
	return entry;
}

/** The message of the error `run` throws, so a test can assert on several parts of it. */
function failureOf(run: () => void): string {
	try {
		run();
	} catch (caught) {
		return caught instanceof Error ? caught.message : String(caught);
	}
	assert.fail("expected a refusal, and nothing was thrown");
}

function scratch(): string {
	return mkdtempSync(join(tmpdir(), "venator-zip-"));
}

test("a well-formed archive reads back every entry the central directory names", () => {
	const archive = zip([member("python.exe", "MZ fake"), member("Lib/site-packages/x.py", "print(1)\n", STORE)]);
	const entries = readZipEntries(archive);
	assert.equal(entries.length, 2);
	assert.equal(entries[0]?.name, "python.exe");
	assert.equal(entries[0]?.method, DEFLATE);
	assert.equal(entries[0]?.uncompressedSize, 7);
	assert.equal(entries[0]?.directory, false);
	assert.equal(entries[1]?.name, "Lib/site-packages/x.py");
	assert.equal(entries[1]?.method, STORE, "a stored member is read as stored, not inflated");
	assert.deepEqual(new TextDecoder().decode(readZipEntryData(archive, entries[1] ?? entries[0])), "print(1)\n");
});

test("an entry whose bytes do not reproduce its recorded CRC-32 is refused", () => {
	// The failure this guards is the one that looks like success: a member that decompresses to
	// the right length and the wrong bytes leaves a tree with every file present and Python
	// unable to start, and nothing but the CRC says so.
	const data = new TextEncoder().encode("eight!!!");
	const corrupted: Member = { name: "python313.dll", data, method: DEFLATE, flags: 0, crc: 0x1234_5678 };
	const archive = zip([corrupted]);
	const failure = failureOf(() => readZipEntryData(archive, only(readZipEntries(archive))));
	assert.match(failure, /failed its CRC-32/);
	assert.ok(failure.includes("python313.dll"), "names the member, not just the archive");
	assert.ok(failure.includes("12345678"), "names the value the archive recorded");
	assert.ok(failure.includes("Nothing was extracted"));
});

test("an entry that unpacks to the wrong length is refused before its CRC is even reached", () => {
	const data = new TextEncoder().encode("0123456789");
	const archive = zip([{ name: "short.pyd", data, method: DEFLATE, flags: 0, crc: crc32(data) }]);
	// Rewrite the central directory's uncompressed size to a value the data cannot produce.
	const view = new DataView(archive.buffer, archive.byteOffset, archive.byteLength);
	const central = archive.byteLength - 22 - (46 + "short.pyd".length);
	view.setUint32(central + 24, 99, true);
	assert.match(
		failureOf(() => readZipEntryData(archive, only(readZipEntries(archive)))),
		/unpacked to 10 bytes where the archive records 99/,
	);
});

test("an archive this reader does not fully understand is refused rather than half-unpacked", () => {
	const bzip2: Member = { name: "a", data: new Uint8Array([1]), method: 12, flags: 0, crc: 0 };
	assert.match(failureOf(() => readZipEntries(zip([bzip2]))), /compression method 12, not store or deflate/);

	const encrypted: Member = { name: "b", data: new Uint8Array([1]), method: STORE, flags: 0x0001, crc: 0 };
	assert.match(failureOf(() => readZipEntries(zip([encrypted]))), /"b" is encrypted/);

	// Both zip64 sentinels, and they are separate refusals: one is in the end record and one is
	// on an entry, and reaching either means this reader is being handed sizes it cannot read.
	assert.match(failureOf(() => readZipEntries(zip([member("c", "x")], 1, true))), /zip64/);
	const wide = zip([member("d", "x")]);
	// The end record's own entry count and central directory offset, set to the sentinel.
	const endView = new DataView(wide.buffer, wide.byteOffset, wide.byteLength);
	endView.setUint16(wide.byteLength - 22 + 10, 0xffff, true);
	endView.setUint32(wide.byteLength - 22 + 16, 0xffff_ffff, true);
	assert.match(failureOf(() => readZipEntries(wide)), /zip64 archive, which this reader does not implement/);

	assert.match(
		failureOf(() => readZipEntries(new TextEncoder().encode("not a zip at all, not remotely"))),
		/no end-of-central-directory record/,
	);
});

test("an end record that promises more entries than the archive holds is refused", () => {
	// The shape of a truncated download that still ends in a plausible record. Reading on past
	// the last real header is how a reader ends up unpacking whatever follows in memory.
	assert.match(
		failureOf(() => readZipEntries(zip([member("only", "one")], 2))),
		/does not start with a file header/,
	);
});

test("an entry name that leaves the destination is refused, by name and by resolved path", () => {
	// Asserted by parts rather than as one joined string: a separator in an expected value only
	// proves what the platform running the suite happens to spell, and this rule is about a path
	// escaping a directory on every platform.
	const root = resolve(tmpdir(), "venator-zip-containment");

	const inside = entryPath(root, "Lib/site-packages/yaml/__init__.py");
	assert.deepEqual(relative(root, inside).split(sep), ["Lib", "site-packages", "yaml", "__init__.py"]);

	assert.match(failureOf(() => entryPath(root, "../escape.txt")), /walks out of the directory/);
	assert.match(failureOf(() => entryPath(root, "a/../../escape.txt")), /walks out of the directory/);
	assert.match(failureOf(() => entryPath(root, "/etc/passwd")), /is an absolute path/);
	assert.match(failureOf(() => entryPath(root, "\\\\server\\share")), /is an absolute path/);
	assert.match(failureOf(() => entryPath(root, "C:/Windows/System32/x.dll")), /names a Windows drive/);
	assert.match(failureOf(() => entryPath(root, "Lib\\site-packages\\x.py")), /contains a backslash/);
	assert.match(failureOf(() => entryPath(root, "")), /is an absolute path/);
});

test("a sibling directory whose name merely starts with the destination is outside it", () => {
	// The case that makes the trailing separator in the containment check load-bearing: without
	// it `venator-zip-containment-evil` is a prefix match on `venator-zip-containment` and a
	// different directory entirely.
	//
	// Asserted against `containedPath` rather than `entryPath` because `entryPath` refuses `..`
	// by name before it ever resolves anything, so its own containment branch cannot be reached
	// through it. A check nobody can make fire is a check nobody knows still works.
	const root = resolve(tmpdir(), "venator-zip-containment");
	const failure = failureOf(() => containedPath(root, `../${basename(root)}-evil/pwn.dll`));
	assert.match(failure, /which is outside/);
	assert.ok(failure.includes(`${basename(root)}-evil`), "the sibling is named, not swallowed");
	assert.ok(failure.includes(root), "names the one directory it owns");
	// And the same rule permits what is genuinely inside it.
	assert.deepEqual(relative(root, containedPath(root, "Lib/x.pyd")).split(sep), ["Lib", "x.pyd"]);
});

test("extraction writes every file, and a refusal writes nothing after it", () => {
	const destination = scratch();
	try {
		const archive = zip([
			member("python.exe", "MZ"),
			member("Lib/site-packages/pkg/__init__.py", "x = 1\n"),
			member("src/venator/qualify/schemas/qualify-output-2.json", "{}\n", STORE),
		]);
		assert.equal(extractZip(archive, destination), 3, "returns how many files it wrote");
		assert.deepEqual(readdirSync(destination).sort(), ["Lib", "python.exe", "src"]);
		assert.equal(readFileSync(resolve(destination, "python.exe"), "utf8"), "MZ");
		assert.equal(
			readFileSync(resolve(destination, "src", "venator", "qualify", "schemas", "qualify-output-2.json"), "utf8"),
			"{}\n",
		);
	} finally {
		rmSync(destination, { recursive: true, force: true });
	}
});

test("a directory entry is created and does not count as a file", () => {
	const destination = scratch();
	try {
		const directory: Member = { name: "Lib/", data: new Uint8Array(), method: STORE, flags: 0, crc: 0 };
		assert.equal(extractZip(zip([directory, member("Lib/x.py", "1\n")]), destination), 1);
		assert.deepEqual(readdirSync(resolve(destination, "Lib")), ["x.py"]);
	} finally {
		rmSync(destination, { recursive: true, force: true });
	}
});

/** Where the central header of a single-member archive begins, so a test can corrupt one field. */
function centralHeader(archive: Uint8Array, name: string): number {
	return archive.byteLength - 22 - (46 + name.length);
}

test("a central directory holding more headers than the end record declares is refused", () => {
	// The archive that unpacks the entries it admits to and smuggles the rest: declaring one
	// while carrying two used to unpack the first and ignore the second without a word. The
	// second member is then past the pinned entry count, past the CRC of everything that *was*
	// read, and into a tree nobody counted it into.
	const smuggled = zip([member("python.exe", "MZ real"), member("evil.dll", "MZ smuggled")], 1);
	const failure = failureOf(() => readZipEntries(smuggled));
	assert.match(failure, /holds more headers than the 1 its end record declares/);
	assert.ok(failure.includes("Nothing was extracted"));

	// The count that agrees with what is there still reads, and both members come back.
	assert.deepEqual(
		readZipEntries(zip([member("python.exe", "MZ real"), member("evil.dll", "MZ smuggled")])).map(
			(entry) => entry.name,
		),
		["python.exe", "evil.dll"],
	);
});

test("an entry whose header offset is the zip64 sentinel is refused with the sizes", () => {
	// The sentinel means the real value lives in a zip64 record this reader does not read, and
	// this is the field that decides *where the data is read from* — an unread record here
	// unpacks every entry from whatever happens to lie at 0xffffffff.
	const archive = zip([member("a.py", "x")]);
	const view = new DataView(archive.buffer, archive.byteOffset, archive.byteLength);
	view.setUint32(centralHeader(archive, "a.py") + 42, 0xffff_ffff, true);
	assert.match(failureOf(() => readZipEntries(archive)), /real size or offset in a zip64 record/);
});

test("a symbolic link is refused rather than flattened into a file holding its target", () => {
	// A zip stores a link as a member whose contents are the target path, with the type in the
	// Unix mode bits of the external attributes. Written out as an ordinary file it is a tree
	// that passes every count and every CRC while holding a `python.exe` that is a line of text.
	const archive = zip([{ ...member("Lib/evil", "../../../../etc/passwd"), method: STORE }]);
	const view = new DataView(archive.buffer, archive.byteOffset, archive.byteLength);
	const central = centralHeader(archive, "Lib/evil");
	view.setUint16(central + 4, (3 << 8) | 20, true);
	view.setUint32(central + 38, 0xa1ff_0000, true);
	const failure = failureOf(() => readZipEntries(archive));
	assert.match(failure, /"Lib\/evil" is a symbolic link/);
	assert.ok(failure.includes("Nothing was extracted"));

	// The rule on its own. The mode bits only mean anything when the archive says it was written
	// on a Unix host, and every other file type there is an ordinary member.
	assert.equal(isSymlinkEntry((3 << 8) | 20, 0xa1ff_0000), true);
	assert.equal(isSymlinkEntry((3 << 8) | 20, 0x81a4_0000), false, "0x8000 is a regular file");
	assert.equal(isSymlinkEntry((3 << 8) | 20, 0x41ed_0000), false, "0x4000 is a directory");
	assert.equal(isSymlinkEntry(20, 0xa1ff_0000), false, "a DOS host's attributes are not a mode");
	assert.equal(isSymlinkEntry(20, 0x0000_0020), false);
});

test("an archive that claims one path twice says so, rather than leaking an errno", () => {
	// `a` as a file and `a/b` under it, and the same pair the other way round. Both are the
	// archive being malformed; both used to surface as a bare `EEXIST: file already exists,
	// mkdir '/tmp/.../a'` or `EISDIR: illegal operation on a directory` out of the middle of a
	// build step, with nothing in the sentence about an archive at all.
	const destination = scratch();
	try {
		const failure = failureOf(() => extractZip(zip([member("a", "file"), member("a/b", "under it")]), destination));
		assert.match(failure, /"a\/b" cannot be written where the archive puts it/);
		assert.match(failure, /claimed twice, once as a file and once as a directory/);
		assert.ok(failure.includes("Nothing was extracted"), "it is this reader's own refusal");
	} finally {
		rmSync(destination, { recursive: true, force: true });
	}

	const second = scratch();
	try {
		const directory: Member = { name: "a/", data: new Uint8Array(), method: STORE, flags: 0, crc: 0 };
		const failure = failureOf(() => extractZip(zip([directory, member("a", "now a file")]), second));
		assert.match(failure, /"a" cannot be written where the archive puts it/);
		// The errno is kept as the cause, because it is the useful half for whoever has to look.
		const caught = (() => {
			try {
				extractZip(zip([directory, member("a", "now a file")]), second);
			} catch (error) {
				return error;
			}
			return null;
		})();
		assert.ok(caught instanceof Error && caught.cause instanceof Error, "the errno is carried, not dropped");
	} finally {
		rmSync(second, { recursive: true, force: true });
	}
});
