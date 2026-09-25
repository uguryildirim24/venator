/**
 * Hash routing, deliberately.
 *
 * Every view is addressable (`#/inspector?rule=education_fit` is a link the Owner can keep), and
 * hash URLs need no server rewrite rule — they resolve identically from the Vite dev server,
 * from a bundle served by the Hono process, and from a desktop webview loading dist/ off disk.
 */

import { useMemo, useSyncExternalStore } from "react";

import { POSTING_STATUSES, JEV_TRIAGE_DECISIONS, type JevTriageDecision, type PostingStatus } from "../shared/contracts.ts";

/**
 * The lists the sidebar switches between.
 *
 * `saved` and `dismissed` are the `applied` status narrowed by application state (`approved`
 * and `rejected`), and `applied` is what is left of it: the three are disjoint.
 */
export type HomeList = "queued" | "needs-review" | "unscored" | "applied" | "saved" | "dismissed" | "filtered";

export const HOME_LISTS: readonly HomeList[] = ["queued", "needs-review", "unscored", "applied", "saved", "dismissed", "filtered"];

/** Where Focus was opened from, including the separately named Early review view. */
export type FocusFrom = HomeList | "triage";

export type InspectorFilters = {
	readonly status: PostingStatus | null;
	readonly rule: string | null;
	readonly search: string;
};

/**
 * The three steps of setting up a Profile, in order.
 *
 * They are in the hash rather than in a component's state so that the step someone is on is a
 * place they can be sent back to, a browser Back button works through the flow, and every
 * screen is renderable on its own by `pnpm smoke`. The flow component stays mounted across
 * all three, so moving between them keeps the draft.
 */
export type OnboardingStep = "runtime" | "resume" | "targeting";

export const ONBOARDING_STEPS: readonly OnboardingStep[] = ["runtime", "resume", "targeting"];

export type Route =
	| { readonly name: "queue"; readonly list: HomeList; readonly search: string; readonly page?: number }
	| { readonly name: "inspector"; readonly filters: InspectorFilters; readonly page?: number }
	| {
			readonly name: "posting";
			readonly key: string;
			/** Which home list Focus was entered from, so the arrows walk that list. */
			readonly from: FocusFrom;
			readonly returnTo?: string | undefined;
	  }
	| { readonly name: "onboarding"; readonly step: OnboardingStep }
	| { readonly name: "employers" }
	| {
			readonly name: "triage";
			readonly decision: JevTriageDecision | null;
			readonly search: string;
			readonly page?: number;
	  };

export const EMPTY_FILTERS: InspectorFilters = { status: null, rule: null, search: "" };

const POSTING_PREFIX = "/postings/";

function parseFilters(search: string): InspectorFilters {
	const parameters = new URLSearchParams(search);
	const status = parameters.get("status");
	const rule = parameters.get("rule");
	return {
		status: POSTING_STATUSES.find((candidate) => candidate === status) ?? null,
		rule: rule === null || rule === "" ? null : rule,
		search: parameters.get("q") ?? "",
	};
}

function parseHomeList(raw: string | null): HomeList {
	return HOME_LISTS.find((candidate) => candidate === raw) ?? "queued";
}

export const PAGE_SIZE = 50;

function parsePage(parameters: URLSearchParams): number {
	const raw = parameters.get("page") ?? "0";
	const page = /^\d+$/.test(raw) ? Number(raw) : 0;
	return Number.isSafeInteger(page) && page <= Math.floor(Number.MAX_SAFE_INTEGER / PAGE_SIZE) ? page : 0;
}

function parseFocusFrom(raw: string | null): FocusFrom {
	if (raw === "triage") return "triage";
	return parseHomeList(raw);
}

