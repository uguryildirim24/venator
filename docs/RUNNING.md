# Running Venator from a terminal

Everything the dashboard does, you can also do from a terminal. Run these commands
from the repository root. Installing Venator doesn't schedule or start anything.

Your Profiles and stores live in the Install's application data directory, not in
the checkout (ADR-0002). Set `VENATOR_HOME` to another directory to use a separate
Install, for testing or development.

## Setup

```bash
uv sync
uv run playwright install chromium   # only for the browser tools
pnpm --dir ui install --frozen-lockfile
```

Two things are optional:

- **Document preparation.** Sign in to the `claude` CLI, or pick the `codex` or
  `api` runtime on purpose (see Completion runtimes below).

A Profile is three YAML files in `<Install>/profiles/<name>/`, or in `profiles/<name>/`
in the checkout. `--profile` takes a name, never a path. A name is looked up in the
Install first, so an Install Profile wins over a checkout Profile with the same name.
If more than one real (non-scaffold) Profile exists, every stage needs `--profile`
and refuses to guess.

## The pipeline

Use the same date for every stage that takes `--as-of`:

```bash
uv run python -m venator.discover.run --profile NAME
uv run python -m venator.match.run --profile NAME --as-of YYYY-MM-DD
uv run python -m venator.view.build --profile NAME --as-of YYYY-MM-DD
```

The dashboard's Refresh runs the same three stages through the loop, with Discover
kept short so the screen gets an answer quickly:

```bash
uv run python -m venator.schedule.loop \
  --only discover,filters,view --profile NAME --interactive-discover
```

Score is a separate press (see step 3). The daily run includes Score, rechecks
new picks and saved Postings, rebuilds View and notifies you. Neither run submits
anything.

### 1. Discover

`venator.discover.run` reads the search terms and registered boards from the
Profile and fetches Postings from Greenhouse, Lever, Ashby, SmartRecruiters,
Workday, iCIMS, Workable, Avature, PeopleClick and TalentBrew boards.

A terminal or scheduled run walks each Workday board to the total it reports, then
fetches full descriptions for the Postings that pass the Hard Filters. It keeps one
paced session per Workday tenant and works on a few tenants at a time. The dashboard
uses shorter windows so Refresh comes back quickly.

New observations go into `data/postings/<date>.jsonl`. When a source fails, the
failure is recorded and shown. An old Posting is not marked closed just because its
board didn't answer.

Test one board without writing anything:

```bash
uv run python -m venator.discover.run \
  --profile NAME --probe greenhouse:cloudflare
```

Find boards and print what to add to a Profile. Neither command edits the Profile:

```bash
uv run python -m venator.discover.harvest --profile NAME
uv run python -m venator.discover.register \
  https://jobs.lever.co/example --name "Example" --profile NAME
```

`harvest` suggests boards linked from Postings you already have. `register` takes a
board URL or an employer's careers page, finds the board, checks that it answers, and
prints the two lines to add.

A board needs both its token under `sources.boards` and the employer's display name
under `sources.names`. The name is used for Dedup, so it counts toward
`filters_version`. The list of boards to poll does not. Add a display name from
`sources.names` or a board token to `filters.employer.exclude` in `targeting.yaml` to
stop fetching that employer and exclude its stored Postings on the next Hard Filter
run. Each stored Posting still gets a Filter Decision.

### 2. Hard Filters

`venator.match.run` applies the Hard Filters the Profile enables
(`work_authorization`, `education_fit`, `role_target`, `eligibility`, `location`) and
appends one Filter Decision per Posting it sees. A filter with no wording kills
nothing. Every Posting gets a decision, including the ones that pass.

To enable the New England location Hard Filter in `targeting.yaml`, add `location`
to `filters.enabled` and set `filters.location.regions` to
`[MA, RI, NH, CT, VT, ME]`. All six codes are required. A Posting passes if any
site is in New England, remote, US-wide or unreadable. Only Postings with every
readable site outside New England are killed, including sites in named US
territories. Workday's “2 Locations” label does not hide its other sites: the
filter also reads sites in `source_facts`, including on older Postings. The
location choice on the dashboard only narrows what's displayed.

