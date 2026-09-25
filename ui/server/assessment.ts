import type { JobAssessment } from "../shared/contracts.ts";
import { asList, asMapping, asText, parseJson } from "./onboarding/json.ts";
import { memberColumn, optionalTextColumn, textColumn, ViewDataError, type SqlRow } from "./rows.ts";

function strings(row: SqlRow, name: string): readonly string[] {
	const list = asList(parseJson(optionalTextColumn(row, name) ?? "[]"));
	if (list === null) throw new ViewDataError(`Invalid ${name} list.`);
	return list.map((item) => {
		const text = asText(item);
		if (text === null) throw new ViewDataError(`Invalid ${name} text.`);
		return text;
	});
}

export function decodeAssessment(row: SqlRow): JobAssessment {
	const evidence = asList(parseJson(optionalTextColumn(row, "evidence") ?? "[]"));
	if (evidence === null) throw new ViewDataError("Invalid assessment evidence.");
	return {
		status: memberColumn(row, "assessment_status", ["suitable", "needs_review", "not_suitable", "unassessed"]),
		assessedBy: memberColumn(row, "assessed_by", ["deterministic", "jev"]),
		assessedAsOf: optionalTextColumn(row, "assessed_as_of"),
		summary: textColumn(row, "assessment_summary"),
		evidence: evidence.map((item) => {
			const value = asMapping(item);
			const requirement = value === null ? null : asText(value.requirement);
			const candidateEvidence = value === null ? null : asText(value.candidate_evidence);
			const source = value === null ? null : asText(value.source);
			if (requirement === null || candidateEvidence === null || source === null) {
				throw new ViewDataError("Incomplete assessment evidence.");
			}
			if (value !== null && Object.hasOwn(value, "basis")) {
				if (value.basis !== "related_skill") throw new ViewDataError("Invalid assessment evidence basis.");
				return { requirement, candidateEvidence, source, basis: value.basis };
			}
			return { requirement, candidateEvidence, source };
		}),
		conflicts: strings(row, "conflicts"),
		unknowns: strings(row, "unknowns"),
		listingStatus: memberColumn(row, "listing_status", ["open", "closed", "unknown"]),
		lastVerifiedAt: optionalTextColumn(row, "last_verified_at"),
		descriptionKind: textColumn(row, "description_kind"),
		applyUrl: textColumn(row, "apply_url"),
		opportunityType: textColumn(row, "opportunity_type"),
	};
}
