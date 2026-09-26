/**
 * A deliberately small YAML writer for the three files a Profile is made of.
 *
 * This is not a YAML implementation and does not want to be one. It emits the subset the
 * Profile loader reads back — mappings, lists, and scalars — and it emits every string as a
 * double-quoted scalar. Quoting everything costs some prettiness and buys the property that
 * matters when a person's own words are being written to disk: nothing they typed can change
 * the *shape* of the file. `no`, `~`, `12:30`, `- ` and a leading `%` are all just text.
 *
 * There is no reader here on purpose. Reading a Profile back is the Python loader's job and
 * it is the arbiter of what a Profile means; a second parser in a second language would be a
 * second opinion. The one YAML reader this surface does have, `profile-form.ts`, is for
 * showing an existing Profile as fields and patching what changed; it decides nothing, and
 * what it writes still goes through the loader's read-back before it is renamed into place.
 */

import type { JsonMapping, JsonValue } from "./json.ts";
import { asList, asMapping, keysOf } from "./json.ts";

/** Deep enough for any Profile the loader defines, shallow enough that no payload can recurse away. */
export const MAXIMUM_DEPTH = 24;

export class YamlWriteError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "YamlWriteError";
	}
}

/**
 * Codepoints the Profile loader cannot read out of a file literally, whatever they mean.
 *
 * This is measured against PyYAML 6.0.3, which is the loader
 * (`src/venator/profile/loader.py` calls `yaml.safe_load`), and it is the correction to a
 * claim this module used to make. Two disjoint reasons, both of them the *stream* being read
 * rather than the escape syntax:
 *
 * - **U+007F–U+009F, U+FFFE and U+FFFF are non-printable to PyYAML's reader.** It refuses the
 *   whole document with a `ReaderError` — "unacceptable character" — before parsing begins.
 * - **U+0085 is a line break to PyYAML, and so are U+2028 and U+2029.** U+0085 sits inside the
 *   range above; the other two are readable but fold. Inside a double-quoted scalar a line
 *   break ends the line, so a value carrying one is silently folded to a space or, in a key,
 *   makes the document a `ScannerError` outright.
 *
 * Everything from U+0000 to U+001F is escaped by `JSON.stringify` already and needs no help
 * here; the rest of this range does not, because those codepoints are all ≥ 0x20.
 */
const UNREADABLE_TO_THE_LOADER = /[\u007F-\u009F\u2028\u2029\uFFFE\uFFFF]/gu;

/**
 * A codepoint with no character behind it, which no Profile file may hold.
 *
 * A lone surrogate — one half of a pair, with no other half — is a legal JavaScript string and
 * has no UTF-8 encoding at all. `JSON.parse` mints one from a crafted `"\ud800"` escape, so it
 * arrives by a hand-written request and never by a paste, and **nothing downstream catches
 * it**: this emitter escapes it to ASCII, PyYAML reads that escape back, and the load-back
 * therefore answers that the Profile loads. What it reaches is `filters_version()`, which
 * re-serializes `targeting.yaml` through `json.dumps(...).encode("utf-8")` and raises
 * `UnicodeEncodeError: surrogates not allowed` — so every `match.run` for that Profile fails
 * from then on, and `sqlite3` would refuse the same string in `view.build`.
 *
 * It lives here rather than in either route because **both** routes need it and one registry
 * is the rule this module already states about itself: `/profile` writes a Profile's whole
 * text and `/employers` appends two blocks to one file of one, and the same crafted request
 * disables the Match stage either way.
 *
 * Deliberately not escaped here, the way `UNREADABLE_TO_THE_LOADER` is. Escaping would be
 * correct YAML and would still write a Profile the pipeline cannot run on; the honest answer
 * is to refuse the input while somebody can still be told. With the `u` flag this matches a
 * *lone* surrogate only — a real astral character is one codepoint and is not in `\p{Cs}` — so
 * an employer name carrying an emoji or a plane-2 ideograph is untouched.
 */
export const LONE_SURROGATE = /\p{Cs}/u;

/**
 * The plain words PyYAML resolves to something that is not text.
 *
 * PyYAML is YAML 1.1, where `yes no on off true false null` — and `~`, which no plain key can
 * be anyway — are a boolean or a null rather than a string. Emitted bare as a key that is
 * exactly what they become, so four distinct board tokens written `yes`, `no`, `on` and
 * `null` land as three keys and one employer is lost without a word. `sources.names` is what
 * Dedup resolves an employer through, and a board with no entry there drops out of every
 * duplicate group.
 *
 * PyYAML's own set is 21 exact spellings — each of those seven words all-lower, Title, and
 * ALL-CAPS — and nothing else: `yEs` loads as text, and so do a bare `y` and `n` that the
 * YAML 1.1 specification does call booleans and PyYAML does not. This pattern is deliberately
 * wider on both counts, because quoting a key that did not need it costs a pair of quotation
 * marks, and being wrong in the other direction costs somebody an employer.
 */
const RESOLVES_TO_NOT_TEXT = /^(?:y|n|yes|no|on|off|true|false|null)$/iu;

/**
 * The shape of a key that may be written without quotation marks.
 *
 * Not exported, and `quoteKey` below is: `targeting-edit.ts` writes keys into a document this
 * module did not write, and the two have to agree about the whole rule rather than about half
 * of it. They held two regular expressions with the same body until both were found to be
 * wrong, so there is now one function and no second copy of anything.
 */
const PLAIN_KEY = /^[A-Za-z_][A-Za-z0-9_.-]*$/u;

function escapeForTheLoader(character: string): string {
	// SAFETY: the character came from a match of the pattern above, so it is a single BMP
	// code unit and `codePointAt(0)` is present.
	const codepoint = character.codePointAt(0) ?? 0;
	return `\\u${codepoint.toString(16).padStart(4, "0")}`;
}

