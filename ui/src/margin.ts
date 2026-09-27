/**
 * What Venator writes in the margin of a Posting, and where on the page each note sits.
 *
 * Pure: it takes the Posting's detail and returns, for every block of the reader page, the
 * underlines to draw in that block and the notes to anchor to it. This module does not measure
 * layout; the Posting page flows anchored notes past each other (ui/DESIGN.md, "The Posting page and its margin").
 *
 * The rules, in the order they are applied:
 *
 * - A résumé fact (every `evidence[]` entry that is not a Hard Filter's own) is looked for in
 *   the page, word for word, ignoring case and spacing. Found, its span is underlined green and its note goes in
 *   that block's margin. Not found, its note goes beside the requirements heading with no
 *   underline. A span is never guessed.
 * - A requirement sentence the assessment quoted (`Qualification not established: …`) is
 *   looked for the same way, as a whole clause ending on a word boundary. Required and not on
 *   the résumé is ember; every other kind is dotted. The header ledger quotes short requirement
 *   clauses; marks and their anchored notes remain beside the requirement blocks.
 * - Other findings sit beside the header or requirements heading. A passed Hard Filter adds
 *   no note; a failed one carries its rule and reason.
 */

import type { FilterDecision, JevTriage, JobAssessment, ReaderBlock } from "../shared/contracts.ts";
import {
	assessmentNoteLabel,
	assessmentNotePlace,
	evidenceSourceLabel,
	jevDecisionLabel,
	requirementFinding,
	ruleLabel,
} from "./labels.ts";
import { inspectorHash } from "./router.ts";

export type NoteTone = "green" | "ember" | "neutral";

/** The source an assessment gives the facts a Hard Filter checked; every other source is the résumé. */
const HARD_FILTER_EVIDENCE = "hard_filter decision";

export type MarginNote = {
	readonly key: string;
	readonly tone: NoteTone;
	readonly title: string;
	readonly subtitle: string | null;
	/** A second line, for a reason that does not fit the subtitle. */
	readonly detail?: string | null | undefined;
	/** A route the note links to, or the sheet it opens. */
	readonly link?: { readonly label: string; readonly hash: string } | undefined;
	readonly opens?: "requirements" | undefined;
};

export type MarkTone = "green" | "ember" | "dotted";

export type Mark = {
	readonly start: number;
	readonly end: number;
	readonly tone: MarkTone;
};

export type MarkedBlock = ReaderBlock & {
	readonly marks: readonly Mark[];
	/** "3 on your résumé", beside the requirements heading only. */
	readonly count: string | null;
	readonly notes: readonly MarginNote[];
};

export type Margin = {
	readonly fit: { readonly count: string | null; readonly tally: readonly NoteTone[]; readonly groups: readonly { readonly tone: NoteTone; readonly title: string; readonly clauses: readonly string[]; readonly at: number | null }[]; readonly jev: MarginNote | null } | null;
	readonly requirementsAt: number | null;
	readonly header: readonly MarginNote[];
	readonly blocks: readonly MarkedBlock[];
	/** The requirement clauses that were not found on the page, for the Requirements sheet. */
	readonly unplaced: readonly { readonly clause: string; readonly title: string; readonly subtitle: string | null }[];
};

export type MarginInput = {
	readonly page: readonly ReaderBlock[];
	readonly decisions: readonly FilterDecision[];
	readonly assessment?: JobAssessment | undefined;
	readonly jevTriage?: JevTriage | undefined;
};

/* --------------------------------------------------------------- matching */

/**
 * A block's text folded for comparison, with a map from each folded character back to the
 * original. Case, runs of spacing, curly quotes, and the assessment's habit of writing a
 * comma as a semicolon are the only differences folded away.
 */
type Folded = { readonly text: string; readonly origin: readonly number[] };

function foldCharacter(character: string): string {
	if (/\s/u.test(character)) return " ";
	if (character === ";") return ",";
	if (character === "’" || character === "‘") return "'";
	if (character === "“" || character === "”") return '"';
	const lower = character.toLowerCase();
	return lower.length === character.length ? lower : character;
}

