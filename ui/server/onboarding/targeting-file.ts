/**
 * Reading and replacing one Profile's `targeting.yaml`, and nothing else about it.
 *
 * This is not a Profile reader. It hands back the file's bytes as text and writes text back;
 * what any of it *means* is `src/venator/profile/`'s to say, and `targeting-edit.ts` is
 * deliberately incapable of understanding more of it than the two blocks it appends to.
 *
 * The replacement is atomic in the way that matters for a file whose absence is a broken
 * Install: the new text is written to a temporary file beside the target, flushed, and moved
 * over it in one rename. An interruption leaves either the Profile that was there or the one
 * with the employer in it — never a truncated policy file, which would load as a Profile with
 * no Hard Filter and pass every Posting.
 *
 * The gap between the staging and the rename is where the load-back goes. `verify.ts` hands
 * the staged file to the pipeline's own loader, and the rename happens only if it loads — so a
 * candidate the loader refuses leaves the Profile that is on disk byte-identical, and leaves an
 * absent one uncreated. That ordering is the whole guarantee: a check after the rename would be
 * a report about damage already done.
 */

import { mkdtempSync, readFileSync, renameSync, rmSync, statSync } from "node:fs";
import { dirname, join } from "node:path";

import { systemContext, type LocationContext } from "../locations.ts";
import { OnboardingError } from "./errors.ts";
import { writeAndFlush } from "./profile.ts";
import { requireLoadableProfile } from "./verify.ts";

/** A Profile's policy is a page of text. Anything larger is not one, and is not read into memory. */
const MAXIMUM_TARGETING_BYTES = 512 * 1024;

export function readTargeting(path: string, profile: string): string {
	const stats = statSync(path, { throwIfNoEntry: false });
	if (stats === undefined || !stats.isFile()) {
		throw new OnboardingError(
			"profile_absent",
			`The Profile "${profile}" has no targeting file to add an employer to.`,
			null,
			"profile",
		);
	}
	if (stats.size > MAXIMUM_TARGETING_BYTES) {
		throw new OnboardingError(
			"targeting_unreadable",
			`The Profile "${profile}" has a targeting file larger than this dashboard will edit.`,
			"Nothing was changed. The board can be added by hand under `sources` in that file.",
			"profile",
		);
	}
	try {
		return readFileSync(path, "utf8");
	} catch (cause) {
		// The caught message carries an absolute path and this surface never returns one.
		process.stderr.write(
			`onboarding: a targeting file could not be read: ${cause instanceof Error ? cause.message : "unknown"}\n`,
		);
		throw new OnboardingError(
			"write_failed",
			`The Profile "${profile}" could not be read, so nothing was changed.`,
			"Check that this account can read its own application data directory.",
			"profile",
		);
	}
}

/**
 * Writes the new text, once the pipeline has read it back.
 *
 * The staging directory is handed to the loader as a Profile directory in its own right, and
 * that is the right question rather than a narrower one: `load_profile` reads a missing
 * `resume.yaml` or `constraints.yaml` as an empty mapping, so what is checked is exactly the
 * document this module is about to move — and only that. The Profile's other two files are not
 * being written and are not this route's to have an opinion about; refusing to register an
 * employer because something else in the Profile is malformed would strand precisely the person
 * this route exists to rescue.
 */
export async function writeTargeting(path: string, text: string, context: LocationContext = systemContext()): Promise<void> {
	const directory = dirname(path);
	const staging = mkdtempSync(join(directory, ".employers-"));
	const staged = join(staging, "targeting.yaml");
	try {
		writeAndFlush(staged, text);
		await requireLoadableProfile(staging, "the employer", context);
		renameSync(staged, path);
	} catch (cause) {
		// A refusal from the load-back is already the answer, worded for the person reading it.
		// Wrapping it as a failed write would replace "the pipeline could not read that" with
		// "the file could not be written", which is not what happened and not what to do about it.
		if (cause instanceof OnboardingError) throw cause;
		process.stderr.write(
			`onboarding: registering an employer failed: ${cause instanceof Error ? cause.message : "unknown"}\n`,
		);
		throw new OnboardingError(
			"write_failed",
			"The employer could not be written into the Profile.",
			"Check that this account can write to its own application data directory, then try again. Nothing was left half-written.",
		);
	} finally {
		rmSync(staging, { recursive: true, force: true });
	}
}
