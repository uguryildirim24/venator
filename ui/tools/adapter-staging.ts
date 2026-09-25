/**
 * The pipeline adapters a packaged app has to carry, and the check that it does.
 *
 * **The defect this exists for.** Routes on the two action surfaces spawn a Python
 * adapter that lives beside the server source — `onboarding/parse_resume.py`,
 * `onboarding/resolve_board.py`, `onboarding/verify_profile.py` and `runs/plan.py` — and every
 * one of them resolves its script
 * as a sibling of its own module: `join(fileURLToPath(new URL(".", import.meta.url)), "….py")`.
 * In a checkout that is the source tree and the file is right there. In a packaged app the
 * server is one bundled `resources/server.mjs`, so the sibling is `resources/….py` — and
 * nothing put it there. `tauri.conf.json` named `resources/server.mjs` and
 * `resources/python/**\/*`, and a `.py` file under `server/` matches neither.
 *
 * So on a packaged Install those spawns failed at launch. `boards/resolve` reported that
 * reading a careers page is not part of this Install; the run surface could describe no run.
 * Neither is loud, which is why it stood. The load-back is what made it urgent: it refuses a
 * write it cannot verify, so an unstaged `verify_profile.py` would mean a packaged app that
 * cannot create a Profile at all.
 *
 * `venator.llm.probe` is unaffected and always was, and the difference is the whole lesson: it
 * is spawned as `-m venator.llm.probe`, which resolves through the *pipeline* source staged
 * under `resources/python/<triple>/src/`. An adapter addressed by path has to be staged by
 * path.
 *
 * **Why this module is not inside `prepare-sidecar.ts`.** That file ends in a top-level
 * `await main()` and runs on import, so a check living in it is a check no test can reach —
 * the same reason `sidecar-staging.ts` and `python-staging.ts` exist, and the reason eleven
 * mutations of unimportable checks once survived a green suite eleven times.
 */

import { copyFileSync, existsSync } from "node:fs";
import { basename, join, resolve } from "node:path";

/**
 * Every adapter, as a path relative to `ui/`.
 *
 * Written out rather than globbed, because this list is also what `tauri.conf.json` names and
 * two lists that drift are what put this file here. `adapter-staging.test.ts` walks `server/`
 * for `.py` files and fails if one of them is missing from here, so adding another adapter is
 * a deliberate edit in three places that a test insists on rather than a thing to remember.
 */
export const PIPELINE_ADAPTERS: readonly string[] = [
	"server/onboarding/parse_resume.py",
	"server/onboarding/resolve_board.py",
	"server/onboarding/verify_profile.py",
	"server/runs/plan.py",
];

/**
 * Where each adapter lands, relative to the Tauri root.
 *
 * Flat, beside `server.mjs`, because that is where the bundled server looks for it. Two
 * adapters with the same basename would silently overwrite one another, so the test asserts
 * these are distinct.
 */
export function stagedResourceNames(): readonly string[] {
	return PIPELINE_ADAPTERS.map((adapter) => `resources/${basename(adapter)}`);
}

/**
 * Copies every adapter beside the bundled server, or refuses.
 *
 * A missing source is a refusal rather than a skip: a bundle that ships without one of these is
 * a bundle whose onboarding cannot write a Profile, and it looks exactly like a bundle that
 * works until somebody tries.
 */
export function stageAdapters(uiRoot: string, resources: string): readonly string[] {
	const staged: string[] = [];
	for (const adapter of PIPELINE_ADAPTERS) {
		const source = resolve(uiRoot, adapter);
		if (!existsSync(source)) {
			throw new Error(
				`the desktop bundle would ship without ${adapter}: it is spawned by path from the ` +
					"bundled server and nothing else would put it there. Nothing was bundled.",
			);
		}
		const target = join(resources, basename(adapter));
		copyFileSync(source, target);
		staged.push(target);
	}
	return staged;
}
