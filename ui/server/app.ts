/**
 * The four routers this server mounts, and the order it mounts them in.
 *
 * Four surfaces, deliberately separate, and the separation is the contract rather than
 * tidiness — each one's guarantee is an enumeration short enough to read:
 *
 * - `/api` is the dashboard: `GET` only, read-only, observing the pipeline. It writes nothing
 *   and spawns nothing, and the view handle is `readOnly: true`.
 * - `/api/onboarding` writes three Profile files into this Install's application data
 *   directory and nothing else. See server/onboarding/routes.ts.
 * - `/api/runs` starts the pipeline stages a person could otherwise only start from a
 *   terminal, and starts nothing else. Node writes nothing there: what a run writes, it writes
 *   by running the same stages the CLI runs. See server/runs/routes.ts.
 * - `/api/applications` prepares, saves and hands off application work through the Python
 *   application service. It is separate from the read-only dashboard API.
 *
 * **Mount order is load-bearing.** Hono's CORS middleware answers a preflight from inside
 * itself and the first matching router wins, so a router mounted at `/api` answers the
 * preflight for every sibling path beneath it. `/api` is therefore mounted **last**, after the
 * two surfaces that need `POST` in their preflight answer. This fails only cross-origin —
 * which is the packaged desktop case, `tauri://localhost` against `http://127.0.0.1:5170` —
 * and never in `pnpm dev`, where Vite proxies `/api` and every request is same-origin.
 *
 * That is exactly the kind of thing diagnosed as "the app is broken" a week after it merges,
 * so it is asserted rather than commented: this module is separate from `main.ts` so
 * `tests/runs-mounting.test.ts` can build the app and drive `OPTIONS` at it without binding a
 * port or starting a process.
 */

import { existsSync } from "node:fs";
import { relative, resolve } from "node:path";

import { serveStatic } from "@hono/node-server/serve-static";
import { Hono } from "hono";

import { API_SERVER_PORT, APP_DEV_PORT } from "../shared/ports.ts";
import { APP_BUNDLE_PATH } from "./locations.ts";
import { createOnboardingRoutes } from "./onboarding/routes.ts";
import { createApiRoutes } from "./routes.ts";
import { createRunRoutes } from "./runs/routes.ts";
import { createApplicationRoutes } from "./applications/routes.ts";

const BUNDLE_MISSING_NOTICE = [
	"venator review dashboard",
	"",
	"The API is running, but the app bundle has not been built.",
	`  development:  pnpm dev      (Vite on http://127.0.0.1:${APP_DEV_PORT}, proxying /api here)`,
	"  production:   pnpm build    (writes ui/dist, then reload this page)",
	"",
	`API root: http://127.0.0.1:${API_SERVER_PORT}/api/summary`,
	"",
].join("\n");

export function createServer(): Hono {
	const app = new Hono();
	// The three action surfaces are mounted BEFORE the dashboard API, and the order is measured
	// rather than reasoned: `cors` answers an `OPTIONS` from inside the middleware without
	// calling `next()`, and a router mounted at `/api` with `use("*")` matches every sibling
	// path under it. With `/api` first, an `OPTIONS /api/runs` from `tauri://localhost` is
	// answered `Access-Control-Allow-Methods: GET, OPTIONS` and the browser refuses the `POST`
	// before sending it. All three are mounted apart from the dashboard API for the same
	// reason they are separate at all: the dashboard API is `GET`-only and read-only and stays
	// that way.
	//
	// The run surface: it starts pipeline stages and nothing else, it never reaches an
	// employer, and Node writes no file on it. ui/RUN-API.md is its contract;
	// server/runs/routes.ts states the limits.
	app.route("/api/runs", createRunRoutes());
	app.route("/api/applications", createApplicationRoutes());
	// The onboarding write surface: the one place a file is written from Node, into this
	// Install's application data directory and nowhere else, and it never performs a
	// state-changing request against an employer or touches submission.
	// ui/ONBOARDING-API.md is its contract; server/onboarding/routes.ts states the limits.
	app.route("/api/onboarding", createOnboardingRoutes());
	app.route("/api", createApiRoutes());

	if (existsSync(APP_BUNDLE_PATH)) {
		// serveStatic resolves against the process cwd, so the bundle is addressed relatively.
		const root = relative(process.cwd(), APP_BUNDLE_PATH);
		app.use("/*", serveStatic({ root }));
		app.get("*", serveStatic({ path: resolve(root, "index.html") }));
	} else {
		app.get("/", (context) => context.text(BUNDLE_MISSING_NOTICE));
	}

	return app;
}
