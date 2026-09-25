/**
 * The real emitter, driven from the Python side. Not a test: the thing a test runs.
 *
 * **Why it exists.** `tests/profile/test_dashboard_verify_adapter.py` holds the sweep that says
 * `server/onboarding/yaml.ts`'s escape set is complete — every codepoint the emitter writes is
 * one PyYAML reads back as itself. Only Python has PyYAML and only this side has the emitter,
 * so the sweep needs both, and it used to solve that by *re-implementing* `quoteScalar` in
 * Python against a hardcoded list of codepoints. That is a second opinion in a second language,
 * which is exactly what `yaml.ts` and `verify_profile.py` both say not to build: narrowing the
 * range in `yaml.ts` reintroduced the original defect for ten codepoints and the sweep stayed
 * green, because it was never running the code it certified.
 *
 * So the sweep runs this instead, over the same seam everything else here uses: one process,
 * one JSON document in, one JSON document out.
 *
 * **The protocol is codepoints rather than text**, in both directions for the input, because a
 * lone surrogate has no UTF-8 encoding and could not survive a pipe as itself. A request is a
 * list of payloads, each payload a list of codepoints; the answer is a list of the scalars
 * `quoteScalar` produced, in the same order. `JSON.stringify` escapes a lone surrogate that
 * survives into the output, so the answer is always encodable.
 */

import { asList, asNumber, parseJson, type JsonValue } from "../server/onboarding/json.ts";
import { quoteScalar } from "../server/onboarding/yaml.ts";

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

/** One payload: a list of codepoints, checked as one rather than trusted. */
function payloadFrom(entry: JsonValue): string {
	const codepoints = asList(entry);
	if (codepoints === null) throw new Error("each payload is a list of codepoints");
	return codepoints
		.map((codepoint) => {
			const value = asNumber(codepoint);
			if (value === null) throw new Error("each codepoint is a number");
			return String.fromCodePoint(value);
		})
		.join("");
}

const request = asList(parseJson(await read()));
if (request === null) throw new Error("the request is a list of payloads");
process.stdout.write(JSON.stringify(request.map((entry) => quoteScalar(payloadFrom(entry)))));
