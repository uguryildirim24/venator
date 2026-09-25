/**
 * The Profile writer, checked on the property that matters: nothing a person types can change
 * the shape of the file it is written into.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { JsonValue } from "../server/onboarding/json.ts";
import { writeYamlDocument, YamlWriteError } from "../server/onboarding/yaml.ts";

test("a mapping is written in the order it arrived, with a header comment", () => {
	const document = writeYamlDocument({ name: "Someone", contact: { email: "a@b.co" } }, ["written by a test"]);
	assert.equal(document, '# written by a test\nname: "Someone"\ncontact:\n  email: "a@b.co"\n');
});

test("a string that would otherwise be another type stays a string", () => {
	const document = writeYamlDocument({ a: "no", b: "true", c: "12:30", d: "~", e: "0755", f: "null" }, []);
	assert.equal(document, 'a: "no"\nb: "true"\nc: "12:30"\nd: "~"\ne: "0755"\nf: "null"\n');
});

test("text that looks like YAML structure cannot become YAML structure", () => {
	const hostile = "hello\nqueries:\n  - injected";
	const document = writeYamlDocument({ label: hostile }, []);
	assert.equal(document, 'label: "hello\\nqueries:\\n  - injected"\n');
	assert.equal(document.split("\n").length, 2);
});

test("booleans, numbers and null are written as themselves", () => {
	assert.equal(writeYamlDocument({ a: true, b: false, c: 75, d: null, e: -1.5 }, []), "a: true\nb: false\nc: 75\nd: null\ne: -1.5\n");
});

test("an empty list, an empty mapping and an empty document each have a spelling", () => {
	assert.equal(writeYamlDocument({ boards: [], names: {} }, []), "boards: []\nnames: {}\n");
	assert.equal(writeYamlDocument({}, []), "{}\n");
});

test("a list of mappings nests under its own dash", () => {
	const document = writeYamlDocument({ levels: [{ name: "mid", title_terms: [] }, { name: "senior" }] }, []);
	assert.equal(document, 'levels:\n  -\n    name: "mid"\n    title_terms: []\n  -\n    name: "senior"\n');
});

test("a key that is not a plain word is quoted", () => {
	assert.equal(writeYamlDocument({ "amgen.wd1~Careers": "Amgen" }, []), '"amgen.wd1~Careers": "Amgen"\n');
});

test("nesting deeper than a Profile ever goes is refused rather than written", () => {
	// The payload is arbitrary JSON from a request, so the depth guard is what stops it.
	let nested: JsonValue = { leaf: 1 };
	for (let depth = 0; depth < 40; depth += 1) nested = { down: nested };
	assert.throws(() => writeYamlDocument({ deep: nested }, []), YamlWriteError);
});

/**
 * The scalar rules, measured against the loader rather than reasoned about.
 *
 * `server/onboarding/yaml.ts` used to claim that JSON's string escaping is a strict subset of
 * YAML's, so `JSON.stringify` produced a scalar YAML reads back character for character. It is
 * false, and the two cases below are the two ways it is false. Both were reproduced through
 * `yaml.safe_load` before they were written down here, and `tests/profile/` holds the Python
 * half — the half that can say what PyYAML actually does, and that goes red if it changes.
 *
 * Every hostile character is written as a JavaScript escape rather than pasted in. That is not
 * fastidiousness: the whole defect is that these codepoints are invisible, and a literal one in
 * this file would be invisible here too.
 */

test("a codepoint the Profile loader cannot read literally is written as an escape", () => {
	// U+0085 is the one that started this: a line break to PyYAML, above U+001F so JSON leaves
	// it alone, and invisible in every editor. A pasted employer name carrying one produced a
	// targeting.yaml the pipeline refuses to load.
	assert.equal(writeYamlDocument({ name: "Acme\u0085Bio" }, []), 'name: "Acme\\u0085Bio"\n');
	// The rest of PyYAML's non-printable range, its two other line breaks, and the two
	// non-characters above U+FFFD.
	assert.equal(
		writeYamlDocument({ a: "\u007f\u0080\u009f", b: "\u2028\u2029", c: "\ufffe\uffff" }, []),
		'a: "\\u007f\\u0080\\u009f"\nb: "\\u2028\\u2029"\nc: "\\ufffe\\uffff"\n',
	);
	// Nothing else moves: what JSON already escapes stays as JSON escaped it, and everything
	// printable stays literal — accents, non-Latin scripts, astral planes and U+00A0 included.
	assert.equal(
		writeYamlDocument({ a: 'a\nb\tc"d\\e', b: "\u00c6r\u00f8 \u6771\u4eac \u{1f9ec}\u00a0" }, []),
		'a: "a\\nb\\tc\\"d\\\\e"\nb: "\u00c6r\u00f8 \u6771\u4eac \u{1f9ec}\u00a0"\n',
	);
});

test("a key YAML 1.1 would load as a boolean or a null is quoted, so distinct keys stay distinct", () => {
	// PyYAML is YAML 1.1. Bare, these four keys load as `True`, `False`, `True` and `None` —
	// three keys out of four board tokens, and one employer gone from `sources.names` without a
	// word. `sources.names` is what Dedup resolves an ATS Posting's employer through.
	assert.equal(
		writeYamlDocument({ yes: "A", no: "B", on: "C", null: "D" }, []),
		'"yes": "A"\n"no": "B"\n"on": "C"\n"null": "D"\n',
	);
	// Every spelling PyYAML resolves, and the two the specification names that PyYAML does not.
	for (const key of ["true", "True", "TRUE", "false", "FALSE", "Off", "OFF", "Null", "NULL", "y", "N"]) {
		assert.equal(writeYamlDocument({ [key]: 1 }, []), `"${key}": 1\n`, key);
	}
	// And a word that only looks like one of them is still a plain key: the exception exists so
	// a written Profile stays diffable against a hand-written one.
	assert.equal(writeYamlDocument({ yesterday: 1, nobody: 1, only: 1 }, []), "yesterday: 1\nnobody: 1\nonly: 1\n");
});
