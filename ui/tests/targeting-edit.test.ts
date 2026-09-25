/**
 * The one edit this dashboard makes to a Profile that already exists.
 *
 * A `targeting.yaml` is somebody's own policy: it decides which Postings live and which die,
 * and a wrong edit to it is silent. So the properties asserted here are, in order of how much
 * they matter: nothing outside `sources` moves, a document written in a way this does not
 * recognise is refused rather than rewritten, and what comes out is written exactly as
 * `yaml.ts` writes the same thing — quoted scalars, two-space indentation, a list under its
 * key — which is the form `src/venator/profile/` already reads back.
 *
 * The last two cases are the ones that matter most: they are the Profiles in this repository,
 * edited as they actually are, comments and all.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import { addEmployers, UneditableTargeting } from "../server/onboarding/targeting-edit.ts";

const SETUP_WRITTEN = [
	"# Profile: what to look for, where to look, and how Postings get judged.",
	"# Written by the onboarding flow.",
	"profile:",
	'  name: "my-search"',
	"search:",
	"  queries:",
	'    - "process development"',
	"  locations: []",
	'  remote: "acceptable"',
	"sources:",
	"  names: {}",
	"  boards: {}",
	"filters:",
	"  enabled:",
	'    - "role_target"',
	"",
].join("\n");

function edit(text: string, ...entries: readonly { source: string; board: string; name: string }[]) {
	return addEmployers(text, entries);
}

test("a Profile setup wrote with no employer gets its first one", () => {
	const result = edit(SETUP_WRITTEN, { source: "greenhouse", board: "ginkgobioworks", name: "Ginkgo Bioworks" });
	assert.deepEqual(result.added, [{ source: "greenhouse", board: "ginkgobioworks", name: "Ginkgo Bioworks" }]);
	assert.equal(result.registered, 1);
	assert.equal(
		result.text,
		[
			"# Profile: what to look for, where to look, and how Postings get judged.",
			"# Written by the onboarding flow.",
			"profile:",
			'  name: "my-search"',
			"search:",
			"  queries:",
			'    - "process development"',
			"  locations: []",
			'  remote: "acceptable"',
			"sources:",
			"  names:",
			'    ginkgobioworks: "Ginkgo Bioworks"',
			"  boards:",
			"    greenhouse:",
			'      - "ginkgobioworks"',
			"filters:",
			"  enabled:",
			'    - "role_target"',
			"",
		].join("\n"),
	);
});

test("nothing outside the sources block moves, byte for byte", () => {
	const before = SETUP_WRITTEN.split("\n");
	const after = edit(SETUP_WRITTEN, { source: "lever", board: "example", name: "Example" }).text.split("\n");
	// Everything above `sources:` and everything below the block is unchanged and in order.
	assert.deepEqual(after.slice(0, 10), before.slice(0, 10));
	assert.deepEqual(after.slice(after.indexOf("filters:")), before.slice(before.indexOf("filters:")));
});

test("a second employer joins the adapter list already there", () => {
	const once = edit(SETUP_WRITTEN, { source: "greenhouse", board: "one", name: "One" }).text;
	const twice = edit(once, { source: "greenhouse", board: "two", name: "Two" }).text;
	assert.match(twice, /  boards:\n    greenhouse:\n      - "one"\n      - "two"\n/u);
	assert.match(twice, /  names:\n    one: "One"\n    two: "Two"\n/u);
});

test("a second adapter arrives as its own key, indented like the first", () => {
	const once = edit(SETUP_WRITTEN, { source: "greenhouse", board: "one", name: "One" }).text;
	const both = edit(once, { source: "lever", board: "two", name: "Two" }).text;
	assert.match(both, /    greenhouse:\n      - "one"\n    lever:\n      - "two"\n/u);
});

test("a board already registered is reported and written no second time", () => {
	const once = edit(SETUP_WRITTEN, { source: "greenhouse", board: "one", name: "One" }).text;
	const again = edit(once, { source: "greenhouse", board: "one", name: "Something Else" });
	assert.equal(again.added.length, 0);
	assert.deepEqual(again.alreadyRegistered, [{ source: "greenhouse", board: "one", name: "Something Else" }]);
	assert.equal(again.registered, 1);
	// Not touched at all, which is what makes "nothing happened" a truthful answer.
	assert.equal(again.text, once);
});

test("the same board twice in one request is registered once", () => {
	const result = edit(
		SETUP_WRITTEN,
		{ source: "greenhouse", board: "one", name: "One" },
		{ source: "greenhouse", board: "one", name: "One" },
	);
	assert.equal(result.added.length, 1);
	assert.equal((result.text.match(/- "one"/gu) ?? []).length, 1);
});

test("an employer name already on file is kept rather than replaced, and that is said", () => {
	const named = SETUP_WRITTEN.replace("  names: {}", '  names:\n    one: "The name they chose"');
	const result = edit(named, { source: "greenhouse", board: "one", name: "A different name" });
	assert.equal(result.added.length, 1);
	assert.equal(result.warnings.length, 1);
	assert.match(result.text, /    one: "The name they chose"/u);
	assert.doesNotMatch(result.text, /A different name/u);
});

test("comments in the block are kept, and nothing is appended after a trailing one", () => {
	const commented = [
		"sources:",
		"  # board token -> employer display name.",
		"  names:",
		"    one: One # the first one",
		"  boards:",
		"    greenhouse:",
		"      - one # consumer app",
		"  # everything below here is a note to self",
		"filters: {}",
		"",
	].join("\n");
	const result = edit(commented, { source: "greenhouse", board: "two", name: "Two" });
	assert.equal(
		result.text,
		[
			"sources:",
			"  # board token -> employer display name.",
			"  names:",
			"    one: One # the first one",
			'    two: "Two"',
			"  boards:",
			"    greenhouse:",
			"      - one # consumer app",
			'      - "two"',
			"  # everything below here is a note to self",
			"filters: {}",
			"",
		].join("\n"),
	);
	assert.equal(result.registered, 2);
});

test("a board token already there behind a comment is recognised, not duplicated", () => {
	const commented = ["sources:", "  names:", "    one: One", "  boards:", "    greenhouse:", "      - one # a note", ""].join("\n");
	const result = edit(commented, { source: "greenhouse", board: "one", name: "One" });
	assert.equal(result.added.length, 0);
	assert.equal(result.text, commented);
});

test("a Workday token keeps its punctuation and is quoted as a key", () => {
	const result = edit(SETUP_WRITTEN, { source: "workday", board: "amgen.wd1~Careers", name: "Amgen" });
	assert.match(result.text, /    "amgen.wd1~Careers": "Amgen"\n/u);
	assert.match(result.text, /    workday:\n      - "amgen.wd1~Careers"\n/u);
});

test("an employer name a person typed cannot become YAML structure", () => {
	const hostile = 'Ha"\nboards:\n  greenhouse:\n    - injected';
	const result = edit(SETUP_WRITTEN, { source: "greenhouse", board: "one", name: hostile });
	assert.match(result.text, /    one: "Ha\\"\\nboards:\\n {2}greenhouse:\\n {4}- injected"\n/u);
	assert.equal(result.text.split("\n").length, SETUP_WRITTEN.split("\n").length + 3);
});

test("an empty adapter list already written out is filled in place", () => {
	const scaffolded = SETUP_WRITTEN.replace(
		"  boards: {}",
		["  boards:", "    greenhouse: []", "    lever: []", "    workday: []"].join("\n"),
	);
	const result = edit(scaffolded, { source: "lever", board: "one", name: "One" });
	assert.match(result.text, /    greenhouse: \[\]\n    lever:\n      - "one"\n    workday: \[\]\n/u);
});

test("a sources block written as an empty collection is built out", () => {
	const bare = SETUP_WRITTEN.replace("sources:\n  names: {}\n  boards: {}", "sources: {}");
	const result = edit(bare, { source: "greenhouse", board: "one", name: "One" });
	assert.match(result.text, /sources:\n  names:\n    one: "One"\n  boards:\n    greenhouse:\n      - "one"\nfilters:/u);
});

test("a sources block with neither key gets both, names above boards", () => {
	const partial = SETUP_WRITTEN.replace("  names: {}\n  boards: {}", '  extra: "kept"');
	const result = edit(partial, { source: "greenhouse", board: "one", name: "One" });
	assert.match(result.text, /sources:\n  names:\n    one: "One"\n  extra: "kept"\n  boards:\n    greenhouse:\n      - "one"\n/u);
});

test("boards written above names, with names still an empty collection, both land in order", () => {
	// Two patches on one line: the `names: {}` line is replaced, and the board appended to the
	// adapter above it inserts at that same index. The replacement has to go first, or it eats
	// the inserted line instead of the line it was written for.
	const inverted = ["sources:", "  boards:", "    greenhouse:", "      - one", "  names: {}", "filters: {}", ""].join("\n");
	const result = edit(inverted, { source: "greenhouse", board: "two", name: "Two" });
	assert.equal(
		result.text,
		[
			"sources:",
			"  boards:",
			"    greenhouse:",
			"      - one",
			'      - "two"',
			"  names:",
			'    two: "Two"',
			"filters: {}",
			"",
		].join("\n"),
	);
});

test("an existing adapter and a new one both appended at the end of the block keep their order", () => {
	// The last adapter's list ends where the boards block ends, so the append to it and the new
	// adapter after it are both insertions at the same index.
	const one = ["sources:", "  names: {}", "  boards:", "    greenhouse:", "      - one", ""].join("\n");
	const result = edit(
		one,
		{ source: "greenhouse", board: "two", name: "Two" },
		{ source: "lever", board: "three", name: "Three" },
	);
	assert.equal(
		result.text,
		[
			"sources:",
			"  names:",
			'    two: "Two"',
			'    three: "Three"',
			"  boards:",
			"    greenhouse:",
			"      - one",
			'      - "two"',
			"    lever:",
			'      - "three"',
			"",
		].join("\n"),
	);
	assert.equal(result.registered, 3);
});

test("a sources key with nothing under it at all is built out in place", () => {
	const bare = SETUP_WRITTEN.replace("sources:\n  names: {}\n  boards: {}", "sources:");
	const result = edit(bare, { source: "greenhouse", board: "one", name: "One" });
	assert.match(result.text, /sources:\n  names:\n    one: "One"\n  boards:\n    greenhouse:\n      - "one"\nfilters:/u);
});

test("a file written with CRLF stays written with CRLF", () => {
	const windows = SETUP_WRITTEN.replaceAll("\n", "\r\n");
	const result = edit(windows, { source: "greenhouse", board: "one", name: "One" });
	assert.ok(result.text.includes('\r\n    one: "One"\r\n'));
	assert.equal(result.text.includes("\n\n"), false);
});

test("a file that mixes line endings keeps every line's own, and matches the majority for new ones", () => {
	// A file somebody opened in one editor and saved in another. Nothing this module did not
	// write may change, and what it does write should look like the rest of the file.
	const mixed = SETUP_WRITTEN.replaceAll("\n", "\r\n").replace("search:\r\n", "search:\n");
	const result = edit(mixed, { source: "greenhouse", board: "one", name: "One" });
	assert.ok(result.text.includes("search:\n  queries:"), "the one bare-newline line was left as it was");
	assert.ok(result.text.includes('\r\n    one: "One"\r\n'), "and what was written matches the rest of the file");
	assert.equal((result.text.match(/[^\r]\n/gu) ?? []).length, 1);
});

test("a file with no trailing newline gets none added", () => {
	const result = edit(SETUP_WRITTEN.trimEnd(), { source: "greenhouse", board: "one", name: "One" });
	assert.equal(result.text.endsWith("\n"), false);
});

for (const [what, document] of [
	["a tab where an indent should be", "sources:\n\tnames: {}\n  boards: {}\n"],
	["a document marker", "---\nsources:\n  names: {}\n  boards: {}\n"],
	["a top-level sequence", "- sources\n"],
	["no sources block at all", "profile:\n  name: x\n"],
	["two sources blocks", "sources:\n  names: {}\nsources:\n  boards: {}\n"],
	["a flow mapping with entries in it", 'sources: {names: {}, boards: {}}\n'],
	["names written as a flow mapping with entries", 'sources:\n  names: {a: b}\n  boards: {}\n'],
	["boards written as a flow list with entries", "sources:\n  names: {}\n  boards: [a]\n"],
	["an adapter holding something other than a list", "sources:\n  names: {}\n  boards:\n    greenhouse: yes\n"],
	["an adapter holding a list of mappings", "sources:\n  names: {}\n  boards:\n    greenhouse:\n      - token: a\n"],
	["entries under sources indented inconsistently", "sources:\n  names: {}\n    boards: {}\n"],
	["an anchor where a key should be", "sources: &shared\n  names: {}\n  boards: {}\n"],
	["one adapter named twice", "sources:\n  names: {}\n  boards:\n    greenhouse: []\n    greenhouse: []\n"],
] as const) {
	test(`${what} is refused, and the document is not touched`, () => {
		assert.throws(() => edit(document, { source: "greenhouse", board: "one", name: "One" }), UneditableTargeting);
	});
}

test("the scaffold Profile this repository ships takes an employer", () => {
	const scaffold = readFileSync(
		fileURLToPath(new URL("../../profiles/example/targeting.yaml", import.meta.url)),
		"utf8",
	);
	const result = edit(scaffold, { source: "greenhouse", board: "ginkgobioworks", name: "Ginkgo Bioworks" });
	assert.equal(result.added.length, 1);
	assert.match(result.text, /    linear: Linear\n    ginkgobioworks: "Ginkgo Bioworks"\n/u);
	assert.match(result.text, /      - duolingo\n      - "ginkgobioworks"\n    lever: \[\]\n/u);
	// The rest of the scaffold — every comment in it — is unchanged.
	assert.equal(result.text.split("\n").length, scaffold.split("\n").length + 2);
	assert.ok(result.text.includes("Editing this file changes filters_version"));
});

test("a Profile whose adapters already hold boards takes another", () => {
	const example = readFileSync(
		fileURLToPath(new URL("../../profiles/example/targeting.yaml", import.meta.url)),
		"utf8",
	);
	const before = addEmployers(example, []);
	const result = edit(example, { source: "lever", board: "example", name: "Example" });
	assert.equal(result.registered, before.registered + 1);
	assert.match(result.text, /    lever:\n      - "example"\n/u);
	assert.match(result.text, /    linear: Linear\n    example: "Example"\n/u);
});

/**
 * The emitter is `yaml.ts`'s, and this is the half of it that reaches an existing Profile.
 *
 * These two functions used to be a second copy of `yaml.ts`'s, kept in step by a comment. Both
 * copies were wrong in the same two ways, which is what a copy kept in step by a comment does.
 * There is one now, imported, and these cases are what say so from this side of it.
 */

