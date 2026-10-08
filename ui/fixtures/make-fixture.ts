/**
 * Fictional data for Avery Example, not Rolf's job search or a model evaluation.
 * Jobs, assessments, probabilities and actions are invented. Links use example.com.
 * Builds a small venator.db that matches coordination/CONTRACTS.md exactly, so the
 * dashboard is reviewable before the match engine writes a real view.
 *
 * The data is deliberately mixed: kills from every Hard Filter rule (Dedup included),
 * queued Matches,
 * one never-decided Posting, and one
 * replayed Posting whose location kill was later overturned by a filters change.
 */

import { existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";


type FixturePosting = {
	readonly key: string;
	readonly source: string;
	readonly board: string;
	/**
	 * The employer, as the view carries it — Discover sets it where the board says so, and
	 * `venator.view.build` fills the rest from the Profile's `sources.names`. Nullable because
	 * a board registered with no display name really does arrive here as NULL, and that is the
	 * case the UI has to degrade honestly on rather than printing the board token.
	 */
	readonly company: string | null;
	readonly title: string;
	readonly location: string;
	readonly url: string;
	readonly postedAt: string | null;
	readonly discoveredAt: string;
	readonly descriptionHtml: string;
};

type FixtureDecision = {
	readonly postingKey: string;
	readonly stage: "hard_filter" | "llm_score";
	readonly verdict: "pass" | "kill" | "queue";
	readonly rule: string | null;
	readonly score: number | null;
	readonly reason: string;
	readonly filtersVersion: string;
	readonly decidedAt: string;
};

type FixtureAssessment = {
	readonly postingKey: string;
	readonly status: "suitable" | "needs_review" | "not_suitable" | "unassessed";
	readonly summary: string;
	readonly evidence: readonly { readonly requirement: string; readonly candidate_evidence: string; readonly source: string }[];
	readonly assessedBy: "deterministic";
	readonly assessedAsOf: string | null;
	readonly conflicts: readonly string[];
	readonly unknowns: readonly string[];
	readonly listingStatus: "open" | "closed" | "unknown";
	readonly lastVerifiedAt: string | null;
	readonly descriptionKind: "full" | "snippet" | "missing";
	readonly applyUrl: string;
	readonly opportunityType: "job" | "talent_pool" | "program" | "unknown";
};

type FixtureSourceHealth = {
	readonly key: string;
	readonly status: "ok" | "partial" | "failed" | "skipped";
	readonly lastAttemptAt: string;
	readonly lastSuccessAt: string | null;
	readonly count: number;
	readonly message: string | null;
	readonly coverage: {
		readonly knownJobs: number;
		readonly fullVerifiedDetails: number;
		readonly needsDetailCheck: number;
	};
};

/** Verbatim from coordination/CONTRACTS.md; the server verifies against these columns. */
const SCHEMA_SQL = `
CREATE TABLE postings (key TEXT PRIMARY KEY, source TEXT, board TEXT, company TEXT,
  title TEXT, location TEXT, location_places TEXT, url TEXT, posted_at TEXT, discovered_at TEXT,
  description_html TEXT);
CREATE TABLE decisions (id INTEGER PRIMARY KEY, posting_key TEXT, stage TEXT,
  verdict TEXT, rule TEXT, score INTEGER, reason TEXT, filters_version TEXT, decided_at TEXT);
CREATE TABLE hard_filter_latest (posting_key TEXT PRIMARY KEY, decision_id INTEGER NOT NULL);
CREATE INDEX decisions_latest ON decisions (posting_key, stage, decided_at DESC, id DESC);
CREATE TRIGGER fixture_latest_hard AFTER INSERT ON decisions WHEN NEW.stage = 'hard_filter'
BEGIN
  INSERT INTO hard_filter_latest (posting_key, decision_id) VALUES (NEW.posting_key, NEW.id)
  ON CONFLICT(posting_key) DO UPDATE SET decision_id = (
    SELECT id FROM decisions WHERE posting_key = NEW.posting_key AND stage = 'hard_filter'
    ORDER BY decided_at DESC, id DESC LIMIT 1
  );
END;
CREATE TABLE track_events (id INTEGER PRIMARY KEY, posting_key TEXT, event TEXT,
  actor TEXT, detail TEXT, at TEXT);
CREATE TABLE application_states (posting_key TEXT PRIMARY KEY, state TEXT, detail TEXT,
  since TEXT);
CREATE TABLE runs (id INTEGER PRIMARY KEY, at TEXT, status TEXT, stage TEXT, pause_reason TEXT, waiting INTEGER);
CREATE TABLE keep_scores (
  posting_key TEXT PRIMARY KEY, input_hash TEXT NOT NULL, model_id TEXT,
  probability REAL CHECK (probability >= 0 AND probability <= 1), scored_at TEXT,
  carried INTEGER NOT NULL DEFAULT 0 CHECK (carried IN (0, 1))
);
CREATE TABLE assessments (posting_key TEXT PRIMARY KEY, status TEXT, summary TEXT,
  evidence TEXT, conflicts TEXT, unknowns TEXT, listing_status TEXT, last_verified_at TEXT,
  description_kind TEXT, apply_url TEXT, opportunity_type TEXT, input_version TEXT,
  assessed_by TEXT NOT NULL DEFAULT 'deterministic' CHECK (assessed_by IN ('deterministic')),
  assessed_as_of TEXT);
CREATE TABLE source_health (source_key TEXT PRIMARY KEY, status TEXT, last_attempt_at TEXT,
	last_success_at TEXT, count INTEGER, message TEXT, known_jobs INTEGER,
	full_verified_details INTEGER, needs_detail_check INTEGER);
`;

/**
 * Track-stage rows derived from the decisions above: every Posting whose latest llm_score
 * verdict is queue gets a lifecycle row, one of them approved so that render path is
 * exercised, plus two run heartbeats so the liveness indicator has something to say.
 */
const TRACK_SQL = `
INSERT INTO application_states (posting_key, state, detail, since)
SELECT posting_key, 'queued', NULL, MAX(decided_at)
FROM decisions d
WHERE stage = 'llm_score' AND verdict = 'queue'
  AND NOT EXISTS (
    SELECT 1 FROM decisions newer
    WHERE newer.posting_key = d.posting_key AND newer.stage = 'llm_score'
      AND (newer.decided_at > d.decided_at OR (newer.decided_at = d.decided_at AND newer.id > d.id))
      AND newer.verdict <> 'queue'
  )
GROUP BY posting_key;
UPDATE application_states
SET state = 'approved', detail = 'Approved for submission; cover letter drafted.',
    since = '2026-08-18T09:12:00+00:00'
WHERE posting_key = 'greenhouse:exampleplatform:sample-14';
INSERT INTO runs (at, status, stage) VALUES
  ('2026-08-17T19:20:00+00:00', 'ok', 'discover'),
  ('2026-08-18T08:47:00+00:00', 'ok', 'view');
`;


/** Fictional filter versions before and after a sample rule change. */
const PREVIOUS_FILTERS = "a3f1c0d92b47";
const CURRENT_FILTERS = "7c2e5b8146fa";

/** Keep the fixture's open listings inside the server's verification window. */
const FIXTURE_VERIFIED_AT = new Date().toISOString();

function description(intro: string, bullets: readonly string[], closing: string): string {
	const items = bullets.map((bullet) => `<li>${bullet}</li>`).join("");
	return `<p>${intro}</p><h3>What you'll do</h3><ul>${items}</ul><p>${closing}</p>`;
}

const POSTINGS: readonly FixturePosting[] = [
	{
		key: "greenhouse:exampleinventory:sample-01",
		source: "greenhouse",
		board: "exampleinventory",
		company: "Example Inventory",
		title: "Backend Engineer, Inventory Services",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-11",
		discoveredAt: "2026-08-14T13:02:11+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:exampleinventory:sample-02",
		source: "greenhouse",
		board: "exampleinventory",
		company: "Example Inventory",
		title: "Principal Research Scientist",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-09",
		discoveredAt: "2026-08-14T13:02:14+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:exampleplatform:sample-03",
		source: "greenhouse",
		board: "exampleplatform",
		company: "Example Platform",
		title: "Platform Engineer, Data Validation",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-12",
		discoveredAt: "2026-08-15T09:41:03+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:examplereporting:sample-04",
		source: "greenhouse",
		board: "examplereporting",
		company: "Example Reporting",
		title: "Data Engineer, Reporting",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-08",
		discoveredAt: "2026-08-15T09:41:07+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:examplemodels:sample-05",
		source: "greenhouse",
		board: "examplemodels",
		company: "Example Models",
		title: "Machine Learning Engineer",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-05",
		discoveredAt: "2026-08-15T09:41:12+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:examplewarehouse:sample-06",
		source: "greenhouse",
		board: "examplewarehouse",
		company: "Example Warehouse",
		title: "Lab Automation Engineer",
		location: "Salt Lake City, UT",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-10",
		discoveredAt: "2026-08-16T07:15:44+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:examplewarehouse:sample-07",
		source: "greenhouse",
		board: "examplewarehouse",
		company: "Example Warehouse",
		title: "Data Engineer (Remote, US)",
		location: "Remote (US)",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-13",
		discoveredAt: "2026-08-16T07:15:49+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "ashby:exampleworkflows:sample-08",
		source: "ashby",
		board: "exampleworkflows",
		company: "Example Workflows",
		title: "Software Engineer, Workflows",
		location: "San Francisco, CA",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-07",
		discoveredAt: "2026-08-16T07:16:02+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "ashby:exampleworkflows:sample-09",
		source: "ashby",
		board: "exampleworkflows",
		company: "Example Workflows",
		title: "Solutions Engineer, Data (Remote US)",
		location: "Remote (US)",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-14",
		discoveredAt: "2026-08-17T11:22:31+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "adzuna:us:sample-10",
		source: "adzuna",
		board: "us",
		company: "Example Workflows",
		title: "Solutions Engineer, Data (Remote US)",
		location: "Chicago, Cook County",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-14",
		discoveredAt: "2026-08-17T20:39:16+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "ashby:examplecircuits:sample-11",
		source: "ashby",
		board: "examplecircuits",
		company: "Example Circuits",
		title: "Research Software Engineer",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-15",
		discoveredAt: "2026-08-17T11:22:36+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:exampleinventory:sample-12",
		source: "greenhouse",
		board: "exampleinventory",
		company: "Example Inventory",
		title: "Certification Programs Analyst",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-04",
		discoveredAt: "2026-08-14T13:02:19+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:examplereporting:sample-13",
		source: "greenhouse",
		board: "examplereporting",
		company: "Example Reporting",
		title: "Manufacturing Technician",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-06",
		discoveredAt: "2026-08-15T09:41:19+00:00",
		descriptionHtml:
			// Render-safety canary: the reader extracts text and the detail view renders it
			// as React text. Employer scripts and event handlers must not run.
			`<p>Operate upstream and downstream unit operations in a GMP suite.</p>` +
			`<script>document.title = "sandbox escaped";</script>` +
			`<img src="x" onerror="document.title='sandbox escaped'">` +
			`<ul><li>Execute batch records under cGMP</li><li>Rotating 12-hour shifts, nights and weekends</li>` +
			`<li>Gowning and cleanroom qualification required</li></ul>` +
			`<p>Full-time permanent role. Associate degree plus 2 years GMP manufacturing experience required.</p>`,
	},
	{
		key: "greenhouse:exampleplatform:sample-14",
		source: "greenhouse",
		board: "exampleplatform",
		company: "Example Platform",
		title: "Data Platform Engineer",
		location: "San Diego, CA",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-12",
		discoveredAt: "2026-08-15T09:41:24+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "ashby:examplecircuits:sample-15",
		source: "ashby",
		board: "examplecircuits",
		company: "Example Circuits",
		title: "Bioinformatics Engineer",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-17",
		discoveredAt: "2026-08-18T06:04:52+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "greenhouse:examplemodels:sample-16",
		source: "greenhouse",
		board: "examplemodels",
		company: "Example Models",
		title: "Operations Coordinator (Contract)",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-03",
		discoveredAt: "2026-08-16T07:15:58+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},

	// The last three exist for the board guard below. Greenhouse and Ashby tokens are single
	// lowercase words that read almost like a name, so a fixture made only of those cannot
	// show what a raw board token on screen looks like. Workday's is `<tenant>.wd<N>~<site>`
	// and Lever's is whatever the employer typed, which is where the damage was: the fixture
	// includes mixed-case and compound routing tokens to exercise board-label rendering.
	{
		key: "workday:exampleprocess.wd1~Careers:sample-17",
		source: "workday",
		board: "exampleprocess.wd1~Careers",
		company: "Example Process",
		title: "Software Engineer, Process Data",
		location: "Chicago, IL",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-12",
		discoveredAt: "2026-08-18T09:41:07+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		key: "lever:ExampleProteins:sample-18",
		source: "lever",
		board: "ExampleProteins",
		company: "Example Proteins",
		title: "Research Associate, Protein Data",
		location: "South San Francisco, CA",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-10",
		discoveredAt: "2026-08-18T09:41:12+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
	{
		// company NULL: a board polled with no `sources.names` entry, or a view built without a
		// resolvable Profile. The UI has no name to show and must say so — "via Workday" — not
		// dress the routing token up as one.
		key: "workday:examplebiology.wd3~ExampleSite:sample-19",
		source: "workday",
		board: "examplebiology.wd3~ExampleSite",
		company: null,
		title: "Computational Biology Engineer",
		location: "Basel, Switzerland",
		url: "https://example.com/jobs/sample",
		postedAt: "2026-08-06",
		discoveredAt: "2026-08-18T09:41:19+00:00",
		descriptionHtml: description(
			"Fictional listing for dashboard development. Not a live vacancy.",
			["Build Python data services", "Maintain SQL reporting"],
			"Sample software and reporting role.",
		),
	},
];

function evidence(requirement: string, candidateEvidence: string, source: string) {
	return { requirement, candidate_evidence: candidateEvidence, source };
}

/** Intentional evidence fixtures exercise suitable, uncertain, conflicting and unassessed rows. */
function assessmentFor(posting: FixturePosting): FixtureAssessment {
	const base = {
		postingKey: posting.key,
		listingStatus: "open" as const,
		lastVerifiedAt: FIXTURE_VERIFIED_AT,
		descriptionKind: "full" as const,
		applyUrl: posting.url,
		opportunityType: "job" as const,
		conflicts: [],
		unknowns: [],
		assessedBy: "deterministic" as const,
		assessedAsOf: null,
	};
	switch (posting.key) {
		case "greenhouse:exampleinventory:sample-01":
			return {
				...base,
				status: "suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [
					evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience"),
					evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience"),
				],
			};
		case "greenhouse:exampleinventory:sample-02":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
			};
		case "greenhouse:exampleplatform:sample-03":
			return {
				...base,
				status: "needs_review",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience")],
				unknowns: ["Experience is unknown in this fictional scenario."],
			};
		case "greenhouse:examplereporting:sample-04":
			return {
				...base,
				status: "suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [
					evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience"),
					evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience"),
				],
			};
		case "greenhouse:examplemodels:sample-05":
			return {
				...base,
				status: "needs_review",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				unknowns: ["Experience is unknown in this fictional scenario."],
			};
		case "greenhouse:examplewarehouse:sample-06":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
			};
		case "greenhouse:examplewarehouse:sample-07":
			return {
				...base,
				status: "suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience")],
			};
		case "ashby:exampleworkflows:sample-08":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
			};
		case "ashby:exampleworkflows:sample-09":
			return {
				...base,
				status: "suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience")],
			};
		case "adzuna:us:sample-10":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
			};
		case "ashby:examplecircuits:sample-11":
			return {
				...base,
				status: "unassessed",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				lastVerifiedAt: null,
			};
		case "greenhouse:exampleinventory:sample-12":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
			};
		case "greenhouse:examplereporting:sample-13":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
			};
		case "greenhouse:exampleplatform:sample-14":
			return {
				...base,
				status: "needs_review",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience")],
				unknowns: ["Experience is unknown in this fictional scenario."],
				opportunityType: "program",
			};
		case "ashby:examplecircuits:sample-15":
			return {
				...base,
				status: "suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience")],
			};
		case "greenhouse:examplemodels:sample-16":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
			};
		case "workday:exampleprocess.wd1~Careers:sample-17":
			return {
				...base,
				status: "suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [evidence("Python data services", "Avery's fictional inventory-service project uses Python and SQL.", "Sample Profile experience")],
			};
		case "lever:ExampleProteins:sample-18":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
			};
		case "workday:examplebiology.wd3~ExampleSite:sample-19":
			return {
				...base,
				status: "not_suitable",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				conflicts: ["Fictional sample requirements are not met."],
				listingStatus: "closed",
			};
		default:
			return {
				...base,
				status: "unassessed",
				summary: "Fictional assessment for Avery Example, not a model result.",
				evidence: [],
				lastVerifiedAt: null,
			};
	}
}