/**
 * A double-quoted YAML scalar the Profile loader reads back character for character.
 *
 * **`JSON.stringify` alone does not do this**, which is what the comment here used to say and
 * is false. JSON escapes what is below U+0020 and leaves everything above it alone; YAML's
 * reader refuses some of what is above it and folds more of it. So the JSON escaping is the
 * first half — it is a legal subset of YAML's double-quoted syntax, and every escape it emits
 * (`\"` `\\` `\b` `\f` `\n` `\r` `\t` `\uXXXX`) is one PyYAML decodes to the same character —
 * and `UNREADABLE_TO_THE_LOADER` is the other half. `\uXXXX` is the escape used for both,
 * because an escape is ASCII in the file and never reaches the reader's printability check at
 * all.
 *
 * Verified rather than reasoned: every codepoint in the Basic Multilingual Plane, in four
 * positions, as a key and as a value, round-trips through `yaml.safe_load` unchanged.
 */
export function quoteScalar(value: string): string {
	return JSON.stringify(value).replace(UNREADABLE_TO_THE_LOADER, escapeForTheLoader);
}

/**
 * Keys are quoted on the same terms as values unless they are plainly a word *and* a word
 * YAML 1.1 leaves as text. The exception is only so a written Profile stays diffable against
 * a hand-written one, and it is never worth an ambiguity: a key that resolves to a boolean or
 * a null is not the key that was asked for, and two of them can collide into one.
 */
export function quoteKey(key: string): string {
	return PLAIN_KEY.test(key) && !RESOLVES_TO_NOT_TEXT.test(key) ? key : quoteScalar(key);
}

function scalar(value: JsonValue): string | null {
	if (value === null) return "null";
	if (value === true) return "true";
	if (value === false) return "false";
	if (Object.getPrototypeOf(value) === Number.prototype) {
		const numeric = Number(value);
		if (!Number.isFinite(numeric)) {
			throw new YamlWriteError("a number that is not finite cannot be written to a Profile");
		}
		return String(numeric);
	}
	if (Object.getPrototypeOf(value) === String.prototype) return quoteScalar(String(value));
	return null;
}

function indentOf(depth: number): string {
	return "  ".repeat(depth);
}

function guardDepth(depth: number): void {
	if (depth > MAXIMUM_DEPTH) {
		throw new YamlWriteError(`a Profile may not nest deeper than ${MAXIMUM_DEPTH} levels`);
	}
}

function emitValue(value: JsonValue, depth: number, lines: string[]): void {
	guardDepth(depth);
	const list = asList(value);
	if (list !== null) {
		emitList(list, depth, lines);
		return;
	}
	const mapping = asMapping(value);
	if (mapping !== null) {
		emitMapping(mapping, depth, lines);
		return;
	}
	throw new YamlWriteError("a Profile holds only mappings, lists, text, numbers, true, false and null");
}

function emitList(list: readonly JsonValue[], depth: number, lines: string[]): void {
	guardDepth(depth);
	for (const item of list) {
		const inline = scalar(item);
		if (inline !== null) {
			lines.push(`${indentOf(depth)}- ${inline}`);
			continue;
		}
		const nestedList = asList(item);
		const nestedMapping = asMapping(item);
		if (nestedList?.length === 0) {
			lines.push(`${indentOf(depth)}- []`);
			continue;
		}
		if (nestedMapping !== null && keysOf(nestedMapping).length === 0) {
			lines.push(`${indentOf(depth)}- {}`);
			continue;
		}
		// A nested block under its own dash: verbose, and unambiguous at every depth.
		lines.push(`${indentOf(depth)}-`);
		emitValue(item, depth + 1, lines);
	}
}

function emitMapping(mapping: JsonMapping, depth: number, lines: string[]): void {
	guardDepth(depth);
	for (const key of keysOf(mapping)) {
		const value = mapping[key] ?? null;
		const inline = scalar(value);
		if (inline !== null) {
			lines.push(`${indentOf(depth)}${quoteKey(key)}: ${inline}`);
			continue;
		}
		const list = asList(value);
		if (list !== null) {
			if (list.length === 0) {
				lines.push(`${indentOf(depth)}${quoteKey(key)}: []`);
				continue;
			}
			lines.push(`${indentOf(depth)}${quoteKey(key)}:`);
			// A list under a key sits at the key's own indentation, which YAML allows and
			// every hand-written Profile in this repository uses.
			emitList(list, depth + 1, lines);
			continue;
		}
		const nested = asMapping(value);
		if (nested !== null) {
			if (keysOf(nested).length === 0) {
				lines.push(`${indentOf(depth)}${quoteKey(key)}: {}`);
				continue;
			}
			lines.push(`${indentOf(depth)}${quoteKey(key)}:`);
			emitMapping(nested, depth + 1, lines);
			continue;
		}
		throw new YamlWriteError(`the value at ${key} is not something a Profile can hold`);
	}
}

/**
 * One Profile file: a header comment naming what wrote it, then the mapping.
 *
 * The header is a comment rather than data because `filters_version` hashes what a decision
 * reads, and a comment in a file with an exclusion is dropped before hashing while a key is
 * not (coordination/CONTRACTS.md).
 */
export function writeYamlDocument(mapping: JsonMapping, header: readonly string[]): string {
	const lines = header.map((line) => (line === "" ? "#" : `# ${line}`));
	if (keysOf(mapping).length === 0) {
		lines.push("{}");
	} else {
		emitMapping(mapping, 0, lines);
	}
	return `${lines.join("\n")}\n`;
}