test("a board token YAML 1.1 would load as a boolean is quoted where it is written as a key", () => {
	// The key is what matters: `sources.names` is how Dedup resolves an ATS Posting's employer,
	// and four tokens that collapse into three keys lose an employer with nothing on screen.
	const edited = addEmployers(SETUP_WRITTEN, [
		{ source: "greenhouse", board: "yes", name: "Employer Yes" },
		{ source: "greenhouse", board: "no", name: "Employer No" },
		{ source: "greenhouse", board: "on", name: "Employer On" },
		{ source: "greenhouse", board: "null", name: "Employer Null" },
	]);
	assert.equal(edited.added.length, 4);
	for (const token of ["yes", "no", "on", "null"]) {
		assert.ok(edited.text.includes(`    "${token}": "Employer `), `${token} was written bare`);
		assert.ok(edited.text.includes(`      - "${token}"`), `${token} as a list item`);
	}
});

test("an employer name is written so the loader reads it back, whatever is in it", () => {
	// Written as an escape: a literal U+0085 in this file would be as invisible here as it was
	// in the defect. `employers.ts` refuses one on the way in — this is the emitter underneath
	// that, which is what a name arriving by any other route meets.
	const edited = addEmployers(SETUP_WRITTEN, [{ source: "greenhouse", board: "acme", name: "Acme\u0085Bio" }]);
	assert.ok(edited.text.includes('acme: "Acme\\u0085Bio"'));
	assert.ok(!/[\u0085\u2028\u2029]/u.test(edited.text));
});