function fold(text: string): Folded {
	let folded = "";
	const origin: number[] = [];
	let index = 0;
	for (const character of text) {
		const next = foldCharacter(character);
		if (!(next === " " && folded.endsWith(" "))) {
			folded += next;
			for (let unit = 0; unit < next.length; unit += 1) origin.push(index);
		}
		index += character.length;
	}
	return { text: folded, origin };
}

function foldNeedle(text: string): string {
	return fold(text.replace(/^[•·●▪◦‣∙*–-]\s+/u, "").trim()).text.trim();
}

const WORD = /[\p{L}\p{N}]/u;

/** Where `needle` sits in `block` as whole words, in the block's own offsets, or null. */
function locate(block: Folded, source: string, needle: string): { readonly start: number; readonly end: number } | null {
	if (needle === "") return null;
	let from = 0;
	while (from <= block.text.length - needle.length) {
		const at = block.text.indexOf(needle, from);
		if (at === -1) return null;
		const before = at === 0 ? "" : (block.text[at - 1] ?? "");
		const after = block.text[at + needle.length] ?? "";
		const opensOnWord = WORD.test(needle[0] ?? "");
		const closesOnWord = WORD.test(needle[needle.length - 1] ?? "");
		if (!(opensOnWord && WORD.test(before)) && !(closesOnWord && WORD.test(after))) {
			const start = block.origin[at] ?? 0;
			const lastOrigin = block.origin[at + needle.length - 1] ?? start;
			const lastCharacter = source.codePointAt(lastOrigin) ?? 0;
			return { start, end: lastOrigin + (lastCharacter > 0xffff ? 2 : 1) };
		}
		from = at + 1;
	}
	return null;
}

function overlaps(marks: readonly Mark[], start: number, end: number): boolean {
	return marks.some((mark) => start < mark.end && mark.start < end);
}

/* --------------------------------------------------------------- places */