`education_fit` reads “<2 years experience” as an upper bound, not a minimum.
It counts a required MD/DO, PharmD, PhD or MS as an advanced degree only in
clear degree context, not in an address like “Rockville, MD” or “DO NOT”. A
co-op page with several degree tracks passes when one track is within reach.

When filter code or the Profile's decision inputs change, `filters_version` changes
and the next run appends a replay. Old rows stay as they are. The effective decision
is the latest row per `(posting_key, stage)`.

The Hard Filter and dashboard location picker share the offline GeoNames place lookup
([attribution](geonames-attribution.md)). It reads US-prefixed cities, counties,
foreign remote sites and campus labels without a network request.

### 3. Score

Score uses the person's own keep model in their Install's private `keep-model.json`.
It sends compact, identity-stripped inputs and configured model metadata to Modal.
No model, adapter, question or Modal account comes with the repository. Score
runs on a press, the daily run or an explicit command:

```bash
uv run python -m venator.score.run --profile NAME --estimate
uv run python -m venator.score.run --profile NAME --execute
```

The estimate is a bounded CPU preflight. Execute uses that receipt, checks the $0.50
cap, then appends scores under `data/keep-scores`. A partial run resumes without
rescoring current inputs. The daily run also has daily and monthly spend limits.
Refresh does not run inference.

### 4. Build the view

```bash
uv run python -m venator.view.build \
  --profile NAME --as-of YYYY-MM-DD
```

This rebuilds `build/venator.db` from the JSONL stores in one atomic step. The file
is disposable: delete it and build it again whenever you like. It holds current
Postings, Filter Decisions, assessments, keep scores, Track state, source health
and run heartbeats. Building it makes no network request and never calls a model.

### 5. Open the dashboard

```bash
pnpm --dir ui dev       # in a browser, at http://127.0.0.1:5173
pnpm --dir ui desktop   # the same app in a Tauri window
```

The API listens only on `127.0.0.1`. **For you** holds keep probabilities at
least 0.5; **Explore** holds 0.014 to below 0.5. Both show highest keep
probability first. **Excluded** holds Hard Filter kills and lower probabilities. Passes without a current score sit in **Awaiting
Score**. Stale Hard Filter passes show as **Awaiting Hard Filters**. Employer HTML
is shown in a sandboxed iframe.

## Applying

The dashboard runs `python -m venator.applications` for every application action.
You can run them directly:

```bash
uv run python -m venator.applications status POSTING_KEY --profile NAME
uv run python -m venator.applications save POSTING_KEY --profile NAME
uv run python -m venator.applications dismiss POSTING_KEY --profile NAME
uv run python -m venator.applications restore POSTING_KEY --profile NAME
uv run python -m venator.applications applied POSTING_KEY --profile NAME
```

`applied` records that you applied. It sends nothing to the employer.

Apply checks the live Posting and reruns Hard Filters. It reads Greenhouse form
questions when available, drafts a résumé and a letter if the form asks for one,
and checks drafted passages against confirmed Profile facts. You can edit the text;
each edit makes a new version. Reviewed files are hashed under `data/applications/`.

```bash
uv run python -m venator.applications apply POSTING_KEY --profile NAME
```

The answer library under `data/answers/` holds answers you wrote for one employer
or for all employers. Profile `screening:` is no longer used. Greenhouse questions
about authorization, sponsorship, consent and other reserved subjects stay for you
to answer. Essays cite statements you wrote in the library; Venator does not make
up your reasons. Changing an answer or statement used by an Application requires a
new Apply before Fill.

To get a prepared file back (`resume.pdf`, `resume.txt` or `letter.pdf`):

```bash
uv run python -m venator.applications file POSTING_KEY \
  --profile NAME --file resume.pdf
```

Fill needs a current, hash-verified bundle:

```bash
uv run python -m venator.applications fill POSTING_KEY --profile NAME
```