export function parseRoute(hash: string): Route {
	const raw = hash.startsWith("#") ? hash.slice(1) : hash;
	const separator = raw.indexOf("?");
	const path = separator === -1 ? raw : raw.slice(0, separator);
	const search = separator === -1 ? "" : raw.slice(separator + 1);

	if (path.startsWith(POSTING_PREFIX)) {
		const parameters = new URLSearchParams(search);
		return {
			name: "posting",
			key: decodeURIComponent(path.slice(POSTING_PREFIX.length)),
			from: parseFocusFrom(parameters.get("from")),
			returnTo: parameters.get("returnTo") ?? undefined,
		};
	}
	if (path.startsWith("/onboarding")) {
		const parameters = new URLSearchParams(search);
		const step = parameters.get("step");
		return { name: "onboarding", step: ONBOARDING_STEPS.find((candidate) => candidate === step) ?? "runtime" };
	}
	if (path === "/employers") return { name: "employers" };
	if (path.startsWith("/inspector")) {
		return { name: "inspector", filters: parseFilters(search), page: parsePage(new URLSearchParams(search)) };
	}
	if (path === "/triage" || path.startsWith("/triage/")) {
		const parameters = new URLSearchParams(search);
		const decisionRaw = parameters.get("decision");
		const decision = JEV_TRIAGE_DECISIONS.find((candidate) => candidate === decisionRaw) ?? null;
		return {
			name: "triage",
			decision,
			search: parameters.get("q") ?? "",
			page: parsePage(parameters),
		};
	}
	const parameters = new URLSearchParams(search);
	return { name: "queue", list: parseHomeList(parameters.get("list")), search: parameters.get("q") ?? "", page: parsePage(parameters) };
}

export function queueHash(list: HomeList = "queued", search = "", page = 0): string {
	const parameters = new URLSearchParams();
	if (list !== "queued") parameters.set("list", list);
	if (search !== "") parameters.set("q", search);
	if (page > 0) parameters.set("page", String(page));
	const query = parameters.toString();
	return query === "" ? "#/queue" : `#/queue?${query}`;
}

export function inspectorHash(filters: InspectorFilters, page = 0): string {
	const parameters = new URLSearchParams();
	if (filters.status !== null) parameters.set("status", filters.status);
	if (filters.rule !== null) parameters.set("rule", filters.rule);
	if (filters.search !== "") parameters.set("q", filters.search);
	if (page > 0) parameters.set("page", String(page));
	const query = parameters.toString();
	return query === "" ? "#/inspector" : `#/inspector?${query}`;
}

export function employersHash(): string {
	return "#/employers";
}

export function onboardingHash(step: OnboardingStep = "runtime"): string {
	return step === "runtime" ? "#/onboarding" : `#/onboarding?step=${step}`;
}

export function postingHash(key: string, from: FocusFrom = "queued", returnTo?: string): string {
	const parameters = new URLSearchParams();
	if (from !== "queued") parameters.set("from", from);
	if (returnTo !== undefined) parameters.set("returnTo", returnTo);
	const query = parameters.toString();
	return `#${POSTING_PREFIX}${encodeURIComponent(key)}${query === "" ? "" : `?${query}`}`;
}

export function postingReturnHash(from: FocusFrom, returnTo?: string): string {
	if (returnTo === undefined) return from === "triage" ? triageHash() : queueHash(from);
	const route = parseRoute(returnTo);
	if (route.name === "queue") return queueHash(route.list, route.search, route.page);
	if (route.name === "inspector") return inspectorHash(route.filters, route.page);
	if (route.name === "triage") return triageHash(route.decision, route.search, route.page);
	return from === "triage" ? triageHash() : queueHash(from);
}

export function triageHash(
	decision: JevTriageDecision | null = null,
	search = "",
	page = 0,
): string {
	const parameters = new URLSearchParams();
	if (decision !== null) parameters.set("decision", decision);
	if (search !== "") parameters.set("q", search);
	if (page > 0) parameters.set("page", String(page));
	const query = parameters.toString();
	return query === "" ? "#/triage" : `#/triage?${query}`;
}

export function navigate(hash: string): void {
	window.location.hash = hash;
}

/** Replaces the entry so typing in the search box does not fill the back stack. */
export function replaceRoute(hash: string): void {
	window.history.replaceState(null, "", hash);
	window.dispatchEvent(new HashChangeEvent("hashchange"));
}

export function goBack(): void {
	window.history.back();
}

function subscribe(onChange: () => void): () => void {
	window.addEventListener("hashchange", onChange);
	return () => window.removeEventListener("hashchange", onChange);
}

function currentHash(): string {
	return window.location.hash;
}

export function useRoute(): Route {
	const hash = useSyncExternalStore(subscribe, currentHash, currentHash);
	return useMemo(() => parseRoute(hash), [hash]);
}
