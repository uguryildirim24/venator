/**
 * The Profile screen's reader and patcher, driven from the Python side. Not a test: the thing
 * a test runs.
 *
 * `tests/profile/test_profile_form_roundtrip.py` needs the files the save route actually
 * produces, so it can read them with PyYAML and `venator.profile.load_profile` — the arbiter
 * of what a Profile means — rather than with a second reader in Python. This hands them over:
 * one JSON document in, one out, the seam the dashboard uses everywhere else.
 *
 * Two requests: `{"documents": …}` answers with the form the screen would show, and
 * `{"original": …, "form": …}` answers with the three files a save would stage, or with the
 * refusal the route would give.
 */

import { OnboardingError } from "../server/onboarding/errors.ts";
import { asMapping, asText, at, parseJson } from "../server/onboarding/json.ts";
import { formOf, patchProfileDocuments, profileFormFromBody } from "../server/onboarding/profile-form.ts";
import type { ProfileDocuments } from "../shared/profile-form.ts";

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

function documentsAt(key: string): ProfileDocuments {
	const mapping = asMapping(at(request ?? {}, key));
	const resume = mapping === null ? null : asText(at(mapping, "resume.yaml"));
	const constraints = mapping === null ? null : asText(at(mapping, "constraints.yaml"));
	const targeting = mapping === null ? null : asText(at(mapping, "targeting.yaml"));
	if (resume === null || constraints === null || targeting === null) throw new Error(`${key} names the three Profile files`);
	return { "resume.yaml": resume, "constraints.yaml": constraints, "targeting.yaml": targeting };
}

try {
	if (at(request, "documents") !== undefined) {
		process.stdout.write(JSON.stringify({ form: formOf(documentsAt("documents")) }));
	} else {
		const form = profileFormFromBody(at(request, "form"));
		process.stdout.write(JSON.stringify({ documents: patchProfileDocuments(documentsAt("original"), form) }));
	}
} catch (cause) {
	if (cause instanceof OnboardingError) {
		process.stdout.write(JSON.stringify({ refused: cause.code, field: cause.field, message: cause.message }));
	} else {
		throw cause;
	}
}
