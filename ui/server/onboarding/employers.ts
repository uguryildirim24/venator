/**
 * Registering an employer's board into a Profile that already exists.
 *
 * **Why this exists.** Setup writes a Profile whether or not any employer was registered into
 * it, and a Profile with an empty board registry is one Discover has nothing to poll. Until
 * this route existed the only way out of that state was a terminal and a text editor — which
 * is precisely what the people this product ships to do not have. Somebody who finished setup
 * on a connection that could not reach a careers page was left on a dashboard whose one
 * control was off, reading a note telling them to edit a file they have never opened.
 *
 * **What it writes, and the whole of what it writes.** One file: the `targeting.yaml` of one
 * Profile that already exists, under this Install's own application data directory. Never a
 * Profile in a checkout — those are somebody's working tree and are refused by name rather
 * than edited. Never `data/`, never `build/`, never a second file in the same Profile. It
 * makes no request of anything: the board on its way in was resolved by `/boards/resolve`
 * before it got here, and resolving is where the network is.
 *
 * **How narrow the edit is** is `targeting-edit.ts`'s to explain: it appends to
 * `sources.names` and `sources.boards` and leaves every other byte of the document, comments
 * included, exactly as it found it. A shape it does not recognise is reported as one, naming
 * the Profile, rather than rewritten on a guess.
 *
 * **And it lands only if the pipeline can read it.** The edited document is staged beside the
 * file and handed to `src/venator/profile/` — the real loader, in a real interpreter — before
 * the rename that makes it real (`verify.ts`). The emitter is what keeps a person's own words
 * from breaking the file; this is the net under the emitter, and a candidate the loader refuses
 * leaves the Profile on disk byte-identical.
 *
 * **On `filters_version`.** `sources.boards` is excluded from the hash
 * (`store.NON_DECIDING_BLOCKS`) and `sources.names` is not, so registering an employer moves
 * the version and the next `match.run` re-decides the corpus. That is correct and is what
 * CLAUDE.md says it is: Dedup resolves an ATS Posting's employer through that map, so a
 * Posting decided before the name existed was decided against a different registry.
 */

import { lstatSync } from "node:fs";
import { join } from "node:path";

import type { EmployerRegistration, EmployerRegisterResponse } from "../../shared/onboarding.ts";
import { systemContext, writableProfilesRoot, type LocationContext } from "../locations.ts";
import { OnboardingError } from "./errors.ts";
import { oneWriterPerProfile } from "./one-writer.ts";
import { profileDirectoryFor, validateProfileName } from "./profile.ts";
import { addEmployers, UneditableTargeting } from "./targeting-edit.ts";
import { readTargeting, writeTargeting } from "./targeting-file.ts";
import { LONE_SURROGATE } from "./yaml.ts";

/** Enough for a paste that named a whole tenant's boards; small enough to stay one edit. */
export const MAXIMUM_EMPLOYERS_PER_REQUEST = 25;

/** As long as any ATS routing string this pipeline has met, and bounded so it stays one line. */
const MAXIMUM_TOKEN_LENGTH = 200;

/** An employer's name as somebody would say it, not a paragraph. */
const MAXIMUM_NAME_LENGTH = 200;

/**
 * An adapter name is an identifier the pipeline chose — `greenhouse`, `lever`, `workday`.
 *
 * Checked as a shape rather than against a list. The list of adapters lives in
 * `venator.discover`, this side keeping a second copy of a registry is how two registries
 * drift apart, and every value that reaches here came out of `venator.discover.register`'s own
 * answer by way of `/boards/resolve`.
 */
const SOURCE_NAME = /^[a-z][a-z0-9_]*$/u;

/**
 * Characters the Profile loader will not read out of a file as themselves.
 *
 * `yaml.ts` states the measurement: PyYAML refuses U+007F–U+009F, U+FFFE and U+FFFF as
 * non-printable, and folds U+0085, U+2028 and U+2029 as line breaks. The emitter escapes every
 * one of them and the loader reads them back, so this is not what keeps a Profile loadable —
 * the load-back before the rename is. It is here because none of them is part of an employer's
 * name or a board token in the first place, and a paste that carries one is a bad paste that
 * should be reported while the person can still fix it rather than written down invisibly.
 *
 * JavaScript's own `\s` is not this set and does not contain it: `\s` matches U+2028 and
 * U+2029 but not U+0085, so a token trimmed and checked for whitespace could still carry a
 * character PyYAML reads as the end of a line.
 */
