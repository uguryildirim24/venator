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
new picks and saved Postings, rebuilds View, pre-drafts documents and notifies you. Neither run submits
anything.

### 1. Discover

`venator.discover.run` reads the search terms and registered boards from the
Profile and fetches Postings from Greenhouse, Lever, Ashby, SmartRecruiters,
Workday, iCIMS, Workable, Avature, PeopleClick and TalentBrew boards.

A terminal or scheduled run walks each Workday board to the total it reports, then
fetches full descriptions for the Postings that pass the Hard Filters. It keeps one
paced session per Workday tenant and works on a few tenants at a time. The dashboard
uses shorter windows so Refresh comes back quickly.

Workable rows with the same shortcode merge into one Posting with every location
retained. Different shortcodes remain separate.

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

To enable the location Hard Filter in `targeting.yaml`, add `location`
to `filters.enabled` and set `filters.location.regions` to any non-empty list of
distinct US state codes or `DC`. For example, `[CA]` selects California and
`[MA, RI, NH, CT, VT, ME]` selects New England. A Posting passes if any
site is in a selected state or is US-wide remote. A remote site tied to a state
counts only in that state. Only physical local sites vouch for an unreadable
site from the same employer. Postings clearly outside the selected states are killed,
including sites in named US territories. Workday's “2 Locations” label does not hide its other sites: the
filter also reads sites in `source_facts`, including on older Postings. An explicit
non-US source country, such as Workday's requisition country or Ashby's postal
country, makes that site outside; the employer's local sites cannot vouch for it.
An explicitly US secondary site in a selected state still keeps a foreign-primary
Posting local. The location choice on the dashboard only narrows what's displayed.

`education_fit` reads “<2 years experience” as an upper bound, not a minimum.
It counts a required MD/DO, PharmD, PhD or MS as an advanced degree only in
clear degree context, not in an address like “Rockville, MD” or “DO NOT”. A
co-op page with several degree tracks passes when one track is within reach.

When filter code or the Profile's decision inputs change, `filters_version` changes
and the next run appends a replay. Old rows stay as they are. The effective decision
is the latest row per `(posting_key, stage)`.

The Hard Filter and dashboard location picker share the offline GeoNames place lookup
([attribution](geonames-attribution.md)). It reads US-prefixed cities, counties,
foreign remote sites and campus labels without a network request. Country readings
that collide with state codes are prefixed `country:`, so Canada does not count as
California.

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
At month rollover, the latest same-model score remains in View, marked **Last
month's score**, until a score for the current inputs lands. Carried scores do not
count as current for Score selection, and a model change carries nothing from the
old model. Score partitions newest-first selections into batches of up to 500;
if a batch exceeds a ceiling it scores the largest newest-first prefix that fits.
Refresh does not run inference.

Retraining uses your own labels, not bundled data. Provide a private CSV with
`posting key` and `decision` columns (`keep` or `skip`). Track decisions made
since then take precedence. The first command shows counts, byte sizes, the
kinds of content sent, and a hash bound to the payload before anything uploads;
the newest labels are held out. Employer names, Posting excerpts and confirmed
résumé facts can leave the machine. Posting keys, reasons, contact details and holdout labels stay local.
Approve the preview hash to train and compare the old and candidate models on
the holdout, then pick which one to use. Nothing swaps until you pick new.

```bash
uv run python -m venator.score.train --profile NAME --labels /path/to/labels.csv
uv run python -m venator.score.train --profile NAME --labels /path/to/labels.csv --approve-sha256 HASH
uv run python -m venator.score.train --profile NAME --comparison /path/to/keep-training-comparison.json --pick new
```

The comparison file is private to the Install. To keep the old model, use
`--pick old`. No model, label CSV, adapter path or Modal account is shipped.

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
least 0.5; **Explore** holds 0.1 to below 0.5. Both show highest keep
probability first. **Excluded** holds Hard Filter kills and lower probabilities.
Passes without a current or carried same-model score sit in **Awaiting Score**. Stale Hard Filter passes show as **Awaiting Hard Filters**. A passing
Posting below 0.1 is labelled **Hidden: low keep score**, with its percentage.
You can **Save** it with **s** or **Dismiss** it with **x**, moving it to **Saved**
or **Dismissed**. Hard Filter kills stay closed to those actions. Hidden Postings
do not offer Apply. Employer HTML is shown in a sandboxed iframe.

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

