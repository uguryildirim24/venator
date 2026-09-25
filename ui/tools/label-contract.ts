/**
 * Pins owner-facing labels against the schema they describe.
 *
 * `applicationStateLabel` was written from the state words rather than from the transitions,
 * and one of them ended up asserting the opposite fact: `filled` — the pipeline dry-run
 * filling an employer's form — rendered as "Position filled", which reads as the employer
 * having filled the role. That is the kind of thing an Owner acts on wrongly, and nothing
 * caught it, because a label is prose and prose has no type.
 *
 * So each state is pinned here next to the clause of coordination/CONTRACTS.md it comes from.
 * Changing a label means editing the clause it is filed under and reading that clause again,
 * which is the whole point. Two structural assertions ride along for the states whose plain
 * English inverts the actor, so a future rewrite cannot quietly restore the inversion while
 * leaving the pin apparently satisfied.
 *
 * Run by `pnpm smoke`, which is a required check.
 */

import { APPLICATION_STATE_NAMES, type ApplicationStateName } from "../shared/contracts.ts";
import { applicationStateLabel, assessmentStatusLabel, earlyReviewLabel, homeListLabel, jevDecisionLabel, statusLabel } from "../src/labels.ts";

type StatePin = {
	readonly state: ApplicationStateName;
	/** The transition from coordination/CONTRACTS.md, and who it can only come from. */
	readonly contract: string;
	/** The label the contract clause supports. */
	readonly label: string;
	/**
	 * Words that would make the label assert the wrong actor or the wrong fact. Only the
	 * states whose ordinary-English reading inverts carry these; the rest are pinned alone.
	 */
	readonly mustNotContain: readonly string[];
	/** Words the clause requires the label to keep, for the same reason. */
	readonly mustContain: readonly string[];
};

const PINS: readonly StatePin[] = [
	{
		state: "queued",
		contract:
			"`queued` is DERIVED, not an event: latest llm_score decision has verdict=queue and " +
			"the Posting has no TrackEvents. It is waiting for the Owner, not for an employer.",
		label: "In queue",
		mustNotContain: [],
		mustContain: [],
	},
	{
		state: "approved",
		contract: "queued --approve--> `approved` (actor: owner). The Owner approved it for submission.",
		label: "Saved",
		mustNotContain: [],
		mustContain: [],
	},
	{
		state: "rejected",
		contract:
			"queued|approved --reject--> `rejected` (terminal; owner declined in review; actor: owner). " +
			"An employer's refusal is an `outcome` event and lands on `concluded` instead, so a bare " +
			"\"Rejected\" here captions the two the wrong way round.",
		label: "Dismissed",
		mustNotContain: ["employer"],
		mustContain: [],
	},
	{
		state: "prepared",
		contract:
			"A local application bundle has been prepared for review; no employer form has been opened or submitted.",
		label: "Documents ready",
		mustNotContain: ["submitted", "sent"],
		mustContain: ["documents"],
	},
	{
		state: "filled",
		contract:
			"approved --fill--> `filled` (dry-run Playwright fill; detail = bundle path; actor: pipeline). " +
			"The pipeline filled the employer's form in a Dry Run. Nothing was sent, and the employer " +
			"did not fill the role.",
		label: "Application assisted",
		mustNotContain: ["position", "role", "sent", "submitted"],
		mustContain: [],
	},
	{
		state: "submitted",
		contract:
			"filled --submit--> `submitted` — NOTHING emits this today; a submit event additionally " +
			"requires the prior approve event to exist.",
		label: "Submitted",
		mustNotContain: [],
		mustContain: [],
	},
	{
		state: "concluded",
		contract:
			"submitted|concluded --outcome--> `concluded` (employer response; re-recordable, latest " +
			"wins — e.g. interview then offer). The state records that an employer responded; it does " +
			"not mean the application is over, since `interview` is one of the responses.",
		label: "Employer responded",
		mustNotContain: ["closed", "finished", "over"],
		mustContain: ["Employer"],
	},
	{
		state: "withdrawn",
		contract: "any non-terminal --withdraw--> `withdrawn` (terminal; actor: owner).",
		label: "Withdrawn",
		mustNotContain: [],
		mustContain: [],
	},
];

/** Every failure found, as a line the caller can print. An empty array means the pins hold. */
export function checkLabelContract(): readonly string[] {
	const failures: string[] = [];
	// Suitable/queued means relevant experience with no known hard conflict, not
	// confirmed eligibility. Skill gaps remain visible; material uncertainty needs review.
	const assessmentLabels = [
		[homeListLabel("queued"), "For you"],
		[statusLabel("queued"), "Potential matches"],
		[assessmentStatusLabel("suitable"), "Potential match"],
		[homeListLabel("needs-review"), "Explore"],
		[statusLabel("needs-review"), "Needs review"],
		[assessmentStatusLabel("needs_review"), "Needs review"],
		[homeListLabel("applied"), "Applications"],
		[statusLabel("applied"), "Saved & applied"],
	];
	for (const [rendered, expected] of assessmentLabels) {
		if (rendered !== expected) failures.push(`assessment label "${rendered}" must be "${expected}"; potential relevance does not confirm eligibility`);
	}

	const jevLabels = [
		[jevDecisionLabel("prioritize"), "Priority for review"],
		[jevDecisionLabel("review"), "Review"],
		[jevDecisionLabel("exclude"), "Skip"],
		[jevDecisionLabel("unassessed"), "Unavailable"],
		[earlyReviewLabel(), "Jev diagnostics"],
	] as const;
	for (const [rendered, expected] of jevLabels) {
		if (rendered !== expected) {
			failures.push(`Jev label "${rendered}" must be "${expected}"`);
		}
	}

	const pinned = new Set(PINS.map((pin) => pin.state));

	for (const state of APPLICATION_STATE_NAMES) {
		if (!pinned.has(state)) {
			failures.push(
				`application state '${state}' has no pin — add one quoting the transition it comes from`,
			);
		}
	}

	const seen = new Map<string, ApplicationStateName>();
	for (const pin of PINS) {
		const rendered = applicationStateLabel(pin.state);
		if (rendered !== pin.label) {
			failures.push(
				`application state '${pin.state}' renders "${rendered}", pinned as "${pin.label}" — ` +
					`the contract says: ${pin.contract}`,
			);
			continue;
		}
		const lowered = rendered.toLowerCase();
		for (const word of pin.mustNotContain) {
			if (lowered.includes(word.toLowerCase())) {
				failures.push(
					`application state '${pin.state}' renders "${rendered}", which claims "${word}" — ` +
						`the contract says: ${pin.contract}`,
				);
			}
		}
		for (const word of pin.mustContain) {
			if (!lowered.includes(word.toLowerCase())) {
				failures.push(
					`application state '${pin.state}' renders "${rendered}", which drops "${word}" — ` +
						`the contract says: ${pin.contract}`,
				);
			}
		}
		const already = seen.get(rendered);
		if (already !== undefined) {
			failures.push(`application states '${already}' and '${pin.state}' both render "${rendered}"`);
		}
		seen.set(rendered, pin.state);
	}

	return failures;
}
