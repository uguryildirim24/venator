/**
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
	readonly assessedBy: "deterministic" | "jev";
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
CREATE TABLE jev_skip (posting_key TEXT PRIMARY KEY, reason TEXT NOT NULL);
CREATE TABLE assessments (posting_key TEXT PRIMARY KEY, status TEXT, summary TEXT,
  evidence TEXT, conflicts TEXT, unknowns TEXT, listing_status TEXT, last_verified_at TEXT,
  description_kind TEXT, apply_url TEXT, opportunity_type TEXT, input_version TEXT,
  assessed_by TEXT NOT NULL DEFAULT 'deterministic' CHECK (assessed_by IN ('deterministic', 'jev')),
  assessed_as_of TEXT);
CREATE TABLE jev_triage (
  posting_key TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('shadow', 'promoted')),
  state TEXT NOT NULL CHECK (state IN ('current', 'stale', 'unavailable')),
  decision TEXT NOT NULL
    CHECK (decision IN ('prioritize', 'review', 'exclude', 'unassessed')),
  fit_probability REAL
    CHECK (fit_probability IS NULL OR fit_probability BETWEEN 0.0 AND 1.0),
  fit_score REAL
    CHECK (fit_score IS NULL OR fit_score BETWEEN 0.0 AND 1.0),
  primary_rule TEXT,
  exclusions TEXT NOT NULL,
  review_flags TEXT NOT NULL,
  diagnostic_flags TEXT NOT NULL,
  qualifier_version TEXT,
  assessment_key TEXT,
  input_version TEXT,
  policy_hash TEXT,
  model_id TEXT,
  as_of_month TEXT NOT NULL,
  decided_at TEXT,
  reason TEXT,
  PRIMARY KEY (posting_key, mode),
  CHECK (decision <> 'unassessed' OR
         (fit_probability IS NULL AND fit_score IS NULL AND primary_rule IS NULL)),
  CHECK (state <> 'current' OR
         (decision <> 'unassessed' AND fit_probability IS NOT NULL AND
          fit_score IS NOT NULL AND assessment_key IS NOT NULL AND
          qualifier_version IS NOT NULL AND input_version IS NOT NULL))
);
CREATE TABLE jev_selection (mode TEXT NOT NULL CHECK (mode IN ('shadow', 'promoted')));
INSERT INTO jev_selection (mode) VALUES ('shadow');
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
WHERE posting_key = 'greenhouse:generatebiomedicines:4728990';
INSERT INTO runs (at, status, stage) VALUES
  ('2026-08-17T19:20:00+00:00', 'ok', 'discover'),
  ('2026-08-18T08:47:00+00:00', 'ok', 'view');
`;


/** The filters_version before the summer-housing clause was added to the Profile's constraints.yaml. */
const PREVIOUS_FILTERS = "a3f1c0d92b47";
const CURRENT_FILTERS = "7c2e5b8146fa";

/** Keep the fixture's open listings inside the server's verification window. */
const FIXTURE_VERIFIED_AT = new Date().toISOString();

type FixtureJevExclusion = {
	readonly rule: string;
	readonly basis: string;
	readonly question_ids: readonly string[];
	readonly probabilities: readonly (readonly [string, number])[];
	readonly guard_code: string;
	readonly policy_value: string | null;
	readonly policy_exclusion: boolean;
};

type FixtureJevTriage = {
	readonly postingKey: string;
	readonly mode: "shadow" | "promoted";
	readonly state: "current" | "stale" | "unavailable";
	readonly decision: "prioritize" | "review" | "exclude" | "unassessed";
	readonly fitProbability: number | null;
	readonly fitScore: number | null;
	readonly primaryRule: string | null;
	readonly exclusions: readonly (string | FixtureJevExclusion)[];
	readonly reviewFlags: readonly string[];
	readonly diagnosticFlags: readonly string[];
	readonly qualifierVersion: string | null;
	readonly assessmentKey: string | null;
	readonly inputVersion: string | null;
	readonly policyHash: string | null;
	readonly modelId: string | null;
	readonly asOfMonth: string;
	readonly decidedAt: string | null;
	readonly reason: string | null;
};

/** R1 golden `qualifier_version` from `tests/qualify/fixtures/jev/release.json`. */
const JEV_QUALIFIER = "jev:7f6cf2d9302311874743e3147870ffb16cf30a886ea1c077e40159573f0c82b3";

