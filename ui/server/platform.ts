/**
 * Which operating system this server is running on, in the onboarding contract's own words.
 *
 * **The server is the one party that knows.** It runs on the person's own machine — as the
 * API behind `pnpm dev`, and as the sidecar inside the desktop host — so `process.platform`
 * is a fact about the machine a screen is being read on. A user agent is not: it can be
 * absent, it can be edited, and in the desktop webview it is the shell's rather than the
 * system's. So the platform is decided here and travels in the response, and no screen
 * sniffs for it.
 *
 * **Every platform this mapping does not name resolves to `unknown`.** Not to the nearest
 * neighbour, not to whichever one is most likely: an install route is a set of instructions
 * somebody will follow literally, and a confidently wrong one is worse than none. `unknown`
 * is a real answer with copy of its own — every route, side by side — on the same principle
 * the Hard Filters follow when a value is not one they know.
 *
 * Nothing here is about a person. It is one word about a machine, it identifies nobody, and
 * it is recorded nowhere: not in `data/`, not in the view, not in a Profile.
 */

import type { Platform } from "../shared/onboarding.ts";

/**
 * `process.platform` as the contract's own value.
 *
 * The parameter exists so a test can state a machine rather than be one — the same seam
 * `LocationContext` gives the path resolver, kept small because this is one mapping and has
 * no business reading an environment.
 */
export function hostPlatform(platform: NodeJS.Platform = process.platform): Platform {
	if (platform === "darwin") return "macos";
	if (platform === "win32") return "windows";
	if (platform === "linux") return "linux";
	return "unknown";
}
