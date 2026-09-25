/**
 * Mount order, asserted — because it is invisible and it fails in only one place.
 *
 * Hono's `cors` middleware answers a preflight from inside itself: it returns a 204 without
 * calling `next()`. A router mounted at `/api` with `use("*")` becomes `/api/*` in the parent,
 * so it matches every sibling path underneath it. With `/api` mounted first, an
 * `OPTIONS /api/runs` therefore comes back `Access-Control-Allow-Methods: GET, OPTIONS` — the
 * dashboard API's read-only policy answering for a surface that is not it — and a browser
 * refuses the `POST` before it is ever sent.
 *
 * `POST` survives that today only because it is a CORS-safelisted method and because the
 * read-only policy echoes arbitrary request headers back. A perfectly reasonable future
 * tightening of that policy would break every write route under `/api`, and nothing would say
 * so: cross-origin is the packaged desktop case (`tauri://localhost` against
 * `http://127.0.0.1:5170`, `ui/.env.desktop`), and `pnpm dev` never sees it because Vite
 * proxies `/api` and every request is same-origin.
 *
 * So the property under test is not "the routes exist". It is: **a preflight at a write path
 * is answered by that path's own surface.** Reordering the two mounts in `server/app.ts` turns
 * these cases red, which is the whole point of them.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { createServer } from "../server/app.ts";
import { createOnboardingRoutes } from "../server/onboarding/routes.ts";
import { ONBOARDING_REQUEST_HEADER } from "../shared/onboarding.ts";
import { RUN_REQUEST_HEADER } from "../shared/runs.ts";

const DESKTOP_ORIGIN = "tauri://localhost";

/**
 * Every route the onboarding surface has, written out here as well as in its router.
 *
 * CLAUDE.md and `coordination/CONTRACTS.md` both enumerate this surface exhaustively, and
 * they say outright that the enumeration *is* the guarantee — a sentence that only holds while
 * the list and the router agree. So the list is duplicated here on purpose and compared
 * against what Hono actually registered, in both directions: a sixth route added without a
 * line in this file turns the case below red, and so does deleting one of these five.
 */
const ONBOARDING_WRITE_PATHS: readonly string[] = [
	"/runtime/probe",
	"/resume/import",
	"/boards/resolve",
	"/profile",
	"/employers",
	"/settings",
];

function preflight(path: string, header: string): Request {
	return new Request(`http://127.0.0.1:5170${path}`, {
		method: "OPTIONS",
		headers: {
			origin: DESKTOP_ORIGIN,
			"access-control-request-method": "POST",
			"access-control-request-headers": `content-type, ${header}`,
		},
	});
}

function allowedMethods(response: Response): readonly string[] {
	return (response.headers.get("access-control-allow-methods") ?? "")
		.split(",")
		.map((method) => method.trim())
		.filter((method) => method !== "");
}

test("the header each action surface requires is the one its contract names", () => {
	// Spelled out literally, once. Every other case here builds its request from the constant,
	// which is the right shape — whatever header the surface requires, its own preflight has to
	// allow it — but it also means a renamed or emptied constant would be followed rather than
	// caught. `coordination/CONTRACTS.md` names both of these; this is where that is checked.
	assert.equal(RUN_REQUEST_HEADER, "x-venator-run");
	assert.equal(ONBOARDING_REQUEST_HEADER, "x-venator-onboarding");
});

test("a preflight at the run surface is answered by the run surface, not by the read-only API", async () => {
	const response = await createServer().request(preflight("/api/runs", RUN_REQUEST_HEADER));

	assert.ok(response.status === 204 || response.status === 200, `preflight status ${String(response.status)}`);
	assert.ok(
		allowedMethods(response).includes("POST"),
		"the browser is told POST is allowed, or it never sends the request",
	);
	assert.equal(response.headers.get("access-control-allow-origin"), DESKTOP_ORIGIN);
});

test("and at every other write path on it", async () => {
	const app = createServer();
	for (const path of ["/api/runs/plan", "/api/runs/current/stop"]) {
		const response = await app.request(preflight(path, RUN_REQUEST_HEADER));
		assert.ok(allowedMethods(response).includes("POST"), `${path} preflight: ${allowedMethods(response).join(",")}`);
	}
});

