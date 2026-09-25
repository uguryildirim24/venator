/**
 * What a parsed resume does to the draft, and — the part that matters — what it does not.
 *
 * The resume step now proposes education, skills and experience, which the step deliberately
 * did not propose before. `server/onboarding/resume.ts` argues, correctly, that a wrong value
 * in one of those sections is a lie on somebody's resume rather than a typo in a form. What
 * answers that argument is not the grounding check in `venator.resume.parse` — that proves a
 * value was spelled out of the uploaded page, and a page can spell an instruction as easily as
 * a fact — it is a person reading the proposal before any of it is written.
 *
 * So the property under test is the one that makes the rest safe: **a section nobody has
 * confirmed does not reach `resume.yaml`.** It is enforced in `resumeMapping` rather than on
 * screen, so it holds however the draft was got into that state.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import type { ParsedResumeResponse } from "../shared/onboarding.ts";
import {
	EMPTY_DRAFT,
	profileProposal,
	unconfirmedFields,
	unconfirmedSections,
	withParsedResume,
	withSuggestion,
	type Draft,
	type ResumeDraft,
} from "../src/onboarding/draft.ts";

/** A parse, in the shape the route answers with. Nothing in it is confirmed. */
const PARSED: ParsedResumeResponse = {
	kind: "parsed",
	resume: {
		name: "Alex Q. Rivera",
		contact: {
			location: "Boston, MA",
			phone: "(617) 555-0142",
			email: "alex.rivera@example.com",
			linkedin: "www.linkedin.com/in/alexqrivera",
		},
		education: [{ org: "Northeastern University", degree: "Bachelor of Science", date: "May 2027" }],
		technical_proficiencies: [{ label: "Laboratory", items: "HPLC, liquid handling" }],
		experience: [{ org: "Some Employer", role: "Laboratory Assistant", bullets: ["Ran 40 assays a week"] }],
	},
	sections: [
		{ section: "education", entries: 1, fields: 6, filled: 3, dropped: 0, confirmed: false },
		{ section: "technical_proficiencies", entries: 1, fields: 2, filled: 2, dropped: 0, confirmed: false },
		{ section: "experience", entries: 1, fields: 5, filled: 3, dropped: 1, confirmed: false },
	],
	pages: 2,
	characters: 2400,
	truncated: false,
	dropped: 1,
	schemaEnforced: true,
	found: ["name", "location", "phone", "email", "linkedin"],
	missing: [],
};

function drafted(resume: ResumeDraft): Draft {
	return { ...EMPTY_DRAFT, resume, targeting: { ...EMPTY_DRAFT.targeting, profileName: "alex" } };
}

function confirmAll(resume: ResumeDraft): ResumeDraft {
	return {
		...resume,
		sections: {
			...resume.sections,
			confirmed: { education: true, technical_proficiencies: true, experience: true },
		},
	};
}

test("a parse lands unconfirmed, in full, and is named as still waiting", () => {
	const resume = withParsedResume(EMPTY_DRAFT.resume, PARSED);
	assert.equal(resume.sections.proposed, true);
	assert.equal(resume.sections.education.length, 1);
	assert.equal(resume.sections.experience[0]?.bullets, "Ran 40 assays a week");
	assert.deepEqual(resume.sections.confirmed, {
		education: false,
		technical_proficiencies: false,
		experience: false,
	});
	assert.deepEqual([...unconfirmedSections(resume)], ["education", "technical_proficiencies", "experience"]);
	// The five contact facts arrive as suggestions too, exactly as a pasted resume's do.
	assert.equal(resume.values.email, "alex.rivera@example.com");
	assert.equal(resume.acknowledged.email, false);
});

test("a section nobody confirmed is not written into the Profile", () => {
	const proposal = profileProposal(drafted(withParsedResume(EMPTY_DRAFT.resume, PARSED)), false);
	assert.equal(proposal.resume?.education, undefined);
	assert.equal(proposal.resume?.technical_proficiencies, undefined);
	assert.equal(proposal.resume?.experience, undefined);
	// And nothing of it survives into what actually goes over the wire.
	const body = JSON.stringify(proposal);
	assert.ok(!body.includes("Northeastern"));
	assert.ok(!body.includes("Ran 40 assays"));
});

