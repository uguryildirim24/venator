/**
 * Entry point for the dashboard API on 127.0.0.1:5170.
 *
 * In development Vite serves the app and proxies /api here. After `pnpm build` this same
 * process also serves dist/, which is the arrangement a desktop wrap reuses: one loopback
 * sidecar, no external origins.
 *
 * The four routers it serves, and the order they are mounted in, are `server/app.ts`.
 *
 * A live run belongs to this process and must not outlive it: the signal handlers at the
 * bottom of this file end one before the server exits, which is what stops a run from
 * spending the Owner's subscription with no window in existence that mentions it.
 */

import { serve } from "@hono/node-server";

import { API_SERVER_PORT } from "../shared/ports.ts";
import { createServer } from "./app.ts";
import { openViewDatabase } from "./db.ts";
import { SAMPLE_DATA_ENVIRONMENT } from "./locations.ts";
import { shutdownRuns } from "./runs/runner.ts";
import { shutdownApplications } from "./applications/routes.ts";

function describeDatabase(): string {
	try {
		const view = openViewDatabase();
		view.database.close();
		if (view.kind !== "empty") return `${view.kind} view at ${view.path}`;
		// Said in full rather than as one word, because this is the line a developer reads when
		// the dashboard they just started came up with nothing in it. It is not a failure: this
		// Install has built no view, and sample Postings belong to whoever they were scraped
		// for, so they are offered rather than assumed.
		return (
			`empty view: nothing is built at ${view.path} yet ` +
			`(\`python -m venator.view.build\` writes it; ${SAMPLE_DATA_ENVIRONMENT}=1 serves the sample Postings instead)`
		);
	} catch (error) {
		const detail = error instanceof Error ? error.message : "unreadable";
		return `no readable view (${detail})`;
	}
}

/** The desktop wrap and the smoke test both need to pick a port; everything else takes 5170. */
function port(): number {
	const configured = Number.parseInt(process.env["VENATOR_API_PORT"] ?? "", 10);
	return Number.isInteger(configured) && configured > 0 ? configured : API_SERVER_PORT;
}

const server = serve({ fetch: createServer().fetch, hostname: "127.0.0.1", port: port() }, (address) => {
	process.stdout.write(`api    http://127.0.0.1:${address.port}/api  ->  ${describeDatabase()}\n`);
});

/**
 * A run must not outlive the app that started it.
 *
 * This is the sharpest edge on the run surface. `venator.schedule.loop` spawns one subprocess
 * per stage, so quitting the desktop app during a run would otherwise leave a process tree
 * running with no window anywhere that mentions it, still polling employers' boards and
 * writing the Install's stores. `shutdownRuns` kills the whole tree; the server is then
 * closed and this process exits with the signal's own conventional status.
 */
for (const signal of ["SIGINT", "SIGTERM"] as const) {
	process.on(signal, () => {
		shutdownRuns();
		shutdownApplications();
		server.close(() => process.exit(signal === "SIGINT" ? 130 : 143));
	});
}
