# Shared contracts

This file records boundaries shared by Python, the dashboard and persisted
history. Implementation detail belongs beside the code; this file keeps only
cross-tool contracts that cannot drift independently.

## Profile and Install

One Install belongs to one person. Profiles name that person's searches; they
are not tenants. A Profile contains `targeting.yaml`, `constraints.yaml` and
`resume.yaml`. `--profile` takes a name, never a path. A directory without
`targeting.yaml` is not a Profile.

One decisions store and one Track store belong to one Profile. The `.profile`
stamp is the lock. Optional `profile_id` row fields are evidence; absent means
unknown. See ADR-0002.

## Append-only records

`data/` is append-only. Replays append; they never rewrite a day file.

A Filter Decision has `stage=hard_filter` with `verdict=pass|kill`. New Hard
Filter rows do not carry `score`. Historical `stage=llm_score` rows with
`verdict=queue|kill` and scores still decode, so the disposable view retains its
nullable `score` column, but nothing writes a new scoring row. Effective state
is the latest row per `(posting_key, stage)`, ordered by timestamp and append
order. Every Posting reaching Hard Filters gets a Filter Decision; no silent
drop is allowed. The current code revision input to `filters_version` is
`20-no-trained-qualifier`.

Hard Filter pass rows may carry `facts`. In Jev mode every new pass or kill also
carries the reserved `jev` assessment-key/fallback token and `jev_input`
input-version/unavailable token. Other facts remain observational pass evidence.
They do not change `filters_version`.

Track events are `approve`, `reject`, `prepare`, `fill`, `submit`, `restore`,
`outcome` and `withdraw`. Their folded states are `approved`, `rejected`,
`prepared`, `filled`, `submitted`, `queued`, `concluded` and `withdrawn`.
`prepare` and `fill` use the pipeline actor; current `submit` is the Owner's
manual applied report. Historical pipeline-actor submit rows remain readable.
Any valid event may follow any state because work can happen outside Venator.

A historical latest `llm_score=queue` may derive `queued` when there is no Track
event. Historical scores never rank the dashboard.

## Runtime probe

`python -m venator.llm.probe [--lane NAME | --all] [--json]` reports structured
lane status. `state` is one of `available`, `not_installed`, `not_spawnable`, `not_signed_in`, `not_configured`, `configured`, `timed_out`, `failed`.

The command exits zero when the probe itself ran, even if the runtime is not
ready. A key endpoint is reported configured without contacting it. CLI child
environments are allowlists. Provider response text must never enter the
append-only store.

TypeSafe System One is not a runtime lane. Only an authorized Jev execute reads
`TYPESAFE_API_KEY`.

## Qualification and Jev

Jev rows are append-only under `data/qualifications/`. Shadow rows are triage
provenance only and never change a Filter Decision. A Jev-only assessment cannot
publish `suitable`: prioritize and review map to `needs_review`; a policy
exclusion maps to `not_suitable`.

Current success requires validated typed output, current input and release
identity, and an assessment key. Older compatible success may be stale but is
never relabelled current. Raw failed HTTP bodies are not persisted.

Invariant I10 permits only the student projection, Posting title and
description, and allowed spans on the Jev wire. Policy is closed enums rather
than free text.

## Disposable view schema

`python -m venator.view.build` atomically rebuilds the disposable SQLite view.
The dashboard verifies named columns before reading it.

```sql
CREATE TABLE postings (
  key TEXT PRIMARY KEY,
  source TEXT,
  board TEXT,
  company TEXT,
  title TEXT,
  location TEXT,
  url TEXT,
  posted_at TEXT,
  discovered_at TEXT,
  description_html TEXT
);
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY,
  posting_key TEXT,
  stage TEXT,
  verdict TEXT,
  rule TEXT,
  score INTEGER,
  reason TEXT,
  filters_version TEXT,
  decided_at TEXT
);
CREATE TABLE track_events (
  id INTEGER PRIMARY KEY,
  posting_key TEXT,
  event TEXT,
  actor TEXT,
  detail TEXT,
  at TEXT
);
CREATE TABLE application_states (
  posting_key TEXT PRIMARY KEY,
  state TEXT,
  detail TEXT,
  since TEXT
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  at TEXT,
  status TEXT,
  stage TEXT
);
```

The assessment and Jev DDL below is the live view contract. The historical Jev
spec retains the pre-retirement assessment schema as design history.

