/**
 * Turning a pasted employer URL into boards a Profile can poll.
 *
 * **Why this exists at all.** A Profile written by onboarding starts with an empty board
 * registry, and Discover only ever answers for an employer somebody registered — so a person
 * who finishes onboarding and runs the pipeline finds nothing. The cold start is the honest
 * hard part of setting this product up, and pasting the careers page of an employer they
 * already have in mind is the one move that needs no curation from anybody.
 *
 * **Why it is a subprocess.** `venator.discover.register` owns what a pasted URL may be, which
 * ATS board URLs are recognised, how a Workday site string is learned out of a tenant's
 * `robots.txt`, and whether a board actually answers. Reimplementing any of that here would
 * be a second opinion in a second language, and this repository has already paid for one of
 * those: a board-token map in TypeScript sat at twelve entries while the Profile grew to
 * thirty-five, and the twenty-three boards it had never heard of rendered their tokens on
 * screen. So the rules stay in one place and this module runs them — the same arrangement
 * `runtime.ts` has with `venator.llm.probe`, down to the shape of the answer.
 *
 * **What it does not do.** It makes no state-changing request of any kind: `register` issues
 * `GET`s and follows no redirect, and nothing here is an application, an account, or a form.
 * It invents no employer display name — `register` refuses to, and the page title travels as
 * a hint the Owner reads while typing the real one. And it writes nothing: a resolved board
 * reaches a Profile only when step three sends it back with a name attached.
 *
 * **An absent pipeline is an answer.** A shipped Install with no `venator` on its Python path
 * says so, in the same words the runtime step uses, rather than looking broken.
 */

import { spawn } from "node:child_process";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { BoardResolveResponse, ResolvedBoard } from "../../shared/onboarding.ts";
import {
	namedPythonInterpreter,
	pipelineWorkingDirectory,
	pythonInterpreter,
	systemContext,
	type LocationContext,
} from "../locations.ts";
import { nonJevInheritedEnvironment } from "../runs/environment.ts";
import { OnboardingError } from "./errors.ts";
import { asBoolean, asList, asMapping, asNumber, asText, at, parseJson } from "./json.ts";
import type { JsonMapping, JsonValue } from "./json.ts";

/** The adapter that runs `register` and answers in JSON. Beside this file, and only here. */
const RESOLVER_SCRIPT = join(fileURLToPath(new URL(".", import.meta.url)), "resolve_board.py");

/**
 * One paste is at most one page fetch plus one `robots.txt` per tenant named on it plus one
 * listing request per site found, each with `register`'s own 20s timeout. This bounds the
 * whole of that: past it the answer is that it did not finish, which is a fact the person can
 * act on, rather than a request left hanging.
 */
const RESOLVE_TIMEOUT_MS = 120_000;

/** Exit 3 from the adapter: the pipeline is not importable in this Install. */
const PIPELINE_MISSING_EXIT = 3;

/**
 * A first look at a pasted URL, before it becomes a subprocess argument.
 *
 * `register.normalize_url` is the real check and it is stricter than this — it refuses
 * credentials before the host, a bare address, and `localhost`. This is the cheap half:
 * enough that a value which is obviously not a URL never becomes an argv entry, and short
 * enough that a pasted document cannot be sent through as one.
 */