test("the run surface's own header survives the preflight it exists to force", async () => {
	const response = await createServer().request(preflight("/api/runs/plan", RUN_REQUEST_HEADER));
	const allowed = (response.headers.get("access-control-allow-headers") ?? "").toLowerCase();

	// The header is not authentication. Its only job is to make the request non-simple so the
	// origin allowlist gets consulted at all — which means the preflight has to permit it, or
	// the dashboard's own requests never arrive.
	//
	// Stated because it was measured: this case stays green with the mounts in the wrong
	// order, because the dashboard API's policy echoes whatever headers a preflight asks for.
	// That echo is half of why the wrong order is survivable today, and it is exactly what a
	// reasonable tightening of that policy would remove. The two cases above are the ones that
	// go red, and they are the reason this file exists.
	assert.ok(allowed.includes(RUN_REQUEST_HEADER), `allow-headers was “${allowed}”`);
});

test("the onboarding surface is the five POST routes its contract names, and nothing else", () => {
	const registered = createOnboardingRoutes()
		.routes.filter((route) => route.method !== "ALL")
		.map((route) => `${route.method} ${route.path}`);
	assert.deepEqual(
		[...registered].sort(),
		[...ONBOARDING_WRITE_PATHS.map((path) => `POST ${path}`), "GET /settings"].sort(),
	);
});

/**
 * A concrete URL a registered path pattern matches.
 *
 * `/*` and `/:name` are patterns, not addresses, so a probe has to make one. The substituted
 * segment is deliberately not a word anybody would route on: what these cases assert is that
 * nothing answers there, and a collision with a real path would make that assertion trivially
 * true for the wrong reason.
 */
function concretePath(pattern: string): string {
	return pattern.replaceAll(/\*/gu, "venator-probe").replaceAll(/:[^/]+/gu, "venator-probe");
}

/**
 * The hole the case above cannot see, closed.
 *
 * `method !== "ALL"` is necessary — Hono registers `use()` middleware as `ALL` — and it is a
 * gap the moment somebody writes `onboarding.all("/anything", handler)`, because Hono's route
 * table records `use()` and `.all()` identically: same method, same shape, and a handler's
 * `length` tells the two apart only until a middleware is written without its `next`. There is
 * nothing in the registration to assert on.
 *
 * So this asserts on behaviour instead, against the built router. **Middleware falls through
 * and a handler does not.** Every `ALL` registration is probed at a concrete path only it can
 * match, with every method, and every one has to reach the 404 — which is what a router with
 * nothing but middleware at that path does. A route smuggled in as `.all()` answers instead,
 * and this goes red.
 *
 * `OPTIONS` is left out on purpose and is not a gap: the CORS middleware answers a preflight
 * from inside itself for every path under this surface, which is the property the top of this
 * file exists to protect. The five cases above are what watch that.
 */
test("every ALL registration on the onboarding surface is middleware, which falls through", async () => {
	const onboarding = createOnboardingRoutes();
	const everyMethod = onboarding.routes.filter((route) => route.method === "ALL");
	assert.ok(everyMethod.length > 0, "no ALL registration at all: this case would pass vacuously");

	for (const route of everyMethod) {
		const path = concretePath(route.path);
		assert.ok(
			!ONBOARDING_WRITE_PATHS.includes(path),
			`an ALL registration sits at ${path}, which is one of the five contract paths`,
		);
		for (const method of ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]) {
			const response = await onboarding.request(
				new Request(`http://127.0.0.1:5170${path}`, {
					method,
					headers: { [ONBOARDING_REQUEST_HEADER]: "1", origin: DESKTOP_ORIGIN },
				}),
			);
			assert.equal(
				response.status,
				404,
				`${method} ${path} (registered as ALL ${route.path}) answered ${String(response.status)} instead of falling through`,
			);
		}
	}
});

/**
 * And the same thing said from the outside, so the enumeration does not rest on the route
 * table being readable at all.
 *
 * Nothing but `POST` at the five contract paths is answered by this surface — not another
 * method at one of them, and not any method at a path that is not one of them. A route added
 * anywhere else, registered any way at all, turns this red without anyone having to know how
 * Hono records it.
 */
test("nothing on the onboarding surface answers outside the five POST routes", async () => {
	const onboarding = createOnboardingRoutes();
	const elsewhere = ["/", "/anything", "/smuggled", "/profile/x", "/employers/all", "/venator-probe"];
	for (const path of [...ONBOARDING_WRITE_PATHS, ...elsewhere]) {
		for (const method of ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]) {
			if ((method === "POST" && ONBOARDING_WRITE_PATHS.includes(path)) || (["GET", "HEAD"].includes(method) && path === "/settings")) continue;
			const response = await onboarding.request(
				new Request(`http://127.0.0.1:5170${path}`, {
					method,
					headers: { [ONBOARDING_REQUEST_HEADER]: "1", origin: DESKTOP_ORIGIN },
				}),
			);
			assert.equal(response.status, 404, `${method} ${path} answered ${String(response.status)}`);
		}
	}
});

