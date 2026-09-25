/**
 * Which Profiles this Install already has, and which one the pipeline would pick.
 *
 * This reads directories; it does not load Profiles. `src/venator/profile/loader.py` is the
 * only thing that decides what a Profile means, and a second full reader in a second language
 * would be a second opinion. What the dashboard needs is narrower: is there a Profile here at
 * all, is one of them a scaffold, and would the pipeline be able to imply one.
 *
 * The scaffold check is the one place that reads inside a file, and it is a deliberately
 * narrow text scan rather than a parser — enough to see `scaffold: true` under `profile:`,
 * and honest about being no more than that. It matters because `profiles/example/` ships with
 * every checkout: counting the scaffold as a Profile would send a person who has never
 * configured anything straight past onboarding into somebody else's search.
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import type { DiscoveredProfile, ProfileLocationKind } from "../../shared/onboarding.ts";
import { type LocationContext, PROFILE_FILES, profileSearchRoots, systemContext, writableProfilesRoot } from "../locations.ts";

/**
 * Reads `profile.scaffold` out of a targeting.yaml by scanning for it.
 *
 * Only a top-level `profile:` block counts, only its own indented lines are read, comment
 * lines are ignored, and anything that is not literally `true` is false — the same default
 * the loader applies. Anything unreadable is false, because a Profile that cannot be read is
 * not a scaffold; it is a problem the pipeline will name.
 */
export function readsAsScaffold(targetingPath: string): boolean {
	let text = "";
	try {
		text = readFileSync(targetingPath, "utf8");
	} catch {
		return false;
	}
	let insideProfile = false;
	for (const raw of text.split("\n")) {
		const line = raw.replace(/\r$/u, "");
		const trimmed = line.trim();
		if (trimmed === "" || trimmed.startsWith("#")) continue;
		const indented = /^\s/u.test(line);
		if (!indented) {
			insideProfile = trimmed === "profile:";
			continue;
		}
		if (!insideProfile) continue;
		const match = /^scaffold:\s*(\S+)/u.exec(trimmed);
		if (match === null) continue;
		return (match[1] ?? "").replace(/#.*$/u, "").trim() === "true";
	}
	return false;
}

function profileFilesIn(directory: string): readonly string[] {
	return PROFILE_FILES.filter((file) => {
		const stats = statSync(join(directory, file), { throwIfNoEntry: false });
		return stats !== undefined && stats.isFile();
	});
}

function readRoot(root: string, kind: ProfileLocationKind): readonly DiscoveredProfile[] {
	let entries: readonly string[] = [];
	try {
		entries = readdirSync(root);
	} catch {
		return [];
	}
	const found: DiscoveredProfile[] = [];
	for (const name of [...entries].sort()) {
		if (name.startsWith(".")) continue;
		const directory = join(root, name);
		const stats = statSync(directory, { throwIfNoEntry: false });
		if (stats === undefined || !stats.isDirectory()) continue;
		const files = profileFilesIn(directory);
		// Python's select.py requires targeting.yaml. A half-written directory with
		// only a resume or constraints has no Hard Filter and is not a Profile.
		if (!files.includes("targeting.yaml")) continue;
		found.push({
			name,
			directory,
			kind,
			scaffold: readsAsScaffold(join(directory, "targeting.yaml")),
			files,
		});
	}
	return found;
}

/**
 * Every Profile this Install can see, in the pipeline's own search order: the Install first,
 * then the checkout. A name found twice is listed once, at the location
 * that wins — which is the location the pipeline would actually load.
 */
export function discoverProfiles(context: LocationContext = systemContext()): readonly DiscoveredProfile[] {
	const writable = writableProfilesRoot(context);
	const found: DiscoveredProfile[] = [];
	const claimed = new Set<string>();
	for (const root of profileSearchRoots(context)) {
		const kind: ProfileLocationKind = root === writable ? "install" : "checkout";
		for (const profile of readRoot(root, kind)) {
			if (claimed.has(profile.name)) continue;
			claimed.add(profile.name);
			found.push(profile);
		}
	}
	return found;
}

/**
 * The Profile a stage would run against with no `--profile`: an explicit
 * `VENATOR_PROFILE` (including a scaffold for a fresh example run), otherwise
 * the only one that is not a scaffold. Null when the named Profile is absent
 * or when more than one non-scaffold Profile exists.
 */
export function impliedProfile(
	profiles: readonly DiscoveredProfile[],
	context: LocationContext = systemContext(),
): DiscoveredProfile | null {
	const named = context.environment("VENATOR_PROFILE")?.trim();
	if (named) return profiles.find((profile) => profile.name === named) ?? null;
	const selectable = profiles.filter((profile) => !profile.scaffold);
	return selectable.length === 1 ? (selectable[0] ?? null) : null;
}
