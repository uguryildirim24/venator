/**
 * Starts the read-only API as a child process and waits until it answers.
 *
 * The verification tools own their server rather than expecting one to be running: the smoke
 * test pins the fixture so its assertions never depend on what the pipeline last wrote, and
 * both tools use a port of their own so they cannot collide with a dev session on 5170.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";

import { SAMPLE_DATA_ENVIRONMENT, UI_ROOT } from "../server/locations.ts";

export type ApiOptions = {
	/**
	 * Absolute or ui-relative path to a view database, or null to use the normal fallback.
	 * An explicit path is opened as given: the caller owns building it if it does not exist,
	 * because only the fallback builds the fixture on first use.
	 */
	readonly databasePath: string | null;
	readonly port: number;
	/**
	 * Whether this API may serve the sample Postings.
	 *
	 * The server refuses them to a run that did not ask (`server/db.ts`), whether they arrive
	 * by fallback or by name, so a tool that wants a populated dashboard says so here. Default
	 * false, because the default has to be what a shipped Install gets — a tool that forgets to
	 * ask then renders an Install with nothing in it, which is a wrong screenshot rather than
	 * somebody else's Postings.
	 */
	readonly sampleData?: boolean;
};

export type RunningApi = {
	readonly origin: string;
	readonly stop: () => void;
};

function delay(milliseconds: number): Promise<void> {
	return new Promise((done) => setTimeout(done, milliseconds));
}

async function waitForApi(origin: string, child: ChildProcess): Promise<void> {
	for (let attempt = 0; attempt < 60; attempt += 1) {
		if (child.exitCode !== null) {
			throw new Error(`the API exited with code ${child.exitCode} before answering`);
		}
		const probe = await fetch(`${origin}/api/summary`).catch(() => null);
		if (probe !== null && probe.ok) return;
		await delay(100);
	}
	child.kill("SIGTERM");
	throw new Error(`the API did not answer on ${origin} within 6s`);
}

export async function startApi(options: ApiOptions): Promise<RunningApi> {
	const environment = { ...process.env, VENATOR_API_PORT: String(options.port) };
	if (options.databasePath !== null) {
		environment["VENATOR_VIEW_DB"] = resolve(UI_ROOT, options.databasePath);
	}
	// Set or cleared, never inherited. A developer with this exported in their own shell would
	// otherwise turn the pass that proves a shipped Install shows nothing into one that shows
	// nineteen Postings, and it would pass.
	if (options.sampleData === true) environment[SAMPLE_DATA_ENVIRONMENT] = "1";
	else delete environment[SAMPLE_DATA_ENVIRONMENT];

	const child = spawn(process.execPath, ["server/main.ts"], {
		cwd: UI_ROOT,
		env: environment,
		stdio: ["ignore", "ignore", "inherit"],
	});

	const origin = `http://127.0.0.1:${options.port}`;
	await waitForApi(origin, child);
	return { origin, stop: () => child.kill("SIGTERM") };
}
