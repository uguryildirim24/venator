import { existsSync } from "node:fs";
import { basename } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { emptyViewDatabase, ensureFixtureDatabase } from "../fixtures/make-fixture.ts";
import {
	defaultViewDatabase,
	FIXTURE_DATABASE_PATH,
	sampleDataRequested,
	systemContext,
	viewDatabaseCandidates,
	viewDatabaseWasConfigured,
	type LocationContext,
} from "./locations.ts";

/**
 * Which of the three things the dashboard can be reading.
 *
 * `empty` is the state of an Install that has never run anything: the contract's schema and
 * no rows. It is not an error and it is not a fixture — see `resolveView` for why it exists
 * and what it replaced.
 */
export type DatabaseKind = "pipeline" | "fixture" | "empty";

export type ViewDatabase = {
	readonly database: DatabaseSync;
	readonly path: string;
	readonly kind: DatabaseKind;
};

/** Which view this Install would open, decided before anything is opened. */
export type ViewSource = {
	/**
	 * The file this view is, or — when the kind is `empty` — the file it would be. A path is
	 * always somewhere real for the person reading it, so the header and the onboarding state
	 * can name where a view will appear rather than going silent.
	 */
	readonly path: string;
	readonly kind: DatabaseKind;
};

/** Thrown when the opened database does not carry the tables coordination/CONTRACTS.md defines. */
export class ViewSchemaError extends Error {
	constructor(message: string, options?: ErrorOptions) {
		super(message, options);
		this.name = "ViewSchemaError";
	}
}

const POSTING_COLUMNS =
	"key, source, board, company, title, location, url, posted_at, discovered_at, description_html";

const DECISION_COLUMNS =
	"id, posting_key, stage, verdict, rule, score, reason, filters_version, decided_at";

const APPLICATION_STATE_COLUMNS = "posting_key, state, detail, since";

const TRACK_EVENT_COLUMNS = "id, posting_key, event, actor, detail, at";

const RUN_COLUMNS = "id, at, status, stage, pause_reason, waiting";

/**
 * Prepares one statement per contract table. Preparing resolves every column name against
 * the live schema without reading a row, so a view built to a different shape fails here —
 * at open, naming the mismatch — rather than halfway through rendering.
 */
function assertContractSchema(database: DatabaseSync, path: string): void {
	try {
		database.prepare(`SELECT ${POSTING_COLUMNS} FROM postings LIMIT 0`);
		database.prepare(`SELECT ${DECISION_COLUMNS} FROM decisions LIMIT 0`);
		// Lists join this derived latest-id table; a view built before it needs rebuilding.
		database.prepare("SELECT posting_key, decision_id FROM hard_filter_latest LIMIT 0");
		database.prepare(`SELECT ${APPLICATION_STATE_COLUMNS} FROM application_states LIMIT 0`);
		database.prepare(`SELECT ${TRACK_EVENT_COLUMNS} FROM track_events LIMIT 0`);
		database.prepare(`SELECT ${RUN_COLUMNS} FROM runs LIMIT 0`);
		database.prepare("SELECT posting_key, status, summary, evidence, conflicts, unknowns, listing_status, last_verified_at, description_kind, apply_url, opportunity_type, input_version, assessed_by, assessed_as_of FROM assessments LIMIT 0");
		database.prepare("SELECT mode FROM jev_selection LIMIT 0");
		database.prepare("SELECT posting_key, reason FROM jev_skip LIMIT 0");
		// Coverage was added after the first view release. The base health fields remain
		// required, while readSourceHealth reports coverage as unmeasured for older views.
		database.prepare("SELECT source_key, status, last_attempt_at, last_success_at, count, message FROM source_health LIMIT 0");
	} catch (cause) {
		throw new ViewSchemaError(
			`${path} does not match the view schema in coordination/CONTRACTS.md. ` +
				`Rebuild it with \`python -m venator.view.build\`, or delete it to start from an empty view.`,
			{ cause },
		);
	}
}

/**
 * Fixture data must never be badged as the real pipeline view, including when something
 * passes a copy of it by path, so sample data is recognised by the file's name.
 */