const REQUIREMENTS_HEADING =
	/qualif|requirement|looking for|you (?:have|bring|need|will need)|about you|who you are|what you(?:'|’)ll need|must have|preferred|skills|experience/iu;

function sectionStart(blocks: readonly ReaderBlock[], index: number): number {
	for (let cursor = index; cursor >= 0; cursor -= 1) {
		if (blocks[cursor]?.kind === "heading") return cursor;
	}
	return index;
}

/* --------------------------------------------------------------- notes */

function hardFilterNote(decisions: readonly FilterDecision[]): MarginNote | null {
	const decision = decisions.findLast((candidate) => candidate.stage === "hard_filter" && candidate.latest) ?? null;
	if (decision?.verdict === "kill") {
		const reason = decision.reason === null ? "No reason was recorded" : assessmentNoteLabel(decision.reason);
		return {
			key: "hard-filter",
			tone: "neutral",
			title: "Excluded by a Hard Filter",
			subtitle: decision.rule === null ? null : ruleLabel(decision.rule),
			detail: reason,
			link:
				decision.rule === null
					? undefined
					: { label: "Everything this rule excluded", hash: inspectorHash({ status: "hard-killed", rule: decision.rule, search: "" }) },
		};
	}
	return null;
}

function earlyReviewNote(triage: JevTriage): MarginNote | null {
	if (triage.state !== "current" || triage.decision === "unassessed") return null;
	const label = jevDecisionLabel(triage.decision);
	return {
		key: "early-review",
		tone: "neutral",
		title: `Jev: ${label.charAt(0).toLowerCase()}${label.slice(1)}`,
		subtitle: null,
	};
}

function unique(notes: readonly MarginNote[]): MarginNote[] {
	const seen = new Set<string>();
	return notes.filter((note) => {
		const identity = `${note.title}\u0000${note.subtitle ?? ""}\u0000${note.detail ?? ""}`;
		if (seen.has(identity)) return false;
		seen.add(identity);
		return true;
	});
}

/* --------------------------------------------------------------- the margin */

export function buildMargin(input: MarginInput): Margin {
	const { page, assessment } = input;
	const folded = page.map((block) => fold(block.text));
	const marks: Mark[][] = page.map(() => []);
	const notes: MarginNote[][] = page.map(() => []);
	const marked = new Set<number>();

	// Résumé evidence, one note per requirement.
	const evidence = (assessment?.evidence ?? []).filter((entry) => entry.source !== HARD_FILTER_EVIDENCE);
	const seenRequirements = new Set<string>();
	const unplacedEvidence: MarginNote[] = [];
	const met: string[] = [];
	const metAt: number[] = [];
	const gaps: { clause: string; tone: NoteTone; at: number | null }[] = [];
	for (const entry of evidence) {
		const needle = foldNeedle(entry.requirement);
		if (needle === "" || seenRequirements.has(needle)) continue;
		seenRequirements.add(needle);
		met.push(entry.requirement.trim());
		const section = evidenceSourceLabel(entry.source);
		const related = entry.basis === "related_skill";
		const resumeEntry = entry.candidateEvidence.replace(/\s*\([^)]*\b(?:19|20)\d{2}\b[^)]*\)/gu, "").trim() || section;
		const note: MarginNote = {
			key: `evidence:${needle}`,
			tone: "green",
			title: related ? `Related skill · ${resumeEntry}` : resumeEntry,
			subtitle: null,
		};
		const at = page.findIndex((block, index) => {
			const found = locate(folded[index] ?? fold(""), block.text, needle);
			if (found === null || overlaps(marks[index] ?? [], found.start, found.end)) return false;
			marks[index]?.push({ ...found, tone: "green" });
			return true;
		});
		if (at === -1) {
			unplacedEvidence.push(note);
		} else {
			metAt.push(at);
			notes[at]?.push(note);
			marked.add(at);
		}
	}

	// Requirement clauses and every other sentence.
	const header: MarginNote[] = [];
	const filterNotes: MarginNote[] = [];
	const requirementNotes: MarginNote[] = [...unplacedEvidence];
	const unplaced: { clause: string; title: string; subtitle: string | null }[] = [];
	const hardFilterReason = input.decisions.findLast((decision) => decision.stage === "hard_filter" && decision.latest)?.reason ?? null;

	const sentences = [
		...(assessment?.conflicts ?? []).map((text) => ({ text, conflict: true })),
		...(assessment?.unknowns ?? []).map((text) => ({ text, conflict: false })),
	];
	for (const [position, { text, conflict }] of sentences.entries()) {
		const finding = requirementFinding(text);
		if (finding !== null) {
			const needle = foldNeedle(finding.clause);
			const tone: MarkTone = finding.required ? "ember" : "dotted";
			const note: MarginNote = {
				key: `clause:${position}`,
				tone: finding.required ? "ember" : "neutral",
				title: finding.required ? "Required · not on your résumé" : "Not on your résumé",
				subtitle: null,
			};
			const at = page.findIndex((block, index) => {
				const found = locate(folded[index] ?? fold(""), block.text, needle);
				if (found === null) return false;
				if (!overlaps(marks[index] ?? [], found.start, found.end)) marks[index]?.push({ ...found, tone });
				return true;
			});
			if (!seenRequirements.has(needle) && !gaps.some((gap) => foldNeedle(gap.clause) === needle)) {
				gaps.push({ clause: finding.clause.trim(), tone: note.tone, at: at === -1 ? null : at });
			}
			if (at === -1) unplaced.push(finding);
			else {
				notes[at]?.push(note);
				marked.add(at);
			}
			continue;
		}
		// Unknown metadata is not a fit finding.
		if (/^(?:[a-z]+ )?(?:application_deadline|opportunity_type)\b/u.test(text)) continue;
		// A conflict that restates the Hard Filter decision is already the Hard Filters note.
		if (conflict && hardFilterReason !== null && text.includes(hardFilterReason)) continue;
		const note: MarginNote = { key: `sentence:${position}`, tone: "neutral", title: assessmentNoteLabel(text), subtitle: null };
		const place = assessmentNotePlace(text);
		if (place === "filters") filterNotes.push(note);
		else if (place === "requirements") requirementNotes.push(note);
		else header.push(note);
	}

	if (unplaced.length > 0) {
		requirementNotes.push({
			key: "unplaced",
			tone: "neutral",
			title: `${unplaced.length} more ${unplaced.length === 1 ? "requirement" : "requirements"} to check`,
			subtitle: null,
			opens: "requirements",
		});
	}

	// Where the first section and the requirements heading are.
	const firstSection = page.length === 0 ? null : 0;
	const firstMarked = [...marked].sort((left, right) => left - right)[0];
	const namedHeading = page.findIndex((block) => block.kind === "heading" && REQUIREMENTS_HEADING.test(block.text));
	const requirementsAt =
		page.length === 0
			? null
			: firstMarked !== undefined
				? sectionStart(page, firstMarked)
				: namedHeading !== -1
					? namedHeading
					: firstSection;

	const sectionNotes: MarginNote[] = [];
	const filter = hardFilterNote(input.decisions);
	if (filter !== null) sectionNotes.push(filter);
	sectionNotes.push(...filterNotes);
	const jev = input.jevTriage === undefined ? null : earlyReviewNote(input.jevTriage);
	const read = assessment?.requirementsRead;
	const validRead = read !== undefined && Number.isSafeInteger(read) && read >= met.length + gaps.length ? read : null;
	const fit = met.length + gaps.length === 0 && jev === null ? null : {
		count: assessment === undefined || assessment.status === "unassessed" ? null
			: validRead === null ? `${met.length} on your résumé` : `Meets ${met.length} of ${validRead}`,
		tally: [...met.map(() => "green" as const), ...gaps.map((gap) => gap.tone),
			...Array.from({ length: (validRead ?? 0) - met.length - gaps.length }, () => "neutral" as const)],
		groups: [
			...(["ember", "neutral"] as const).map((tone) => ({
				tone, title: tone === "ember" ? "Required · not on your résumé" : "Not on your résumé",
				clauses: gaps.filter((gap) => gap.tone === tone).map((gap) => gap.clause),
				at: gaps.find((gap) => gap.tone === tone && gap.at !== null)?.at ?? null,
			})).filter((group) => group.clauses.length > 0),
			...(met.length ? [{ tone: "green" as const, title: "On your résumé", clauses: met, at: metAt[0] ?? null }] : []),
		],
		jev,
	};

	if (firstSection === null) {
		// No page: everything sits beside the header, the application first.
		return {
			fit,
			requirementsAt,
			header: unique([...header, ...sectionNotes, ...requirementNotes]),
			blocks: [],
			unplaced,
		};
	}

	const blocks: MarkedBlock[] = page.map((block, index) => {
		const lead = [
			...(index === firstSection ? sectionNotes : []),
			...(index === requirementsAt ? requirementNotes : []),
		];
		return {
			...block,
			marks: [...(marks[index] ?? [])].sort((left, right) => left.start - right.start),
			count: null,
			notes: unique([...lead, ...(notes[index] ?? [])]),
		};
	});
	return { fit, requirementsAt, header: unique(header), blocks, unplaced };
}

/** A block's text cut at its marks, for rendering: plain runs and marked runs, in order. */
export function segments(block: MarkedBlock): readonly { readonly text: string; readonly tone: MarkTone | null }[] {
	const marks: Mark[] = [];
	for (const mark of [...block.marks].sort((left, right) => left.start - right.start)) {
		const last = marks.at(-1);
		if (last !== undefined && mark.start <= last.end) {
			// A second overlapping mark must not repeat text. Adjacent marks of one tone
			// are one underline, so they cannot create an extra break inside a word.
			if (mark.tone === last.tone) marks[marks.length - 1] = { ...last, end: Math.max(last.end, mark.end) };
			continue;
		}
		marks.push(mark);
	}
	const runs: { text: string; tone: MarkTone | null }[] = [];
	let cursor = 0;
	for (const mark of marks) {
		if (mark.start > cursor) runs.push({ text: block.text.slice(cursor, mark.start), tone: null });
		runs.push({ text: block.text.slice(mark.start, mark.end), tone: mark.tone });
		cursor = mark.end;
	}
	if (cursor < block.text.length) runs.push({ text: block.text.slice(cursor), tone: null });
	return runs;
}
