/**
 * Every read the dashboard makes, expressed as SQL against the contract view.
 *
 * Effective state is the latest Filter Decision per Posting and stage. The view materializes
 * latest Hard Filter ids once after replay; the detail timeline still reads every decision
 * and identifies the latest by timestamp and id.
 */

import type { DatabaseSync } from "node:sqlite";

import {
	APPLICATION_STATE_NAMES,
	POSTING_STATUSES,
	type ApplicationFilter,
	type ApplicationState,
	type DatabaseInfo,
	type DecisionStage,
	type DecisionVerdict,
	type FilterDecision,
	type Funnel,
	type Posting,
	type PostingDetail,
	type PostingEntry,
	type PostingStatus,
	type SourceHealth,
	type TrackEvent,
} from "../shared/contracts.ts";
import {
	booleanColumn,
	integerColumn,
	memberColumn,
	optionalIntegerColumn,
	optionalNumberColumn,
	optionalTextColumn,
	textColumn,
	type SqlRow,
} from "./rows.ts";
import { decodeAssessment } from "./assessment.ts";
import { readerBlocks } from "./reader.ts";
import { asMapping, asText, parseJson } from "./onboarding/json.ts";

const STAGES: readonly DecisionStage[] = ["hard_filter", "llm_score"];
const VERDICTS: readonly DecisionVerdict[] = ["pass", "kill", "queue"];

export type PostingSort = "verified" | "discovered" | "title";

export const POSTING_SORTS: readonly PostingSort[] = ["verified", "discovered", "title"];

export type PostingQuery = {
	readonly search: string | null;
	readonly locationKeys?: string | null;
	readonly status: PostingStatus | null;
	/** Narrows to Saved, Dismissed, or the rest of the Postings with application progress. */
	readonly application?: ApplicationFilter | null;
	readonly rule: string | null;
	readonly sort: PostingSort;
	readonly limit: number;
	readonly offset?: number;
};

/**
 * The latest completed fetch, and the one before it, from the `runs` heartbeats.
 *
 * A Discover heartbeat is appended when the stage finishes, so a Posting discovered after the
 * previous completed fetch and no later than the latest one was discovered in the latest
 * completed fetch. That is the whole of "new": it is read from data the pipeline already
 * writes, and nothing records who has seen what.
 */
const FETCH_WINDOW = `
fetches AS (
  SELECT julianday(at) AS finished FROM runs
  WHERE stage = 'discover' AND status = 'ok' AND julianday(at) IS NOT NULL
  ORDER BY finished DESC LIMIT 2
),
fetch_window AS (
  SELECT (SELECT max(finished) FROM fetches) AS latest,
         (SELECT CASE WHEN count(*) = 2 THEN min(finished) END FROM fetches) AS previous
)`;

const IS_NEW = `CASE WHEN fw.latest IS NOT NULL
      AND julianday(p.discovered_at) <= fw.latest
      AND (fw.previous IS NULL OR julianday(p.discovered_at) > fw.previous)
    THEN 1 ELSE 0 END`;

const APP_PROGRESS = "aps.state IN ('approved', 'rejected', 'prepared', 'filled', 'submitted', 'concluded', 'withdrawn')";
/** Live dashboard standing; neither résumé evidence nor a historical Match Score routes a Posting. */
const STATUS_SQL = `
    CASE
      WHEN ${APP_PROGRESS} THEN 'applied'
      WHEN hf.verdict = 'kill' THEN 'hard-killed'
      WHEN a.listing_status = 'closed' THEN 'closed'
      WHEN hf.verdict = 'pass' AND k.probability >= 0.5 THEN 'queued'
      WHEN hf.verdict = 'pass' AND k.probability < 0.1 THEN 'hard-killed'
      WHEN hf.verdict = 'pass' AND k.probability IS NOT NULL THEN 'needs-review'
      WHEN hf.verdict = 'pass' AND k.posting_key IS NOT NULL THEN 'unscored'
      ELSE 'not-filtered'
    END`;