const ASSESSMENTS: readonly FixtureAssessment[] = POSTINGS.map(assessmentFor);

function fixtureCoverage(
	key: string,
	status: FixtureSourceHealth["status"],
): FixtureSourceHealth["coverage"] {
	const knownJobs = POSTINGS.filter((posting) => `${posting.source}:${posting.board}` === key).length;
	const workday = key.startsWith("workday:");
	const fullVerifiedDetails = workday
		? 0
		: status === "ok"
			? knownJobs
			: Math.max(knownJobs - 1, 0);
	return {
		knownJobs,
		fullVerifiedDetails,
		needsDetailCheck: knownJobs - fullVerifiedDetails,
	};
}

const SOURCE_HEALTH: readonly FixtureSourceHealth[] = [
	{ key: "adzuna:us", status: "failed", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: "2026-09-03T12:00:00+00:00", count: 0, message: "Aggregator unavailable; existing records were retained.", coverage: fixtureCoverage("adzuna:us", "failed") },
	{ key: "ashby:exampleworkflows", status: "partial", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: FIXTURE_VERIFIED_AT, count: 2, message: "One listing detail needs review.", coverage: fixtureCoverage("ashby:exampleworkflows", "partial") },
	{ key: "ashby:examplecircuits", status: "ok", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: FIXTURE_VERIFIED_AT, count: 2, message: null, coverage: fixtureCoverage("ashby:examplecircuits", "ok") },
	{ key: "greenhouse:exampleinventory", status: "ok", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: FIXTURE_VERIFIED_AT, count: 3, message: null, coverage: fixtureCoverage("greenhouse:exampleinventory", "ok") },
	{ key: "workday:exampleprocess.wd1~Careers", status: "ok", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: FIXTURE_VERIFIED_AT, count: 1, message: null, coverage: fixtureCoverage("workday:exampleprocess.wd1~Careers", "ok") },
];

