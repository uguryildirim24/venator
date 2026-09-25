import assert from "node:assert/strict";
import { test } from "node:test";
import { decodeAssessment } from "../server/assessment.ts";
import { ViewDataError } from "../server/rows.ts";

const evidence = { requirement: "Programming", candidate_evidence: "Python", source: "profile.resume.technical_proficiencies" };
function decode(items: readonly unknown[]) {
	return decodeAssessment({
		assessment_status: "needs_review", assessment_summary: "Review related evidence",
		evidence: JSON.stringify(items), conflicts: "[]", unknowns: "[]", listing_status: "open",
		last_verified_at: null, description_kind: "full", apply_url: "https://example.test/job", opportunity_type: "job",
		assessed_by: "deterministic", assessed_as_of: null,
	});
}

test("deterministic assessment retains its provenance", () => {
	const result = decode([evidence]);
	assert.equal(result.assessedBy, "deterministic");
	assert.equal(result.assessedAsOf, null);
});

test("a Jev assessment retains typed provenance", () => {
	const result = decodeAssessment({
		assessment_status: "needs_review", assessment_summary: "Priority for review.",
		evidence: JSON.stringify([evidence]), conflicts: "[]", unknowns: "[]",
		listing_status: "open", last_verified_at: null, description_kind: "full",
		apply_url: "https://example.test", opportunity_type: "job",
		assessed_by: "jev", assessed_as_of: "2026-09",
	});
	assert.equal(result.assessedBy, "jev");
	assert.equal(result.status, "needs_review");
});

test("retired assessment provenance is refused", () => {
	assert.throws(() => decodeAssessment({
		assessment_status: "suitable", assessment_summary: "Potential match",
		evidence: JSON.stringify([evidence]), conflicts: "[]", unknowns: "[]",
		listing_status: "open", last_verified_at: null, description_kind: "full",
		apply_url: "https://example.test", opportunity_type: "job",
		assessed_by: "model", assessed_as_of: "2026-09",
	}), ViewDataError);
});

test("related skill evidence retains its basis while unmarked evidence stays unchanged", () => {
	const result = decode([{ ...evidence, basis: "related_skill" }, evidence]);
	assert.deepEqual(result.evidence, [
		{ requirement: "Programming", candidateEvidence: "Python", source: evidence.source, basis: "related_skill" },
		{ requirement: "Programming", candidateEvidence: "Python", source: evidence.source },
	]);
	assert.equal(Object.hasOwn(result.evidence[1] ?? {}, "basis"), false);
});

test("unknown and malformed evidence bases are refused instead of presented as exact evidence", () => {
	for (const basis of ["exact", "model_finding", "", null, false, 1, [], { type: "related_skill" }]) {
		assert.throws(() => decode([{ ...evidence, basis }]), ViewDataError);
	}
});