It opens a visible browser with its own saved profile, so logins stick. On
Greenhouse it fills confirmed fields and uploads only the reviewed PDFs. Other
sources open for you to fill in by hand. Review the form, answer what's missing,
deal with any CAPTCHA and press Submit yourself. A detected Greenhouse confirmation
records an applied Application; use `applied` if you submitted without detection.

## Dry Run browser tools

The Dry Run tools are still useful for looking at a form:

```bash
uv run python -m venator.browser.recon POSTING_KEY --profile NAME
uv run python -m venator.fill.plan POSTING_KEY --profile NAME \
  --form-spec build/fill/<posting>/form-spec.json
uv run python -m venator.browser.fill POSTING_KEY --profile NAME \
  --plan build/fill/<posting>/plan.json
```

A FillPlan must contain the literal `never_submit: true`. The driver aborts every
`POST`, `PUT`, `PATCH` and `DELETE`, cancels and counts submit events, skips file
fields, and reports each planned field as filled, skipped or failed. Recon notes a
CAPTCHA and never solves it.

## Track

Track events are appended under `data/track/`:

```bash
uv run python -m venator.track.status --profile NAME
uv run python -m venator.track.record approve POSTING_KEY --profile NAME
uv run python -m venator.track.record outcome POSTING_KEY \
  --profile NAME --detail interview
```

The events are `approve`, `reject`, `prepare`, `fill`, `submit`, `restore`,
`outcome` and `withdraw`. `submit` is your own report that you applied. It is not a
request to the employer. Older `submit` rows written by the pipeline still load.

## Completion runtimes

By default, completions go to the local `claude` CLI. Another runtime is used only
when you pick it:

```bash
uv run python -m venator.llm.probe --json
VENATOR_LLM_RUNTIME=codex uv run python -m venator.llm.probe --json
```

The key endpoint stays off unless `VENATOR_LLM_API_URL`, `VENATOR_LLM_API_MODEL` and
`VENATOR_LLM_API_KEY_VAR` are all set and you select the `api` lane. There is no
fallback from one runtime to another. Child processes get an allowlisted
environment, and provider text is never copied into the stores.

## Stores

| Path | Written by | Kept |
|---|---|---|
| `data/postings/<date>.jsonl` | Discover | append-only |
| `data/decisions/<date>.jsonl` | Hard Filters | append-only |
| `data/keep-scores/` | Score | append-only |
| `data/track/<date>.jsonl` | Track | append-only |
| `data/runs.jsonl` | the loop | append-only |
| `data/applications/` | Apply and edits | versioned bundles |
| `data/answers/` | answer library | append-only |
| `build/venator.db` | view build | disposable |
| `build/fill/` | Dry Run tools | disposable |

All of these paths are inside the Install directory, even when you run commands from
a checkout.

The decisions store and the Track store each belong to one Profile, enforced by a
`.profile` stamp in the directory. A `profile_id` on a row is only evidence. Old
`llm_score` rows still load, but nothing writes new ones.

To merge another Install's history into this one, stop both writers and run
`uv run python -m venator.import_stores --from ROOT`, where `ROOT` holds a `data/`
directory. It only appends what is missing.

## Scheduling

```bash
uv run python -m venator.schedule.loop --dry-run --profile NAME
```

The loop's daily stages are `discover, filters, score, recheck, view, notify`.
`--only` picks a subset in that order; Refresh runs only Discover, Hard Filters and
View. `commit` runs only when named and `data/` is inside a Git work tree. The loop
never pushes. A cross-process lock keeps two runs from writing together. On a Mac,
the installed app owns a daily launchd agent; see [ops/README.md](../ops/README.md).

## Checks

```bash
VENATOR_HANDOFF_HEADLESS=1 uv run pytest -q
pnpm --dir ui lint
pnpm --dir ui typecheck
pnpm --dir ui test
pnpm --dir ui build
pnpm --dir ui smoke
```

There's no Python linter or type checker. The UI uses strict TypeScript and the
anti-slop lint rules. None of these checks calls a paid model or submits an application.