const JEV_TRIAGE: readonly FixtureJevTriage[] = [
	{
		postingKey: "greenhouse:ginkgobioworks:5185285007",
		mode: "shadow",
		state: "current",
		decision: "prioritize",
		fitProbability: 0.85,
		fitScore: 0.8875,
		primaryRule: null,
		exclusions: [],
		reviewFlags: [],
		diagnosticFlags: [],
		qualifierVersion: JEV_QUALIFIER,
		assessmentKey: "ak-priority-ginkgo",
		inputVersion: "in-2026-09",
		policyHash: "policy-fixture",
		modelId: "jev-1.13.0",
		asOfMonth: "2026-09",
		decidedAt: "2026-09-10T12:00:00+00:00",
		reason: null,
	},
	{
		postingKey: "greenhouse:recursionpharmaceuticals:6612991",
		mode: "shadow",
		state: "current",
		decision: "review",
		fitProbability: 0.4,
		fitScore: 0.775,
		primaryRule: null,
		exclusions: [],
		reviewFlags: ["mandatory_gap_review"],
		diagnosticFlags: [],
		qualifierVersion: JEV_QUALIFIER,
		assessmentKey: "ak-review-ginkgo",
		inputVersion: "in-2026-09",
		policyHash: "policy-fixture",
		modelId: "jev-1.13.0",
		asOfMonth: "2026-09",
		decidedAt: "2026-09-10T12:01:00+00:00",
		reason: null,
	},
	{
		postingKey: "greenhouse:generatebiomedicines:4728845",
		mode: "shadow",
		state: "current",
		decision: "exclude",
		fitProbability: 0.85,
		fitScore: 0.8875,
		primaryRule: "restricted_role",
		exclusions: [
			{
				rule: "restricted_role",
				basis: "model_assisted_policy",
				question_ids: ["citizenship_or_clearance"],
				probabilities: [["citizenship_or_clearance", 0.95]],
				guard_code: "fired",
				policy_value: "exclude",
				policy_exclusion: true,
			},
		],
		reviewFlags: [],
		diagnosticFlags: [],
		qualifierVersion: JEV_QUALIFIER,
		assessmentKey: "ak-exclude-generate",
		inputVersion: "in-2026-09",
		policyHash: "policy-fixture",
		modelId: "jev-1.13.0",
		asOfMonth: "2026-09",
		decidedAt: "2026-09-10T12:02:00+00:00",
		reason: "Excluded by your policy.",
	},
	{
		postingKey: "greenhouse:tesseratherapeutics:4901233",
		mode: "shadow",
		state: "unavailable",
		decision: "unassessed",
		fitProbability: null,
		fitScore: null,
		primaryRule: null,
		exclusions: [],
		reviewFlags: [],
		diagnosticFlags: [],
		qualifierVersion: null,
		assessmentKey: null,
		inputVersion: null,
		policyHash: null,
		modelId: null,
		asOfMonth: "2026-09",
		decidedAt: null,
		reason: "Jev assessment is unavailable",
	},
	{
		postingKey: "greenhouse:dynotherapeutics:4455102",
		mode: "shadow",
		state: "stale",
		decision: "review",
		fitProbability: 0.4,
		fitScore: 0.775,
		primaryRule: null,
		exclusions: [],
		reviewFlags: ["mandatory_gap_review"],
		diagnosticFlags: [],
		qualifierVersion: JEV_QUALIFIER,
		assessmentKey: "ak-stale-dyno",
		inputVersion: "in-2026-08",
		policyHash: "policy-fixture",
		modelId: null,
		asOfMonth: "2026-08",
		decidedAt: "2026-08-12T09:00:00+00:00",
		reason: null,
	},
];


function description(intro: string, bullets: readonly string[], closing: string): string {
	const items = bullets.map((bullet) => `<li>${bullet}</li>`).join("");
	return `<p>${intro}</p><h3>What you'll do</h3><ul>${items}</ul><p>${closing}</p>`;
}