const DECISIONS: readonly FixtureDecision[] = [
	{
		// Dedup: the aggregator's copy of a Posting the Ashby board already carries.
		// Recorded at hard_filter with rule "duplicate" — not a third stage — and it
		// names the survivor by employer and title, never by a key holding a board slug.
		//
		// The reason below is hand-authored to match what the pipeline writes.
		// `dedup.duplicate_reason` in src/venator/match/dedup.py is the source of
		// truth for that wording; if it changes, change this string with it.
		postingKey: "adzuna:us:sample-10",
		stage: "hard_filter",
		verdict: "kill",
		rule: "duplicate",
		score: null,
		reason:
			"the same role as 'Solutions Engineer, Data (Remote US)' at Example Workflows, already discovered from Ashby",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:03+00:00",
	},
	{
		postingKey: "greenhouse:exampleinventory:sample-01",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:04+00:00",
	},
	{
		postingKey: "greenhouse:exampleinventory:sample-01",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 88,
		reason:
			"Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:14:22+00:00",
	},
	{
		postingKey: "greenhouse:exampleinventory:sample-02",
		stage: "hard_filter",
		verdict: "kill",
		rule: "education_fit",
		score: null,
		reason: "Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:05+00:00",
	},
	{
		postingKey: "greenhouse:exampleplatform:sample-03",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:06+00:00",
	},
	{
		postingKey: "greenhouse:exampleplatform:sample-03",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 84,
		reason:
			"Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:14:31+00:00",
	},
	{
		postingKey: "greenhouse:examplereporting:sample-04",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:08+00:00",
	},
	{
		// A historical Match Score recorded under the previous filters_version, with the
		// hard_filter decision above already replayed at the current one. It decodes and orders
		// nothing.
		postingKey: "greenhouse:examplereporting:sample-04",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 79,
		reason:
			"Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: PREVIOUS_FILTERS,
		decidedAt: "2026-08-17T19:14:39+00:00",
	},
	{
		postingKey: "greenhouse:examplemodels:sample-05",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:10+00:00",
	},
	{
		postingKey: "greenhouse:examplemodels:sample-05",
		stage: "llm_score",
		verdict: "kill",
		rule: null,
		score: 58,
		reason:
			"Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:14:47+00:00",
	},
	{
		postingKey: "greenhouse:examplewarehouse:sample-06",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason:
			"Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:12+00:00",
	},
	{
		postingKey: "greenhouse:examplewarehouse:sample-07",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:13+00:00",
	},
	{
		postingKey: "greenhouse:examplewarehouse:sample-07",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 81,
		reason:
			"Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:14:55+00:00",
	},
	{
		postingKey: "ashby:exampleworkflows:sample-08",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason: "Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:15+00:00",
	},
	{
		postingKey: "ashby:exampleworkflows:sample-09",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T06:30:02+00:00",
	},
	{
		postingKey: "ashby:examplecircuits:sample-11",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T06:30:04+00:00",
	},
	{
		postingKey: "greenhouse:exampleinventory:sample-12",
		stage: "hard_filter",
		verdict: "kill",
		rule: "work_authorization",
		score: null,
		reason: "Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:17+00:00",
	},
	{
		postingKey: "greenhouse:examplereporting:sample-13",
		stage: "hard_filter",
		verdict: "kill",
		rule: "education_fit",
		score: null,
		reason:
			"Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:18+00:00",
	},
	{
		postingKey: "greenhouse:exampleplatform:sample-14",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason: "Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: PREVIOUS_FILTERS,
		decidedAt: "2026-08-16T18:12:40+00:00",
	},
	{
		postingKey: "greenhouse:exampleplatform:sample-14",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason:
			"Fictional replay allowed remote sites.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:20+00:00",
	},
	{
		postingKey: "greenhouse:exampleplatform:sample-14",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 76,
		reason:
			"Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:15:09+00:00",
	},
	{
		postingKey: "greenhouse:examplemodels:sample-16",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:22+00:00",
	},
	{
		postingKey: "greenhouse:examplemodels:sample-16",
		stage: "llm_score",
		verdict: "kill",
		rule: null,
		score: 42,
		reason:
			"Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:15:17+00:00",
	},

	// The Workday and Lever Postings: one through to the Review Queue, two killed, so a board
	// token has a route to reach the queue, the inspector and Focus if the UI ever prints one.
	{
		postingKey: "workday:exampleprocess.wd1~Careers:sample-17",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Fictional decision for Avery Example: sample requirements reviewed.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T09:42:01+00:00",
	},
	{
		postingKey: "workday:exampleprocess.wd1~Careers:sample-17",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 83,
		reason:
			"Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T09:48:30+00:00",
	},
	{
		postingKey: "lever:ExampleProteins:sample-18",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason: "Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T09:42:04+00:00",
	},
	{
		postingKey: "workday:examplebiology.wd3~ExampleSite:sample-19",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason: "Fictional decision for Avery Example: excluded by a sample rule.",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T09:42:07+00:00",
	},
];

