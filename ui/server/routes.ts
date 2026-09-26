/**
 * The read-only HTTP surface. Nothing here writes: the dashboard observes the pipeline,
 * and approve/reject stays disabled until a submission pipeline exists.
 */

import { Hono } from "hono";
import { cors } from "hono/cors";

import {
	APPLICATION_FILTERS,
	JEV_TRIAGE_DECISIONS,
	POSTING_STATUSES,
	type ApplicationFilter,
	type ErrorResponse,
	type JevTriageDecision,
	type JevTriageResponse,
	type PostingStatus,
	type PostingsResponse,
	type SummaryResponse,
} from "../shared/contracts.ts";
import { API_SERVER_PORT, APP_DEV_PORT } from "../shared/ports.ts";
import { readFromView, ViewSchemaError } from "./db.ts";
import { readOnboardingState } from "./onboarding/state.ts";
import { matchingLocationKeys, readLocationSelection } from "./location-filter.ts";
import {
	countJevTriageEntries,
	POSTING_SORTS,
	readDatabaseInfo,
	readFunnel,
	readJevTriageEntries,
	readPostingDetail,
	readPostingEntries,
	countPostingEntryTotals,
	readSourceHealth,
	type JevTriageQuery,
	type PostingQuery,
	type PostingSort,
} from "./queries.ts";
import { ViewDataError } from "./rows.ts";
import { ensureReadableView, ViewUpdateError } from "./view-recovery.ts";

const DEFAULT_LIMIT = 50;
const MAX_LIMIT = 1000;

/**
 * The API binds to loopback, but a browser page from anywhere could still reach it, so the
 * origins that may read the owner's Posting data are named explicitly. The tauri entries
 * are what a packaged desktop build will present once the dashboard is wrapped.
 */
const ALLOWED_ORIGINS: readonly string[] = [
	`http://localhost:${APP_DEV_PORT}`,
	`http://127.0.0.1:${APP_DEV_PORT}`,
	`http://localhost:${API_SERVER_PORT}`,
	`http://127.0.0.1:${API_SERVER_PORT}`,
	"tauri://localhost",
	"http://tauri.localhost",
];

class BadRequestError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "BadRequestError";
	}
}

function parseStatus(raw: string | undefined): PostingStatus | null {
	if (raw === undefined || raw === "") return null;
	const status = POSTING_STATUSES.find((candidate) => candidate === raw);
	if (status === undefined) {
		throw new BadRequestError(`Unknown status "${raw}"; expected ${POSTING_STATUSES.join(", ")}.`);
	}
	return status;
}

function parseApplication(raw: string | undefined): ApplicationFilter | null {
	if (raw === undefined || raw === "") return null;
	const application = APPLICATION_FILTERS.find((candidate) => candidate === raw);
	if (application === undefined) {
		throw new BadRequestError(`Unknown application filter "${raw}"; expected ${APPLICATION_FILTERS.join(", ")}.`);
	}
	return application;
}

function parseSort(raw: string | undefined, fallback: PostingSort): PostingSort {
	if (raw === undefined || raw === "") return fallback;
	const sort = POSTING_SORTS.find((candidate) => candidate === raw);
	if (sort === undefined) {
		throw new BadRequestError(`Unknown sort "${raw}"; expected ${POSTING_SORTS.join(", ")}.`);
	}
	return sort;
}

function parseJevDecision(raw: string | undefined): JevTriageDecision | null {
	if (raw === undefined || raw === "") return null;
	const decision = JEV_TRIAGE_DECISIONS.find((candidate) => candidate === raw);
	if (decision === undefined) {
		throw new BadRequestError(
			`Unknown Jev decision "${raw}"; expected ${JEV_TRIAGE_DECISIONS.join(", ")}.`,
		);
	}
	return decision;
}

function parseLimit(raw: string | undefined): number {
	if (raw === undefined || raw === "") return DEFAULT_LIMIT;
	const limit = /^\d+$/.test(raw) ? Number(raw) : NaN;
	if (!Number.isSafeInteger(limit) || limit < 1) {
		throw new BadRequestError(`Invalid limit "${raw}"; expected a positive integer.`);
	}
	return Math.min(limit, MAX_LIMIT);
}