test("an escape the emitter can write is an escape this reads back", () => {
	// A token already registered must be recognised as registered, or it is written twice. That
	// means whatever `yaml.ts` can emit, this has to be able to read — including `\uXXXX`.
	const document = [
		"profile:",
		'  name: "mine"',
		"sources:",
		"  names:",
		'    "\\u0041cme": "Acme"',
		"  boards:",
		"    greenhouse:",
		'      - "\\x41cme"',
		"",
	].join("\n");
	const edited = addEmployers(document, [{ source: "greenhouse", board: "Acme", name: "Acme Again" }]);
	assert.equal(edited.added.length, 0);
	assert.deepEqual(edited.alreadyRegistered, [{ source: "greenhouse", board: "Acme", name: "Acme Again" }]);
	assert.equal(edited.text, document);
});

test("an escape that is not one is a document this refuses rather than misreads", () => {
	// A truncated numeric escape read leniently would yield a token that is not the token on
	// disk, and this module would then append a board it already has.
	for (const item of ['"\\u00"', '"\\uZZZZ"', '"\\q"', '"\\U0011FFFF"']) {
		const document = ["sources:", "  names: {}", "  boards:", "    greenhouse:", `      - ${item}`, ""].join("\n");
		assert.throws(
			() => addEmployers(document, [{ source: "greenhouse", board: "acme", name: "Acme" }]),
			UneditableTargeting,
			item,
		);
	}
});