const POSTINGS: readonly FixturePosting[] = [
	{
		key: "greenhouse:ginkgobioworks:5185285007",
		source: "greenhouse",
		board: "ginkgobioworks",
		company: "Ginkgo Bioworks",
		title: "Summer 2027 Intern, Automation & Lab Informatics",
		location: "Boston, MA",
		url: "https://boards.greenhouse.io/ginkgobioworks/jobs/5185285007",
		postedAt: "2026-08-11",
		discoveredAt: "2026-08-14T13:02:11+00:00",
		descriptionHtml: description(
			"Ginkgo's Foundry runs thousands of strain builds a week. Our summer interns sit with the automation team and make that data legible.",
			[
				"Write Python glue between liquid handlers and our internal LIMS",
				"Build dashboards that surface failed runs to the bench scientists who own them",
				"Pair with software engineers on data-pipeline reliability work",
			],
			"Open to undergraduates returning to a degree program in Fall 2027. Housing stipend available for interns relocating to Boston.",
		),
	},
	{
		key: "greenhouse:ginkgobioworks:5185301442",
		source: "greenhouse",
		board: "ginkgobioworks",
		company: "Ginkgo Bioworks",
		title: "Senior Staff Scientist, Strain Engineering",
		location: "Boston, MA",
		url: "https://boards.greenhouse.io/ginkgobioworks/jobs/5185301442",
		postedAt: "2026-08-09",
		discoveredAt: "2026-08-14T13:02:14+00:00",
		descriptionHtml: description(
			"We are hiring a senior scientist to lead strain engineering campaigns end to end.",
			[
				"Design and execute multi-round metabolic engineering campaigns",
				"Mentor a team of four associate scientists",
				"Own program-level technical decisions with commercial partners",
			],
			"PhD in a relevant discipline plus 8+ years of industry experience required.",
		),
	},
	{
		key: "greenhouse:generatebiomedicines:4728845",
		source: "greenhouse",
		board: "generatebiomedicines",
		company: "Generate Biomedicines",
		title: "Co-op, Protein Data Pipelines (Summer 2027)",
		location: "Somerville, MA",
		url: "https://boards.greenhouse.io/generatebiomedicines/jobs/4728845",
		postedAt: "2026-08-12",
		discoveredAt: "2026-08-15T09:41:03+00:00",
		descriptionHtml: description(
			"Generate:Biomedicines turns generative models into real therapeutics. This co-op supports the data platform behind our protein programs.",
			[
				"Maintain ingestion pipelines for assay and structure data",
				"Add validation checks that catch bad plate metadata before modeling",
				"Document dataset lineage for the research teams",
			],
			"Six-month co-op, on-site in Somerville. Undergraduates in a science or engineering program encouraged to apply.",
		),
	},
	{
		key: "greenhouse:tesseratherapeutics:4901233",
		source: "greenhouse",
		board: "tesseratherapeutics",
		company: "Tessera Therapeutics",
		title: "Intern, Gene Writing Analytics",
		location: "Somerville, MA",
		url: "https://boards.greenhouse.io/tesseratherapeutics/jobs/4901233",
		postedAt: "2026-08-08",
		discoveredAt: "2026-08-15T09:41:07+00:00",
		descriptionHtml: description(
			"Tessera's Gene Writing platform generates deep sequencing data faster than we can read it. Help us close that gap.",
			[
				"Summarize NGS run outputs into review-ready tables",
				"Automate QC reporting currently done by hand in Excel",
				"Work alongside the molecular biology team on assay readouts",
			],
			"Summer 2027, on-site in Somerville. Rising juniors and seniors welcome.",
		),
	},
	{
		key: "greenhouse:dynotherapeutics:4455102",
		source: "greenhouse",
		board: "dynotherapeutics",
		company: "Dyno Therapeutics",
		title: "Machine Learning Intern, AAV Capsid Design",
		location: "Chicago, MA",
		url: "https://boards.greenhouse.io/dynotherapeutics/jobs/4455102",
		postedAt: "2026-08-05",
		discoveredAt: "2026-08-15T09:41:12+00:00",
		descriptionHtml: description(
			"Dyno applies deep learning to AAV capsid design. This internship sits inside the ML research group.",
			[
				"Train and evaluate sequence models on capsid fitness data",
				"Reproduce results from recent protein-design literature",
				"Present findings at the weekly research review",
			],
			"Strong preference for PhD students; MS candidates with published deep-learning work considered.",
		),
	},
	{
		key: "greenhouse:recursionpharmaceuticals:6612884",
		source: "greenhouse",
		board: "recursionpharmaceuticals",
		company: "Recursion",
		title: "Summer Intern, Lab Automation",
		location: "Salt Lake City, UT",
		url: "https://boards.greenhouse.io/recursionpharmaceuticals/jobs/6612884",
		postedAt: "2026-08-10",
		discoveredAt: "2026-08-16T07:15:44+00:00",
		descriptionHtml: description(
			"Recursion runs one of the largest automated cell-biology labs in the world, in Salt Lake City.",
			[
				"Support scheduling software for our automated imaging fleet",
				"Track and triage instrument errors across shifts",
				"Improve run-metadata capture at the bench",
			],
			"On-site in Salt Lake City for the full 12 weeks. Interns arrange their own housing.",
		),
	},
	{
		key: "greenhouse:recursionpharmaceuticals:6612991",
		source: "greenhouse",
		board: "recursionpharmaceuticals",
		company: "Recursion",
		title: "Data Engineering Intern (Remote, US)",
		location: "Remote (US)",
		url: "https://boards.greenhouse.io/recursionpharmaceuticals/jobs/6612991",
		postedAt: "2026-08-13",
		discoveredAt: "2026-08-16T07:15:49+00:00",
		descriptionHtml: description(
			"A fully remote internship on the data platform team supporting Recursion's phenomics datasets.",
			[
				"Build ETL steps that land experiment metadata in our warehouse",
				"Write tests for pipeline transformations",
				"Improve internal documentation for dataset consumers",
			],
			"Remote anywhere in the US. Open to undergraduates; no prior industry experience required.",
		),
	},
	{
		key: "ashby:benchling:9f2c41a8",
		source: "ashby",
		board: "benchling",
		company: "Benchling",
		title: "Software Engineering Intern, Lab Informatics",
		location: "San Francisco, CA",
		url: "https://jobs.ashbyhq.com/benchling/9f2c41a8",
		postedAt: "2026-08-07",
		discoveredAt: "2026-08-16T07:16:02+00:00",
		descriptionHtml: description(
			"Benchling builds the R&D cloud for life sciences. Interns ship to production alongside a mentor.",
			[
				"Implement features in our notebook and registry products",
				"Write and review TypeScript and Python",
				"Participate in on-call shadowing",
			],
			"This role is on-site in San Francisco five days a week. Relocation is not provided.",
		),
	},
	{
		key: "ashby:benchling:1a77b0e3",
		source: "ashby",
		board: "benchling",
		company: "Benchling",
		title: "Solutions Intern, Scientific Data (Remote US)",
		location: "Remote (US)",
		url: "https://jobs.ashbyhq.com/benchling/1a77b0e3",
		postedAt: "2026-08-14",
		discoveredAt: "2026-08-17T11:22:31+00:00",
		descriptionHtml: description(
			"Work with Benchling's field team to model customer lab workflows in our platform.",
			[
				"Configure schemas and workflows for scientific customers",
				"Translate bench protocols into structured data models",
				"Shadow customer calls and write up findings",
			],
			"Remote within the US. Comfortable with spreadsheets, basic scripting, and talking to scientists.",
		),
	},
	{
		key: "adzuna:us:5847155128",
		source: "adzuna",
		board: "us",
		company: "Benchling",
		title: "Solutions Intern, Scientific Data (Remote US)",
		location: "Boston, Suffolk County",
		url: "https://www.adzuna.com/land/ad/5847155128",
		postedAt: "2026-08-14",
		discoveredAt: "2026-08-17T20:39:16+00:00",
		descriptionHtml: description(
			"Work with Benchling's field team to model customer lab workflows in our platform.",
			["Configure schemas and workflows for scientific customers"],
			"Remote within the US.",
		),
	},
	{
		key: "ashby:asimov:55b1d902",
		source: "ashby",
		board: "asimov",
		company: "Asimov",
		title: "Research Intern, Synthetic Biology",
		location: "Boston, MA",
		url: "https://jobs.ashbyhq.com/asimov/55b1d902",
		postedAt: "2026-08-15",
		discoveredAt: "2026-08-17T11:22:36+00:00",
		descriptionHtml: description(
			"Asimov designs genetic circuits for mammalian cell engineering. Our interns run real experiments.",
			[
				"Execute transfection and flow cytometry experiments",
				"Analyze circuit performance data in Python",
				"Keep protocol records in the electronic notebook",
			],
			"On-site in Boston. Coursework in biochemistry or molecular biology expected.",
		),
	},
	{
		key: "greenhouse:ginkgobioworks:5185277781",
		source: "greenhouse",
		board: "ginkgobioworks",
		company: "Ginkgo Bioworks",
		title: "Government Programs Analyst, Biosecurity",
		location: "Boston, MA",
		url: "https://boards.greenhouse.io/ginkgobioworks/jobs/5185277781",
		postedAt: "2026-08-04",
		discoveredAt: "2026-08-14T13:02:19+00:00",
		descriptionHtml: description(
			"Support Concentric's biosecurity programs with federal and state partners.",
			[
				"Prepare briefing materials for government stakeholders",
				"Coordinate reporting across public health programs",
				"Track deliverables against contract milestones",
			],
			"Must be a US citizen and able to obtain and maintain a security clearance.",
		),
	},
	{
		key: "greenhouse:tesseratherapeutics:4901880",
		source: "greenhouse",
		board: "tesseratherapeutics",
		company: "Tessera Therapeutics",
		title: "Manufacturing Associate II, Drug Substance",
		location: "Somerville, MA",
		url: "https://boards.greenhouse.io/tesseratherapeutics/jobs/4901880",
		postedAt: "2026-08-06",
		discoveredAt: "2026-08-15T09:41:19+00:00",
		descriptionHtml:
			// Render-safety canary: a Posting is employer-authored HTML, so the detail view
			// renders it inside a sandboxed frame. If that sandbox ever regressed, this line
			// would rewrite the document title instead of staying inert text.
			`<p>Operate upstream and downstream unit operations in a GMP suite.</p>` +
			`<script>document.title = "sandbox escaped";</script>` +
			`<img src="x" onerror="document.title='sandbox escaped'">` +
			`<ul><li>Execute batch records under cGMP</li><li>Rotating 12-hour shifts, nights and weekends</li>` +
			`<li>Gowning and cleanroom qualification required</li></ul>` +
			`<p>Full-time permanent role. Associate degree plus 2 years GMP manufacturing experience required.</p>`,
	},
	{
		key: "greenhouse:generatebiomedicines:4728990",
		source: "greenhouse",
		board: "generatebiomedicines",
		company: "Generate Biomedicines",
		title: "Undergraduate Summer Research Program 2027 (Housing Provided)",
		location: "San Diego, CA",
		url: "https://boards.greenhouse.io/generatebiomedicines/jobs/4728990",
		postedAt: "2026-08-12",
		discoveredAt: "2026-08-15T09:41:24+00:00",
		descriptionHtml: description(
			"A ten-week residential research program for undergraduates at our San Diego site.",
			[
				"Own a scoped research question with a staff scientist mentor",
				"Learn protein expression and characterization workflows",
				"Present results at the closing symposium",
			],
			"<strong>Program housing is provided</strong> for all participants, plus a travel stipend. Open to rising juniors and seniors.",
		),
	},
	{
		key: "ashby:asimov:77c2ef15",
		source: "ashby",
		board: "asimov",
		company: "Asimov",
		title: "Bioinformatics Intern",
		location: "Boston, MA",
		url: "https://jobs.ashbyhq.com/asimov/77c2ef15",
		postedAt: "2026-08-17",
		discoveredAt: "2026-08-18T06:04:52+00:00",
		descriptionHtml: description(
			"Join the computational team supporting Asimov's mammalian cell engineering platform.",
			[
				"Process sequencing data from genetic circuit experiments",
				"Maintain analysis notebooks used by the wet lab",
				"Help benchmark new alignment tooling",
			],
			"Summer 2027 in Boston. Python coursework expected; bioinformatics experience is a plus, not a requirement.",
		),
	},
	{
		key: "greenhouse:dynotherapeutics:4455999",
		source: "greenhouse",
		board: "dynotherapeutics",
		company: "Dyno Therapeutics",
		title: "Lab Operations Coordinator (Contract)",
		location: "Chicago, MA",
		url: "https://boards.greenhouse.io/dynotherapeutics/jobs/4455999",
		postedAt: "2026-08-03",
		discoveredAt: "2026-08-16T07:15:58+00:00",
		descriptionHtml: description(
			"Keep the Chicago lab stocked, scheduled, and audit-ready.",
			[
				"Manage consumable inventory and vendor orders",
				"Coordinate instrument service visits",
				"Maintain safety documentation",
			],
			"Twelve-month contract through a staffing partner. On-site five days a week.",
		),
	},

	// The last three exist for the board guard below. Greenhouse and Ashby tokens are single
	// lowercase words that read almost like a name, so a fixture made only of those cannot
	// show what a raw board token on screen looks like. Workday's is `<tenant>.wd<N>~<site>`
	// and Lever's is whatever the employer typed, which is where the damage was: the queue
	// rendered `Amgen.wd1~Careers` and `LyciaTherapeutics` at an Owner for as long as the
	// board-token fallback existed, through a green build every time.
	{
		key: "workday:amgen.wd1~Careers:R-98765",
		source: "workday",
		board: "amgen.wd1~Careers",
		company: "Amgen",
		title: "Process Development Intern, Drug Substance Technologies",
		location: "Cambridge, MA",
		url: "https://amgen.wd1.myworkdayjobs.com/en-US/Careers/job/R-98765",
		postedAt: "2026-08-12",
		discoveredAt: "2026-08-18T09:41:07+00:00",
		descriptionHtml: description(
			"Amgen's Cambridge process development group runs the assays that decide whether a molecule scales.",
			[
				"Run bench-scale purification experiments alongside a staff scientist",
				"Write Python to reduce chromatography data into the group's reporting templates",
				"Present a summer project to the process development team",
			],
			"Twelve-week summer 2027 internship. Open to undergraduates returning to a degree program in the fall.",
		),
	},
	{
		key: "lever:LyciaTherapeutics:6b4d21fe",
		source: "lever",
		board: "LyciaTherapeutics",
		company: "Lycia Therapeutics",
		title: "Research Associate I, Protein Sciences",
		location: "South San Francisco, CA",
		url: "https://jobs.lever.co/LyciaTherapeutics/6b4d21fe",
		postedAt: "2026-08-10",
		discoveredAt: "2026-08-18T09:41:12+00:00",
		descriptionHtml: description(
			"Lycia's protein sciences team supports the lysosomal targeting chimera platform end to end.",
			[
				"Express and purify recombinant proteins for the discovery groups",
				"Keep binding-assay records current in the electronic notebook",
			],
			"On-site in South San Francisco five days a week.",
		),
	},
	{
		// company NULL: a board polled with no `sources.names` entry, or a view built without a
		// resolvable Profile. The UI has no name to show and must say so — "via Workday" — not
		// dress the routing token up as one.
		key: "workday:roche.wd3~ROG-A2O-GENE:202608-114",
		source: "workday",
		board: "roche.wd3~ROG-A2O-GENE",
		company: null,
		title: "Summer Intern, Computational Biology",
		location: "Basel, Switzerland",
		url: "https://roche.wd3.myworkdayjobs.com/en-US/ROG-A2O-GENE/job/202608-114",
		postedAt: "2026-08-06",
		discoveredAt: "2026-08-18T09:41:19+00:00",
		descriptionHtml: description(
			"Join a computational biology group working on target discovery in Basel.",
			["Analyse single-cell datasets under the supervision of a senior scientist"],
			"On-site in Basel. Relocation is not supported for interns.",
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
		case "greenhouse:ginkgobioworks:5185285007":
			return {
				...base,
				status: "suitable",
				summary: "Python and lab automation requirements have direct Profile evidence; the Boston internship is open.",
				evidence: [
					evidence("Python glue and data pipelines", "Python and pipeline work are listed in Profile experience.", "Profile experience"),
					evidence("Undergraduate internship", "Education lists an undergraduate program continuing through 2027.", "Profile education"),
				],
			};
		case "greenhouse:ginkgobioworks:5185301442":
			return {
				...base,
				status: "not_suitable",
				summary: "The listing requires a PhD and 8+ years of industry experience.",
				evidence: [],
				conflicts: ["Required PhD and 8+ years of industry experience are not met."],
			};
		case "greenhouse:generatebiomedicines:4728845":
			return {
				...base,
				status: "needs_review",
				summary: "Pipeline and validation work overlap with the Profile; the six-month co-op timing needs confirmation.",
				evidence: [evidence("Data ingestion and validation", "Pipeline and validation work are listed in Profile experience.", "Profile experience")],
				unknowns: ["Course load compatibility with a six-month co-op is unknown."],
			};
		case "greenhouse:tesseratherapeutics:4901233":
			return {
				...base,
				status: "suitable",
				summary: "Python and reporting work overlap with the QC automation requirements at this open Somerville internship.",
				evidence: [
					evidence("QC reporting in Excel", "Spreadsheet reporting is listed in Profile skills and projects.", "Profile skills"),
					evidence("Summer undergraduate internship", "Profile education is an undergraduate program.", "Profile education"),
				],
			};
		case "greenhouse:dynotherapeutics:4455102":
			return {
				...base,
				status: "needs_review",
				summary: "The role names sequence-model training; the Profile shows adjacent automation work but no direct model-training evidence.",
				evidence: [],
				unknowns: ["Published deep-learning or protein-design experience is not established in the Profile."],
			};
		case "greenhouse:recursionpharmaceuticals:6612884":
			return {
				...base,
				status: "not_suitable",
				summary: "The open listing is on-site in Salt Lake City and provides no housing signal.",
				evidence: [],
				conflicts: ["Location and housing requirements conflict with the Profile's Boston or remote constraints."],
			};
		case "greenhouse:recursionpharmaceuticals:6612991":
			return {
				...base,
				status: "suitable",
				summary: "Remote ETL and pipeline testing requirements have direct Profile evidence.",
				evidence: [evidence("ETL and testing", "ETL pipeline and test work are listed in Profile experience.", "Profile experience")],
			};
		case "ashby:benchling:9f2c41a8":
			return {
				...base,
				status: "not_suitable",
				summary: "The open role requires on-site work in San Francisco without relocation support.",
				evidence: [],
				conflicts: ["The work location conflicts with the Profile's Boston or remote constraints."],
			};
		case "ashby:benchling:1a77b0e3":
			return {
				...base,
				status: "suitable",
				summary: "Remote scientific-data workflow work matches the Profile's data and scripting experience.",
				evidence: [evidence("Scientific data workflows", "Data modeling and scripting projects are listed in Profile experience.", "Profile projects")],
			};
		case "adzuna:us:5847155128":
			return {
				...base,
				status: "not_suitable",
				summary: "This aggregator record duplicates the Benchling listing already found on the employer board.",
				evidence: [],
				conflicts: ["Duplicate of the canonical employer listing."],
			};
		case "ashby:asimov:55b1d902":
			return {
				...base,
				status: "unassessed",
				summary: "No assessment has been recorded for this listing yet.",
				evidence: [],
				lastVerifiedAt: null,
			};
		case "greenhouse:ginkgobioworks:5185277781":
			return {
				...base,
				status: "not_suitable",
				summary: "The listing requires US citizenship and a security clearance.",
				evidence: [],
				conflicts: ["Work-authorization requirements conflict with the Profile."],
			};
		case "greenhouse:tesseratherapeutics:4901880":
			return {
				...base,
				status: "not_suitable",
				summary: "The permanent manufacturing role requires an associate degree and two years of GMP experience.",
				evidence: [],
				conflicts: ["Required manufacturing experience is not established in the Profile."],
			};
		case "greenhouse:generatebiomedicines:4728990":
			return {
				...base,
				status: "needs_review",
				summary: "The mentored research program has housing support; protein-workflow experience needs a closer review.",
				evidence: [evidence("Undergraduate research program", "The Profile records an undergraduate science education.", "Profile education")],
				unknowns: ["Direct protein-expression experience is not established in the Profile."],
				opportunityType: "program",
			};
		case "ashby:asimov:77c2ef15":
			return {
				...base,
				status: "suitable",
				summary: "Sequencing-data and Python coursework requirements overlap with Profile evidence.",
				evidence: [evidence("Sequencing data and Python", "Python coursework and data-analysis projects are listed in the Profile.", "Profile coursework and projects")],
			};
		case "greenhouse:dynotherapeutics:4455999":
			return {
				...base,
				status: "not_suitable",
				summary: "The contract role centers on inventory coordination and a twelve-month commitment.",
				evidence: [],
				conflicts: ["The commitment and role scope conflict with the Profile's internship target."],
			};
		case "workday:amgen.wd1~Careers:R-98765":
			return {
				...base,
				status: "suitable",
				summary: "Python reporting and a Cambridge summer internship match explicit Profile evidence.",
				evidence: [evidence("Python reporting", "Python data reduction is listed in Profile experience.", "Profile experience")],
			};
		case "lever:LyciaTherapeutics:6b4d21fe":
			return {
				...base,
				status: "not_suitable",
				summary: "The open role is on-site in South San Francisco five days a week.",
				evidence: [],
				conflicts: ["The work location conflicts with the Profile's Boston or remote constraints."],
			};
		case "workday:roche.wd3~ROG-A2O-GENE:202608-114":
			return {
				...base,
				status: "not_suitable",
				summary: "The listing is closed and relocation is not supported for interns.",
				evidence: [],
				conflicts: ["The employer listing is closed."],
				listingStatus: "closed",
			};
		default:
			return {
				...base,
				status: "unassessed",
				summary: "No assessment has been recorded for this listing yet.",
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
	{ key: "ashby:benchling", status: "partial", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: FIXTURE_VERIFIED_AT, count: 2, message: "One listing detail needs review.", coverage: fixtureCoverage("ashby:benchling", "partial") },
	{ key: "ashby:asimov", status: "ok", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: FIXTURE_VERIFIED_AT, count: 2, message: null, coverage: fixtureCoverage("ashby:asimov", "ok") },
	{ key: "greenhouse:ginkgobioworks", status: "ok", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: FIXTURE_VERIFIED_AT, count: 3, message: null, coverage: fixtureCoverage("greenhouse:ginkgobioworks", "ok") },
	{ key: "workday:amgen.wd1~Careers", status: "ok", lastAttemptAt: FIXTURE_VERIFIED_AT, lastSuccessAt: FIXTURE_VERIFIED_AT, count: 1, message: null, coverage: fixtureCoverage("workday:amgen.wd1~Careers", "ok") },
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
		postingKey: "adzuna:us:5847155128",
		stage: "hard_filter",
		verdict: "kill",
		rule: "duplicate",
		score: null,
		reason:
			"the same role as 'Solutions Intern, Scientific Data (Remote US)' at Benchling, already discovered from Ashby",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:03+00:00",
	},
	{
		postingKey: "greenhouse:ginkgobioworks:5185285007",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Boston MA, commutable from Chicago; internship for summer 2027; no degree floor stated",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:04+00:00",
	},
	{
		postingKey: "greenhouse:ginkgobioworks:5185285007",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 88,
		reason:
			"Lab informatics plus automation is the exact AI-adjacent target; Python and dashboard work maps to the profile's data-pipeline experience, and it is a commutable Boston site",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:14:22+00:00",
	},
	{
		postingKey: "greenhouse:ginkgobioworks:5185301442",
		stage: "hard_filter",
		verdict: "kill",
		rule: "education_fit",
		score: null,
		reason: "requires a completed PhD and 8+ years of industry experience; profile is a BS expected May 2027",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:05+00:00",
	},
	{
		postingKey: "greenhouse:generatebiomedicines:4728845",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Somerville MA, commutable; co-op open to undergraduates",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:06+00:00",
	},
	{
		postingKey: "greenhouse:generatebiomedicines:4728845",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 84,
		reason:
			"Data pipeline and validation work against assay data lines up with the profile's automation experience; six-month co-op timing needs a check against the spring 2027 course load",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:14:31+00:00",
	},
	{
		postingKey: "greenhouse:tesseratherapeutics:4901233",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Somerville MA, commutable; summer 2027 internship open to rising juniors and seniors",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:08+00:00",
	},
	{
		// A historical Match Score recorded under the previous filters_version, with the
		// hard_filter decision above already replayed at the current one. It decodes and orders
		// nothing.
		postingKey: "greenhouse:tesseratherapeutics:4901233",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 79,
		reason:
			"NGS QC automation is a real match for the profile's Python and Excel reporting work; the biochemistry coursework covers the assay context, though no prior NGS experience is listed",
		filtersVersion: PREVIOUS_FILTERS,
		decidedAt: "2026-08-17T19:14:39+00:00",
	},
	{
		postingKey: "greenhouse:dynotherapeutics:4455102",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Chicago MA, commutable; PhD preference is stated as a preference, not a requirement",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:10+00:00",
	},
	{
		postingKey: "greenhouse:dynotherapeutics:4455102",
		stage: "llm_score",
		verdict: "kill",
		rule: null,
		score: 58,
		reason:
			"Research group expects published deep-learning work and literature reproduction; the profile's ML exposure is agentic tooling rather than model training, so this reads as a stretch that would displace better-fitting applications",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:14:47+00:00",
	},
	{
		postingKey: "greenhouse:recursionpharmaceuticals:6612884",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason:
			"on-site in Salt Lake City UT and the posting states interns arrange their own housing; outside Boston-metro with no housing signal",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:12+00:00",
	},
	{
		postingKey: "greenhouse:recursionpharmaceuticals:6612991",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "remote within the US; no degree floor or citizenship requirement stated",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:13+00:00",
	},
	{
		postingKey: "greenhouse:recursionpharmaceuticals:6612991",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 81,
		reason:
			"Remote ETL internship with no experience floor; pipeline and testing work matches the profile, and remote removes the housing question entirely",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:14:55+00:00",
	},
	{
		postingKey: "ashby:benchling:9f2c41a8",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason: "on-site five days a week in San Francisco CA with no relocation; not Boston-metro or remote",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:15+00:00",
	},
	{
		postingKey: "ashby:benchling:1a77b0e3",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "remote within the US; undergraduate-friendly scope",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T06:30:02+00:00",
	},
	{
		postingKey: "ashby:asimov:55b1d902",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Boston MA, commutable; biochemistry coursework is the stated expectation",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T06:30:04+00:00",
	},
	{
		postingKey: "greenhouse:ginkgobioworks:5185277781",
		stage: "hard_filter",
		verdict: "kill",
		rule: "work_authorization",
		score: null,
		reason: "requires US citizenship and an obtainable security clearance; profile is an F-1 international student",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:17+00:00",
	},
	{
		postingKey: "greenhouse:tesseratherapeutics:4901880",
		stage: "hard_filter",
		verdict: "kill",
		rule: "education_fit",
		score: null,
		reason:
			"requires an associate degree plus 2 years of GMP manufacturing experience; profile is a BS expected May 2027 with no industry manufacturing history",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:18+00:00",
	},
	{
		postingKey: "greenhouse:generatebiomedicines:4728990",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason: "on-site in San Diego CA; outside Boston-metro and not remote",
		filtersVersion: PREVIOUS_FILTERS,
		decidedAt: "2026-08-16T18:12:40+00:00",
	},
	{
		postingKey: "greenhouse:generatebiomedicines:4728990",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason:
			"replayed under the summer-2027 housing clause: San Diego is out of metro, but the program provides housing and a travel stipend, so the location rule no longer fires",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:20+00:00",
	},
	{
		postingKey: "greenhouse:generatebiomedicines:4728990",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 76,
		reason:
			"Residential undergraduate research program with housing provided; protein characterization is adjacent to the biochemistry coursework, and the mentored research framing fits a rising senior",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:15:09+00:00",
	},
	{
		postingKey: "greenhouse:dynotherapeutics:4455999",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Chicago MA, commutable; no degree floor stated",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:00:22+00:00",
	},
	{
		postingKey: "greenhouse:dynotherapeutics:4455999",
		stage: "llm_score",
		verdict: "kill",
		rule: null,
		score: 42,
		reason:
			"Contract lab-operations coordination through a staffing partner: on-site inventory and scheduling work with no research or data component, and a twelve-month commitment that collides with the spring semester",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-17T19:15:17+00:00",
	},

	// The Workday and Lever Postings: one through to the Review Queue, two killed, so a board
	// token has a route to reach the queue, the inspector and Focus if the UI ever prints one.
	{
		postingKey: "workday:amgen.wd1~Careers:R-98765",
		stage: "hard_filter",
		verdict: "pass",
		rule: null,
		score: null,
		reason: "Cambridge MA, commutable from Chicago; summer 2027 internship open to undergraduates",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T09:42:01+00:00",
	},
	{
		postingKey: "workday:amgen.wd1~Careers:R-98765",
		stage: "llm_score",
		verdict: "queue",
		rule: null,
		score: 83,
		reason:
			"Bench purification work with a Python reporting component; the data-reduction half maps onto the profile's automation experience and the site is commutable, though the wet-lab half is a step outside it",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T09:48:30+00:00",
	},
	{
		postingKey: "lever:LyciaTherapeutics:6b4d21fe",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason: "on-site five days a week in South San Francisco CA; not Boston-metro or remote",
		filtersVersion: CURRENT_FILTERS,
		decidedAt: "2026-08-18T09:42:04+00:00",
	},
	{
		postingKey: "workday:roche.wd3~ROG-A2O-GENE:202608-114",
		stage: "hard_filter",
		verdict: "kill",
		rule: "location",
		score: null,
		reason: "on-site in Basel with no relocation support for interns; not Boston-metro or remote",
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

		const insertJev = database.prepare(
			`INSERT INTO jev_triage (
			   posting_key, mode, state, decision, fit_probability, fit_score, primary_rule,
			   exclusions, review_flags, diagnostic_flags, qualifier_version, assessment_key,
			   input_version, policy_hash, model_id, as_of_month, decided_at, reason
			 ) VALUES (
			   $postingKey, $mode, $state, $decision, $fitProbability, $fitScore, $primaryRule,
			   $exclusions, $reviewFlags, $diagnosticFlags, $qualifierVersion, $assessmentKey,
			   $inputVersion, $policyHash, $modelId, $asOfMonth, $decidedAt, $reason
			 )`,
		);
		for (const row of JEV_TRIAGE) {
			insertJev.run({
				postingKey: row.postingKey,
				mode: row.mode,
				state: row.state,
				decision: row.decision,
				fitProbability: row.fitProbability,
				fitScore: row.fitScore,
				primaryRule: row.primaryRule,
				exclusions: JSON.stringify(row.exclusions),
				reviewFlags: JSON.stringify(row.reviewFlags),
				diagnosticFlags: JSON.stringify(row.diagnosticFlags),
				qualifierVersion: row.qualifierVersion,
				assessmentKey: row.assessmentKey,
				inputVersion: row.inputVersion,
				policyHash: row.policyHash,
				modelId: row.modelId,
				asOfMonth: row.asOfMonth,
				decidedAt: row.decidedAt,
				reason: row.reason,
			});
		}

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