function postingEntries(): string {
	return `WITH ${FETCH_WINDOW},
entries AS (
  SELECT
    p.key AS key, p.source AS source, p.board AS board, p.company AS company,
    p.title AS title,
    p.location AS location, p.url AS url, p.posted_at AS posted_at,
    p.discovered_at AS discovered_at,
    hf.id AS hf_id, hf.verdict AS hf_verdict, hf.rule AS hf_rule,
    hf.reason AS hf_reason, hf.filters_version AS hf_filters_version,
    hf.decided_at AS hf_decided_at,
    k.probability AS keep_probability, k.carried AS keep_score_carried,
    aps.state AS app_state, aps.detail AS app_detail, aps.since AS app_since,
    coalesce(a.status, 'unassessed') AS assessment_status,
    coalesce(a.summary, 'Refresh jobs and check eligibility to assess this listing.') AS assessment_summary,
    a.evidence, a.conflicts, a.unknowns,
    coalesce(a.assessed_by, 'deterministic') AS assessed_by, a.assessed_as_of,
    coalesce(a.listing_status, 'unknown') AS listing_status, a.last_verified_at,
    coalesce(a.description_kind, 'missing') AS description_kind,
    coalesce(a.apply_url, p.url) AS apply_url,
    coalesce(a.opportunity_type, 'unknown') AS opportunity_type,
    ${STATUS_SQL} AS status,
    ${IS_NEW} AS is_new
  FROM postings p
  CROSS JOIN fetch_window fw
  LEFT JOIN hard_filter_latest hl ON hl.posting_key = p.key
  LEFT JOIN decisions hf ON hf.id = hl.decision_id
  LEFT JOIN application_states aps ON aps.posting_key = p.key
  LEFT JOIN assessments a ON a.posting_key = p.key
  LEFT JOIN keep_scores k ON k.posting_key = p.key
)`;
}

/** Search spans the fields an owner would recall: what it was, where, and why it was decided. */
const ENTRY_FILTER = `
  WHERE ($locationKeys IS NULL OR key IN (SELECT value FROM json_each($locationKeys)))
    AND ($status IS NULL OR status = $status)
    AND ($application IS NULL
      OR ($application = 'saved' AND app_state = 'approved')
      OR ($application = 'dismissed' AND app_state = 'rejected')
      OR ($application = 'active' AND status = 'applied' AND app_state NOT IN ('approved', 'rejected')))
    AND ($rule IS NULL OR (hf_verdict = 'kill' AND hf_rule = $rule))
    AND ($pattern IS NULL OR (
      lower(title) LIKE $pattern
      OR lower(coalesce(company, '')) LIKE $pattern
      OR lower(board) LIKE $pattern
      OR lower(coalesce(location, '')) LIKE $pattern
      OR lower(key) LIKE $pattern
      OR lower(coalesce(hf_reason, '')) LIKE $pattern
      OR lower(assessment_summary) LIKE $pattern
    ))`;

// Presentation grouping, not identity dedup: different requisition links remain intact.
// Match dedup's case/spacing/edge-punctuation title normalization without merging sites.
const GROUPED = `, filtered AS (SELECT * FROM entries ${ENTRY_FILTER}),
identified AS (SELECT *, CASE WHEN trim(coalesce(company, '')) = '' OR trim(title) = '' OR trim(coalesce(location, '')) = ''
    THEN key ELSE lower(trim(company)) || char(31) || lower(trim(title, ' -–—,.;:/\\|·•*&_+()[]{}''"')) || char(31) || lower(trim(location)) END AS group_id FROM filtered),
ranked AS (SELECT *, row_number() OVER (PARTITION BY group_id
  ORDER BY keep_probability DESC NULLS LAST, last_verified_at DESC, key ASC) AS group_rank,
  count(*) OVER (PARTITION BY group_id) AS group_count,
  max(is_new) OVER (PARTITION BY group_id) AS group_is_new FROM identified),
heads AS (SELECT * FROM ranked WHERE group_rank = 1)`;