const PASTED_URL = /^https?:\/\/[^\s/$.?#][^\s]*$/iu;

const MAXIMUM_URL_LENGTH = 2048;

export function validatePastedUrl(raw: string): string {
	const url = raw.trim();
	if (url.length > MAXIMUM_URL_LENGTH) {
		throw new OnboardingError(
			"unresolvable_url",
			"That is longer than a web address.",
			"Paste the address of the employer's careers page, or of their job board.",
			"url",
		);
	}
	if (!PASTED_URL.test(url)) {
		throw new OnboardingError(
			"unresolvable_url",
			"That is not a web address this step can read.",
			"Paste the whole address, starting with https://.",
			"url",
		);
	}
	return url;
}

type ResolverProcess = {
	readonly code: number | null;
	readonly stdout: string;
	readonly stderr: string;
	readonly timedOut: boolean;
	readonly launchFailed: boolean;
};

function runResolver(url: string, context: LocationContext): Promise<ResolverProcess> {
	return new Promise((settle) => {
		const child = spawn(pythonInterpreter(context), [RESOLVER_SCRIPT, url], {
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
		}, RESOLVE_TIMEOUT_MS);
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

function unavailable(message: string, remedy: string | null = null): OnboardingError {
	return new OnboardingError("resolver_unavailable", message, remedy);
}

const PIPELINE_ABSENT = unavailable(
	"Reading an employer's careers page is not part of this Install, so a pasted address cannot be turned into a board here.",
	"A board can still be added by hand: the Profile's targeting.yaml lists them under sources.",
);

/**
 * The interpreter would not start at all — which is not the same as this Install having no
 * pipeline in it, and used to be reported as if it were.
 *
 * A desktop bundle ships its own interpreter and the host names it, so on that machine the
 * resolver *is* part of the Install and what failed is the interpreter. Naming it is what makes
 * the failure actionable; when nothing named one, the Install genuinely has no pipeline and
 * `PIPELINE_ABSENT` is still the truthful answer.
 */
function interpreterUnstartable(context: LocationContext): OnboardingError {
	const named = namedPythonInterpreter(context);
	if (named === null) return PIPELINE_ABSENT;
	return unavailable(
		`The interpreter this Install reads an employer's careers page with could not be started: ${named}`,
		"A board can still be added by hand: the Profile's targeting.yaml lists them under sources.",
	);
}

function readBoard(entry: JsonMapping): ResolvedBoard | null {
	const source = asText(at(entry, "source"));
	const board = asText(at(entry, "board"));
	if (source === null || source === "" || board === null || board === "") return null;
	return {
		source,
		board,
		route: asText(at(entry, "route")) ?? "",
		confirmed: asBoolean(at(entry, "confirmed")) ?? false,
		postingCount: asNumber(at(entry, "postingCount")) ?? 0,
		complete: Object.hasOwn(entry, "complete") ? asBoolean(at(entry, "complete")) ?? false : true,
	};
}

/**
 * Reads the adapter's document.
 *
 * A `failure` key is the adapter saying the paste could not be resolved, and its message —
 * when it has one — is `register`'s own wording, written for the person reading it and
 * naming nothing but the URL they pasted. Anything this cannot read at all is reported as
 * unreadable rather than guessed at, on the same principle the view database follows.
 */
export function readResolverDocument(text: string): BoardResolveResponse {
	let parsed: JsonValue;
	try {
		parsed = parseJson(text);
	} catch {
		throw unavailable("The address could not be read, so nothing is being reported about it.");
	}
	const document = asMapping(parsed);
	if (document === null) {
		throw unavailable("The address could not be read, so nothing is being reported about it.");
	}
	const failure = asMapping(at(document, "failure"));
	if (failure !== null) {
		const kind = asText(at(failure, "kind"));
		const message = asText(at(failure, "message"));
		if (kind === "unresolvable") {
			throw new OnboardingError(
				"unresolvable_url",
				message ?? "That address named no job board this pipeline can poll.",
				"Open one Posting on the employer's site and paste that Posting's address instead.",
				"url",
			);
		}
		if (kind === "unreachable") {
			throw unavailable(
				message ?? "That page could not be reached from this machine.",
				"Check the address, or try again when this machine can reach the employer's site.",
			);
		}
		throw unavailable("Something went wrong reading that address, and the cause is not known.");
	}
	const entries = asList(at(document, "boards"));
	if (entries === null) {
		throw unavailable("The address could not be read, so nothing is being reported about it.");
	}
	const boards: ResolvedBoard[] = [];
	for (const entry of entries) {
		const mapping = asMapping(entry);
		const board = mapping === null ? null : readBoard(mapping);
		if (board === null) {
			throw unavailable("That address resolved to something this dashboard cannot describe, so none of it is being shown.");
		}
		boards.push(board);
	}
	return {
		url: asText(at(document, "url")) ?? "",
		titleHint: asText(at(document, "titleHint")),
		boards,
	};
}

/**
 * Resolves one pasted URL.
 *
 * Throws an `OnboardingError` for everything a person can act on — a URL that is not one, a
 * page that named no board, a machine that cannot reach the employer, an Install with no
 * pipeline in it. The route hands those straight to the browser; nothing else is described.
 */
export async function resolveBoards(
	rawUrl: string,
	context: LocationContext = systemContext(),
): Promise<BoardResolveResponse> {
	const url = validatePastedUrl(rawUrl);
	const finished = await runResolver(url, context);
	if (finished.launchFailed) throw interpreterUnstartable(context);
	if (finished.code === PIPELINE_MISSING_EXIT) throw PIPELINE_ABSENT;
	if (finished.timedOut) {
		throw unavailable(
			"Reading that address did not finish in time, so nothing is being reported about it.",
			"Try again, or paste the address of the employer's job board directly.",
		);
	}
	if (finished.code !== 0) {
		process.stderr.write(
			`onboarding: the board resolver exited ${String(finished.code)}: ${finished.stderr.slice(0, 400)}\n`,
		);
		throw unavailable("The address could not be read in this Install, so nothing is being reported about it.");
	}
	return readResolverDocument(finished.stdout);
}