/** Rebuilds the fixture database from scratch at `path`. */
export function buildFixtureDatabase(path: string): void {
	writeView(path, DECISIONS);
}

function writeView(path: string, decisions: readonly FixtureDecision[]): void {
	mkdirSync(dirname(path), { recursive: true });
	rmSync(path, { force: true });

	const database = new DatabaseSync(path);
	try {
		database.exec(SCHEMA_SQL);
		const insertPosting = database.prepare(
			`INSERT INTO postings (key, source, board, company, title, location, url, posted_at, discovered_at, description_html)
			 VALUES ($key, $source, $board, $company, $title, $location, $url, $postedAt, $discoveredAt, $descriptionHtml)`,
		);
		for (const posting of POSTINGS) {
			insertPosting.run({
				key: posting.key,
				source: posting.source,
				board: posting.board,
				company: posting.company,
				title: posting.title,
				location: posting.location,
				url: posting.url,
				postedAt: posting.postedAt,
				discoveredAt: posting.discoveredAt,
				descriptionHtml: posting.descriptionHtml,
			});
		}

		const insertDecision = database.prepare(
			`INSERT INTO decisions (posting_key, stage, verdict, rule, score, reason, filters_version, decided_at)
			 VALUES ($postingKey, $stage, $verdict, $rule, $score, $reason, $filtersVersion, $decidedAt)`,
		);
		for (const decision of decisions) {
			insertDecision.run({
				postingKey: decision.postingKey,
				stage: decision.stage,
				verdict: decision.verdict,
				rule: decision.rule,
				score: decision.score,
				reason: decision.reason,
				filtersVersion: decision.filtersVersion,
				decidedAt: decision.decidedAt,
			});
		}

		const insertAssessment = database.prepare(
			`INSERT INTO assessments (posting_key, status, summary, evidence, conflicts, unknowns, listing_status, last_verified_at, description_kind, apply_url, opportunity_type, assessed_by, assessed_as_of)
			 VALUES ($postingKey, $status, $summary, $evidence, $conflicts, $unknowns, $listingStatus, $lastVerifiedAt, $descriptionKind, $applyUrl, $opportunityType, $assessedBy, $assessedAsOf)`,
		);
		for (const assessment of ASSESSMENTS) {
			insertAssessment.run({
				postingKey: assessment.postingKey,
				status: assessment.status,
				summary: assessment.summary,
				evidence: JSON.stringify(assessment.evidence),
				conflicts: JSON.stringify(assessment.conflicts),
				unknowns: JSON.stringify(assessment.unknowns),
				assessedBy: assessment.assessedBy,
				assessedAsOf: assessment.assessedAsOf,
				listingStatus: assessment.listingStatus,
				lastVerifiedAt: assessment.lastVerifiedAt,
				descriptionKind: assessment.descriptionKind,
				applyUrl: assessment.applyUrl,
				opportunityType: assessment.opportunityType,
			});
		}

		// Synthetic scores for the sample View; no private model outputs live here.
		database.exec(`INSERT INTO keep_scores (posting_key, input_hash, model_id, probability)
			SELECT d.posting_key, 'fixture-input', 'fixture-model',
				CASE WHEN d.posting_key = 'greenhouse:exampleinventory:sample-01' THEN 0.8
				WHEN d.posting_key = 'greenhouse:examplewarehouse:sample-07' THEN 0.2
				WHEN d.posting_key = 'greenhouse:exampleplatform:sample-03' THEN 0.001 END
			FROM hard_filter_latest hl
			JOIN decisions d ON d.id = hl.decision_id AND d.verdict = 'pass'`);

		const insertSourceHealth = database.prepare(
			`INSERT INTO source_health (source_key, status, last_attempt_at, last_success_at, count, message, known_jobs, full_verified_details, needs_detail_check)
			 VALUES ($key, $status, $lastAttemptAt, $lastSuccessAt, $count, $message, $knownJobs, $fullVerifiedDetails, $needsDetailCheck)`,
		);
		for (const source of SOURCE_HEALTH) {
			insertSourceHealth.run({
				key: source.key,
				status: source.status,
				lastAttemptAt: source.lastAttemptAt,
				lastSuccessAt: source.lastSuccessAt,
				count: source.count,
				message: source.message,
				knownJobs: source.coverage.knownJobs,
				fullVerifiedDetails: source.coverage.fullVerifiedDetails,
				needsDetailCheck: source.coverage.needsDetailCheck,
			});
		}

		database.exec(TRACK_SQL);
	} finally {
		database.close();
	}
}