function orderClause(sort: PostingSort): string {
	// Most recently verified first. A historical Match Score never orders the board.
	if (sort === "verified") return "last_verified_at DESC, discovered_at DESC, key ASC";
	if (sort === "title") return "title ASC, board ASC, key ASC";
	return "discovered_at DESC, key ASC";
}

function likePattern(search: string | null): string | null {
	const trimmed = search?.trim() ?? "";
	if (trimmed === "") return null;
	const escaped = trimmed.toLowerCase().replaceAll("%", "\\%").replaceAll("_", "\\_");
	return `%${escaped}%`;
}

function decodePosting(row: SqlRow): Posting {
	return {
		key: textColumn(row, "key"),
		source: textColumn(row, "source"),
		board: textColumn(row, "board"),
		company: optionalTextColumn(row, "company"),
		title: textColumn(row, "title"),
		location: optionalTextColumn(row, "location"),
		url: textColumn(row, "url"),
		postedAt: optionalTextColumn(row, "posted_at"),
		discoveredAt: textColumn(row, "discovered_at"),
	};
}

/** Reads one stage's latest decision out of a joined entry row, or null if the stage never ran. */
function decodeEntryDecision(
	row: SqlRow,
	prefix: string,
	stage: DecisionStage,
): FilterDecision | null {
	const id = optionalIntegerColumn(row, `${prefix}_id`);
	if (id === null) return null;
	return {
		id,
		postingKey: textColumn(row, "key"),
		stage,
		verdict: memberColumn(row, `${prefix}_verdict`, VERDICTS),
		rule: optionalTextColumn(row, `${prefix}_rule`),
		reason: optionalTextColumn(row, `${prefix}_reason`),
		filtersVersion: optionalTextColumn(row, `${prefix}_filters_version`),
		decidedAt: optionalTextColumn(row, `${prefix}_decided_at`),
		latest: true,
	};
}

/** The Track stage's lifecycle row, when one exists. An empty detail reads as no detail. */
function decodeApplication(row: SqlRow): ApplicationState | null {
	const state = optionalTextColumn(row, "app_state");
	if (state === null) return null;
	const detail = optionalTextColumn(row, "app_detail");
	return {
		state: memberColumn(row, "app_state", APPLICATION_STATE_NAMES),
		detail: detail === null || detail.trim() === "" ? null : detail,
		since: optionalTextColumn(row, "app_since"),
	};
}

function decodeEntry(row: SqlRow): PostingEntry {
	return {
		posting: decodePosting(row),
		status: memberColumn(row, "status", POSTING_STATUSES),
		hardFilter: decodeEntryDecision(row, "hf", "hard_filter"),
		keepProbability: optionalNumberColumn(row, "keep_probability"),
		keepScoreCarried: optionalIntegerColumn(row, "keep_score_carried") === 1,
		application: decodeApplication(row),
		assessment: decodeAssessment(row),
		isNew: booleanColumn(row, "is_new"),
	};
}

function decodeDecision(row: SqlRow): FilterDecision {
	return {
		id: integerColumn(row, "id"),
		postingKey: textColumn(row, "posting_key"),
		stage: memberColumn(row, "stage", STAGES),
		verdict: memberColumn(row, "verdict", VERDICTS),
		rule: optionalTextColumn(row, "rule"),
		reason: optionalTextColumn(row, "reason"),
		filtersVersion: optionalTextColumn(row, "filters_version"),
		decidedAt: optionalTextColumn(row, "decided_at"),
		latest: booleanColumn(row, "is_latest"),
	};
}

