/**
 * The real Profile write, driven from the Python side. Not a test: the thing a test runs.
 *
 * **Why it exists.** `tests/register-employers.ts` lets a Python case hash the document the
 * *employer* route writes. This is its sibling for the route every person goes through first.
 * The claim it exists to settle is the one the load-back cannot make on its own: a
 * `targeting.yaml` can be read back perfectly by `venator.profile` and still make
 * `filters_version()` raise, which disables the Match stage for that Profile permanently. Only
 * Python can ask the second question, and only this side can write the file.
 *
 * So this runs `writeProfile` itself — the whole route below the JSON parsing, including the
 * load-back subprocess — into a temporary Install the caller names, and says where it landed or
 * which refusal it was. Nothing here reimplements a rule; the point is that there is no second
 * copy of the writer for a test to agree with.
 */

import type { LocationContext } from "../server/locations.ts";
import { OnboardingError } from "../server/onboarding/errors.ts";
import { asMapping, asText, at, parseJson } from "../server/onboarding/json.ts";
import { validateProfileName, writeProfile } from "../server/onboarding/profile.ts";

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
const home = asText(at(request, "home"));
const python = asText(at(request, "python"));
const name = asText(at(request, "name"));
if (home === null || python === null || name === null) {
	throw new Error("the request names a temporary Install, an interpreter and a Profile name");
}

// A temporary Install with no checkout, so nothing can shadow the Profile being written and
// the writable Profiles root is the directory the caller made.
const environment = new Map([
	["VENATOR_HOME", home],
	["VENATOR_PYTHON", python],
]);
const context: LocationContext = {
	platform: "linux",
	home,
	workingDirectory: home,
	checkoutRoot: null,
	environment: (variable) => environment.get(variable),
};

try {
	const written = await writeProfile(
		{
			name: validateProfileName(name),
			overwrite: false,
			resume: asMapping(at(request, "resume")),
			constraints: asMapping(at(request, "constraints")),
			targeting: asMapping(at(request, "targeting")),
		},
		context,
	);
	process.stdout.write(JSON.stringify({ directory: written.directory, files: written.files }));
} catch (cause) {
	if (!(cause instanceof OnboardingError)) throw cause;
	process.stdout.write(JSON.stringify({ refused: cause.code, field: cause.field }));
}
