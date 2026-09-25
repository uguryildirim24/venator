/**
 * The check every write on this loopback server passes, in one place.
 *
 * **Why it exists.** The API binds 127.0.0.1 and that is the boundary (ADR-0002). CORS on
 * top of it is a browser-side courtesy, and it is not sufficient on its own: a page on some
 * other origin can post a form or a plain-text body to a loopback port without the browser
 * asking anyone's permission, because those requests are CORS-safelisted and are sent before
 * any policy is consulted. Requiring a header the request cannot carry without a preflight is
 * what turns the origin allowlist from advice into a gate — the preflight happens, the
 * allowlist refuses it, and the request is never sent.
 *
 * **It is not authentication.** It proves nothing about who is asking. Its only job is to
 * force the preflight, which is why the value is a literal `1` and why each surface passes
 * its own header name: a run request that claimed to be an onboarding request would be a
 * small lie in the one place the code should be literal.
 *
 * This module refuses and says why; it throws nothing. Each surface raises its own error
 * type from the answer, so neither surface's error vocabulary leaks into the other's.
 */

import { API_SERVER_PORT, APP_DEV_PORT } from "../../shared/ports.ts";

/**
 * The origins that may act on this Install. The same list the dashboard's read API allows —
 * the action surfaces need no others. The tauri entries are what a packaged desktop build
 * presents.
 */
export const LOCAL_ACTION_ORIGINS: readonly string[] = [
	`http://localhost:${APP_DEV_PORT}`,
	`http://127.0.0.1:${APP_DEV_PORT}`,
	`http://localhost:${API_SERVER_PORT}`,
	`http://127.0.0.1:${API_SERVER_PORT}`,
	"tauri://localhost",
	"http://tauri.localhost",
];

export type LocalActionRefusal = {
	readonly reason: "forbidden_origin" | "bad_request";
	readonly message: string;
	readonly remedy: string;
};

/**
 * Null when the request may act; a refusal, in the words to answer with, when it may not.
 *
 * A request with no `Origin` at all is not refused on that count — a command-line client on
 * the Owner's own machine sends none, and the loopback binding is what is actually
 * protecting this. It still has to carry the header.
 */
export function checkLocalAction(
	headerName: string,
	expected: string,
	headerValue: string | undefined,
	origin: string | undefined,
): LocalActionRefusal | null {
	if (origin !== undefined && !LOCAL_ACTION_ORIGINS.includes(origin)) {
		return {
			reason: "forbidden_origin",
			message: "This request came from a page that is not part of this Install.",
			remedy: "Open the dashboard from its own address.",
		};
	}
	if (headerValue !== expected) {
		return {
			reason: "bad_request",
			message: `A request that acts on this Install must carry the ${headerName} header.`,
			remedy: "The dashboard sends it on every such request; a request without it did not come from the dashboard.",
		};
	}
	return null;
}
