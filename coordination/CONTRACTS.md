# Shared contracts

These are the rules that Python, the dashboard and the stored history all depend on.
If one side changes them, the others break. Details that only matter to one module
live next to that module's code.

## Profile and Install

One Install belongs to one person. Profiles are that person's searches, not separate
users. A Profile is a directory with `targeting.yaml`, `constraints.yaml` and
`resume.yaml`. `--profile` takes a name, never a path. A directory without
`targeting.yaml` is not a Profile.

Each decisions store and each Track store belongs to one Profile. The `.profile`
stamp in the directory is the lock. A `profile_id` on a row is evidence only; if it
is missing, the owner of that row is unknown. See ADR-0002.

## Append-only records

`data/` is append-only. A replay adds rows. It never rewrites a day file.

A Filter Decision has `stage=hard_filter` and `verdict=pass|kill`. New Hard Filter
rows have no `score`. Old `stage=llm_score` rows with `verdict=queue|kill` and a score
still load, which is why the view keeps a nullable `score` column. Nothing writes a
new scoring row.

The effective decision is the latest row per `(posting_key, stage)`, by timestamp and
then append order. Every Posting that reaches the Hard Filters gets a Filter Decision.
Nothing is dropped silently.

The code half of `filters_version` is `FILTERS_REVISION` in
`src/venator/match/store.py`. Bump it when filter code changes.

Hard Filter pass rows may carry `facts`. In Jev mode, every new pass or kill also
carries two reserved facts: `jev` (the assessment key, or a fallback token) and
`jev_input` (the input version, or an unavailable token). Other facts are extra
evidence on a pass. None of them change `filters_version`.

Track events are `approve`, `reject`, `prepare`, `fill`, `submit`, `restore`,
`outcome` and `withdraw`. They fold into the states `approved`, `rejected`,
`prepared`, `filled`, `submitted`, `queued`, `concluded` and `withdrawn`. `prepare`
and `fill` are written by the pipeline. `submit` is the Owner's own report that they
applied. Older `submit` rows written by the pipeline still load. Any valid event may
follow any state, because people also apply outside Venator.

If a Posting has no Track event and its latest old `llm_score` row says `queue`, it
shows as `queued`. Old scores never decide the order of a list.

## Runtime probe

`python -m venator.llm.probe [--lane NAME | --all] [--json]` reports each lane's
status. `state` is one of `available`, `not_installed`, `not_spawnable`, `not_signed_in`, `not_configured`, `configured`, `timed_out`, `failed`.

The command exits 0 whenever the probe itself ran, even if a runtime isn't ready. A
key endpoint shows as `configured` without being contacted. Child processes get an
allowlisted environment. Text from a provider's response never goes into the
append-only store.

TypeSafe System One is not a lane. Only an explicit Jev execute reads
`TYPESAFE_API_KEY`.

## Qualification and Jev

Jev rows are appended under `data/qualifications/`. Shadow rows record how Jev
sorted a Posting. They never change a Filter Decision. A Jev-only assessment can't
say `suitable`: prioritize and review become `needs_review`, and a policy exclusion
becomes `not_suitable`.

A result counts as current only when the typed output validated, the input and the
release still match, and it has an assessment key. An older compatible result can be
stale. It is never relabelled current. Raw bodies from failed HTTP calls are not
saved.

Invariant I10: only the student projection, the Posting title and description, and
allowed spans go over the wire to Jev. Policy is sent as closed enums, never free
text.

## View schema

