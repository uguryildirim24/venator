/**
 * The real employer edit, driven from the Python side. Not a test: the thing a test runs.
 *
 * **Why it exists.** The gap this closes is the one the lone-surrogate defect lived in: the
 * load-back answered `{"outcome": "loads"}` for a `targeting.yaml` that then made
 * `filters_version()` raise `UnicodeEncodeError`, which disables the Match stage for that
 * Profile permanently. "The loader reads it" and "the pipeline can use it" are two claims, and
 * only Python can make the second one — `filters_version` is `src/venator/match/store.py` and
 * re-serializes exactly this file.
 *
 * So the Python case needs the document the route actually produces, and this hands it over:
 * the real `validateEmployer`, the real `addEmployers`, one JSON document in, one out. Building
 * an equivalent document in Python would be a second emitter in a second language, which is
 * the shape `verify_profile.py`'s own docstring argues against.
 *
 * A refusal is an answer rather than a crash, so the same seam says which entries the route
 * will not take at all.
 */

import type { EmployerRegistration } from "../shared/onboarding.ts";
import { validateEmployer } from "../server/onboarding/employers.ts";
import { OnboardingError } from "../server/onboarding/errors.ts";
import { asList, asMapping, asText, at, parseJson } from "../server/onboarding/json.ts";
import { addEmployers, UneditableTargeting } from "../server/onboarding/targeting-edit.ts";

function read(): Promise<string> {
	return new Promise((whole) => {
		let text = "";
		process.stdin.setEncoding("utf8");
		process.stdin.on("data", (chunk: string) => {
			text += chunk;
		});
		process.stdin.on("end", () => {
			whole(text);
		});
	});
}

const request = asMapping(parseJson(await read()));
if (request === null) throw new Error("the request is a mapping");
const targeting = asText(at(request, "targeting"));
const boards = asList(at(request, "boards"));
if (targeting === null || boards === null) throw new Error("the request names a targeting document and a board list");

const entries: EmployerRegistration[] = boards.map((entry) => {
	const mapping = asMapping(entry);
	if (mapping === null) throw new Error("each board is a mapping");
	const source = asText(at(mapping, "source")) ?? "";
	const board = asText(at(mapping, "board")) ?? "";
	const name = asText(at(mapping, "name")) ?? "";
	return { source, board, name };
});

try {
	const checked = entries.map((entry, index) => validateEmployer(entry, index));
	const edited = addEmployers(targeting, checked);
	process.stdout.write(JSON.stringify({ text: edited.text, added: edited.added.length }));
} catch (cause) {
	if (cause instanceof OnboardingError) {
		process.stdout.write(JSON.stringify({ refused: cause.code, field: cause.field }));
	} else if (cause instanceof UneditableTargeting) {
		process.stdout.write(JSON.stringify({ refused: "targeting_unreadable", field: "profile" }));
	} else {
		throw cause;
	}
}
