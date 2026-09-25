/**
 * Reading contact facts out of a resume someone uploaded.
 *
 * The whole design of this module is what it refuses to do. Extraction is a **suggestion**:
 * the endpoint that calls it writes nothing, returns what it found and what it could not
 * find, and the person confirms or corrects every field before any of it reaches disk. So
 * every rule below is written to fail towards "not found" rather than towards a plausible
 * guess, and every value it does return is a verbatim slice of the document. Nothing is
 * reformatted, normalised, title-cased, or rounded — a phone number comes back spelled the
 * way it was written, because the person is about to read it back and a silently tidied value
 * is one they will not look at twice.
 *
 * What it reads: plain text. What it does not read: PDF, Word, or anything else binary, and
 * it says so rather than extracting something wrong out of a container it does not
 * understand. It also does not attempt education, experience, bullets or any section body —
 * those are facts an Application draws on directly, and a wrong one there is a lie on a
 * resume rather than a typo in a form.
 *
 * **A PDF now has somewhere to go, and this module is still not it.** An uploaded PDF is
 * turned into text by `venator.resume.parse` through `resume-pdf.ts`, and that text comes
 * straight back here to be read by the rules below — one extractor, whichever way the document
 * arrived. What changed is only the refusal: a PDF pasted or posted as a body is still refused,
 * and the remedy now says to attach the file rather than telling somebody to select-all out of
 * a document this step could have read for them. The sentence about the sections is untouched
 * and remains true of this module; the *other* reading of a PDF does attempt them, and answers
 * with a proposal nobody has confirmed rather than with facts (`shared/onboarding.ts`).
 */

import type { ExtractedField, ResumeSuggestion } from "../../shared/onboarding.ts";
import { EXTRACTED_FIELDS } from "../../shared/onboarding.ts";

/** Enough for any resume as text; small enough that nothing here is a memory question. */
export const MAXIMUM_RESUME_BYTES = 512 * 1024;

export class ResumeReadError extends Error {
	readonly remedy: string;

	constructor(message: string, remedy: string) {
		super(message);
		this.name = "ResumeReadError";
		this.remedy = remedy;
	}
}

const PDF_MAGIC = "%PDF-";
const ZIP_MAGIC = "PK";

/**
 * Refuses anything that is not text, by looking at it rather than by trusting a filename.
 *
 * A `.txt` holding a PDF is still a PDF, and a document whose bytes were decoded as text
 * would yield a name made of font tables. The refusal names what to do instead.
 */
export function readResumeText(bytes: Uint8Array): string {
	if (bytes.length === 0) {
		throw new ResumeReadError("That file is empty.", "Choose the resume file, or paste the text instead.");
	}
	if (bytes.length > MAXIMUM_RESUME_BYTES) {
		throw new ResumeReadError(
			"That file is larger than this step accepts.",
			"Paste the resume text instead, or save a plain-text copy of it.",
		);
	}
	const text = new TextDecoder("utf8", { fatal: false }).decode(bytes);
	if (text.startsWith(PDF_MAGIC)) {
		throw new ResumeReadError(
			"That is a PDF, and a pasted or posted body is read as plain text.",
			"Attach the PDF as a file instead — this step reads an uploaded one, on this machine.",
		);
	}
	if (text.startsWith(ZIP_MAGIC)) {
		throw new ResumeReadError(
			"That is a Word document or another packaged file, and this step reads plain text only.",
			"Open the resume, select all of it, and paste the text — or save a plain-text copy and choose that.",
		);
	}
	if (text.includes("\u0000")) {
		throw new ResumeReadError(
			"That file is not text.",
			"Paste the resume text instead, or save a plain-text copy of it.",
		);
	}
	return text;
}

/**
 * Which of the five the renderer needs are present, and which are not.
 *
 * Shared by both readings of a resume, because "which of the five did this produce" is the
 * same question whether a regular expression or a completion produced them, and the screen
 * that shows the answer is one screen. Blank counts as absent: a value that is whitespace is
 * a value the renderer cannot print.
 */
export type FieldStanding = {
	readonly found: readonly ExtractedField[];
	readonly missing: readonly ExtractedField[];
};

export function fieldStanding(values: ReadonlyMap<ExtractedField, string | null>): FieldStanding {
	const present = (field: ExtractedField): boolean => (values.get(field) ?? "").trim() !== "";
	return {
		found: EXTRACTED_FIELDS.filter(present),
		missing: EXTRACTED_FIELDS.filter((field) => !present(field)),
	};
}