/**
 * A view with the contract's schema and not one row in it.
 *
 * This is the state of a brand new Install: a Profile exists, the pipeline has never run, and
 * every list is empty because nothing has been discovered rather than because something went
 * wrong. It is the first screen anybody sees after setting up, so it is worth rendering against
 * on purpose — an empty database is not an error path and must never look like one.
 */
export function buildEmptyViewDatabase(path: string): void {
	mkdirSync(dirname(path), { recursive: true });
	rmSync(path, { force: true });
	const database = new DatabaseSync(path);
	try {
		database.exec(SCHEMA_SQL);
	} finally {
		database.close();
	}
}

/**
 * The same thing with no file behind it: the contract's schema, no rows, in memory.
 *
 * This is what the server serves an Install that has never run anything (`server/db.ts`), so
 * it is a shipped code path rather than a test one. It lives here because `SCHEMA_SQL` does —
 * the DDL is copied from coordination/CONTRACTS.md once, and a second copy in the server
 * would be a second thing to keep in step with it.
 *
 * In memory rather than on disk because there is nothing to keep: the moment the pipeline
 * writes a real view the server opens that instead, and a shipped Install's resource
 * directory is not somewhere to be writing a database anyway.
 */
export function emptyViewDatabase(): DatabaseSync {
	const database = new DatabaseSync(":memory:");
	database.exec(SCHEMA_SQL);
	return database;
}