/**
 * Everything the **served app** registers under the onboarding prefix, whichever router owns it.
 *
 * The three cases above ask `createOnboardingRoutes()` — the router on its own — and that is a
 * hole rather than a guarantee: the enumeration is what CLAUDE.md and
 * `coordination/CONTRACTS.md` say the never-submit invariant rests on, and the thing a person
 * can reach is the app, not the router. Registering `app.put("/api/onboarding/smuggled", …)` in
 * `server/app.ts` — beside the router rather than inside it — answered `200` with the whole
 * suite green. This list is against `createServer()`, so a route added anywhere in the mounting
 * has to appear here.
 *
 * `GET /api/onboarding/state` is in it and is not a sixth write route: it belongs to the
 * dashboard API, which is `GET`-only and read-only and is mounted at `/api`. It is enumerated
 * here because a reader of this list has to be able to see everything that answers under this
 * prefix, and "it is somebody else's router" is not something a request can tell.
 */
const SERVED_ONBOARDING_ROUTES: readonly string[] = [
	...ONBOARDING_WRITE_PATHS.map((path) => `POST /api/onboarding${path}`),
	"GET /api/onboarding/state",
	"GET /api/onboarding/settings",
];

test("the served app registers nothing under /api/onboarding but the routes its contract names", () => {
	const registered = createServer()
		.routes.filter((route) => route.method !== "ALL" && route.path.startsWith("/api/onboarding"))
		.map((route) => `${route.method} ${route.path}`);

	assert.deepEqual([...registered].sort(), [...SERVED_ONBOARDING_ROUTES].sort());
});

/**
 * And the same thing asked of the app's behaviour, so it does not rest on the route table
 * being readable — a route registered as `ALL` does not appear in the case above at all.
 *
 * `GET` and `HEAD` are not probed here and it is not a gap: with a built bundle on disk the
 * app serves `dist/index.html` for any unmatched path, which is what a single-page app has to
 * do, so a `GET` at a nonsense path is answered `200` by design. A smuggled `GET` is caught by
 * the route table above; every method that could *write* is caught here, whichever way it was
 * registered.
 */
test("and the served app answers no write method under /api/onboarding outside those routes", async () => {
	const app = createServer();
	const elsewhere = ["/", "/state", "/anything", "/smuggled", "/profile/x", "/employers/all", "/venator-probe"];
	for (const path of [...ONBOARDING_WRITE_PATHS, ...elsewhere]) {
		for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
			if (method === "POST" && ONBOARDING_WRITE_PATHS.includes(path)) continue;
			const response = await app.request(
				new Request(`http://127.0.0.1:5170/api/onboarding${path}`, {
					method,
					headers: { [ONBOARDING_REQUEST_HEADER]: "1", origin: DESKTOP_ORIGIN },
				}),
			);
			assert.equal(response.status, 404, `${method} /api/onboarding${path} answered ${String(response.status)}`);
		}
	}
});

test("the onboarding surface answers its own preflight too, which it did not before this mount order", async () => {
	// Measured on the arrangement this replaces: `OPTIONS /api/onboarding/profile` with a
	// desktop origin was answered `GET, OPTIONS` by the dashboard API's middleware, because
	// `/api` was mounted first and matched the sibling path. Same one-line cause, same fix.
	const app = createServer();
	for (const path of ONBOARDING_WRITE_PATHS) {
		const response = await app.request(preflight(`/api/onboarding${path}`, ONBOARDING_REQUEST_HEADER));
		assert.ok(
			allowedMethods(response).includes("POST"),
			`${path} preflight allow-methods was “${allowedMethods(response).join(",")}”`,
		);
	}
});

/**
 * Everything the served app registers under the run surface's prefix.
 *
 * The same hole the onboarding cases above close, closed on the surface where it matters more:
 * this is the one that starts processes. Every method case for the run surface probed four
 * paths it already knew, so `app.put("/api/runs/smuggled", …)` registered in `server/app.ts`
 * would answer while the whole suite stayed green — on the surface whose enumeration is what
 * the never-submit invariant rests on.
 *
 * This list is the one `ui/RUN-API.md`, `coordination/CONTRACTS.md` and CLAUDE.md all state:
 * three `POST` routes, and two `GET`s that read this server process's own memory rather than
 * any store. It is written out here as well, and compared against what Hono actually
 * registered, so a sixth route is a line somebody adds on purpose and a reviewer can see.
 * Nothing about which *stages* a run may name is asserted here — that is `RUN_KIND_STAGES`'s
 * own closed set and its own tests; this case is only about what answers at a URL.
 */
