/**
 * Decode `jev_triage` rows. Machine identifiers stay in the payload; English lives in labels.
 *
 * An older view without this table is not an error. Callers that asked for the table check
 * it exists first and return an empty list, so a pipeline view built before Jev still opens.
 */

import type { JevTriage } from "../shared/contracts.ts";
import { JEV_TRIAGE_DECISIONS, JEV_TRIAGE_MODES, JEV_TRIAGE_STATES } from "../shared/contracts.ts";
import { asList, asMapping, asText, at, parseJson } from "./onboarding/json.ts";
import {
	memberColumn,
	optionalTextColumn,
	textColumn,
	ViewDataError,
	type SqlRow,
} from "./rows.ts";

function flagNames(row: SqlRow, name: string): readonly string[] {
	const list = asList(parseJson(textColumn(row, name)));
	if (list === null) throw new ViewDataError(`Invalid ${name} list.`);
	return list.map((item) => {
		const text = asText(item);
		if (text !== null) return text;
		const mapping = asMapping(item);
		if (mapping !== null) {
			const named =
				asText(at(mapping, "rule")) ?? asText(at(mapping, "code")) ?? asText(at(mapping, "id"));
			if (named !== null) return named;
		}
		throw new ViewDataError(`Invalid ${name} entry.`);
	});
}

export function decodeJevTriage(row: SqlRow): JevTriage {
	return {
		postingKey: textColumn(row, "posting_key"),
		mode: memberColumn(row, "mode", JEV_TRIAGE_MODES),
		state: memberColumn(row, "state", JEV_TRIAGE_STATES),
		decision: memberColumn(row, "decision", JEV_TRIAGE_DECISIONS),
		primaryRule: optionalTextColumn(row, "primary_rule"),
		exclusions: flagNames(row, "exclusions"),
		reviewFlags: flagNames(row, "review_flags"),
		diagnosticFlags: flagNames(row, "diagnostic_flags"),
		qualifierVersion: optionalTextColumn(row, "qualifier_version"),
		assessmentKey: optionalTextColumn(row, "assessment_key"),
		inputVersion: optionalTextColumn(row, "input_version"),
		policyHash: optionalTextColumn(row, "policy_hash"),
		modelId: optionalTextColumn(row, "model_id"),
		asOfMonth: textColumn(row, "as_of_month"),
		decidedAt: optionalTextColumn(row, "decided_at"),
		reason: optionalTextColumn(row, "reason"),
	};
}
