/**
 * The load-back: nothing onboarding writes lands unless `src/venator/profile/` reads it.
 *
 * **Why this exists.** Two writers on this surface render YAML — `/profile`, which creates the
 * three files a Profile is made of, and `/employers`, which appends two blocks to one of them
 * — and both of them used to rest on a single sentence in `yaml.ts`: that JSON's string
 * escaping is a subset of YAML's, so `JSON.stringify` produces a scalar YAML reads back
 * character for character. It is not true. PyYAML is the loader, it is YAML 1.1, and it treats
 * U+0085 as a line break and U+007F–U+009F as characters a document may not contain — none of
 * which `JSON.stringify` escapes, because all of them are above U+001F. A pasted employer name
 * carrying one produced a `targeting.yaml` the pipeline refuses to load, from first setup, with
 * nothing on screen to say so.
 *
 * The emitter is fixed and that is the primary defence. This is the net underneath it, and the
 * two are not alternatives: an emitter is only ever as correct as its author's list of things
 * that can go wrong, and the loader is the arbiter of what a Profile means.
 *
 * **Where it runs.** Between the staging directory being fully written and the rename that
 * makes it real. Both writers already land in one `renameSync`, so a refusal here leaves the
 * original byte-identical and an absent file uncreated — which is the property that makes a
 * check worth having at all, rather than a warning printed after the damage.
 *
 * **How it asks.** The seam `runtime/probe` and `boards/resolve` already use: the interpreter
 * `pythonInterpreter` resolves, one adapter script, one JSON document read off its stdout. An
 * Install whose Python cannot import `venator` is reported as an answer rather than crashing —
 * and it is an answer that refuses the write, because "this Install cannot check what it is
 * about to write" is not a reason to write it. That Install can run no stage either.
 */

import { spawn } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { nonJevInheritedEnvironment } from "../runs/environment.ts";
import { pipelineWorkingDirectory, pythonInterpreter, systemContext, type LocationContext } from "../locations.ts";
import { OnboardingError } from "./errors.ts";
import { asMapping, asText, at, parseJson } from "./json.ts";

/** The adapter that runs the real loader and answers in JSON. Beside this file, and only here. */
const VERIFIER_SCRIPT = join(fileURLToPath(new URL(".", import.meta.url)), "verify_profile.py");

/**
 * Reading three small files and validating them. There is no network and no LLM in it, so this
 * bounds a machine that is thrashing rather than work that legitimately takes time — and it
 * bounds it tightly, because a person is waiting on a button with a half-written staging
 * directory on disk behind it.
 */
const VERIFY_TIMEOUT_MS = 20_000;

/** Exit 3 from the adapter: the pipeline is not importable in this Install. */
const PIPELINE_MISSING_EXIT = 3;

/**
 * What the loader said about a candidate.
 *
 * `refused` carries the loader's own sentence, which names the staging directory — so it is
 * for this server's stderr and never for a browser. Everything else is a category.
 */
export type VerifyOutcome =
	| { readonly outcome: "loads" }
	| { readonly outcome: "refused"; readonly message: string | null }
	/** The adapter caught something it could not classify and put the original on stderr. */
	| { readonly outcome: "failed" }
	/** The adapter answered in a shape this dashboard does not know. Never guessed at. */
	| { readonly outcome: "mismatch" }
	/** The pipeline is not part of this Install, or the interpreter would not start. */
	| { readonly outcome: "absent" }
	| { readonly outcome: "timed-out" };

/**
 * Reads the adapter's document.
 *
 * Exported for its own tests. A document this does not recognise is a `mismatch` rather than a
 * guess, on `readProbeDocument`'s own principle — and a mismatch refuses the write just as a
 * refusal does, because the two sides being out of step is not evidence that the Profile loads.
 */
export function readVerifyDocument(text: string): VerifyOutcome {
	let document;
	try {
		document = asMapping(parseJson(text));
	} catch {
		return { outcome: "mismatch" };
	}
	if (document === null) return { outcome: "mismatch" };
	const outcome = asText(at(document, "outcome"));
	if (outcome === "loads") return { outcome: "loads" };
	if (outcome === "refused") return { outcome: "refused", message: asText(at(document, "message")) };
	if (outcome === "failed") return { outcome: "failed" };
	return { outcome: "mismatch" };
}

