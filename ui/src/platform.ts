/**
 * Which host the dashboard is running in, and — when it is the desktop host — which operating
 * system that host is on.
 *
 * Tauri v2 sets `isTauri` on the window before any app code runs; in a browser it is simply
 * absent. The desktop window is opaque on every platform: the app draws its own grounds there
 * exactly as it does in a tab. What differs is the frame. On macOS the title bar is overlaid,
 * so the traffic lights sit over the top of the sidebar, and over the toolbar when the sidebar
 * is hidden. On Windows and Linux the title bar is the system's own bar above the webview.
 *
 * **The user agent is read here and nowhere else, and it decides nothing a person acts on.**
 * `server/platform.ts` remains the authority on which machine an Install is running on —
 * `process.platform`, travelling in the onboarding response — because that answer picks the
 * install instructions somebody will follow literally. This answer picks a toolbar inset, it
 * is needed before the first paint rather than after a fetch, and being wrong in either
 * direction costs nothing but a gap. Nothing here is recorded and nothing here is about a
 * person.
 */

import type { Platform } from "../shared/onboarding.ts";

declare global {
	interface Window {
		readonly isTauri?: boolean;
	}
}

export const isDesktop = window.isTauri === true;

/**
 * The operating system named in a user agent string, in the onboarding contract's own words.
 *
 * One token each, matched in the order that keeps them exclusive: every desktop webview says
 * `Macintosh` (WKWebView), `Windows NT` (WebView2) or `Linux` (WebKitGTK). Anything else —
 * an empty string, a user agent somebody rewrote, a platform nobody has shipped this to yet —
 * is `unknown` rather than the nearest guess, which is the rule the Hard Filters follow and
 * the same one `hostPlatform` follows on the server.
 */
export function platformFromUserAgent(userAgent: string): Platform {
	if (userAgent.includes("Macintosh")) return "macos";
	if (userAgent.includes("Windows")) return "windows";
	if (userAgent.includes("Linux")) return "linux";
	return "unknown";
}

/** The desktop host's operating system. `unknown` in a browser, where it decides nothing. */
export const desktopPlatform: Platform = isDesktop ? platformFromUserAgent(navigator.userAgent) : "unknown";

/**
 * What the app's outermost element carries, given the host.
 *
 * `app-native` says the window is the system's, so the app stops drawing one of its own. The
 * browser build floats a rounded, shadowed sheet on a canvas ground; inside a real window that
 * sheet would be a second window edge a few pixels inside the first. True of every desktop
 * host, whatever its operating system.
 */
export const nativeClasses: string = isDesktop ? "app-native" : "";

/**
 * Whether the toolbar has to leave room for window controls drawn on top of it.
 *
 * Only macOS does that, and only because the window asks for it: `titleBarStyle: "Overlay"`
 * puts the traffic lights over the top-left of the window. With the sidebar showing they sit
 * over the sidebar; with it hidden they sit over the toolbar, which then keeps that gap clear.
 */
export const reservesTrafficLights: boolean = isDesktop && desktopPlatform === "macos";