const SERVED_RUN_ROUTES: readonly string[] = [
	"POST /api/runs/plan",
	"POST /api/runs",
	"POST /api/runs/current/stop",
	"GET /api/runs/current",
	"GET /api/runs/recent",
];

test("the served app registers nothing under /api/runs but the routes its contract names", () => {
	const registered = createServer()
		.routes.filter((route) => route.method !== "ALL" && route.path.startsWith("/api/runs"))
		.map((route) => `${route.method} ${route.path}`);

	assert.deepEqual([...registered].sort(), [...SERVED_RUN_ROUTES].sort());
});

/**
 * And the same asked of behaviour, so it does not rest on the route table being readable — a
 * route registered as `ALL` never appears in the case above.
 *
 * Only methods that could *write* are probed, and never `POST` at a path the contract names:
 * `POST /api/runs` is the start route, and a test that is willing to press it is a test that
 * can start a pipeline run. `GET` and `HEAD` are left out for the reason the onboarding case
 * gives — with a built bundle on disk the app answers any unmatched path with the app itself —
 * and a smuggled `GET` is what the route table above catches.
 */
test("and the served app answers no write method under /api/runs outside those routes", async () => {
	const app = createServer();
	const contractPaths = ["", "/plan", "/current", "/current/stop", "/recent"];
	const elsewhere = ["/smuggled", "/anything", "/plan/x", "/current/start", "/venator-probe"];
	for (const path of [...contractPaths, ...elsewhere]) {
		for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
			// Never POST at a contract path: one of them starts a run.
			if (method === "POST" && contractPaths.includes(path)) continue;
			const response = await app.request(
				new Request(`http://127.0.0.1:5170/api/runs${path}`, {
					method,
					headers: { [RUN_REQUEST_HEADER]: "1", origin: DESKTOP_ORIGIN },
				}),
			);
			assert.equal(response.status, 404, `${method} /api/runs${path} answered ${String(response.status)}`);
		}
	}
});

test("the dashboard API is still answered as read-only, and still answers", async () => {
	const app = createServer();

	const preflighted = await app.request(
		new Request("http://127.0.0.1:5170/api/summary", {
			method: "OPTIONS",
			headers: { origin: DESKTOP_ORIGIN, "access-control-request-method": "GET" },
		}),
	);
	assert.deepEqual(allowedMethods(preflighted), ["GET", "OPTIONS"]);

	const summary = await app.request(
		new Request("http://127.0.0.1:5170/api/summary", { headers: { origin: DESKTOP_ORIGIN } }),
	);
	assert.equal(summary.status, 200);
});

test("a page from somewhere else is refused at the preflight, whatever it asks for", async () => {
	const response = await createServer().request(
		new Request("http://127.0.0.1:5170/api/runs/plan", {
			method: "OPTIONS",
			headers: {
				origin: "https://boards.example.com",
				"access-control-request-method": "POST",
				"access-control-request-headers": RUN_REQUEST_HEADER,
			},
		}),
	);

	// No allow-origin header means the browser refuses the request before sending it. That is
	// the whole mechanism: the run surface's header cannot be set without this preflight.
	assert.equal(response.headers.get("access-control-allow-origin"), null);
});

test("there is no PUT, PATCH or DELETE anywhere on the onboarding surface either", async () => {
	const app = createServer();
	for (const method of ["PUT", "PATCH", "DELETE"]) {
		for (const path of ONBOARDING_WRITE_PATHS) {
			const response = await app.request(
				new Request(`http://127.0.0.1:5170/api/onboarding${path}`, {
					method,
					headers: { [ONBOARDING_REQUEST_HEADER]: "1", origin: DESKTOP_ORIGIN },
				}),
			);
			assert.equal(response.status, 404, `${method} ${path} answered ${String(response.status)}`);
		}
	}
});

test("there is no PUT, PATCH or DELETE anywhere on the run surface", async () => {
	const app = createServer();
	for (const method of ["PUT", "PATCH", "DELETE"]) {
		for (const path of ["/api/runs", "/api/runs/plan", "/api/runs/current", "/api/runs/current/stop"]) {
			const response = await app.request(
				new Request(`http://127.0.0.1:5170${path}`, {
					method,
					headers: { [RUN_REQUEST_HEADER]: "1", origin: DESKTOP_ORIGIN },
				}),
			);
			assert.equal(response.status, 404, `${method} ${path} answered ${String(response.status)}`);
		}
	}
});
