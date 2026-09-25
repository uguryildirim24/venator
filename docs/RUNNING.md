# Running Venator

Run commands from the repository root. Nothing is scheduled or activated by
installing the project. Personal stores live in the Install's application-data
directory, never the checkout. Set `VENATOR_HOME` to a fresh directory to select
a different Install for development or testing.

## Prerequisites

```bash
uv sync
uv run playwright install chromium   # browser tools only
cd ui && pnpm install --frozen-lockfile && cd ..
```

Optional integrations:

- Jev execution: export `TYPESAFE_API_KEY` only for the authorized command.
  Preparation, Hard Filters, status, view builds and probes do not read it.
- Document preparation: sign in to the local `claude` CLI, or explicitly select
  a configured `codex` or `api` runtime.

A Profile is three YAML files under `profiles/<name>/` in a checkout or under
the Install's application-data directory. `--profile` takes a name, never a
path. When more than one non-scaffold Profile exists, every stage requires the
name rather than guessing.

## The normal pipeline

Use one calendar date for stages that take `--as-of`:

```bash
uv run python -m venator.discover.run --profile NAME
uv run python -m venator.match.run --profile NAME --as-of YYYY-MM-DD
uv run python -m venator.view.build --profile NAME --as-of YYYY-MM-DD
```

At this revision the dashboard Refresh action runs the same stages, in that order, with bounded
interactive discovery:

```bash
uv run python -m venator.schedule.loop \
  --only discover,filters,view --profile NAME --as-of YYYY-MM-DD \
  --interactive-discover
```

Refresh uses `fetch-and-filter`; **Jev it** is a separate Owner-authorized
Jev run. Neither starts document preparation, browser handoff or submission.

### 1. Discover

`venator.discover.run` reads search terms and board registrations from the
Profile. It refreshes Greenhouse, Lever, Ashby, SmartRecruiters and Workday
boards. A terminal or scheduled run walks each Workday board to its reported
total, then drains descriptions for every Posting that passes the Hard Filters.
The drain keeps one paced session per Workday tenant, rotates across employers,
and runs a few tenants at once. The dashboard uses bounded listing and detail
windows so its interactive refresh returns promptly. New observations append to
`data/postings/<date>.jsonl`; source failures remain visible and do not turn an
old Posting into a closed one.

Probe one board without writing the store:

```bash
uv run python -m venator.discover.run \
  --profile NAME --probe greenhouse:ginkgobioworks
```

Find or register boards without editing the Profile automatically:

```bash
uv run python -m venator.discover.harvest --profile NAME
uv run python -m venator.discover.register \
  https://jobs.lever.co/example --name "Example" --profile NAME
```

A board registration needs both its board token and its employer display name
under `sources`. The name participates in Dedup and therefore in
`filters_version`; the board polling list itself does not.

### 2. Hard Filters

`venator.match.run` applies configured deterministic Hard Filters and appends
one Filter Decision per Posting it reaches. An unconfigured filter kills
nothing. No Posting disappears without a decision.

A real change to filter code or Profile decision inputs moves
`filters_version`; the next run appends a replay rather than changing old rows.
Effective state is the latest row per `(posting_key, stage)`.

With `qualification_mode: jev`, `--as-of` is required so Jev freshness and Hard
Filter evidence resolve against the same month. Hard Filters still decide;
shadow Jev results do not change Hard Filter Decisions; current Jev results
route passed Postings into the dashboard's For you, Explore and Excluded lists.

### 3. Jev qualification

An Install Profile can use Jev in shadow mode. Previewing is local and makes
no HTTPS request:

```bash
uv run python -m venator.qualify.jev \
  --profile NAME --as-of YYYY-MM-DD --mode shadow
```

The preview reports selected cases. To execute an authorized pass:

```bash
export TYPESAFE_API_KEY=...  # set without printing or writing it
uv run python -m venator.qualify.jev \
  --profile NAME --as-of YYYY-MM-DD --mode shadow --execute
unset TYPESAFE_API_KEY
```

`--max-usd` and `--max-requests` are optional per-command caps. If omitted,
there is no cap. `--posting-key` limits selection to named Postings.
`--offline --execute` records only reusable validated cache entries and makes no
HTTPS request.

Rows append to `data/qualifications/<date>.jsonl`; validated response bodies are
cached under `data/qualifications/jev/responses/`. A killed, closed, snippet-only
or protected Posting is not sent. The wire input is limited by invariant I10 to
the student projection, Posting title and description, and allowed spans.

Do not use `--mode promoted`. Promotion requires an active frozen release and
the protected Gate Q process, whose tool is not built.

### 4. Build the view

```bash
uv run python -m venator.view.build \
  --profile NAME --as-of YYYY-MM-DD
```

This atomically rebuilds `build/venator.db` from the JSONL stores. The database
is disposable and may be deleted and rebuilt. It contains current Postings,
Filter Decisions, assessments, Jev triage, Track state, source health and run
heartbeats.

A view build makes no network request and never invokes Jev or another model.

### 5. Review in the dashboard

```bash
cd ui
pnpm dev       # browser development server
pnpm desktop   # Tauri development host
```