function parseOffset(raw: string | undefined): number {
	if (raw === undefined || raw === "") return 0;
	const offset = /^\d+$/.test(raw) ? Number(raw) : NaN;
	if (!Number.isSafeInteger(offset) || offset < 0) {
		throw new BadRequestError(`Invalid offset "${raw}"; expected a nonnegative integer.`);
	}
	return offset;
}

function optionalParameter(raw: string | undefined): string | null {
	return raw === undefined || raw === "" ? null : raw;
}

function errorBody(message: string): ErrorResponse {
	return { error: message };
}

export function createApiRoutes(): Hono {
	const api = new Hono();

	api.use("*", cors({ origin: [...ALLOWED_ORIGINS], allowMethods: ["GET", "OPTIONS"] }));
	api.use("*", async (context, next) => {
		if (context.req.method === "GET") await ensureReadableView();
		await next();
	});

	/** Funnel plus kill groups: everything the Filter Inspector needs above the fold. */
	api.get("/summary", (context) => {
		const summary = readFromView((view): SummaryResponse => {
			return {
				funnel: readFunnel(view.database, matchingLocationKeys(view.database, readLocationSelection())),
				unfilteredDiscovered: readFunnel(view.database).discovered,
				database: readDatabaseInfo(view.database, view.path, view.kind),
				sources: readSourceHealth(view.database),
			};
		});
		return context.json(summary);
	});

	api.get("/postings", (context) => {
		const query: PostingQuery = {
			search: optionalParameter(context.req.query("q")),
			status: parseStatus(context.req.query("status")),
			application: parseApplication(context.req.query("application")),
			rule: optionalParameter(context.req.query("rule")),
			sort: parseSort(context.req.query("sort"), "discovered"),
			limit: parseLimit(context.req.query("limit")),
			offset: parseOffset(context.req.query("offset")),
		};
		const response = readFromView((view): PostingsResponse => {
			const filtered = { ...query, locationKeys: matchingLocationKeys(view.database, readLocationSelection()) };
			const counts = countPostingEntryTotals(view.database, filtered);
			const entries = counts.total === 0 ? [] : readPostingEntries(view.database, filtered);
			return {
				entries,
				total: counts.total,
				newCount: counts.newCount,
				limit: query.limit,
				offset: query.offset ?? 0,
			};
		});
		return context.json(response);
	});

	/**
	 * Whether this Install has a Profile yet, so the app can decide between onboarding and the
	 * dashboard. A read, and so it belongs here rather than on the onboarding write surface.
	 */
	api.get("/onboarding/state", (context) => {
		return context.json(readOnboardingState());
	});

	api.get("/postings/:key", (context) => {
		const key = context.req.param("key");
		const detail = readFromView((view) => readPostingDetail(view.database, key));
		if (detail === null) {
			return context.json(errorBody(`No Posting with key "${key}".`), 404);
		}
		return context.json(detail);
	});

	api.get("/jev-triage", (context) => {
		const query: JevTriageQuery = {
			decision: parseJevDecision(context.req.query("decision")),
			search: optionalParameter(context.req.query("q")),
			limit: parseLimit(context.req.query("limit")),
			offset: parseOffset(context.req.query("offset")),
		};
		const response = readFromView((view): JevTriageResponse => {
			const filtered = { ...query, locationKeys: matchingLocationKeys(view.database, readLocationSelection()) };
			return {
				entries: readJevTriageEntries(view.database, filtered),
				total: countJevTriageEntries(view.database, filtered),
				limit: query.limit,
				offset: query.offset,
			};
		});
		return context.json(response);
	});

	api.onError((error, context) => {
		if (error instanceof BadRequestError) {
			return context.json(errorBody(error.message), 400);
		}
		if (error instanceof ViewUpdateError || error instanceof ViewSchemaError || error instanceof ViewDataError) {
			return context.json(errorBody("Your job list could not be updated. Try Refresh again."), 503);
		}
		return context.json(errorBody(error.message), 500);
	});

	return api;
}