test("a section the Owner confirmed is written, as they left it", () => {
	const proposal = profileProposal(drafted(confirmAll(withParsedResume(EMPTY_DRAFT.resume, PARSED))), false);
	assert.equal(proposal.resume?.education?.[0]?.org, "Northeastern University");
	assert.equal(proposal.resume?.education?.[0]?.date, "May 2027");
	assert.equal(proposal.resume?.technical_proficiencies?.[0]?.items, "HPLC, liquid handling");
	assert.deepEqual([...(proposal.resume?.experience?.[0]?.bullets ?? [])], ["Ran 40 assays a week"]);
});

test("a blank field is left out rather than written empty, and an emptied entry disappears", () => {
	const parsed = withParsedResume(EMPTY_DRAFT.resume, PARSED);
	const emptied: ResumeDraft = confirmAll({
		...parsed,
		sections: {
			...parsed.sections,
			education: [{ org: "", location: "", date: "", degree: "", gpa: "", coursework: "" }],
			experience: parsed.sections.experience.map((entry) => ({ ...entry, bullets: "  \n\n  " })),
		},
	});
	const proposal = profileProposal(drafted(emptied), false);
	assert.equal(proposal.resume?.education, undefined, "an entry with nothing in it is not an entry");
	assert.equal(proposal.resume?.experience?.[0]?.bullets, undefined, "no empty bullet is written");
	assert.equal(proposal.resume?.experience?.[0]?.org, "Some Employer", "the rest of the entry stands");
});

test("the five the renderer needs are untouched by any of this", () => {
	// The write still refuses a resume missing any of them, and this step still collects them
	// the way it always did — a section is an addition to that gate, never a substitute for it.
	const proposal = profileProposal(drafted(withParsedResume(EMPTY_DRAFT.resume, PARSED)), false);
	assert.equal(proposal.resume?.name, "Alex Q. Rivera");
	assert.equal(proposal.resume?.contact.linkedin, "www.linkedin.com/in/alexqrivera");
});

test("a skipped résumé step sends no résumé at all, rather than five blanks the write refuses", () => {
	const proposal = profileProposal(drafted(withParsedResume(EMPTY_DRAFT.resume, PARSED)), false, false);
	assert.equal(proposal.resume, undefined);
	assert.ok(!JSON.stringify(proposal).includes('"resume"'));
});

test("replacing a résumé does not carry its confirmed suggestions into the new document", () => {
	const first = withParsedResume(EMPTY_DRAFT.resume, PARSED);
	const ticked: ResumeDraft = {
		...first,
		acknowledged: { name: true, location: true, phone: true, email: true, linkedin: true },
		sections: { ...first.sections, confirmed: { education: true, technical_proficiencies: true, experience: true } },
		values: { ...first.values, phone: "Person-typed number" },
	};
	const second = withSuggestion(ticked, {
		kind: "read",
		resume: { name: "Another Person", contact: { location: null, phone: null, email: null, linkedin: null } },
		found: ["name"], missing: ["location", "phone", "email", "linkedin"], notAttempted: [],
	});
	assert.equal(second.values.name, "Another Person");
	assert.equal(second.values.location, "");
	assert.equal(second.values.phone, "Person-typed number");
	assert.equal(second.acknowledged.name, false);
	assert.ok(unconfirmedFields(second).includes("name"));
	assert.equal(second.sections.experience.length, 0);
	assert.equal(profileProposal(drafted(second), false).resume?.experience, undefined);
});

test("a second parse replaces what the first proposed, and unconfirms it again", () => {
	const first = confirmAll(withParsedResume(EMPTY_DRAFT.resume, PARSED));
	const second = withParsedResume(first, {
		...PARSED,
		resume: { ...PARSED.resume, name: "Another Person", education: [{ org: "Another University" }] },
	});
	assert.equal(second.values.name, "Another Person");
	assert.equal(second.acknowledged.name, false);
	assert.equal(second.sections.education.length, 1);
	assert.equal(second.sections.education[0]?.org, "Another University");
	assert.equal(second.sections.confirmed.education, false, "a tick is about the values it was ticked over");
});
