/**
 * Appending employers to a `targeting.yaml` that already exists, as an edit to its text.
 *
 * **This is not a YAML implementation and it is not a Profile reader.** `src/venator/profile/`
 * is the arbiter of what a Profile means, and a second parser in a second language would be a
 * second opinion. What this does is far smaller: it finds the two blocks it is allowed to
 * touch — `sources.names` and `sources.boards` — appends to them, and leaves every other byte
 * of the document exactly as it found it, comments and all. Re-emitting the file would need a
 * reader; appending to it does not.
 *
 * **It refuses far more readily than it guesses.** The shapes it recognises are the ones
 * `yaml.ts` writes and the ones the Profiles in this repository are written in: block
 * mappings, a list under its key, and an empty collection written `{}` or `[]`. Anything else
 * — a flow mapping with entries in it, an anchor, an alias, a tab where an indent should be,
 * a document this cannot walk from the top — is reported as a shape it will not edit rather
 * than being rewritten on a guess. A Profile is somebody's own policy and a wrong edit to it
 * is silent: it changes which Postings live and die.
 *
 * **Two things it deliberately does not do.** It never renames an employer already registered
 * — a token already under `names` keeps the name it has, and that is reported rather than
 * done quietly — and it never touches anything outside `sources`, so the search terms, the
 * Hard Filters are as unreadable to it as they are to the dashboard.
 *
 * **On `filters_version`.** `sources.boards` is excluded from the hash
 * (`store.NON_DECIDING_BLOCKS`) and `sources.names` is not, so registering an employer moves
 * the version and the next `match.run` re-decides the corpus. That is correct and is what
 * CLAUDE.md says it is: Dedup resolves an ATS Posting's employer through that map, so a
 * Posting decided before the name existed was decided against a different registry.
 */

import type { EmployerRegistration } from "../../shared/onboarding.ts";
import { quoteKey, quoteScalar } from "./yaml.ts";

/**
 * A document this will not edit, and why in the file's own terms.
 *
 * Every message describes how the document is written and never quotes a line of it: a
 * targeting file holds one person's own policy, and a refusal is read on a screen.
 */
export class UneditableTargeting extends Error {
	constructor(message: string) {
		super(message);
		this.name = "UneditableTargeting";
	}
}

export type TargetingEdit = {
	/** The whole document, with the entries appended. Identical when nothing was added. */
	readonly text: string;
	readonly added: readonly EmployerRegistration[];
	readonly alreadyRegistered: readonly EmployerRegistration[];
	/** Board tokens the Profile registers once this text is on disk, across every adapter. */
	readonly registered: number;
	readonly warnings: readonly string[];
};

function isBlank(raw: string): boolean {
	return raw.trim() === "";
}

function isComment(raw: string): boolean {
	return raw.trimStart().startsWith("#");
}

/** Blank lines and comment lines carry no structure, so nothing is decided from them. */
function isStructural(raw: string): boolean {
	return !isBlank(raw) && !isComment(raw);
}

/**
 * How deep a line sits, or the refusal it is.
 *
 * A tab in the leading whitespace is refused rather than counted: YAML forbids one there, and
 * a file that has one is a file whose indentation this cannot reckon at all.
 */
function indentOf(raw: string): number {
	let indent = 0;
	for (const character of raw) {
		if (character === " ") {
			indent += 1;
			continue;
		}
		if (character === "\t") {
			throw new UneditableTargeting("it indents a line with a tab, which is not something YAML allows");
		}
		break;
	}
	return indent;
}

type KeyLine = {
	readonly key: string;
	/** Whatever followed the colon, trimmed. May be a value, a comment, or nothing. */
	readonly rest: string;
};

/**
 * The one-character escapes a double-quoted scalar may carry.
 *
 * Exactly the ones `yaml.ts` can emit, which are JSON's, and every one of them is an escape
 * PyYAML decodes to the same character. Anything else is a document this did not write, and it
 * is left unread rather than reinterpreted.
 */
