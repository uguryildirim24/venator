import assert from "node:assert/strict";
import { test } from "node:test";
import { decodeAssessment } from "../server/assessment.ts";

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