function filterParameters(query: PostingQuery) {
	return {
		status: query.status,
		locationKeys: query.locationKeys ?? null,
		application: query.application ?? null,
		rule: query.rule,
		pattern: likePattern(query.search),
	};
}

export function readPostingEntries(
	database: DatabaseSync,
	query: PostingQuery,
): readonly PostingEntry[] {
	// Sort only keys: pulling evidence JSON into the sort costs a full-corpus read.
	const keepOrder = query.status === "queued" || query.status === "needs-review" ? "keep_probability DESC, " : "";
	const rows = database.prepare(
		`${postingEntries()} ${GROUPED} SELECT key, group_is_new FROM heads ORDER BY ${keepOrder}${orderClause(query.sort)} LIMIT $limit OFFSET $offset`,
	).all({ ...filterParameters(query), limit: query.limit, offset: query.offset ?? 0 });
	const keys = rows.map((row) => textColumn(row, "key"));
	if (keys.length === 0) return [];
	// One batched read rather than one query per Posting. Restore the requested sort order.
	const placeholders = keys.map(() => "?").join(",");
	const entries = database.prepare(`${postingEntries()} SELECT * FROM entries WHERE key IN (${placeholders})`)
		.all(...keys).map(decodeEntry);
	const byKey = new Map(entries.map((entry) => [entry.posting.key, entry]));
	const groupedKeys = database.prepare(`${postingEntries()} ${GROUPED}
		SELECT h.key AS head, r.key AS member FROM heads h JOIN ranked r ON h.group_id = r.group_id
		WHERE h.key IN (${placeholders}) ORDER BY r.keep_probability DESC NULLS LAST, r.key ASC`)
		.all({ ...filterParameters(query) }, ...keys);
	const members = new Map<string, string[]>();
	for (const row of groupedKeys) {
		const head = textColumn(row, "head");
		members.set(head, [...(members.get(head) ?? []), textColumn(row, "member")]);
	}
	return keys.map((key, index) => ({ ...byKey.get(key)!, isNew: integerColumn(rows[index]!, "group_is_new") !== 0, groupKeys: members.get(key) ?? [key] }));
}

export function countPostingEntryTotals(database: DatabaseSync, query: PostingQuery): { total: number; newCount: number } {
	const row = database.prepare(
		`${postingEntries()} ${GROUPED} SELECT COUNT(*) AS total, coalesce(SUM(group_is_new), 0) AS new_count FROM heads`,
	).get(filterParameters(query));
	return row === undefined ? { total: 0, newCount: 0 } : {
		total: integerColumn(row, "total"),
		newCount: integerColumn(row, "new_count"),
	};
}

export function countPostingEntries(database: DatabaseSync, query: PostingQuery): number {
	return countPostingEntryTotals(database, query).total;
}

function decodeEntitiesOnce(value: string): string {
	return value
		.replaceAll("&lt;", "<")
		.replaceAll("&gt;", ">")
		.replaceAll("&quot;", '"')
		.replaceAll("&#39;", "'")
		.replaceAll("&#x27;", "'")
		.replaceAll("&nbsp;", " ")
		.replaceAll("&amp;", "&");
}