/** What the adapter's process did, as the four facts the outcome is decided from. */
export type VerifyProcess = {
	readonly code: number | null;
	readonly stdout: string;
	readonly stderr: string;
	readonly timedOut: boolean;
	readonly launchFailed: boolean;
};

/**
 * What a finished adapter process means.
 *
 * Separated from the spawn so every branch is a pure function of four values and can be
 * asserted without a child process, which is what `runs/plan.ts` does and for the same reason:
 * the branches are then testable identically on every host.
 */
export function readVerifyResult(finished: VerifyProcess): VerifyOutcome {
	if (finished.launchFailed) return { outcome: "absent" };
	if (finished.timedOut) return { outcome: "timed-out" };
	if (finished.code === PIPELINE_MISSING_EXIT) return { outcome: "absent" };
	if (finished.code !== 0) {
		// Logged rather than returned: an adapter that exited non-zero prints paths and
		// environment contents, and none of that is the caller's to see.
		process.stderr.write(
			`onboarding: the Profile verifier exited ${String(finished.code)}: ${finished.stderr.slice(0, 400)}\n`,
		);
		return { outcome: "failed" };
	}
	return readVerifyDocument(finished.stdout);
}

function runVerifier(directory: string, context: LocationContext): Promise<VerifyProcess> {
	return new Promise((settle) => {
		const child = spawn(pythonInterpreter(context), [VERIFIER_SCRIPT, "--directory", directory], {
			cwd: pipelineWorkingDirectory(context),
			stdio: ["ignore", "pipe", "pipe"],
			env: nonJevInheritedEnvironment(context),
		});
		let stdout = "";
		let stderr = "";
		let timedOut = false;
		let launchFailed = false;
		const timer = setTimeout(() => {
			timedOut = true;
			child.kill("SIGKILL");
		}, VERIFY_TIMEOUT_MS);
		child.stdout?.setEncoding("utf8");
		child.stderr?.setEncoding("utf8");
		child.stdout?.on("data", (chunk: string) => {
			stdout += chunk;
		});
		child.stderr?.on("data", (chunk: string) => {
			stderr += chunk;
		});
		child.on("error", () => {
			launchFailed = true;
		});
		child.on("close", (code) => {
			clearTimeout(timer);
			settle({ code, stdout, stderr, timedOut, launchFailed });
		});
	});
}

/** Asks the pipeline whether it can read the Profile staged at `directory`. */
export async function verifyProfileDirectory(
	directory: string,
	context: LocationContext = systemContext(),
): Promise<VerifyOutcome> {
	return readVerifyResult(await runVerifier(directory, context));
}

/**
 * The refusal each outcome is, worded for the person who pressed the button.
 *
 * Nothing the loader said is repeated here. Its sentence names an absolute path inside a
 * staging directory, which is neither useful to a reader nor this surface's to disclose, so it
 * goes to stderr and what comes back is constructed.
 */
function refusalFor(outcome: VerifyOutcome, what: string): OnboardingError | null {
	if (outcome.outcome === "loads") return null;
	if (outcome.outcome === "absent") {
		return new OnboardingError(
			"verification_unavailable",
			`This Install cannot read a Profile back, so ${what} was not written.`,
			"The pipeline that reads Profiles is not part of this Install, or its interpreter would not start. Nothing was changed.",
		);
	}
	if (outcome.outcome === "timed-out") {
		return new OnboardingError(
			"verification_unavailable",
			`Checking the Profile did not finish in time, so ${what} was not written.`,
			"Nothing was changed. Try again.",
		);
	}
	return new OnboardingError(
		"unloadable_profile",
		`The pipeline could not read the Profile that would have resulted, so ${what} was not written.`,
		"Nothing was changed. Check the employer names and search terms for anything pasted out of another document.",
	);
}

/**
 * Verifies a staged Profile directory, or throws the refusal it is.
 *
 * `what` names the thing that was not written, in the words the route would use — "the
 * Profile", "the employer" — because the sentence a person reads should say what did not
 * happen rather than name a module.
 */
export async function requireLoadableProfile(
	directory: string,
	what: string,
	context: LocationContext = systemContext(),
): Promise<void> {
	const outcome = await verifyProfileDirectory(directory, context);
	if (outcome.outcome === "refused" && outcome.message !== null) {
		process.stderr.write(`onboarding: a candidate Profile did not load: ${outcome.message.slice(0, 600)}\n`);
	}
	const refusal = refusalFor(outcome, what);
	if (refusal !== null) throw refusal;
}