One dashboard **Apply** press checks the live Posting, prepares the documents and
opens the filled form. The terminal keeps preparation and visible Fill separate.

Apply checks the live Posting and reruns Hard Filters. It reads Greenhouse form
questions when available, remembers Ashby and Workday questions per employer for
the next Apply, and drafts a résumé and a letter if the form asks for one,
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
new Apply before Fill. Answers you type in trusted Greenhouse, Ashby and Workday
forms are kept automatically for that employer. Undo retracts a newly kept answer
without rewriting history; Profile lets you edit or remove it later. Authorization,
sponsorship, EEO, consent, verification codes, signatures, passwords and file fields
are never auto-kept.

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
Greenhouse or Ashby it fills confirmed fields and uploads only the reviewed PDFs
when the trusted form belongs to the Posting. On Workday, sign in and pick
**Autofill with Resume**. Venator puts the verified tailored résumé in the upload
field; you press Continue to let Workday parse it. Between steps, Venator fills
blanks and corrects parser values from your confirmed Profile facts, the prepared
version and your answer library. It picks a drop-down option only when its text
matches exactly, and types dates by keyboard. You press every **Save and Continue**
and **Submit**. Venator never presses Back, Add or Delete, and stops filling if a
drop-down pick triggers a save. If an old résumé is attached, remove it yourself.

Your `resume.yaml` can hold `contact.legal_name`, `contact.address`,
`contact.phone_type` and `languages` with proficiency levels; see
`profiles/example/resume.yaml`. Missing facts stay for you to fill. EEO fields
need explicit EEO answers in your answer library; an explicit universal row can
be reused across employers.

Lever opens for you to fill in by hand. Review the form, answer what's missing,
deal with any CAPTCHA and press Submit yourself. A detected Greenhouse
confirmation records an applied Application. Workday records Applied only after
your Submit and a Candidate Home row matching the Posting's title and requisition;
a URL or completion modal alone is not enough. Use `applied` after Ashby or if you
submitted without detection.

## Contacts

Import your private contacts CSV into the Install:

```bash
uv run python -m venator.contacts.import_csv --profile NAME --csv /path/to/contacts.csv
uv run python -m venator.contacts.import_csv --profile NAME --csv /path/to/contacts.csv \
  --triage /path/to/triage.csv
```

The CSV columns are `company`, `person`, `title`, `conversation_angle`,
`contact_route` and `sources`, with optional `board`. An optional triage CSV joins
by company and reads `board_url_to_register`; an explicit contact board wins.
Exact duplicates are skipped. Records append under `data/contacts/`, stamped for
one Profile. The source CSV is never changed.

Contacts match by board when one is supplied, otherwise by normalized employer
name. The Posting margin shows matching people, titles, conversation angles and
safe email or web links. **Reached out** appends `outreach`; pressing the dated
button undoes it with `outreach_undo`. Both are separate from Application standing:
they never change lists or keep/skip labels.

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
`outcome`, `withdraw`, `outreach` and `outreach_undo`. The last two record contact
outreach without changing Application state. `submit` is your own report that you applied. It is not a
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
| `data/answers/` | answer library and auto-kept form answers | append-only |
| `data/contacts/<date>.jsonl` | contacts import | append-only |
| `data/predrafts/<date>.jsonl` | daily pre-draft attempts | append-only |
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

The loop's daily stages are `discover, filters, score, recheck, view, predraft, notify`.
Pre-drafting uses current-score **For you** Postings, newest first, that are open
and untouched, on Greenhouse, Ashby, Workday or Lever. It prepares documents without
opening a browser or recording Track events. A current bundle is reused by Apply;
changed Posting text or Profile facts require a new draft. The default daily limit
is ten attempts, including failed attempts; a drafting failure pauses the stage.
Set `predraft.enabled: false` or change `predraft.limit` in `targeting.yaml`, or use
the Profile form. Drafting uses your chosen assistant and its plan.
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