const UNREADABLE_TO_THE_LOADER = /[\u007F-\u009F\u2028\u2029\uFFFE\uFFFF]/u;

/**
 * A board token, as narrowly as this can be said without keeping a second copy of the registry.
 *
 * No whitespace, no quote, no backslash and no comment mark, so it stays one scalar on one
 * line; and no colon or slash, because a Posting key is `source:board:external_id` and the key
 * is a path parameter in the dashboard's own API (CLAUDE.md says both). A Workday token joins
 * its two identifiers with `~`, which is why that is not excluded. The second half is the
 * characters above, excluded by codepoint rather than by `\s`, which does not cover them —
 * and `\p{Cs}`, which is not a character at all.
 */
const BOARD_TOKEN = /^[^\s"'\\#:/{}[\],&*\p{Cs}]+$/u;

/** One Profile this Install owns, and the one file of it this surface may append to. */
type ProfileTargeting = { readonly directory: string; readonly file: string };

function invalidEmployer(field: string, message: string, remedy: string | null = null): OnboardingError {
	return new OnboardingError("invalid_profile", message, remedy, field);
}

/**
 * Checks one entry on its way in.
 *
 * The employer name is required for the same reason the setup step requires it: Dedup resolves
 * an ATS Posting's employer through `sources.names`, so a board registered without one has no
 * employer at all and its Postings drop out of every duplicate group — the same role, shown
 * twice, with nothing on screen to say why.
 */
export function validateEmployer(entry: EmployerRegistration, index: number): EmployerRegistration {
	const where = `boards.${index}`;
	const source = entry.source.trim();
	const board = entry.board.trim();
	const name = entry.name.trim();
	if (!SOURCE_NAME.test(source)) {
		throw invalidEmployer(`${where}.source`, "That is not a job board this pipeline knows how to poll.");
	}
	if (board === "" || board.length > MAXIMUM_TOKEN_LENGTH || !BOARD_TOKEN.test(board)) {
		throw invalidEmployer(`${where}.board`, "That is not a board this pipeline can register.");
	}
	if (UNREADABLE_TO_THE_LOADER.test(board)) {
		throw invalidEmployer(`${where}.board`, "That is not a board this pipeline can register.");
	}
	if (name === "") {
		throw invalidEmployer(
			`${where}.name`,
			"Every board needs the employer's name.",
			"Postings from a board with no name here drop out of duplicate detection, so the same role can reach you twice.",
		);
	}
	if (name.length > MAXIMUM_NAME_LENGTH) {
		throw invalidEmployer(`${where}.name`, "That is longer than an employer's name.");
	}
	// The one character rule on a name, and it is deliberately not a shape. An employer's name
	// is whatever the employer calls themselves, in whatever script — nothing here may decide
	// that a name is spelled wrongly. What it may say is that a name is one line of text: a
	// line break, a control character, or a codepoint the loader reads as the end of a line is
	// a paste that went wrong, and this is the last moment anybody can still see it.
	if (/[\p{Cc}]/u.test(name) || UNREADABLE_TO_THE_LOADER.test(name)) {
		throw invalidEmployer(
			`${where}.name`,
			"That employer name carries a line break or a control character.",
			"Type the employer's name as one line, or paste it again from somewhere that is not a document.",
		);
	}
	// Said separately from the line above, because it is not the same thing and the sentence a
	// person reads should not say it is: a lone surrogate is neither a line break nor a control
	// character, and unlike those two it cannot arrive from a paste at all.
	if (LONE_SURROGATE.test(name)) {
		throw invalidEmployer(
			`${where}.name`,
			"That employer name holds half of a surrogate pair, which is not a character.",
			"Send the name as text rather than as a `\\u` escape. A Profile carrying one is read back perfectly well and then stops every stage that comes after, so it is refused here instead.",
		);
	}
	return { source, board, name };
}

/**
 * The Profile's own targeting file, or the refusal it is.
 *
 * Only this Install's application data directory is writable here, which is the same invariant
 * the Profile write holds: nothing on this surface writes outside
 * `<application data directory>/profiles/`. A **symbolic link**, anywhere on the way,
 * is refused rather than written through — the
 *   Profiles directory, the Profile, and the file. That is the same rule `writeProfile` holds,
 *   for the same reason: a link is somewhere else, and somewhere else is not this Install's
 *   own application data directory.
 */
function targetingFileFor(name: string, context: LocationContext): ProfileTargeting {
	const directory = profileDirectoryFor(name, context);
	const root = lstatSync(writableProfilesRoot(context), { throwIfNoEntry: false });
	if (root === undefined) {
		throw new OnboardingError(
			"profile_absent",
			"This Install holds no Profiles of its own yet.",
			"Set one up first: a Profile is what says which employers to look at and what to keep.",
			"profile",
		);
	}
	if (root.isSymbolicLink() || !root.isDirectory()) {
		throw new OnboardingError(
			"write_failed",
			"The Profiles directory is a symbolic link, and onboarding will not write through one.",
			"Replace it with a real directory, or point $VENATOR_HOME somewhere else.",
		);
	}
	const stats = lstatSync(directory, { throwIfNoEntry: false });
	if (stats === undefined) {
		throw new OnboardingError(
			"profile_absent",
			`This Install has no Profile named "${name}" of its own to register an employer in.`,
			"Set one up first: a Profile is what says which employers to look at and what to keep.",
			"profile",
		);
	}
	if (stats.isSymbolicLink() || !stats.isDirectory()) {
		throw new OnboardingError(
			"write_failed",
			`What is at the name "${name}" in this Install is not a Profile directory.`,
			null,
			"profile",
		);
	}
	const file = join(directory, "targeting.yaml");
	const target = lstatSync(file, { throwIfNoEntry: false });
	if (target === undefined) {
		throw new OnboardingError(
			"profile_absent",
			`The Profile "${name}" has no targeting file, so there is nowhere to register an employer.`,
			"A directory without one is not a Profile: it would load with no Hard Filter and pass every Posting.",
			"profile",
		);
	}
	if (target.isSymbolicLink() || !target.isFile()) {
		throw new OnboardingError(
			"write_failed",
			`The Profile "${name}" keeps its targeting file as something this will not write through.`,
			"Replace it with a real file, or register the board by hand.",
			"profile",
		);
	}
	return { directory, file };
}

/**
 * Registers employers into an existing Profile, and answers with what changed.
 *
 * A token already registered is reported rather than added again, and reported rather than
 * silently skipped: "nothing happened" and "that employer was already registered" are
 * different answers and the screen says which one it got. When every entry was already
 * registered the file is not touched at all.
 */
export async function registerEmployers(
	profile: string,
	entries: readonly EmployerRegistration[],
	context: LocationContext = systemContext(),
): Promise<EmployerRegisterResponse> {
	const name = validateProfileName(profile);
	const checked = entries.map((entry, index) => validateEmployer(entry, index));
	// Reading the request is a pure function of the request, so it stays outside the queue: a
	// body that is wrong is refused straight away rather than behind somebody else's write.
	return oneWriterPerProfile(profileDirectoryFor(name, context), () => addTo(name, checked, context));
}

/**
 * The read-modify-write itself, and the whole of what has to happen alone.
 *
 * Every line of it is inside `oneWriterPerProfile`'s queue for this Profile, and that is the
 * point: `writeTargeting` `await`s the load-back between staging the new document and renaming
 * it over the target, and an `await` is a yield. Two requests that both read `before` here and
 * then both rename would end with one employer on disk and two responses saying `200`,
 * `"changed": true`, each naming its own board under `"added"` — the answer read back out of
 * each request's own stale pre-read rather than out of the file.
 */
async function addTo(
	name: string,
	checked: readonly EmployerRegistration[],
	context: LocationContext,
): Promise<EmployerRegisterResponse> {
	const { file } = targetingFileFor(name, context);
	const before = readTargeting(file, name);
	let edited;
	try {
		edited = addEmployers(before, checked);
	} catch (cause) {
		if (!(cause instanceof UneditableTargeting)) throw cause;
		throw new OnboardingError(
			"targeting_unreadable",
			`The Profile "${name}" is written in a shape this dashboard will not edit: ${cause.message}.`,
			"Nothing was changed. The board can still be added by hand, under `sources` in that Profile's targeting file.",
			"profile",
		);
	}
	const changed = edited.added.length > 0;
	// The pipeline reads the candidate back before any of it lands; `targeting-file.ts` does
	// that between the staging and the rename, so a refusal leaves this file byte-identical.
	if (changed) await writeTargeting(file, edited.text, context);
	return {
		profile: name,
		added: edited.added,
		alreadyRegistered: edited.alreadyRegistered,
		registered: edited.registered,
		changed,
		warnings: edited.warnings,
	};
}