`python -m venator.view.build` rebuilds the SQLite view in one atomic step. The
dashboard checks the columns it reads when it opens the file (`ui/server/db.ts`). The
tables it relies on:

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
-- The latest Hard Filter decision id per Posting; the lists join on it.
CREATE TABLE hard_filter_latest (posting_key TEXT PRIMARY KEY, decision_id INTEGER NOT NULL);
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
  stage TEXT,
  pause_reason TEXT,
  waiting INTEGER
);
```

The assessment and Jev tables:

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

`source_health`'s last three columns came later. The dashboard reads coverage as
unmeasured in a view built before them.

### Posting status

The dashboard works out one status per Posting (`STATUS_SQL` in
`ui/server/queries.ts`), checking in this order:

1. Application progress: `applied`.
2. A Hard Filter kill: `hard-killed`.
3. A closed listing: `closed`, even if Jev has a result for it.
4. A Hard Filter pass with a current Jev result from the selected release:
   prioritize is `queued`, review is `needs-review`, exclude is `hard-killed`.
5. A pass with a `jev_skip` reason: `protected`, `too-long` or `no-text`.
6. Any other pass: `unscored`. This is the Awaiting Jev list.
7. No Hard Filter pass at all: `not-filtered`.

`jev_selection` picks the release mode for both the lists and Jev diagnostics, no
matter how many results are current. Closed and skipped Postings stay searchable in
the filter inspector and on the Employers page. The résumé assessment shows up as a
margin note, not as a list. Lists sort by when a Posting was last verified, never by a
Jev number or an old score.

## Dashboard HTTP surfaces

The server listens on `127.0.0.1` only. The action routers are mounted before the
read-only one. The order matters: Hono's CORS middleware answers a preflight itself,
so if `/api` came first it would answer every write path's preflight with
`GET, OPTIONS`.

| Surface | Methods | What it does |
|---|---|---|
| `/api/runs` | `POST`, plus `GET` for this process's own run state | starts `fetch-and-filter` (`discover,filters,view`) or `jev-it` (Jev, then a view rebuild) from a single-use plan token |
| `/api/applications` | `POST`, plus `GET` for status and prepared files | status, file, prepare, save, dismiss, restore, applied and handoff through `venator.applications` |
| `/api/onboarding` | `POST`, plus `GET /settings` | writes Profile files under the Install's application data directory, saves the assistant choice and the Jev key; its read and probe routes write nothing |
| `/api` | `GET` | reads `build/venator.db`; starts nothing and writes nothing |

Each action surface needs its own header (`X-Venator-Run`, `X-Venator-Application`,
`X-Venator-Onboarding`) and an allowed local `Origin`.

Onboarding writes one Profile at a time inside a server process. It refuses lone
surrogates and symbolic links, stages the YAML, loads it back through
`venator.profile`, and renames it into place only if that load works. Two separate
server processes are not coordinated.

A plan token works once, and starting a run checks the plan again. Stopping a run
signals the whole child process tree.

## Applications and the browser

`venator.applications prepare` checks the exact Posting again, reruns the Hard
Filters and writes a new versioned bundle. Every file that is opened or downloaded
is checked against its hash and against the Posting and Profile input versions.

`venator.browser.fill` is a Dry Run:

- every FillPlan has the literal `never_submit: true`, and the driver refuses
  anything else;
- `POST`, `PUT`, `PATCH` and `DELETE` requests are aborted;
- submit events are cancelled and counted;
- file fields are skipped;
- a CAPTCHA is recorded, never solved.

The visible handoff is separate. It may fill confirmed Greenhouse fields and upload
verified prepared documents. It doesn't block the final submit, and no Venator code
clicks it.

Screening answers come from confirmed Profile facts or stay empty. A sponsorship
question is answered only when `work_authorization.requires_sponsorship: true`, and
the planner can never answer it "No". EEO answers are never made up.

## Command output

Commands print which stage is running and which store they resolved to on stderr, so
stdout stays clean for commands that print JSON. The loop prints these progress lines
and flushes each one:

- `<stage>: starting` on stdout
- `<stage>: ok` on stdout
- `<stage>: ERROR — <message>` on stderr

Readers ignore lines they don't recognise. The exit status and `data/runs.jsonl` are
what count. A heartbeat row has `at`, `status`, `stage` and an optional `error`, and
the writer scrubs it before it is saved.

## Toolchains

Python needs 3.13+ and `uv`. There is no Python linter or type checker. The UI uses
pnpm, strict TypeScript and the anti-slop lint rules, and a UI change is done only
when `pnpm lint` and `pnpm typecheck` pass. `ui/src/labels.ts` is the only place a
stored identifier turns into English on screen.