/** Section words a resume's own headings use. A line that is one of these is not a name. */
const HEADINGS: readonly string[] = [
	"resume",
	"curriculum vitae",
	"cv",
	"education",
	"experience",
	"work experience",
	"professional experience",
	"relevant experience",
	"employment",
	"skills",
	"technical skills",
	"technical proficiencies",
	"projects",
	"summary",
	"objective",
	"profile",
	"contact",
	"contact information",
	"memberships",
	"activities",
	"awards",
	"publications",
	"references",
];

const EMAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/u;

/** A LinkedIn profile address, however it was written, returned exactly as written. */
const LINKEDIN = /(?:https?:\/\/)?(?:[A-Za-z0-9-]+\.)?linkedin\.com\/in\/[A-Za-z0-9%_-]+\/?/u;

/**
 * A telephone number as a resume writes one, in any of the shapes a contact line uses.
 *
 * Deliberately narrow: an optional country code, then groups of digits separated by one
 * space, dot or dash, or wrapped in parentheses. A bare run of digits is not matched, because
 * on a resume that is more often a date range or a postal code than a phone number.
 */
const PHONE = /(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}\b/u;

/**
 * "City, ST" or "City, Country" as it appears on a contact line.
 *
 * Only ever taken from a line that also carries another contact fact, which is what keeps it
 * from picking an employer's city out of a job entry.
 */
const PLACE = /\b([A-Z][A-Za-z.'-]+(?:[ -][A-Z][A-Za-z.'-]+)*,\s*(?:[A-Z]{2}\b|[A-Z][A-Za-z.'-]+(?:[ -][A-Z][A-Za-z.'-]+)*))/u;

function firstMatch(pattern: RegExp, text: string): string | null {
	const match = pattern.exec(text);
	return match === null ? null : (match[0] ?? null).trim();
}

function looksLikeName(line: string): boolean {
	const trimmed = line.trim();
	if (trimmed === "" || trimmed.length > 60) return false;
	if (HEADINGS.includes(trimmed.toLowerCase().replace(/[:.]+$/u, ""))) return false;
	if (/[0-9@|/\\]/u.test(trimmed)) return false;
	if (trimmed.includes("•") || trimmed.includes("·")) return false;
	const words = trimmed.split(/\s+/u);
	return words.length >= 2 && words.length <= 5;
}

/**
 * The location, taken only from a line that already proved itself to be contact information.
 *
 * A resume's contact block is the two or three lines under the name, and every one of them
 * that names a place also names an address, a number or a link. Requiring that is what stops
 * "Cambridge, MA" being lifted out of an employer's address further down the page.
 */
function extractLocation(lines: readonly string[], email: string | null, phone: string | null): string | null {
	for (const line of lines.slice(0, 12)) {
		const carriesContact =
			(email !== null && line.includes(email)) ||
			(phone !== null && line.includes(phone)) ||
			LINKEDIN.test(line) ||
			line.includes("•") ||
			line.includes("·") ||
			line.includes("|");
		if (!carriesContact) continue;
		const place = PLACE.exec(line);
		if (place !== null) return (place[1] ?? "").trim();
	}
	return null;
}

/**
 * Reads what can be read, and reports the rest as missing.
 *
 * Nothing here is inferred from anything else: an email domain is not turned into an
 * employer, a phone country code is not turned into a location, and a missing field stays
 * missing rather than becoming a plausible default.
 */
export function extractResume(text: string): ResumeSuggestion {
	// The three unambiguous facts are searched for across the whole document, because a
	// contact block sometimes sits at the foot of the page; the name and the location are
	// read only from the head, where a resume puts them.
	const lines = text.split(/\r?\n/u);
	const email = firstMatch(EMAIL, text);
	const phone = firstMatch(PHONE, text);
	const linkedin = firstMatch(LINKEDIN, text);
	const location = extractLocation(lines, email, phone);
	const name = lines.slice(0, 8).find((line) => looksLikeName(line))?.trim() ?? null;

	const values: ReadonlyMap<ExtractedField, string | null> = new Map([
		["name", name],
		["location", location],
		["phone", phone],
		["email", email],
		["linkedin", linkedin],
	]);
	const { found, missing } = fieldStanding(values);

	return {
		kind: "read",
		resume: { name, contact: { location, phone, email, linkedin } },
		found,
		missing,
		notAttempted: [
			"Education, experience, skills and every other section body. Those are facts an Application quotes directly, so they are typed in rather than guessed at.",
			"Anything that is not plain text. A PDF or a Word document is refused rather than decoded approximately.",
		],
	};
}