function isSampleView(path: string): boolean {
	return basename(path) === basename(FIXTURE_DATABASE_PATH);
}

/**
 * The view to open, or null when this Install has none yet.
 *
 * `locations.ts` owns the order — an explicit `$VENATOR_VIEW_DB` first, then the
 * application data directory, then a checkout. An explicit path is honoured whether or not
 * it exists, because a caller that named one wants the error rather than a substitute.
 */
function namedOrFoundView(context: LocationContext): string | null {
	const candidates = viewDatabaseCandidates(context);
	if (viewDatabaseWasConfigured(context)) return candidates[0] ?? null;
	return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

/**
 * What this Install is entitled to read, and the one place that is decided.
 *
 * The fixture is nineteen Postings from real employers and none of them is the Owner's. It
 * used to be the automatic fallback for any Install with no view of its own, which meant the
 * first thing somebody saw after installing the app was a full review queue of a stranger's
 * job search, with one badge in the header to say so. A badge is not containment: the
 * Postings were on screen, in the lists, in Focus, and on the wire to anything that could
 * reach the loopback API.
 *
 * So sample data is served **only when this run asked for it** (`$VENATOR_SAMPLE_DATA`), and
 * asking is a thing only the developer tools do. The check is on the data rather than on the
 * route it arrived by: a fixture named outright in `$VENATOR_VIEW_DB` is refused by the same
 * line, because otherwise any host that fills that variable in on somebody's behalf launders
 * the sample corpus straight past the gate — which is exactly what the desktop host did.
 *
 * What replaces it is not an error and not a refusal to answer. An Install with no view gets
 * a view with the schema and nothing in it, so every route renders the truth — nothing
 * discovered, nothing filtered, nothing queued — and the queue shows the screen written for
 * it (`src/views/first-run.tsx`). Refusing to open anything would have produced the
 * dashboard's red "could not read the view database" banner instead, which describes a
 * failure, and nothing here has failed.
 *
 * This is the server's decision rather than the app's because provenance is something only
 * the server knows: the browser cannot tell a fixture somebody asked for from one it fell
 * into, and a rule the app enforced would be a rule every future view has to remember while
 * the data sat on the wire regardless.
 */
export function resolveView(context: LocationContext = systemContext()): ViewSource {
	const sample = sampleDataRequested(context);
	const named = namedOrFoundView(context);
	if (named === null) {
		if (sample) return { path: FIXTURE_DATABASE_PATH, kind: "fixture" };
		return { path: defaultViewDatabase(context), kind: "empty" };
	}
	if (!isSampleView(named)) return { path: named, kind: "pipeline" };
	return { path: named, kind: sample ? "fixture" : "empty" };
}

/**
 * Opens whatever `resolveView` says this Install may read, read-only.
 *
 * The handle is per-call by design: the view is disposable and gets rebuilt underneath us,
 * so a long-lived handle would keep serving a deleted file. Opening SQLite costs
 * microseconds, and it means the dashboard switches from an empty view to real data the
 * moment the match engine writes one — no restart.
 */
export function openViewDatabase(): ViewDatabase {
	const { path, kind } = resolveView();
	// An empty view is built, not opened: there is no file, and there is deliberately not
	// going to be one. It still goes through the schema check below, because what it answers
	// with has to satisfy the same contract every query is written against.
	if (kind === "empty") {
		const database = emptyViewDatabase();
		try {
			assertContractSchema(database, path);
		} catch (error) {
			database.close();
			throw error;
		}
		return { database, path, kind };
	}
	if (kind === "fixture" && path === FIXTURE_DATABASE_PATH) ensureFixtureDatabase(FIXTURE_DATABASE_PATH);
	const database = new DatabaseSync(path, { readOnly: true });
	try {
		assertContractSchema(database, path);
	} catch (error) {
		database.close();
		throw error;
	}
	return { database, path, kind };
}

/** Runs one read against a freshly opened view and always closes it. */
export function readFromView<Result>(read: (view: ViewDatabase) => Result): Result {
	const view = openViewDatabase();
	try {
		return read(view);
	} finally {
		view.database.close();
	}
}
