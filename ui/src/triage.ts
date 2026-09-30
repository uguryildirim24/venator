import type { FilterDecision, PostingDetail, PostingEntry } from "../shared/contracts.ts";

export function latestHardFilter(detail: PostingDetail): FilterDecision | null {
	return detail.decisions.findLast((decision) => decision.stage === "hard_filter" && decision.latest) ?? null;
}

export function scoreHidden(decision: FilterDecision | null, probability: number | null): boolean {
	return decision?.verdict === "pass" && probability !== null && probability < 0.1;
}

/** A keep score can hide a Posting, but only a Hard Filter closes triage. */
export function triageAllowed(posting: PostingEntry | PostingDetail, decision: FilterDecision | null): boolean {
	return decision?.verdict !== "kill" && (posting.status !== "hard-killed" || scoreHidden(decision, posting.keepProbability));
}
