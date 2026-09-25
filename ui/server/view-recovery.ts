import type { ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { openViewDatabase, resolveView, ViewSchemaError } from "./db.ts";
import { systemContext, type LocationContext } from "./locations.ts";
import { discoverProfiles, impliedProfile } from "./onboarding/discovery.ts";
import { currentRun, spawnViewStage } from "./runs/runner.ts";

export class ViewUpdateError extends Error {
	constructor() {
		super("Your job list could not be updated. Try Refresh again.");
	}
}

// A shared flight means concurrent list requests start one View stage. A failed rebuild
// remains failed until a new server session or an explicit Refresh; GET cannot retry forever.
let pending: Promise<void> | null = null;
let failedPath: string | null = null;

export type ViewStage = (profile: string, context: LocationContext) => ChildProcess;

function build(profile: string, context: LocationContext, start: ViewStage): Promise<void> {
	return new Promise((resolve, reject) => {
		const child = start(profile, context);
		const timeout = setTimeout(() => child.kill("SIGKILL"), 10 * 60_000);
		child.on("error", () => { clearTimeout(timeout); reject(new ViewUpdateError()); });
		child.on("close", (code) => {
			clearTimeout(timeout);
			if (code === 0) resolve();
			else reject(new ViewUpdateError());
		});
	});
}

export async function ensureReadableView(context: LocationContext = systemContext(), start: ViewStage = spawnViewStage): Promise<void> {
	if (pending !== null) return pending;
	// A developer checkout's committed stores are not a new person's Install. Do not
	// auto-run its pipeline merely because a test or developer opens a read-only API.
	if (!context.environment("VENATOR_HOME") && context.checkoutRoot !== null && existsSync(join(context.checkoutRoot, "src", "venator"))) return;
	const source = resolveView(context);
	if (source.kind === "fixture") return;
	let stale = source.kind === "empty";
	if (!stale) {
		try {
			const view = openViewDatabase();
			view.database.close();
			failedPath = null;
			return;
		} catch (error) {
			if (!(error instanceof ViewSchemaError)) throw error;
			stale = true;
		}
	}
	if (!stale) return;
	const profile = impliedProfile(discoverProfiles(context));
	if (profile === null) return; // A new Install needs onboarding, not a View stage.
	if (failedPath === source.path || currentRun() !== null) throw new ViewUpdateError();
	pending = (async () => {
		try {
			await build(profile.name, context, start);
			const view = openViewDatabase();
			view.database.close();
			failedPath = null;
		} catch {
			failedPath = source.path;
			throw new ViewUpdateError();
		} finally {
			pending = null;
		}
	})();
	return pending;
}
