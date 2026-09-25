/**
 * The one question the app asks before it decides what to show: does this Install have a
 * Profile yet?
 *
 * A fresh checkout ships `profiles/example/`, and it is marked `scaffold: true` precisely so
 * that nothing treats it as somebody's search. That is why `configured` counts non-scaffold
 * Profiles rather than directories, and why `ambiguous` exists: with two real Profiles the
 * pipeline refuses to guess which one a run belongs to, and the dashboard is in no better
 * position to guess than the pipeline is.
 */

import type { OnboardingStateResponse } from "../../shared/onboarding.ts";
import { systemContext, profileSearchRoots, writableProfilesRoot, type LocationContext } from "../locations.ts";
import { openViewDatabase, resolveView } from "../db.ts";
import { discoverProfiles, impliedProfile } from "./discovery.ts";

export function readOnboardingState(context: LocationContext = systemContext()): OnboardingStateResponse {
	const profiles = discoverProfiles(context);
	const selectable = profiles.filter((profile) => !profile.scaffold);
	const implied = impliedProfile(profiles, context);
	// A previous person's stale view must not block a fresh Install's setup screen.
	// With no Profile there is no View stage we could legitimately run yet.
	const source = implied === null && selectable.length === 0 ? { path: resolveView(context).path, kind: "empty" as const } : null;
	const viewInfo = source ?? (() => {
		const view = openViewDatabase();
		view.database.close();
		return { kind: view.kind, path: view.path };
	})();
	return {
		configured: implied !== null || selectable.length > 0,
		implied: implied === null ? null : implied.name,
		ambiguous: implied === null && selectable.length > 1,
		profiles,
		roots: { search: profileSearchRoots(context), write: writableProfilesRoot(context) },
		view: viewInfo,
	};
}
