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

- **Jev.** Export `TYPESAFE_API_KEY` only for the command that runs Jev. Preparing
  applications, the Hard Filters, status, view builds and probes never read it.
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

Jev it is a separate run (see step 3). Neither Refresh nor Jev it prepares
documents, opens a browser or submits anything.

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
`filters_version`. The list of boards to poll does not.

### 2. Hard Filters

`venator.match.run` applies the Hard Filters the Profile enables
(`work_authorization`, `education_fit`, `role_target`, `eligibility`) and appends one
Filter Decision per Posting it sees. A filter with no wording kills nothing. Every
Posting gets a decision, including the ones that pass.

When filter code or the Profile's decision inputs change, `filters_version` changes
and the next run appends a replay. Old rows stay as they are. The effective decision
is the latest row per `(posting_key, stage)`.

With `qualification_mode: jev`, `--as-of` is required so that Jev freshness and the
Hard Filter evidence use the same month. Jev never changes a Filter Decision. Current
Jev results only decide which dashboard list a passing Posting lands in: For you,
Explore or Excluded.

### 3. Jev

A Profile can run Jev in shadow mode. A preview is local and sends nothing:

```bash
uv run python -m venator.qualify.jev \
  --profile NAME --as-of YYYY-MM-DD --mode shadow
```

The preview lists what would be sent. To send it:

```bash
export TYPESAFE_API_KEY=...  # set it without printing or saving it
uv run python -m venator.qualify.jev \
  --profile NAME --as-of YYYY-MM-DD --mode shadow --execute
unset TYPESAFE_API_KEY
```

Options:

- `--max-usd` and `--max-requests` cap one command. Without them there is no cap.
- `--posting-key` limits the run to the Postings you name.
- `--offline --execute` records only validated results that are already cached and
  sends nothing.

The dashboard's Jev it runs `venator.schedule.loop --only jev --as-of DATE`, which
calls Jev with `--pipeline`. In that mode the cap comes from `VENATOR_JEV_MAX_USD`,
or $10 if it isn't set. Jev it always sets it to $10. If there is no key, no credit,
a service or network failure, or the cap is reached, Jev stops cleanly, keeps earlier
results and records how many Postings are still waiting. The next Jev it picks them
up.

Results are appended to `data/qualifications/<date>.jsonl`, and validated responses
are cached under `data/qualifications/jev/responses/`. Killed, closed, snippet-only
and protected Postings are never sent. Invariant I10 limits what goes over the wire
to the student projection, the Posting title and description, and allowed spans.

Don't use `--mode promoted`. Promotion needs an active frozen release and a
protected final check, and that tool isn't in this repository.

### 4. Build the view

```bash
uv run python -m venator.view.build \
  --profile NAME --as-of YYYY-MM-DD
```

This rebuilds `build/venator.db` from the JSONL stores in one atomic step. The file
is disposable: delete it and build it again whenever you like. It holds current
Postings, Filter Decisions, assessments, Jev results, Track state, source health and
run heartbeats. Building it makes no network request and never calls Jev or any
other model.

### 5. Open the dashboard

```bash
pnpm --dir ui dev       # in a browser, at http://127.0.0.1:5173
pnpm --dir ui desktop   # the same app in a Tauri window
```

The API listens only on `127.0.0.1`. The lists are **For you** (Jev says look first),
**Explore** (Jev says review), **Awaiting Jev**, **Applications**, and **Excluded**
(Hard Filter kills and Jev exclusions). Employer HTML is shown in a sandboxed iframe.

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

To prepare documents, pick the runtime explicitly:

```bash
uv run python -m venator.applications prepare POSTING_KEY \
  --profile NAME --provider claude
# add --cover-letter if you want one
```

Preparing checks the employer's listing again and reruns the Hard Filters. Then one
completion drafts résumé bullets (and a cover letter if asked) from confirmed Profile
facts, and a second completion checks every drafted passage against those facts.
A rewritten bullet that fails the check goes back to your original wording, and a
cover-letter paragraph that fails it is removed. Files are versioned and hashed under
`data/applications/`. If the Posting, the Profile or a file has changed, the old
bundle is refused rather than reused.

To get a prepared file back (`resume.pdf`, `resume.txt` or `letter.txt`):

```bash
uv run python -m venator.applications file POSTING_KEY \
  --profile NAME --file resume.pdf
```

Handoff needs a current, verified bundle:

```bash
uv run python -m venator.applications handoff POSTING_KEY --profile NAME
```

It opens a visible browser with its own saved profile, so logins stick. For
Greenhouse it fills confirmed fields and uploads the prepared files. Other sources
open for you to fill in by hand. Either way you review the form, answer what's
missing, deal with any CAPTCHA, and press submit yourself.

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

TypeSafe (Jev) is not one of these runtimes. Only `venator.qualify.jev --execute`
reads `TYPESAFE_API_KEY`.

## Stores

| Path | Written by | Kept |
|---|---|---|
| `data/postings/<date>.jsonl` | Discover | append-only |
| `data/decisions/<date>.jsonl` | Hard Filters | append-only |
| `data/qualifications/<date>.jsonl` | Jev | append-only |
| `data/qualifications/jev/responses/` | Jev | validated cache, never edited |
| `data/track/<date>.jsonl` | Track | append-only |
| `data/runs.jsonl` | the loop | append-only |
| `data/applications/` | preparation | versioned bundles |
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

The loop knows five stages, in this order: `discover, filters, jev, view, commit`.
With no `--only` it runs `discover,filters,view`. `--only` picks a subset but can't
change the order. `jev` runs only when you name it. `commit` runs only when you name
it and `data/` is inside a Git work tree, which an Install normally isn't. The loop
never pushes.

To run it every six hours on a Mac, see [ops/README.md](../ops/README.md). Nothing in
this repository loads a launchd job for you.

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
anti-slop lint rules. None of these checks calls TypeSafe or submits an application.