const DOUBLE_QUOTED_ESCAPES = new Map<string, string>([
	['"', '"'],
	["\\", "\\"],
	["/", "/"],
	["n", "\n"],
	["t", "\t"],
	["r", "\r"],
	["b", "\b"],
	["f", "\f"],
]);

/**
 * The numeric escapes, and how many hexadecimal digits each takes.
 *
 * `yaml.ts` emits `\uXXXX` for every codepoint the Profile loader will not read out of a file
 * literally — PyYAML's non-printable range and the three characters it folds as line breaks —
 * so a document this module wrote can carry one and this module has to be able to read it
 * back. `\x` and `\U` are here because PyYAML accepts them and a hand-written Profile may use
 * them; reading a board token wrong is not a risk worth taking to save two entries.
 */
const NUMERIC_ESCAPES = new Map<string, number>([
	["x", 2],
	["u", 4],
	["U", 8],
]);

type QuotedScalar = { readonly value: string; readonly after: string };

/** A quoted scalar, read the way it was written. Returns the value and what follows it. */
function readQuoted(text: string): QuotedScalar | null {
	const opener = text[0];
	if (opener !== '"' && opener !== "'") return null;
	let value = "";
	let index = 1;
	while (index < text.length) {
		const character = text[index] ?? "";
		if (opener === '"' && character === "\\") {
			const escaped = text[index + 1];
			if (escaped === undefined) return null;
			const digits = NUMERIC_ESCAPES.get(escaped);
			if (digits !== undefined) {
				const hexadecimal = text.slice(index + 2, index + 2 + digits);
				// Read strictly: `parseInt` would happily take the first digit of a truncated
				// escape and call it a codepoint, and a board token read half-right is worse
				// than a document this refuses.
				if (!new RegExp(`^[0-9A-Fa-f]{${String(digits)}}$`, "u").test(hexadecimal)) return null;
				const codepoint = Number.parseInt(hexadecimal, 16);
				if (codepoint > 0x10ffff) return null;
				value += String.fromCodePoint(codepoint);
				index += 2 + digits;
				continue;
			}
			const replacement = DOUBLE_QUOTED_ESCAPES.get(escaped);
			if (replacement === undefined) return null;
			value += replacement;
			index += 2;
			continue;
		}
		if (character === opener) {
			// YAML's single-quoted style escapes a quote by doubling it.
			if (opener === "'" && text[index + 1] === "'") {
				value += "'";
				index += 2;
				continue;
			}
			return { value, after: text.slice(index + 1) };
		}
		value += character;
		index += 1;
	}
	return null;
}

/**
 * One `key: value` line, or null when the line is not one.
 *
 * Plain keys are read up to the first `: ` or a colon that ends the line, which is YAML's own
 * rule for where a plain key stops. A key holding a character that starts something else —
 * a flow collection, an anchor, an alias, a tag — is not read as a plain key at all.
 */