export const FIXTURE_POSTING_COUNT = POSTINGS.length;
export const FIXTURE_DECISION_COUNT = DECISIONS.length;

/** One board the fixture holds Postings from, and the employer name the view carries for it. */
export type FixtureBoard = {
	readonly board: string;
	readonly company: string | null;
};

/**
 * Every board in the fixture, derived rather than listed, so a Posting added above is covered
 * by `tools/smoke.ts`'s board guard without anyone remembering to extend anything.
 */
export const FIXTURE_BOARDS: readonly FixtureBoard[] = [
	...new Map(POSTINGS.map((posting) => [posting.board, posting])).values(),
].map((posting) => ({ board: posting.board, company: posting.company }));

/**
 * Every employer and every Posting title the fixture carries, derived rather than listed.
 *
 * `tools/smoke.ts` asserts that none of it reaches the screen of an Install that never asked
 * for sample data. Derived for the same reason `FIXTURE_BOARDS` is: a hand-picked list can
 * only catch the Postings somebody remembered, and the property is about all of them.
 */
export const FIXTURE_ON_SCREEN_TEXT: readonly string[] = [
	...new Set(POSTINGS.flatMap((posting) => (posting.company === null ? [] : [posting.company]))),
	...new Set(POSTINGS.map((posting) => posting.title)),
];

/** Builds the fixture only when it is missing, so the server can fall back without ceremony. */
export function ensureFixtureDatabase(path: string): void {
	if (existsSync(path)) return;
	buildFixtureDatabase(path);
}