```sql
CREATE TABLE assessments (
  posting_key TEXT PRIMARY KEY,
  status TEXT,
  summary TEXT,
  evidence TEXT,
  conflicts TEXT,
  unknowns TEXT,
  listing_status TEXT,
  last_verified_at TEXT,
  description_kind TEXT,
  apply_url TEXT,
  opportunity_type TEXT,
  input_version TEXT,
  assessed_by TEXT NOT NULL DEFAULT 'deterministic'
    CHECK (assessed_by IN ('deterministic', 'jev')),
  assessed_as_of TEXT
);
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
-- Exactly one row: activation is release-wide, even when no promoted results are current.
CREATE TABLE jev_selection (mode TEXT NOT NULL CHECK (mode IN ('shadow', 'promoted')));
-- One reason per Posting Jev cannot select; generated during view build.
CREATE TABLE jev_skip (posting_key TEXT PRIMARY KEY, reason TEXT NOT NULL);
```

```sql
CREATE TABLE source_health (
  source_key TEXT PRIMARY KEY, status TEXT, last_attempt_at TEXT,
  last_success_at TEXT, count INTEGER, message TEXT,
  known_jobs INTEGER, full_verified_details INTEGER, needs_detail_check INTEGER
);
```

Dashboard Posting status first follows application progress (`applied`) or a Hard
Filter kill (`hard-killed`). A remaining closed listing is `closed`, even with a
historical or current Jev result. For an open or unknown listing with a Hard
Filter pass, current selected-release Jev results route to `queued`,
`needs-review`, or `hard-killed`; without one, a `jev_skip` reason routes to
`no-text`, `too-long`, or `protected`, otherwise `unscored` (Awaiting Jev).
Without a Hard Filter pass it is `not-filtered`. Only `unscored` is awaiting Jev.
`jev_selection` chooses the active release mode for both lists and diagnostics,
regardless of how many results are current. Closed and unscoreable Postings
remain searchable in the inspector and Employers. Résumé assessment is margin
evidence, not a list decision. Review ordering is verification recency, never
a Jev number or historical score.

## Dashboard HTTP surfaces

The server binds to `127.0.0.1` and mounts action routers before the read-only
router. Mount order is load-bearing because Hono CORS middleware can answer a
preflight before a sibling router sees it.

| Surface | Methods | Effect |
|---|---|---|
| `/api/runs` | `POST`, plus process-local status `GET`s | starts only `fetch-and-filter` (`discover,filters,view`) from a single-use plan token |
| `/api/applications` | `POST` | status/file/prepare/save/dismiss/restore/applied/handoff through `venator.applications` |
| `/api/onboarding` | `POST` | writes Profile files only under the Install's application-data directory; read/probe adapters write nothing |
| `/api` | `GET` | reads `build/venator.db`; spawns and writes nothing |

Action surfaces require their local-action header and an allowed local Origin.
Onboarding Profile writes are serialized per Profile in one server process,
refuse lone surrogates and symbolic links, stage the YAML, load it through
`venator.profile`, and rename only after successful load-back. The in-process
writer does not claim to serialize separate server processes.

The run surface accepts only `fetch-and-filter`. A plan token is single-use and
the start rechecks what was planned. Stopping targets the complete child process
tree.

## Application and browser boundaries

`venator.applications prepare` reverifies the exact Posting, reruns Hard Filters
and writes a versioned bundle. Every opened/downloaded file is checked against
its hash and Posting/Profile input versions.

`venator.browser.fill` is a Dry Run:

- every FillPlan has literal `never_submit: true` and the driver refuses any
  other value;
- `POST`, `PUT`, `PATCH` and `DELETE` requests are aborted;
- submit events are cancelled and counted;
- file fields are skipped;
- CAPTCHA presence is recorded and never solved.

Visible handoff is separate. It may fill confirmed Greenhouse fields and upload
verified prepared documents. It does not intercept the person's final submit;
no Venator code clicks it.

Screening answers derive from confirmed Profile facts or remain absent. A
sponsorship question is answered only when
`work_authorization.requires_sponsorship: true`; the planner cannot answer it
“No.” EEO values are never fabricated.

## Command output streams

Stage identity and store resolution are announced on stderr so stdout remains
machine-readable where a command publishes JSON. The scheduled loop emits these
progress lines, flushed as written:

- `<stage>: starting` on stdout
- `<stage>: ok` on stdout
- `<stage>: ERROR — <message>` on stderr

Readers ignore unknown lines. Exit status and `data/runs.jsonl` remain the
authority. Heartbeat rows carry `at`, `status`, `stage` and optional `error` and
are scrubbed at the writer.

## Toolchains

Python requires 3.13+ and `uv`; no Python linter or type checker is configured.
The UI uses pnpm, strict TypeScript and anti-slop lint rules. UI changes must
pass lint and typecheck before they are done. `ui/src/labels.ts` is the only
place a stored identifier becomes visible English.