function parseKeyLine(body: string): KeyLine | null {
	const quoted = readQuoted(body);
	if (quoted !== null) {
		const after = quoted.after;
		if (!after.startsWith(":")) return null;
		const rest = after.slice(1);
		if (rest !== "" && !rest.startsWith(" ")) return null;
		return { key: quoted.value, rest: rest.trim() };
	}
	const colon = body.search(/:(\s|$)/u);
	if (colon <= 0) return null;
	const key = body.slice(0, colon).trim();
	if (key === "" || /[#{}[\]&*!,]/u.test(key)) return null;
	return { key, rest: body.slice(colon + 1).trim() };
}

/** True when what followed the colon is nothing at all — a block value follows on later lines. */
function opensBlock(rest: string): boolean {
	return rest === "" || rest.startsWith("#");
}

/** True when what followed the colon is an empty collection, written either way. */
function isEmptyCollection(rest: string): boolean {
	return rest === "{}" || rest === "[]";
}

/** One `- item` line's scalar, or null when the line is not a list item this can read. */
function parseListItem(body: string): string | null {
	if (!body.startsWith("- ") && body !== "-") return null;
	const value = body.slice(1).trim();
	if (value === "" || value.startsWith("#")) return null;
	const quoted = readQuoted(value);
	if (quoted !== null) {
		const after = quoted.after.trim();
		if (after !== "" && !after.startsWith("#")) return null;
		return quoted.value;
	}
	const comment = value.search(/\s#/u);
	const plain = (comment === -1 ? value : value.slice(0, comment)).trim();
	// A plain board token and nothing else. A list item holding a mapping, a flow collection,
	// an anchor or an alias is a list this must not append a bare scalar to, so it refuses.
	if (!/^[^\s"'\\{}[\]&*!,:#]+$/u.test(plain)) return null;
	return plain;
}

type Document = {
	/**
	 * The document's lines with their own line endings still on them.
	 *
	 * Split on `\n` alone rather than on `\r?\n`, so a line that ended `\r\n` still carries its
	 * `\r` and is written back byte for byte. Splitting the `\r` off and rejoining with one
	 * terminator would rewrite the line endings of a file that mixes them — which is a small
	 * thing, and "every other byte of the document exactly as it was found" is a claim three
	 * documents in this repository make about this module.
	 */
	readonly lines: readonly string[];
	/** Whether a line this module *writes* should end `\r\n`, taken from the document's own. */
	readonly crlf: boolean;
	/** True when the text ended with a line break, so the join can put it back. */
	readonly trailingBreak: boolean;
};

function readDocument(text: string): Document {
	const split = text.split("\n");
	const trailingBreak = split.length > 1 && split[split.length - 1] === "";
	const lines = trailingBreak ? split.slice(0, -1) : split;
	const carried = lines.filter((line) => line.endsWith("\r")).length;
	return { lines, crlf: carried * 2 > lines.length, trailingBreak };
}

function writeDocument(document: Document, lines: readonly string[]): string {
	return lines.join("\n") + (document.trailingBreak ? "\n" : "");
}

/** Where a key's block ends: the first structural line at or above its own indentation. */
function blockEnd(lines: readonly string[], from: number, indent: number): number {
	let index = from;
	while (index < lines.length) {
		const raw = lines[index] ?? "";
		if (isStructural(raw) && indentOf(raw) <= indent) return index;
		index += 1;
	}
	return lines.length;
}

/** The last structural line of a range, so an insertion lands before a block's trailing comments. */
function lastStructural(lines: readonly string[], from: number, to: number): number | null {
	for (let index = to - 1; index >= from; index -= 1) {
		if (isStructural(lines[index] ?? "")) return index;
	}
	return null;
}

/** The entries of one block mapping, and the column they are written at. */
type Block = { readonly indent: number; readonly entries: readonly Entry[] };

/** The scalars of one list, and the column they are written at. */
type Items = { readonly indent: number; readonly items: readonly string[] };

/** A `sources` written `{}`: no entries, and nothing yet to take an indentation from. */
const NO_ENTRIES: Block = { indent: -1, entries: [] };

type Entry = {
	readonly key: string;
	readonly rest: string;
	readonly line: number;
	/** One past the last line of this entry's own block. */
	readonly end: number;
};

/**
 * The entries of one block mapping, at one indentation.
 *
 * A structural line inside the range that is neither an entry at this indentation nor part of
 * one is a shape this does not recognise, and it refuses rather than skipping past it — the
 * whole value of walking the file is that what it did not understand is what it says.
 */
function entriesOf(lines: readonly string[], from: number, to: number, what: string): Block {
	let indent = -1;
	const entries: Entry[] = [];
	let index = from;
	while (index < to) {
		const raw = lines[index] ?? "";
		if (!isStructural(raw)) {
			index += 1;
			continue;
		}
		const at = indentOf(raw);
		if (indent === -1) indent = at;
		if (at !== indent) {
			throw new UneditableTargeting(`the entries under ${what} are not all indented the same way`);
		}
		const parsed = parseKeyLine(raw.trim());
		if (parsed === null) {
			throw new UneditableTargeting(`something under ${what} is not a plain \`key: value\` entry`);
		}
		const end = opensBlock(parsed.rest) ? blockEnd(lines, index + 1, at) : index + 1;
		entries.push({ key: parsed.key, rest: parsed.rest, line: index, end });
		index = end;
	}
	return { indent, entries };
}

/** The scalars of a list written under its key, and the indentation they are written at. */
function listItemsOf(lines: readonly string[], from: number, to: number, what: string): Items {
	let indent = -1;
	const items: string[] = [];
	for (let index = from; index < to; index += 1) {
		const raw = lines[index] ?? "";
		if (!isStructural(raw)) continue;
		const at = indentOf(raw);
		if (indent === -1) indent = at;
		if (at !== indent) {
			throw new UneditableTargeting(`the boards under ${what} are not all indented the same way`);
		}
		const item = parseListItem(raw.trim());
		if (item === null) {
			throw new UneditableTargeting(`something under ${what} is not a board written as a list item`);
		}
		items.push(item);
	}
	return { indent, items };
}

/** One insertion or replacement, applied from the bottom of the file up so indices hold. */
type Patch = { readonly line: number; readonly replace: boolean; readonly lines: readonly string[] };

/**
 * Applies the whole edit, bottom-up, so no patch has to be reckoned against another's effect.
 *
 * Two patches can land on one line, and the order between them is not arbitrary. A
 * *replacement* goes first: it consumes the original line, and an insertion applied before it
 * would leave the replacement eating the inserted text instead. Between two insertions, the
 * one written later in this module goes first, so that after the earlier one is applied above
 * it the two read in the order they were composed — `names` above `boards`, which is how
 * every Profile in this repository is written.
 */
function applyPatches(document: Document, patches: readonly Patch[]): readonly string[] {
	const ordered = patches.map((patch, order) => ({ patch, order }));
	ordered.sort(
		(left, right) =>
			right.patch.line - left.patch.line ||
			(right.patch.replace ? 1 : 0) - (left.patch.replace ? 1 : 0) ||
			right.order - left.order,
	);
	const result = [...document.lines];
	for (const { patch } of ordered) {
		const written = document.crlf ? patch.lines.map((line) => `${line}\r`) : patch.lines;
		result.splice(patch.line, patch.replace ? 1 : 0, ...written);
	}
	return result;
}

function spaces(count: number): string {
	return " ".repeat(count);
}

/**
 * Registers employers into a targeting document, and answers with what it changed.
 *
 * The whole edit is worked out before a byte of it is applied, so a document this refuses is
 * one it has not touched. Duplicates within the request collapse; a board already registered
 * under the same adapter is reported and never written twice.
 */
export function addEmployers(text: string, requested: readonly EmployerRegistration[]): TargetingEdit {
	const document = readDocument(text);
	const lines = document.lines;

	// Every top-level line has to parse, or this is not a document that can be walked from the
	// top: a document marker, a directive, a top-level sequence and a flow document all land
	// here, and every one of them is a file only a person should be editing.
	const top: Entry[] = [];
	let index = 0;
	while (index < lines.length) {
		const raw = lines[index] ?? "";
		if (!isStructural(raw) || indentOf(raw) !== 0) {
			index += 1;
			continue;
		}
		const parsed = parseKeyLine(raw);
		if (parsed === null) {
			throw new UneditableTargeting("it is not a plain mapping of `key: value` blocks at the top level");
		}
		const end = opensBlock(parsed.rest) ? blockEnd(lines, index + 1, 0) : index + 1;
		top.push({ key: parsed.key, rest: parsed.rest, line: index, end });
		index = end;
	}
	if (top.filter((entry) => entry.key === "sources").length > 1) {
		throw new UneditableTargeting("it names `sources` more than once");
	}
	const sources = top.find((entry) => entry.key === "sources");
	if (sources === undefined) {
		throw new UneditableTargeting("it has no `sources` block to register an employer in");
	}

	const patches: Patch[] = [];

	/** `sources:` written as an empty collection becomes a block, and the block is built fresh. */
	const sourcesEmpty = isEmptyCollection(sources.rest);
	if (!sourcesEmpty && !opensBlock(sources.rest)) {
		throw new UneditableTargeting("its `sources` is written on one line as something other than an empty collection");
	}
	if (sourcesEmpty) patches.push({ line: sources.line, replace: true, lines: ["sources:"] });

	const block = sourcesEmpty ? NO_ENTRIES : entriesOf(lines, sources.line + 1, sources.end, "`sources`");
	const blockIndent = block.indent === -1 ? 2 : block.indent;
	for (const key of ["names", "boards"]) {
		if (block.entries.filter((entry) => entry.key === key).length > 1) {
			throw new UneditableTargeting(`its \`sources\` names \`${key}\` more than once`);
		}
	}
	const names = block.entries.find((entry) => entry.key === "names");
	const boards = block.entries.find((entry) => entry.key === "boards");

	// What is registered now. `names` is read for its keys alone: a display name already on
	// file belongs to whoever wrote it and is never rewritten from here.
	const registeredNames = new Set<string>();
	if (names !== undefined && !isEmptyCollection(names.rest)) {
		if (!opensBlock(names.rest)) {
			throw new UneditableTargeting("its `sources.names` is written on one line as something other than an empty collection");
		}
		for (const entry of entriesOf(lines, names.line + 1, names.end, "`sources.names`").entries) {
			registeredNames.add(entry.key);
		}
	}

	const registeredBoards = new Map<string, readonly string[]>();
	const boardEntries = new Map<string, Entry>();
	/** Where the adapter keys under `boards` are written, so a new one lines up with them. */
	let boardKeyIndent = -1;
	if (boards !== undefined && !isEmptyCollection(boards.rest)) {
		if (!opensBlock(boards.rest)) {
			throw new UneditableTargeting("its `sources.boards` is written on one line as something other than an empty collection");
		}
		const under = entriesOf(lines, boards.line + 1, boards.end, "`sources.boards`");
		boardKeyIndent = under.indent;
		for (const entry of under.entries) {
			if (boardEntries.has(entry.key)) {
				throw new UneditableTargeting("its `sources.boards` names one job board twice");
			}
			boardEntries.set(entry.key, entry);
			if (isEmptyCollection(entry.rest)) {
				registeredBoards.set(entry.key, []);
				continue;
			}
			if (!opensBlock(entry.rest)) {
				throw new UneditableTargeting("a job board under `sources.boards` holds something other than a list of boards");
			}
			registeredBoards.set(entry.key, listItemsOf(lines, entry.line + 1, entry.end, "`sources.boards`").items);
		}
	}

	// What the request actually asks for, once duplicates within it have collapsed and
	// everything already on file has been set aside.
	const added: EmployerRegistration[] = [];
	const alreadyRegistered: EmployerRegistration[] = [];
	const warnings: string[] = [];
	const seen = new Set<string>();
	for (const entry of requested) {
		const pair = `${entry.source} ${entry.board}`;
		if (seen.has(pair)) continue;
		seen.add(pair);
		if ((registeredBoards.get(entry.source) ?? []).includes(entry.board)) {
			alreadyRegistered.push(entry);
			continue;
		}
		added.push(entry);
	}

	const registered =
		[...registeredBoards.values()].reduce((total, tokens) => total + tokens.length, 0) + added.length;

	if (added.length === 0) {
		return { text, added, alreadyRegistered, registered, warnings };
	}

	// `names`, first. A token already named keeps the name it has: overwriting a display name
	// somebody chose is not what "register this employer" means, so it is reported instead.
	const newNames: string[] = [];
	for (const entry of added) {
		if (registeredNames.has(entry.board)) {
			warnings.push(
				`This Profile already names the employer that board belongs to, so the name it has was kept rather than replaced by “${entry.name}”.`,
			);
			continue;
		}
		registeredNames.add(entry.board);
		newNames.push(`${quoteKey(entry.board)}: ${quoteScalar(entry.name)}`);
	}
	if (newNames.length > 0) {
		if (names === undefined) {
			// No `names` at all: it goes in at the top of the block, which is where every
			// Profile in this repository writes it and where the scaffold documents it.
			const at = sourcesEmpty ? sources.line + 1 : (block.entries[0]?.line ?? sources.line + 1);
			patches.push({
				line: at,
				replace: false,
				lines: [`${spaces(blockIndent)}names:`, ...newNames.map((line) => `${spaces(blockIndent + 2)}${line}`)],
			});
		} else if (isEmptyCollection(names.rest)) {
			const indent = indentOf(lines[names.line] ?? "");
			patches.push({
				line: names.line,
				replace: true,
				lines: [`${spaces(indent)}names:`, ...newNames.map((line) => `${spaces(indent + 2)}${line}`)],
			});
		} else {
			const inner = entriesOf(lines, names.line + 1, names.end, "`sources.names`");
			const indent = inner.indent === -1 ? indentOf(lines[names.line] ?? "") + 2 : inner.indent;
			const anchor = lastStructural(lines, names.line + 1, names.end);
			patches.push({
				line: (anchor ?? names.line) + 1,
				replace: false,
				lines: newNames.map((line) => `${spaces(indent)}${line}`),
			});
		}
	}

	// `boards`, second. Each adapter is its own list, so an employer joins the list its board
	// belongs to and a new adapter arrives as a new key.
	const bySource = new Map<string, string[]>();
	for (const entry of added) {
		const tokens = bySource.get(entry.source) ?? [];
		tokens.push(entry.board);
		bySource.set(entry.source, tokens);
	}
	const freshSources: string[] = [];
	const boardsIndent = boards === undefined ? blockIndent : indentOf(lines[boards.line] ?? "");
	const adapterIndent = boardKeyIndent === -1 ? boardsIndent + 2 : boardKeyIndent;
	for (const [source, tokens] of bySource) {
		const entry = boardEntries.get(source);
		const items = tokens.map((token) => `- ${quoteScalar(token)}`);
		if (entry === undefined) {
			freshSources.push(`${spaces(adapterIndent)}${quoteKey(source)}:`);
			freshSources.push(...items.map((line) => `${spaces(adapterIndent + 2)}${line}`));
			continue;
		}
		if (isEmptyCollection(entry.rest)) {
			const indent = indentOf(lines[entry.line] ?? "");
			patches.push({
				line: entry.line,
				replace: true,
				lines: [`${spaces(indent)}${quoteKey(source)}:`, ...items.map((line) => `${spaces(indent + 2)}${line}`)],
			});
			continue;
		}
		const existing = listItemsOf(lines, entry.line + 1, entry.end, "`sources.boards`");
		const indent = existing.indent === -1 ? indentOf(lines[entry.line] ?? "") + 2 : existing.indent;
		const anchor = lastStructural(lines, entry.line + 1, entry.end);
		patches.push({
			line: (anchor ?? entry.line) + 1,
			replace: false,
			lines: items.map((line) => `${spaces(indent)}${line}`),
		});
	}
	if (freshSources.length > 0) {
		if (boards === undefined) {
			// No `boards` at all: it goes in at the end of the `sources` block, after `names`,
			// which is the order every Profile here is written in.
			const anchor = sourcesEmpty ? sources.line : lastStructural(lines, sources.line + 1, sources.end);
			patches.push({
				line: (anchor ?? sources.line) + 1,
				replace: false,
				lines: [`${spaces(blockIndent)}boards:`, ...freshSources],
			});
		} else if (isEmptyCollection(boards.rest)) {
			patches.push({
				line: boards.line,
				replace: true,
				lines: [`${spaces(boardsIndent)}boards:`, ...freshSources],
			});
		} else {
			const anchor = lastStructural(lines, boards.line + 1, boards.end);
			patches.push({ line: (anchor ?? boards.line) + 1, replace: false, lines: freshSources });
		}
	}

	return { text: writeDocument(document, applyPatches(document, patches)), added, alreadyRegistered, registered, warnings };
}