function escapeText(value: string): string {
	return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

/**
 * The view stores descriptions however the board delivered them: Greenhouse rows arrive
 * entity-escaped (sometimes twice), Adzuna rows are plain text. The dashboard's sandbox
 * frame expects HTML, so both shapes are normalised here — escaped markup is decoded until
 * real tags appear, and plain text becomes paragraphs.
 */
export function normalizeDescription(raw: string | null): string {
	if (raw === null) return "";
	let html = raw.trim();
	for (let round = 0; round < 3 && !html.includes("<") && /&(lt|amp|gt|quot|#\d+);/.test(html); round += 1) {
		html = decodeEntitiesOnce(html);
	}
	if (html === "") return "";
	if (!html.includes("<")) {
		return html
			.split(/\n{2,}/)
			.map((paragraph) => `<p>${escapeText(paragraph).replaceAll("\n", "<br>")}</p>`)
			.join("");
	}
	return html;
}

function trackDetail(row: SqlRow): string | null {
	const detail = optionalTextColumn(row, "detail");
	if (detail === null || !["outreach", "outreach_undo"].includes(textColumn(row, "event"))) return detail;
	try { return asText(asMapping(parseJson(detail))?.person); }
	catch { return detail; }
}

function readTrackEvents(database: DatabaseSync, key: string): readonly TrackEvent[] {
	return database
		.prepare("SELECT id, event, actor, detail, at FROM track_events WHERE posting_key = $key ORDER BY at ASC, id ASC")
		.all({ key })
		.map((row) => ({
			id: integerColumn(row, "id"),
			event: textColumn(row, "event"),
			actor: optionalTextColumn(row, "actor"),
			detail: trackDetail(row),
			at: optionalTextColumn(row, "at"),
		}));
}

export function readPostingDetail(database: DatabaseSync, key: string): PostingDetail | null {
	const entryRow = database
		.prepare(`${postingEntries()} SELECT * FROM entries WHERE key = $key`)
		.get({ key });
	if (entryRow === undefined) return null;

	const descriptionRow = database
		.prepare("SELECT description_html FROM postings WHERE key = $key")
		.get({ key });
	const descriptionHtml =
		descriptionRow === undefined
			? null
			: optionalTextColumn(descriptionRow, "description_html");

	const decisions = database
		.prepare(
			`SELECT d.id AS id, d.posting_key AS posting_key, d.stage AS stage, d.verdict AS verdict,
			   d.rule AS rule, d.reason AS reason,
			   d.filters_version AS filters_version, d.decided_at AS decided_at,
			   CASE WHEN d.id = (SELECT recent.id FROM decisions recent
                 WHERE recent.posting_key = d.posting_key AND recent.stage = d.stage
                 ORDER BY recent.decided_at DESC, recent.id DESC LIMIT 1)
               THEN 1 ELSE 0 END AS is_latest
			 FROM decisions d
			 WHERE d.posting_key = $key
			 ORDER BY d.decided_at ASC, d.id ASC`,
		)
		.all({ key })
		.map(decodeDecision);

	const html = normalizeDescription(descriptionHtml);
	const detail: PostingDetail = {
		posting: decodePosting(entryRow),
		status: memberColumn(entryRow, "status", POSTING_STATUSES),
		keepProbability: optionalNumberColumn(entryRow, "keep_probability"),
		keepScoreCarried: optionalIntegerColumn(entryRow, "keep_score_carried") === 1,
		descriptionHtml: html,
		page: readerBlocks(html),
		decisions,
		trackEvents: readTrackEvents(database, key),
		application: decodeApplication(entryRow),
		assessment: decodeAssessment(entryRow),
	};
	return detail;
}

export function readSourceHealth(database: DatabaseSync): readonly SourceHealth[] {
	const columns = database.prepare("PRAGMA table_info(source_health)").all().map((row) => textColumn(row, "name"));
	const hasCoverage = ["known_jobs", "full_verified_details", "needs_detail_check"].every((name) => columns.includes(name));
	return database.prepare(`SELECT h.*, (SELECT p.company FROM postings p
		WHERE p.source != 'adzuna' AND p.source = substr(h.source_key, 1, instr(h.source_key, ':') - 1)
          AND p.board = substr(h.source_key, instr(h.source_key, ':') + 1)
          AND p.company IS NOT NULL LIMIT 1) AS employer
		FROM source_health h ORDER BY source_key`).all().map((row) => ({
		key: textColumn(row, "source_key"),
		employer: optionalTextColumn(row, "employer"),
		status: textColumn(row, "status"),
		lastAttemptAt: optionalTextColumn(row, "last_attempt_at"),
		lastSuccessAt: optionalTextColumn(row, "last_success_at"),
		count: integerColumn(row, "count"),
		message: optionalTextColumn(row, "message"),
		coverage: hasCoverage
			? {
					knownJobs: integerColumn(row, "known_jobs"),
					fullVerifiedDetails: integerColumn(row, "full_verified_details"),
					needsDetailCheck: integerColumn(row, "needs_detail_check"),
				}
			: null,
	}));
}

export function readFunnel(database: DatabaseSync, locationKeys: string | null = null): Funnel {
	const row = database.prepare(`${postingEntries()} SELECT
		COUNT(*) AS discovered,
		SUM(status = 'unscored') AS unscored,
		SUM(status = 'hard-killed') AS hard_killed,
		SUM(status = 'queued') AS queued,
		SUM(status = 'needs-review') AS needs_review,
		SUM(status = 'applied') AS applied,
		SUM(status = 'closed') AS closed,
		SUM(status = 'no-text') AS no_text,
		SUM(status = 'too-long') AS too_long,
		SUM(status = 'protected') AS protected,
		SUM(status = 'not-filtered') AS not_filtered,
		SUM(app_state = 'approved') AS saved,
		SUM(app_state = 'rejected') AS dismissed
		FROM entries WHERE ($locationKeys IS NULL OR key IN (SELECT value FROM json_each($locationKeys)))`).get({ locationKeys });
	const count = (name: string) => row === undefined ? 0 : (optionalIntegerColumn(row, name) ?? 0);
	return {
		discovered: count("discovered"), unscored: count("unscored"),
		hardKilled: count("hard_killed"), queued: count("queued"),
		needsReview: count("needs_review"), applied: count("applied"),
		closed: count("closed"), noText: count("no_text"), tooLong: count("too_long"),
		protected: count("protected"), notFiltered: count("not_filtered"),
		saved: count("saved"), dismissed: count("dismissed"),
	};
}

export function readDatabaseInfo(
	database: DatabaseSync,
	path: string,
	kind: DatabaseInfo["kind"],
): DatabaseInfo {
	const versions = database
		.prepare(
			"SELECT DISTINCT filters_version AS filters_version FROM decisions ORDER BY filters_version ASC",
		)
		.all()
		.map((row) => optionalTextColumn(row, "filters_version"))
		.filter((version) => version !== null);

	const latestRow = database.prepare("SELECT MAX(decided_at) AS decided_at FROM decisions").get();
	const lastDecisionAt =
		latestRow === undefined ? null : optionalTextColumn(latestRow, "decided_at");

	// CLI Discover writes source health even when no dashboard run was started. Keep the
	// latest check from either record; a verified-open Posting is the fallback for older
	// imports with no fetch records at all.
	const fetchRow = database
		.prepare(`SELECT at FROM (
			SELECT at FROM runs WHERE stage = 'discover' AND status = 'ok'
			UNION ALL SELECT last_attempt_at AS at FROM source_health
		) WHERE julianday(at) IS NOT NULL ORDER BY julianday(at) DESC LIMIT 1`)
		.get() ?? database.prepare(`SELECT last_verified_at AS at FROM assessments
			WHERE listing_status = 'open' AND julianday(last_verified_at) IS NOT NULL
			ORDER BY julianday(last_verified_at) DESC LIMIT 1`).get();
	const lastFetchAt = fetchRow === undefined ? null : optionalTextColumn(fetchRow, "at");

	const pauseRow = database.prepare("SELECT status, pause_reason, waiting FROM runs WHERE stage = 'score' ORDER BY id DESC LIMIT 1").get();
	const reason = pauseRow === undefined ? null : optionalTextColumn(pauseRow, "pause_reason");
	const scorePause = pauseRow?.status === "paused" && reason !== null
		? { reason, waiting: Number(pauseRow.waiting) } : null;
	return { path, kind, filtersVersions: versions, lastDecisionAt, lastFetchAt, scorePause };
}
