import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { OnboardingError } from "../server/onboarding/errors.ts";
import { formOf, patchProfileDocuments, profileFormFromBody } from "../server/onboarding/profile-form.ts";
import { validateProfileForm, type ProfileDocuments, type ProfileForm } from "../shared/profile-form.ts";

const EXAMPLE = fileURLToPath(new URL("../../profiles/example/", import.meta.url));

function example(): ProfileDocuments {
	return {
		"resume.yaml": readFileSync(join(EXAMPLE, "resume.yaml"), "utf8"),
		"constraints.yaml": readFileSync(join(EXAMPLE, "constraints.yaml"), "utf8"),
		"targeting.yaml": readFileSync(join(EXAMPLE, "targeting.yaml"), "utf8"),
	};
}

function refusal(work: () => void): OnboardingError {
	try {
		work();
	} catch (cause) {
		if (cause instanceof OnboardingError) return cause;
		throw cause;
	}
	assert.fail("nothing was refused");
}

test("the example Profile reads as fields, with the flags and policy the form does not show left out", () => {
	const form = formOf(example());
	assert.equal(form.resume.name, "Avery Example");
	assert.equal(form.resume.contact.email, "avery@example.com");
	assert.deepEqual(form.resume.education.map((entry) => [entry.origin, entry.degree, entry.gpa]), [[0, "Bachelor of Science in Computer Science", ""]]);
	assert.deepEqual(form.resume.experience[0]?.bullets.length, 2);
	assert.deepEqual(form.resume.technical_proficiencies.map((entry) => entry.label), ["Languages", "Infrastructure"]);
	assert.deepEqual(form.constraints.work_authorization, { status: "", requires_sponsorship: "unanswered", authorized_to_work: "unanswered" });
	assert.equal(form.constraints.screening.eeo.gender, "");
	assert.equal(form.targeting.profileName, "example");
	assert.deepEqual(form.targeting.search, { queries: ["backend engineer", "platform engineer"], locations: ["Chicago, IL", "Remote"], remote: "preferred" });
	assert.deepEqual(form.targeting.employers, [
		{ source: "greenhouse", board: "cloudflare", name: "Cloudflare" },
		{ source: "greenhouse", board: "duolingo", name: "Duolingo" },
		{ source: "ashby", board: "linear", name: "Linear" },
	]);
	assert.deepEqual(form.targeting.filters.enabled, ["education_fit", "role_target"]);
	assert.deepEqual(form.targeting.filters.configured, ["work_authorization", "education_fit", "role_target"]);
	assert.deepEqual(form.targeting.filters.role_target.levels, ["intern", "entry", "mid", "senior", "executive"]);
	assert.deepEqual(form.targeting.filters.role_target.accept, ["mid", "senior"]);
	assert.deepEqual(form.targeting.filters.education_fit, { holds: "bachelor", in_progress: null, experience_years_kill: 8, experience_years_implausible: 15 });
	assert.deepEqual(validateProfileForm(form), []);
});

test("a form nobody changed writes nothing: every file comes back byte for byte", () => {
	const original = example();
	assert.deepEqual(patchProfileDocuments(original, formOf(original)), original);
});

