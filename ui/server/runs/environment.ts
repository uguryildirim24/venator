/**
 * The environment a pipeline child is given, built from a list rather than inherited.
 *
 * **Why a list.** `src/venator/llm/environment.py` builds the environment for the `claude`
 * child from an allowlist so a stale `ANTHROPIC_API_KEY` in somebody's shell cannot outrank
 * their subscription login and bill an API account silently. This is the same discipline one
 * process earlier: what the server passes down is what it named, so a variable that reaches
 * the pipeline reached it because somebody put it on this list.
 *
 * The document runtime comes from this Install's explicit setting, never an inherited
 * environment variable. There is no fallback if the chosen runtime is unavailable.
 *
 * **`$GIT_DIR` and friends are not on it either.** `venator.schedule.loop` strips them before
 * every `git` it runs, because with `$GIT_DIR` set a shipped Install with no repository would
 * sail past the commit stage's work-tree gate. The dashboard never asks for the commit stage,
 * so this is a second lock rather than the only one — but a server that forwarded them would
 * be handing the loop the exact environment its own gate was written against.
 */

import { documentRuntime } from "../install-settings.ts";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { asMapping, asNumber, parseJson } from "../onboarding/json.ts";
import { DATA_DIRECTORY_ENVIRONMENT, PYTHON_ENVIRONMENT, BUNDLED_PYTHON_ENVIRONMENT, applicationDataDirectory, systemContext, type LocationContext } from "../locations.ts";

/**
 * Everything a pipeline child is allowed to see, and why each one is here.
 *
 * `PATH` and the home variables so a subprocess can find its own tools and its own
 * configuration; `TMPDIR` and the Windows equivalents because Python writes there; the locale
 * variables because they decide how text is decoded; `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`
 * because Discover makes HTTPS requests on a machine that may carry its own certificate
 * store; the proxy variables for the same reason. The `VENATOR_*` three are this Install
 * saying where it lives and what interpreter it runs.
 */
const FORWARDED: readonly string[] = [
	"PATH",
	"HOME",
	"USERPROFILE",
	"SYSTEMROOT",
	"WINDIR",
	"COMSPEC",
	"PATHEXT",
	"APPDATA",
	"LOCALAPPDATA",
	"XDG_DATA_HOME",
	"TMPDIR",
	"TEMP",
	"TMP",
	"LANG",
	"LC_ALL",
	"LC_CTYPE",
	"SSL_CERT_FILE",
	"SSL_CERT_DIR",
	"REQUESTS_CA_BUNDLE",
	"HTTP_PROXY",
	"HTTPS_PROXY",
	"NO_PROXY",
	"http_proxy",
	"https_proxy",
	"no_proxy",
	DATA_DIRECTORY_ENVIRONMENT,
	PYTHON_ENVIRONMENT,
	BUNDLED_PYTHON_ENVIRONMENT,
	// The desktop host sets this to its signed Node sidecar; the staged Playwright driver
	// has no private Node and must receive the override in every pipeline child.
	"PLAYWRIGHT_NODEJS_PATH",
];

/**
 * The child environment, plus the one variable this server sets itself.
 *
 * `PYTHONUNBUFFERED=1` is not decoration: the loop flushes its own three stage lines, but
 * everything else a stage prints sits in the child's pipe buffer until it fills. Without it a
 * failed run's transcript is whatever happened to have been flushed, which is the least
 * useful part of it.
 */
export type ChildEnvironment = Record<string, string>;

// Every Python adapter and pipeline child uses one of the environments below. In a desktop
// Install its interpreter lives inside the signed app bundle; imports must never write there.
export const NO_BYTECODE_ENVIRONMENT = "PYTHONDONTWRITEBYTECODE";

/** Onboarding tools do not receive the retired TypeSafe credential. */
export function inheritedToolEnvironment(context: LocationContext = systemContext()): NodeJS.ProcessEnv {
	const environment: NodeJS.ProcessEnv = { ...process.env, VENATOR_LLM_RUNTIME: documentRuntime(context), [NO_BYTECODE_ENVIRONMENT]: "1" };
	delete environment.TYPESAFE_API_KEY;
	return environment;
}

export function runEnvironment(context: LocationContext): ChildEnvironment {
	const named: [string, string][] = [["PYTHONUNBUFFERED", "1"], [NO_BYTECODE_ENVIRONMENT, "1"]];
	for (const name of FORWARDED) {
		const value = context.environment(name);
		if (value !== undefined) named.push([name, value]);
	}
	named.push(["VENATOR_LLM_RUNTIME", documentRuntime(context)]);
	const settings = join(applicationDataDirectory(context), "daily-settings.json");
	if (existsSync(settings)) {
		const record = asMapping(parseJson(readFileSync(settings, "utf8")));
		for (const [field, variable] of [["dailyUsd", "VENATOR_SCORE_DAILY_USD"], ["monthlyUsd", "VENATOR_SCORE_MONTHLY_USD"]] as const) {
			const amount = asNumber(record?.[field]);
			if (amount !== null && amount > 0 && Number.isFinite(amount)) named.push([variable, String(amount)]);
		}
	}
	return Object.fromEntries(named);
}
