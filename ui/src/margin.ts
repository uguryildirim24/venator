/**
 * What Venator writes in the margin of a Posting, and where on the page each note sits.
 *
 * Pure: it takes the Posting's detail and returns, for every block of the reader page, the
 * underlines to draw in that block and the notes to set level with it. No layout is measured.
 * The page grid puts a block and its notes in the same row, so placing a note here is placing
 * it on screen (ui/DESIGN.md, "The Posting page and its margin").
 *
 * The rules, in the order they are applied:
 *
 * - A résumé fact (every `evidence[]` entry that is not a Hard Filter's own) is looked for in
 *   the page, word for word, ignoring case and spacing. Found, its span is underlined green and its note goes in
 *   that block's margin. Not found, its note goes beside the requirements heading with no
 *   underline. A span is never guessed.
 * - A requirement sentence the assessment quoted (`Qualification not established: …`) is
 *   looked for the same way, as a whole clause ending on a word boundary. Required and not on
 *   the résumé is ember; every other kind is dotted. One that is not on the page is counted
 *   into a single note beside the requirements heading rather than quoted: employer words do
 *   not go in the margin.
 * - Every other sentence is about the whole Posting, its requirements, or a Hard Filter fact,
 *   and sits beside the header, the requirements heading, or the Hard Filters note.
 */

import type { FilterDecision, JevTriage, JobAssessment, ReaderBlock } from "../shared/contracts.ts";
import {
	assessmentNoteLabel,
	assessmentNotePlace,
	EARLY_REVIEW_CAVEAT,
	evidenceSourceLabel,
	jevDecisionLabel,
	jevRuleLabel,
	jevStateLabel,
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
	/** Notes beside the header, after the application. */
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

function hardFilterNote(decisions: readonly FilterDecision[], rulesChecked: number): MarginNote {
	const decision = decisions.findLast((candidate) => candidate.stage === "hard_filter" && candidate.latest) ?? null;
	if (decision === null) {
		return {
			key: "hard-filter",
			tone: "neutral",
			title: "Not checked by the Hard Filters yet",
			subtitle: "The next run decides it",
		};
	}
	if (decision.verdict === "kill") {
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
	return {
		key: "hard-filter",
		tone: "neutral",
		title: "Hard Filters passed",
		subtitle: rulesChecked > 0 ? `All ${rulesChecked} of your rules` : null,
	};
}

function earlyReviewNote(triage: JevTriage): MarginNote {
	const label = jevDecisionLabel(triage.decision);
	return {
		key: "early-review",
		tone: "neutral",
		title: triage.state === "current"
			? `Jev: ${label.charAt(0).toLowerCase()}${label.slice(1)}`
			: triage.state === "unavailable"
				? jevStateLabel(triage.state)
				: `Jev: ${jevStateLabel(triage.state).toLowerCase()}`,
		subtitle: triage.mode === "shadow"
			? "An estimate, not a verdict"
			: triage.state === "current" ? null : EARLY_REVIEW_CAVEAT,
		detail: triage.state === "current" && triage.decision === "exclude" && triage.primaryRule !== null
			? jevRuleLabel(triage.primaryRule) : null,
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
	for (const entry of evidence) {
		const needle = foldNeedle(entry.requirement);
		if (needle === "" || seenRequirements.has(needle)) continue;
		seenRequirements.add(needle);
		const section = evidenceSourceLabel(entry.source);
		const related = entry.basis === "related_skill";
		const note: MarginNote = {
			key: `evidence:${needle}`,
			tone: "green",
			title: related ? "Related skill on your résumé" : "On your résumé",
			subtitle: related ? `${entry.candidateEvidence} · ${section}` : section,
		};
		const at = page.findIndex((block, index) => {
			const found = locate(folded[index] ?? fold(""), block.text, needle);
			if (found === null || overlaps(marks[index] ?? [], found.start, found.end)) return false;
			marks[index]?.push({ ...found, tone: "green" });
			return true;
		});
		if (at === -1) {
			unplacedEvidence.push({ ...note, subtitle: `${entry.requirement} · ${section}` });
		} else {
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
				title: finding.title,
				subtitle: finding.subtitle,
			};
			const at = page.findIndex((block, index) => {
				const found = locate(folded[index] ?? fold(""), block.text, needle);
				if (found === null) return false;
				if (!overlaps(marks[index] ?? [], found.start, found.end)) marks[index]?.push({ ...found, tone });
				return true;
			});
			if (at === -1) unplaced.push(finding);
			else {
				notes[at]?.push(note);
				marked.add(at);
			}
			continue;
		}
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
			subtitle: "Not found word for word on this page",
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

	const rulesChecked = (assessment?.evidence ?? []).filter((entry) => entry.source === HARD_FILTER_EVIDENCE).length;
	const sectionNotes: MarginNote[] = [];
	sectionNotes.push(hardFilterNote(input.decisions, rulesChecked), ...filterNotes);
	if (input.jevTriage !== undefined) sectionNotes.push(earlyReviewNote(input.jevTriage));

	const count = evidence.length === 0 ? null : `${seenRequirements.size} on your résumé`;

	if (firstSection === null) {
		// No page: everything sits beside the header, the application first.
		return {
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
			count: index === requirementsAt ? count : null,
			notes: unique([...lead, ...(notes[index] ?? [])]),
		};
	});
	return { header: unique(header), blocks, unplaced };
}

/** A block's text cut at its marks, for rendering: plain runs and marked runs, in order. */
export function segments(block: MarkedBlock): readonly { readonly text: string; readonly tone: MarkTone | null }[] {
	const runs: { text: string; tone: MarkTone | null }[] = [];
	let cursor = 0;
	for (const mark of block.marks) {
		if (mark.start > cursor) runs.push({ text: block.text.slice(cursor, mark.start), tone: null });
		runs.push({ text: block.text.slice(mark.start, mark.end), tone: mark.tone });
		cursor = mark.end;
	}
	if (cursor < block.text.length) runs.push({ text: block.text.slice(cursor), tone: null });
	return runs;
}