test("what changed is written into the same document, and what the form does not show survives", () => {
	const original = example();
	const form = formOf(original);
	const edited: ProfileForm = {
		resume: {
			...form.resume,
			name: "no",
			contact: { ...form.resume.contact, email: "avery@example.org" },
			education: [{ ...form.resume.education[0]!, gpa: "3.9/4.0", location: "" }],
			technical_proficiencies: [form.resume.technical_proficiencies[1]!],
			experience: [...form.resume.experience, { origin: null, org: "New Co", role: "Engineer", location: "", dates: "2024 - 2025", bullets: ["Did: things", "- dashy"] }],
		},
		constraints: {
			work_authorization: { status: "Citizen", requires_sponsorship: "no", authorized_to_work: "yes" },
			screening: { ...form.constraints.screening, how_heard: "12:30" },
		},
		targeting: {
			...form.targeting,
			search: { queries: ["backend engineer", "sre"], locations: [], remote: null },
			employers: [
				{ source: "greenhouse", board: "cloudflare", name: "Cloudflare, Inc." },
				{ source: "ashby", board: "linear", name: "Linear" },
				{ source: "workday", board: "yes", name: "Yes Corp" },
			],
			filters: {
				...form.targeting.filters,
				enabled: ["education_fit", "role_target", "work_authorization"],
				role_target: { ...form.targeting.filters.role_target, accept: ["senior"], exclude_terms: [] },
				education_fit: { ...form.targeting.filters.education_fit, holds: "master", in_progress: "doctorate", experience_years_kill: null },
			},
		},
	};
	const patched = patchProfileDocuments(original, edited);
	const resume = patched["resume.yaml"];
	assert.match(resume, /^name: "no"$/mu, "a word YAML 1.1 reads as a boolean is quoted");
	assert.match(resume, /email: avery@example\.org/u);
	assert.match(resume, /primary: true/u, "an entry flag the form does not show stays");
	assert.match(resume, /gpa: 3\.9\/4\.0/u);
	assert.match(resume, /- org: Example University\n {4}date: May 2020/u, "a blanked entry field is deleted, not written empty");
	assert.doesNotMatch(resume, /label: Languages/u);
	assert.match(resume, /- org: New Co\n {4}role: Engineer\n {4}dates: 2024 - 2025\n {4}bullets:\n {6}- "Did: things"\n {6}- "- dashy"/u);
	assert.match(resume, /# Fictional evidence for testing only/u, "comments survive");
	assert.match(resume, /^memberships: \[\]$/mu);

	const constraints = patched["constraints.yaml"];
	assert.match(constraints, /status: Citizen/u);
	assert.match(constraints, /requires_sponsorship: false/u);
	assert.match(constraints, /authorized_to_work: true/u);
	assert.match(constraints, /how_heard: "12:30"/u);
	assert.match(constraints, /gender: null/u);
	assert.match(constraints, /^option_aliases: \{\}$/mu);
	assert.match(constraints, /the planner never guesses "No" on your behalf\./u);

	const targeting = patched["targeting.yaml"];
	assert.match(targeting, /queries:\n {4}- backend engineer\n {4}- sre\n {2}locations: \[\]/u);
	assert.match(targeting, /remote: null # required \| preferred \| acceptable \| no/u, "clearing keeps the comment beside the key");
	assert.match(targeting, /names:\n {4}cloudflare: Cloudflare, Inc\.\n {4}linear: Linear\n {4}"yes": Yes Corp/u);
	assert.doesNotMatch(targeting, /duolingo/u);
	assert.match(targeting, /workday:\n {6}- "yes"/u);
	assert.match(targeting, /qualification_mode: jev\n {2}jev:\n {4}policy_version: example-shadow-1/u, "the Jev policy is untouched");
	assert.match(targeting, /enabled:\n {4}- education_fit\n {4}- role_target\n {4}- work_authorization/u);
	assert.match(targeting, /accept:\n {6}- senior/u);
	assert.match(targeting, /terms: \[\]/u);
	assert.match(targeting, /holds: master/u);
	assert.match(targeting, /in_progress: doctorate/u);
	assert.match(targeting, /experience_years_kill: null/u);
	assert.match(targeting, /restriction_patterns: \[\]/u);
	assert.match(targeting, /scaffold: true/u);
	assert.deepEqual(formOf(patched).targeting.employers.map((employer) => employer.board), ["cloudflare", "linear", "yes"]);
	assert.deepEqual(formOf(patched).resume.experience.map((entry) => entry.org), ["Example Software Studio", "New Co"]);
});

test("a blank answer that was never written is not written, and a stale origin is refused", () => {
	const original = { ...example(), "constraints.yaml": "screening: {}\n" };
	const form = formOf(original);
	assert.equal(patchProfileDocuments(original, form)["constraints.yaml"], "screening: {}\n");
	const answered = { ...form, constraints: { ...form.constraints, screening: { ...form.constraints.screening, country: "Ireland" } } };
	assert.equal(patchProfileDocuments(original, answered)["constraints.yaml"], "screening:\n  country: Ireland\n");
	const stale = { ...form, resume: { ...form.resume, education: [{ ...form.resume.education[0]!, origin: 4 }] } };
	assert.equal(refusal(() => patchProfileDocuments(original, stale)).code, "bad_request");
	const twice = { ...form, resume: { ...form.resume, education: [form.resume.education[0]!, form.resume.education[0]!] } };
	assert.equal(refusal(() => patchProfileDocuments(original, twice)).field, "resume.education");
});

test("the form off the wire is checked field by field", () => {
	const form = formOf(example());
	const body: unknown = JSON.parse(JSON.stringify(form));
	// SAFETY: a round trip through JSON of a ProfileForm is a JSON value.
	assert.deepEqual(profileFormFromBody(body as ProfileForm), form);
	const control = { ...form, resume: { ...form.resume, name: "Avery\u0085Example" } };
	assert.equal(refusal(() => profileFormFromBody(control)).field, "resume.name");
	const notTri = { ...form, constraints: { ...form.constraints, work_authorization: { ...form.constraints.work_authorization, requires_sponsorship: "maybe" } } };
	// SAFETY: deliberately not a TriState, to see it refused.
	assert.equal(refusal(() => profileFormFromBody(notTri as ProfileForm)).field, "constraints.work_authorization.requires_sponsorship");
	const badBoard = { ...form, targeting: { ...form.targeting, employers: [{ source: "greenhouse", board: "a/b", name: "A" }] } };
	assert.equal(refusal(() => profileFormFromBody(badBoard)).field, "targeting.employers.0.board");
});

test("what the loader would refuse holds Save, beside the field it names", () => {
	const form = formOf(example());
	const filters = form.targeting.filters;
	const at = (edited: ProfileForm) => validateProfileForm(edited).map((issue) => issue.field);
	assert.deepEqual(at({ ...form, targeting: { ...form.targeting, filters: { ...filters, enabled: [] } } }), ["targeting.filters.enabled"]);
	assert.deepEqual(at({ ...form, targeting: { ...form.targeting, filters: { ...filters, education_fit: { ...filters.education_fit, experience_years_kill: 16 } } } }), ["targeting.filters.education_fit.experience_years_kill"]);
	assert.deepEqual(at({ ...form, targeting: { ...form.targeting, filters: { ...filters, education_fit: { ...filters.education_fit, holds: "master", in_progress: "bachelor" } } } }), ["targeting.filters.education_fit.in_progress"]);
	assert.deepEqual(at({ ...form, targeting: { ...form.targeting, filters: { ...filters, role_target: { ...filters.role_target, accept: [] } } } }), ["targeting.filters.role_target.accept"]);
	assert.deepEqual(at({ ...form, targeting: { ...form.targeting, employers: [...form.targeting.employers, { source: "lever", board: "x", name: "" }] } }), ["targeting.employers"]);
	assert.deepEqual(at({ ...form, resume: { ...form.resume, contact: { ...form.resume.contact, email: " " } } }), ["resume.contact.email"]);
	const empty: ProfileForm = { ...form, resume: { name: "", contact: { location: "", phone: "", email: "", linkedin: "" }, education: [], technical_proficiencies: [], experience: [] } };
	assert.deepEqual(at(empty), [], "a Profile with no résumé at all is not held on contact");
});
