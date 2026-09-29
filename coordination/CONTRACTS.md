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

Hard Filter pass rows may carry `facts` as extra evidence. None of them change
`filters_version`.

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
  location_places TEXT,
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

The assessment and keep-score tables:

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
    CHECK (assessed_by IN ('deterministic')),
  assessed_as_of TEXT
);
CREATE TABLE keep_scores (
  posting_key TEXT PRIMARY KEY, input_hash TEXT NOT NULL, model_id TEXT,
  probability REAL CHECK (probability >= 0 AND probability <= 1), scored_at TEXT
);
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
3. A closed listing: `closed`, even if it has a score.
4. A Hard Filter pass with a current keep score: at least 0.5 is `queued`,
   below 0.014 is `hard-killed`, and between them is `needs-review`.
5. Any other pass: `unscored`. This is Awaiting Score.
6. No Hard Filter pass at all: `not-filtered`.

Scores bind to a Posting, its compact input hash and model identity. Closed
Postings stay searchable in the filter inspector and on the Employers page.
The résumé assessment is a margin note. For you and Explore sort by keep
probability, highest first, then verification recency.

## Dashboard HTTP surfaces

The server listens on `127.0.0.1` only. The action routers are mounted before the
read-only one. The order matters: Hono's CORS middleware answers a preflight itself,
so if `/api` came first it would answer every write path's preflight with
`GET, OPTIONS`.

| Surface | Methods | What it does |
|---|---|---|
| `/api/runs` | `POST`, plus `GET` for this process's own run state | starts `fetch-and-filter` (`discover,filters,view`) or `score` (keep inference, then View) from a single-use plan token |
| `/api/applications` | `POST`, plus `GET` for status and prepared files | status, file, prepare, save, dismiss, restore, applied and handoff through `venator.applications` |
| `/api/onboarding` | `POST`, plus `GET /settings` and `GET /existing-profile` | creates or edits Profile files under the Install's application data directory, saves the assistant choice; its read and probe routes write nothing |
| `/api/locations` | `GET`, `POST` | reads available locations and saves the list choice in this Install; does not change Filter Decisions |
| `/api` | `GET` | reads `build/venator.db`; starts nothing and writes nothing |

Each action surface needs its own header (`X-Venator-Run`, `X-Venator-Application`,
`X-Venator-Onboarding`, `X-Venator-Location`) and an allowed local `Origin`.

The location Hard Filter and dashboard picker use the same offline GeoNames
lookup ([attribution](../docs/geonames-attribution.md)). It covers US-prefixed
cities, counties, foreign remote sites and campus labels.

Onboarding writes one Profile at a time inside a server process. It refuses lone
surrogates and symbolic links, stages the YAML, loads it back through
`venator.profile`, and renames it into place only if that load works. Two separate
server processes are not coordinated. Editing a saved Profile compares the original
three YAML files before writing only changed form fields; unseen fields survive.

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
