/**
 * Profile names, and the validations that stand between a person and a Profile that loads
 * while filtering nothing.
 *
 * Every semantic case below is one `src/venator/profile/loader.py` refuses. If one of these
 * ever stops matching the loader, the onboarding flow writes a Profile the pipeline will not
 * load — or, worse, one it will load and run with no Hard Filter at all.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { OnboardingError } from "../server/onboarding/errors.ts";
import type { JsonMapping } from "../server/onboarding/json.ts";
import { validateProfileName, validateProposal, type ProfileProposal } from "../server/onboarding/profile.ts";

function proposal(targeting: JsonMapping, constraints: JsonMapping | null = null): ProfileProposal {
	return {
		name: "someone",
		overwrite: false,
		resume: {
			name: "A Person",
			contact: { location: "Boston, MA", phone: "555-0100", email: "a@b.co", linkedin: "linkedin.com/in/x" },
		},
		constraints,
		targeting,
	};
}

function refusal(run: () => void): OnboardingError {
	try {
		run();
	} catch (error) {
		assert.ok(error instanceof OnboardingError, "expected an onboarding refusal");
		return error;
	}
	throw new assert.AssertionError({ message: "expected a refusal, and none came" });
}

test("a plain name is accepted, trimmed", () => {
	assert.equal(validateProfileName("  sample  "), "sample");
	assert.equal(validateProfileName("second-search"), "second-search");
	assert.equal(validateProfileName("search_2"), "search_2");
});

test("an empty name is refused — it used to resolve to the working directory", () => {
	assert.equal(refusal(() => validateProfileName("")).code, "invalid_profile_name");
	assert.equal(refusal(() => validateProfileName("   ")).code, "invalid_profile_name");
});

test("a name holding a separator is refused, on either platform's spelling", () => {
	for (const attempt of ["../escape", "a/b", "..\\escape", "a\\b", "/etc/passwd", "C:\\Windows"]) {
		assert.equal(refusal(() => validateProfileName(attempt)).code, "invalid_profile_name", attempt);
	}
});

test("a leading dot is refused, including the two that traverse", () => {
	for (const attempt of [".", "..", ".hidden", "...."]) {
		assert.equal(refusal(() => validateProfileName(attempt)).code, "invalid_profile_name", attempt);
	}
});

test("a name that is not plainly a name is refused", () => {
	for (const attempt of ["a:b", "a b", "a\u0000b", "a\nb", "naïve", "-leading", "x".repeat(65)]) {
		assert.equal(refusal(() => validateProfileName(attempt)).code, "invalid_profile_name", JSON.stringify(attempt));
	}
});

test("a filter policy with nothing enabled is refused — every Posting would pass unfiltered", () => {
	const error = refusal(() =>
		validateProposal(proposal({ filters: { role_target: { exclude: { terms: ["sales"] } } } })),
	);
	assert.equal(error.code, "invalid_profile");
	assert.equal(error.field, "targeting.filters.enabled");
	assert.match(error.message, /pass unfiltered/u);
});

test("a filter policy that enables what it configures is accepted", () => {
	const warnings = validateProposal(
		proposal({ filters: { enabled: ["role_target"], role_target: { exclude: { terms: ["sales"] } } } }),
	);
	assert.ok(!warnings.some((warning) => warning.includes("unfiltered")));
});

test("a Hard Filter that does not exist is refused", () => {
	const error = refusal(() => validateProposal(proposal({ filters: { enabled: ["salary"] } })));
	assert.equal(error.field, "targeting.filters.enabled.0");
});

test("an accepted rung that is not on the ladder is refused", () => {
	const error = refusal(() =>
		validateProposal(
			proposal({
				filters: {
					enabled: ["role_target"],
					role_target: { levels: [{ name: "mid" }, { name: "senior" }], accept: ["staff"] },
				},
			}),
		),
	);
	assert.equal(error.field, "targeting.filters.role_target.accept.0");
	assert.match(error.message, /not a rung/u);
});

test("a duplicated rung is refused", () => {
	const error = refusal(() =>
		validateProposal(
			proposal({ filters: { enabled: ["role_target"], role_target: { levels: [{ name: "mid" }, { name: "mid" }] } } }),
		),
	);
	assert.match(error.message, /already a rung/u);
});

test("a kill threshold above the implausibility ceiling is refused, default ceiling included", () => {
	const explicit = refusal(() =>
		validateProposal(
			proposal({
				filters: {
					enabled: ["education_fit"],
					education_fit: { experience_years_kill: 20, experience_years_implausible: 15 },
				},
			}),
		),
	);
	assert.equal(explicit.field, "targeting.filters.education_fit.experience_years_kill");

	// With no ceiling written the loader uses 15, so the same comparison has to happen here.
	const implied = refusal(() =>
		validateProposal(proposal({ filters: { enabled: ["education_fit"], education_fit: { experience_years_kill: 16 } } })),
	);
	assert.match(implied.message, /\(15\)/u);
});

test("the retired location Hard Filter cannot be enabled", () => {
	const error = refusal(() => validateProposal(proposal({ filters: { enabled: ["location"] } })));
	assert.equal(error.field, "targeting.filters.enabled.0");
	assert.match(error.message, /not a Hard Filter/u);
});

test("a Profile name inside targeting.yaml that disagrees with the directory is refused", () => {
	const error = refusal(() => validateProposal(proposal({ profile: { name: "somebody-else" } })));
	assert.equal(error.field, "targeting.profile.name");
});

test("a scaffold cannot be written from onboarding — nothing would ever imply it", () => {
	assert.equal(refusal(() => validateProposal(proposal({ profile: { scaffold: true } }))).field, "targeting.profile.scaffold");
});

test("a resume without the five fields the renderer reads is refused", () => {
	const bare: ProfileProposal = { ...proposal({}), resume: { name: "A Person" } };
	assert.equal(refusal(() => validateProposal(bare)).field, "resume.contact");

	const partial: ProfileProposal = {
		...proposal({}),
		resume: { name: "A Person", contact: { location: "Boston, MA", phone: "555-0100", email: "a@b.co" } },
	};
	assert.equal(refusal(() => validateProposal(partial)).field, "resume.contact.linkedin");
});

test("a sponsorship answer is refused unless the Profile says sponsorship is required", () => {
	const error = refusal(() =>
		validateProposal(
			proposal({}, { work_authorization: { requires_sponsorship: null }, screening: { requires_sponsorship: "No" } }),
		),
	);
	assert.equal(error.field, "constraints.screening.requires_sponsorship");
	assert.match(error.message, /states outright/u);

	// Literally true is the only thing that permits one.
	validateProposal(
		proposal({}, { work_authorization: { requires_sponsorship: true }, screening: { requires_sponsorship: "Yes" } }),
	);
	// And a blank answer is always fine — that is the default state of the whole block.
	validateProposal(proposal({}, { screening: { requires_sponsorship: null, eeo: { gender: null } } }));
});

test("a key that would reach an object's prototype is refused rather than dropped", () => {
	const hostile: JsonMapping = JSON.parse('{"filters": {"__proto__": {"polluted": true}}}');
	assert.equal(refusal(() => validateProposal(proposal(hostile))).code, "invalid_profile");
});

test("an empty Profile is written, and says out loud that it filters nothing", () => {
	const warnings = validateProposal({ name: "someone", overwrite: false, resume: null, constraints: null, targeting: null });
	assert.ok(warnings.some((warning) => warning.includes("no profile filters run")));
	assert.ok(warnings.some((warning) => warning.includes("resume.yaml")));
});

test("regular expressions in a Profile are surfaced rather than checked silently", () => {
	const warnings = validateProposal(
		proposal({
			filters: {
				enabled: ["work_authorization"],
				work_authorization: { restriction_patterns: ["(a+)+$"] },
			},
		}),
	);
	assert.ok(warnings.some((warning) => warning.includes("regular expressions")));
});
