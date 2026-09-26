import { cpSync, existsSync, lstatSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmSync } from "node:fs";
import { join } from "node:path";

import type { ProfileDocuments } from "../../shared/profile-form.ts";
import { systemContext, writableProfilesRoot, type LocationContext } from "../locations.ts";
import { OnboardingError } from "./errors.ts";
import { oneWriterPerProfile } from "./one-writer.ts";
import { profileDirectoryFor, writeAndFlush } from "./profile.ts";
import { requireLoadableProfile } from "./verify.ts";

const FILES = ["resume.yaml", "constraints.yaml", "targeting.yaml"] as const;
export type { ProfileDocuments };

function directoryFor(name: string, context: LocationContext): string {
	const root = writableProfilesRoot(context);
	const target = profileDirectoryFor(name, context);
	// Never follow a link in the Install path, including a missing or dangling Profile.
	let path = root;
	const ancestors: string[] = [];
	while (true) {
		ancestors.push(path);
		const parent = join(path, "..");
		if (parent === path) break;
		path = parent;
	}
	for (const ancestor of ancestors) {
		const stat = lstatSync(ancestor, { throwIfNoEntry: false });
		if (stat?.isSymbolicLink()) throw new OnboardingError("write_failed", "The Install path contains a symbolic link.");
	}
	const stat = lstatSync(target, { throwIfNoEntry: false });
	if (!stat?.isDirectory() || stat.isSymbolicLink()) throw new OnboardingError("invalid_profile", "This Install has no editable Profile by that name.");
	for (const file of FILES) {
		const entry = lstatSync(join(target, file), { throwIfNoEntry: false });
		if (entry !== undefined && !entry.isFile()) throw new OnboardingError("write_failed", "A Profile file is not a regular file.");
	}
	return target;
}

export function readProfileDocuments(name: string, context: LocationContext = systemContext()) {
	const target = directoryFor(name, context);
	return {
		"resume.yaml": readFileSync(join(target, "resume.yaml"), "utf8"),
		"constraints.yaml": readFileSync(join(target, "constraints.yaml"), "utf8"),
		"targeting.yaml": readFileSync(join(target, "targeting.yaml"), "utf8"),
	} satisfies ProfileDocuments;
}

/** Preserve every Profile file and asset; validate the candidate with the pipeline before replacing anything. */
export async function editProfileDocuments(name: string, documents: ProfileDocuments, original: ProfileDocuments, context: LocationContext = systemContext()): Promise<void> {
	const target = profileDirectoryFor(name, context);
	await oneWriterPerProfile(target, async () => {
		const current = readProfileDocuments(name, context);
		for (const file of FILES) {
			const text = documents[file];
			if (Buffer.byteLength(text, "utf8") > 512_000 || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text)) {
				throw new OnboardingError("bad_request", "Enter valid Profile text under 512 KB per file.");
			}
			if (original[file] !== current[file]) {
				throw new OnboardingError("write_failed", "This Profile changed since you opened it. Reopen it before saving so you do not erase those changes.");
			}
		}
		const staging = mkdtempSync(join(writableProfilesRoot(context), `.profile-edit-${name}-`));
		const retired = `${staging}.replaced`;
		let moved = false;
		try {
			for (const entry of readdirSync(target)) {
				const source = join(target, entry);
				if (lstatSync(source).isSymbolicLink()) throw new OnboardingError("write_failed", "The Profile contains a symbolic link.");
				cpSync(source, join(staging, entry), { recursive: true, dereference: false });
			}
			for (const file of FILES) {
				if (documents[file] !== current[file]) writeAndFlush(join(staging, file), documents[file]);
			}
			await requireLoadableProfile(staging, "the Profile", context);
			// Refuse a path swapped for a link or a Profile changed during verification.
			const beforeRename = readProfileDocuments(name, context);
			if (FILES.some((file) => beforeRename[file] !== current[file])) {
				throw new OnboardingError("write_failed", "This Profile changed while it was being checked. Reopen it before saving.");
			}
			renameSync(target, retired);
			moved = true;
			renameSync(staging, target);
		} catch (error) {
			if (moved && !existsSync(target)) renameSync(retired, target);
			throw error;
		} finally {
			rmSync(staging, { recursive: true, force: true });
			if (existsSync(target)) rmSync(retired, { recursive: true, force: true });
		}
	});
}