The API binds to `127.0.0.1`. Mount order is intentional: runs, applications
and onboarding are mounted before the read-only `/api` surface so desktop CORS
preflights reach the correct write surface.

The lists are **For you** (Jev prioritize), **Explore** (Jev review),
**Awaiting Jev**, **Applications** and **Excluded** (Hard Filter kills and Jev
exclusions). Employer HTML is rendered in a sandboxed iframe.

## Application actions

The dashboard calls `python -m venator.applications`; the same actions can be
run directly:

```bash
uv run python -m venator.applications status POSTING_KEY --profile NAME
uv run python -m venator.applications save POSTING_KEY --profile NAME
uv run python -m venator.applications dismiss POSTING_KEY --profile NAME
uv run python -m venator.applications restore POSTING_KEY --profile NAME
uv run python -m venator.applications applied POSTING_KEY --profile NAME
```

`applied` records the Owner's manual report. It does not send anything to an
employer.

Preparation reverifies the exact employer listing, reruns the Hard Filters, and
then uses the explicitly selected completion runtime:

```bash
uv run python -m venator.applications prepare POSTING_KEY \
  --profile NAME --provider claude
# add --cover-letter only when wanted
```

Prepared files are versioned and hashed under `data/applications/`. A changed
Posting, Profile or file is refused rather than silently reused.

A visible handoff requires a current verified bundle:

```bash
uv run python -m venator.applications handoff POSTING_KEY --profile NAME
```

Greenhouse handoff can fill confirmed fields and upload verified prepared
files. Other sources may use a manual/download fallback. The browser stays
visible for the person to review, answer anything still missing, handle a
CAPTCHA if present, and submit.

## Dry Run browser tools

The separate Dry Run path remains useful for form reconnaissance:

```bash
uv run python -m venator.browser.recon POSTING_KEY --profile NAME
uv run python -m venator.fill.plan POSTING_KEY --profile NAME \
  --form-spec build/fill/<posting>/form-spec.json
uv run python -m venator.browser.fill POSTING_KEY --profile NAME \
  --plan build/fill/<posting>/plan.json
```

A FillPlan must carry literal `never_submit: true`. The driver aborts
`POST`/`PUT`/`PATCH`/`DELETE`, cancels and counts submit events, skips file
fields, and reports every planned field as filled, skipped or failed. Recon
records CAPTCHAs and never solves them.

## Track CLI

Track events are append-only under `data/track/`:

```bash
uv run python -m venator.track.status --profile NAME
uv run python -m venator.track.record approve POSTING_KEY --profile NAME
uv run python -m venator.track.record outcome POSTING_KEY \
  --profile NAME --detail interview
```

Events are `approve`, `reject`, `prepare`, `fill`, `submit`, `restore`,
`outcome` and `withdraw`. `submit` is an Owner report, not an employer request.
Historical pipeline-actor submit rows remain readable.

## Completion runtimes

The default completion runtime is the local `claude` CLI. Another runtime is
used only when explicitly selected:

```bash
uv run python -m venator.llm.probe --json
VENATOR_LLM_RUNTIME=codex uv run python -m venator.llm.probe --json
```

The key endpoint stays off unless all of `VENATOR_LLM_API_URL`,
`VENATOR_LLM_API_MODEL` and `VENATOR_LLM_API_KEY_VAR` are set and the `api`
lane is selected. CLI child environments are allowlisted. Failures never quote
provider text into the append-only store.

TypeSafe System One is separate from these completion runtimes. Only
`venator.qualify.jev --execute` reads `TYPESAFE_API_KEY`.

## Stores

| Path | Writer | Lifetime |
|---|---|---|
| `data/postings/<date>.jsonl` | Discover | append-only |
| `data/decisions/<date>.jsonl` | Hard Filters | append-only |
| `data/qualifications/<date>.jsonl` | Jev | append-only |
| `data/qualifications/jev/responses/` | Jev | immutable validated cache |
| `data/track/<date>.jsonl` | Track | append-only |
| `data/runs.jsonl` | pipeline loop | append-only |
| `data/applications/` | preparation | versioned bundles |
| `build/venator.db` | view build | disposable |
| `build/fill/` | Dry Run tools | disposable |

One decisions store and one Track store belong to one Profile. Their `.profile`
stamps enforce that boundary; `profile_id` on rows is evidence, not a second
lock. Historical `llm_score` rows still decode, but nothing writes a new one.

Stores and Profiles live in the platform application-data directory, including
when commands run from a checkout. `VENATOR_HOME` selects another Install.
See [ADR-0002](adr/0002-checkout-is-the-tenant-boundary.md).

## Scheduling

```bash
uv run python -m venator.schedule.loop --dry-run --profile NAME
```

The stage order is `discover, filters, view, commit`. `--only` chooses a subset
without reordering it. Personal stores are outside Git; the commit stage skips
them. The loop never pushes.

A macOS launchd file can be generated using [ops/README.md](../ops/README.md),
but none is loaded by this repository.

## Validation

```bash
VENATOR_HANDOFF_HEADLESS=1 uv run pytest -q
cd ui
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm smoke
```

No Python linter or type checker is configured. The UI uses strict TypeScript
and anti-slop lint rules. These gates make no live TypeSafe request and do not
submit an application.
