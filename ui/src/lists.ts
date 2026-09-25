/**
 * What each list asks the API for. Every list is ordered the one way the queue is: most
 * recently verified first (ui/DESIGN.md, "List"). No Jev number and no score orders anything.
 */

import type { JevTriageDecision, PostingEntry } from "../shared/contracts.ts";
import { PAGE_SIZE, type HomeList, type InspectorFilters } from "./router.ts";

/** The home list's query: its status or application state, the search, and the page. */
export function homeListQuery(list: HomeList, search: string, offset = 0, limit = PAGE_SIZE): string {
	const parameters = new URLSearchParams();
	if (list === "queued") parameters.set("status", "queued");
	if (list === "needs-review") parameters.set("status", "needs-review");
	if (list === "filtered") parameters.set("status", "hard-killed");
	if (list === "unscored") parameters.set("status", "unscored");
	if (list === "applied") {
		parameters.set("status", "applied");
		parameters.set("application", "active");
	}
	if (list === "saved") parameters.set("application", "saved");
	if (list === "dismissed") parameters.set("application", "dismissed");
	parameters.set("sort", "verified");
	if (search !== "") parameters.set("q", search);
	parameters.set("limit", String(limit));
	parameters.set("offset", String(offset));
	return `?${parameters.toString()}`;
}

export function triageListQuery(decision: JevTriageDecision | null, search: string, offset = 0, limit = PAGE_SIZE): string {
	const parameters = new URLSearchParams();
	if (decision !== null) parameters.set("decision", decision);
	if (search !== "") parameters.set("q", search);
	parameters.set("limit", String(limit));
	parameters.set("offset", String(offset));
	return `?${parameters.toString()}`;
}

export function inspectorQuery(filters: InspectorFilters, offset = 0, limit = PAGE_SIZE): string {
	const parameters = new URLSearchParams();
	if (filters.status !== null) parameters.set("status", filters.status);
	if (filters.rule !== null) parameters.set("rule", filters.rule);
	if (filters.search !== "") parameters.set("q", filters.search);
	parameters.set("sort", "verified");
	parameters.set("offset", String(offset));
	parameters.set("limit", String(limit));
	return `?${parameters.toString()}`;
}

/** The home list a Posting belongs to, so a Posting opened from a table sits in its own list. */
export function homeListOf(entry: PostingEntry): HomeList {
	if (entry.status === "hard-killed") return "filtered";
	if (entry.status === "unscored") return "unscored";
	if (entry.status === "needs-review") return "needs-review";
	if (entry.status === "queued") return "queued";
	if (entry.status === "applied") {
		if (entry.application?.state === "approved") return "saved";
		if (entry.application?.state === "rejected") return "dismissed";
		return "applied";
	}
	return "filtered";
}
