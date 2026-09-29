<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="ui/design/logo/venator-wordmark-dark.svg">
    <img src="ui/design/logo/venator-wordmark-light.svg" alt="Venator" width="302" height="80">
  </picture>
</h1>

Venator is a local, personal job application app. It runs on your own computer and
works for one person. It pulls job postings from the employer job boards you pick,
throws out the ones you can't or don't want to apply to, and uses your own keep
model to sort the rest. You read them on a
dashboard, prepare a résumé and cover letter for the ones you like, and apply
yourself. Venator never presses submit for you.

## What's in it

- **Discover** fetches Postings from Greenhouse, Lever, Ashby, SmartRecruiters,
  Workday and a few other job boards.
- **Hard Filters** apply fixed rules from your Profile (work authorization,
  education, role level, eligibility, and location when enabled). Every Posting
  gets a recorded decision, so nothing disappears without a reason.
- **Score** sorts Hard Filter passes using your keep model, configured in your
  Install. It runs only when you press **Score**.
- **The dashboard** shows the lists, each Posting as a page with notes beside it,
  and buttons to save, dismiss, Apply and Fill. Its
  résumé assessment treats confirmed post-secondary education, including current
  enrolment, as meeting a high school requirement. This covers ordinary wording
  such as “(Required)” and high school as one option, but not separate required
  qualifications. Preferred extras stay separate.
- **Apply** checks the live Posting and drafts a résumé and letter PDF from confirmed
  facts. You can edit the drafts; each edit makes a new reviewed version.
- **Fill** opens the employer's form in a visible browser. On Greenhouse and
  Ashby it fills confirmed answers and uploads only reviewed, hash-checked PDFs
  when the form belongs to the Posting. Ashby questions are remembered per employer
  for the next Apply. Lever stays manual. You check the form and press Submit
  yourself. Greenhouse confirmation can be recorded automatically; for Ashby,
  use **I Applied** after submitting.

Everything personal (your Profile, the Postings, every decision) stays in a folder on
your computer, outside this repository.

Without a keep model configured in your Install, passing Postings wait in
**Awaiting Score**. No model or question comes with the repository.

## Install

You need Git, Python 3.13 or newer, [uv](https://docs.astral.sh/uv/), Node.js 24 or
newer, and pnpm.

```bash
git clone https://github.com/uguryildirim24/venator.git venator
cd venator
uv sync
pnpm --dir ui install --frozen-lockfile
```

Browser handoff and the Dry Run tools also need Chromium:
`uv run playwright install chromium`.

To build the Mac app, you also need a Rust toolchain. Then run
`pnpm --dir ui desktop:build`. The `.app` and `.dmg` land in
`ui/src-tauri/target/release/bundle/`. On Apple silicon the app carries its own
Node and Python, so it runs on a Mac with neither installed. It is only ad hoc
signed, so macOS warns the first time you open it.

## First-time setup

Start the dashboard and open <http://127.0.0.1:5173>:

```bash
pnpm --dir ui dev
```

`pnpm dev` is the development server. Until your Install has built its first view, it
fills the lists with sample data and labels it as such. The Mac app never shows sample
data.

With no Profile yet, it opens **Set Up Venator**. There are three steps, and each one
has **Skip for Now**.

1. **Connect an assistant.** Pick **Use Claude** (Claude Code, signed in with your
   Claude plan) or **Use ChatGPT** (Codex, signed in with your ChatGPT plan). Venator
   uses it to read your résumé and to draft application documents. Checking the
   connection uses a few words of your plan.
2. **Add your résumé.** Drop in a PDF or paste the text. Reading the PDF on your
   computer is free but only finds your contact details. Reading it with the
   assistant fills in experience, education and skills, and costs one request on your
   plan. Either way you check every value, and only what you tick goes into your
   Profile.
3. **Choose jobs.** Add job titles, level, places, and whether you want remote jobs.
   Then add employers: paste an employer's careers page, press **Look Up**, type the
   employer's name, and add the boards it found. **Find Jobs** saves your Profile and
   starts the first fetch.

After setup, **Profile** in the sidebar opens a form for editing the Profile you
already saved: contact and résumé facts, target jobs, filters, employers and settings.
Save checks your changes before writing and keeps Profile fields the form does not
show. It then reruns Hard Filters on stored Postings and rebuilds the lists, without
fetching boards or scoring. If you skipped the employers, the empty list offers **Add Employers…**.

## Everyday use

- **Refresh** fetches your boards again, runs the Hard Filters and rebuilds the
  lists without inference. Passes without a current score wait in **Awaiting Score**.
  Postings needing fresh Hard Filters show **Awaiting Hard Filters** instead.
- **Score** sits above your Profile name. It runs the keep model configured in your
  Install on Modal, then rebuilds the lists. If a run stops, earlier scores stay and
  remaining Postings wait for the next press. Refresh sits below Employers.
- The location choice above a list narrows the visible Postings and counts. Open
  a state or country to choose its cities, or choose the whole state or country.
  Boston spellings share one city choice. Campus names and job-posting labels use
  their city; joined place lists split into their cities. Remote spellings share
  one Remote choice. Several locations, No location and Other places have their
  own choices. A Posting with several listed places appears in each one. This
  doesn't change the Profile or rerun Hard Filters. The picker and the location
  Hard Filter use the same offline GeoNames place lookup
  ([attribution](docs/geonames-attribution.md)). It recognises US-prefixed cities,
  counties, foreign remote sites and campus labels. A remote site tied to a state
  counts only in that state; only a physical local site can vouch for an unreadable
  site from the same employer.
- The last-checked line below Employers shows when Discover last checked your boards,
  including checks run from the command line. It says **Not checked yet** until
  a board check has been recorded.
- Postings for the same requisition at several sites fold into one row. Open it to
  see its sites; each site's Posting is still kept in history.
- **For you** holds keep probabilities of 0.5 or above. **Explore** holds probabilities
  from 0.014 to below 0.5. Both lists show the highest keep probability first. **Excluded** holds Hard Filter kills and probabilities
  below 0.014. **Applications**, **Saved** and **Dismissed** hold what you've acted on.
- Switching Postings fades and raises the new page. The description opens with
  eight faded lines. **Read More** shows the rest; **Read Less** folds it back.
- A Posting's margin is one ledger. It starts with **Meets N of M** and a tally
  of the requirements Venator can check against your résumé. Press a requirement
  line to open the description at that line. **Open Listing** goes to the
  employer's page when Venator can't prepare an application. **Save** and **Dismiss** record your
  choice, and **Restore** undoes it. **Apply** drafts a résumé and letter PDF.
  Review or edit the drafts, then **Fill** opens the form. **I Applied** records
  an Application if you submitted it and confirmation was not detected.
- **Profile** has an answer library for employer questions. Save answers for one
  employer or all employers, and add your own statements for essay questions.
  Reserved questions remain for you to answer on the form; Venator never guesses.

The installed Mac app can run Discover, Hard Filters, a capped batch of Score,
rechecks, View and a notification once a day. It uses a cross-process lock and daily
and monthly Score limits. See [ops/README.md](ops/README.md).

## Try it with the example Profile

`profiles/example/` is a fictional Profile for trying things out. Don't apply with its
résumé. This runs the pipeline in a throwaway Install:

```bash
export VENATOR_HOME="$(mktemp -d)"  # keep this shell open for all the commands
export VENATOR_PROFILE=example      # the dashboard uses it too
uv run python -m venator.discover.run --profile example
uv run python -m venator.match.run --profile example --as-of "$(date +%F)"
uv run python -m venator.view.build --profile example --as-of "$(date +%F)"
pnpm --dir ui dev
```

The example watches three public boards, so it needs internet access, and results
change over time. If a board fails, you'll see the failure. Venator doesn't swap in
demo data. `VENATOR_HOME` moves all stores and the view. It doesn't change which
Profile runs. Once the view exists, the dashboard shows it instead of the sample
data.

## Writing a Profile by hand

A Profile is three YAML files: `targeting.yaml`, `constraints.yaml` and
`resume.yaml`. Copy `profiles/example/` to `$VENATOR_HOME/profiles/<name>/` (or to
`profiles/<name>/` in a checkout you won't publish). Replace every fictional fact,
board, search term and constraint. Delete `scaffold: true`, set the name, and run each
stage with `--profile <name>`. The name is a name, never a path.

Each board needs its token under `sources.boards` and the employer's name under
`sources.names`. To stop fetching an employer and exclude its stored Postings, add
its display name (or board token) to `filters.employer.exclude` in `targeting.yaml`.
To keep only New England sites, add `location` to `filters.enabled` in
`targeting.yaml` and set:

```yaml
filters:
  location:
    regions: [MA, RI, NH, CT, VT, ME]
```

Use all six codes, once each. A Posting with a New England site passes. A remote
site tied to a state counts only in that state, not everywhere. Only a physical
local site can vouch for an unreadable site from the same employer. A Posting is
excluded when its sites clearly fall outside New England. This is a Hard Filter
on Postings, separate from the location choice above a dashboard list.

`education_fit` treats “<2 years experience” as a ceiling, not a two-year
minimum. Required advanced degrees count only when the wording clearly asks for
a degree; “Rockville, MD” and “DO NOT” do not. A co-op page offering several
degree tracks passes when one track is within reach.

The answer library starts empty. Add only what's true. The example
files explain the Profile fields in comments, and [docs/RUNNING.md](docs/RUNNING.md) covers
registering boards.

## From the terminal

Every button has a command behind it, and the example above shows the pipeline ones.
Discover and the Hard Filters append to history; View rebuilds a disposable SQLite
file. None of them scores. [docs/RUNNING.md](docs/RUNNING.md) covers Score,
applications, tracking, stores and the loop.

## Score

Your Install's private `keep-model.json` names its Modal profile, app and volume,
base and adapter paths, adapter hash and fixed binary question. The repository has
none of these values. Score builds compact inputs from Posting text and confirmed
Profile facts, with identity stripped. Only anonymous row IDs, compact inputs and
configured model metadata go to Modal. The Posting join stays local.

`uv run python -m venator.score.run --profile NAME --estimate` runs a bounded CPU
preflight. `--execute` checks the estimate against the $0.50 cap and scores missing
or stale passes. Scores append under `data/keep-scores`. Score does not train or
download weights.

To retrain your own keep model, supply your private label CSV (`posting key` and
`decision` columns, with `keep` or `skip` values). A preview names what would
leave your machine and gives counts, byte sizes and a hash of the payload:
anonymous training labels, Posting excerpts and confirmed résumé facts. Posting keys, contact details, reasons and holdout labels stay local.
Nothing uploads until you approve the exact preview hash. The newest labels form
a holdout; compare the old and candidate models there, then pick **old** or **new**.
Only picking new changes the active adapter. No label CSV or model is shipped in
this repository.

```bash
uv run python -m venator.score.train --profile NAME --labels /path/to/labels.csv
uv run python -m venator.score.train --profile NAME --labels /path/to/labels.csv --approve-sha256 HASH
uv run python -m venator.score.train --profile NAME --comparison /path/to/keep-training-comparison.json --pick new
```

## Environment variables

| Variable | What it does |
|---|---|
| `VENATOR_HOME` | use a different Install folder for Profiles, stores and the view |
| `VENATOR_PROFILE` | the Profile to use when none is named |
| `VENATOR_VIEW_DB` | open this view database in the dashboard |
| `VENATOR_SAMPLE_DATA` | allow the sample data when there's no view (`pnpm dev` sets it) |
| `VENATOR_PYTHON` | the Python the dashboard runs the pipeline with |
| `VENATOR_LLM_RUNTIME` | which completion runtime to use (`claude` by default) |
| `VENATOR_LLM_API_URL`, `VENATOR_LLM_API_MODEL`, `VENATOR_LLM_API_KEY_VAR` | settings for your own key endpoint |
| `VENATOR_HANDOFF_HEADLESS` | keep browser tests from opening a window |

The example only needs `VENATOR_HOME` and `VENATOR_PROFILE`.

## Tests

```bash
VENATOR_HANDOFF_HEADLESS=1 uv run pytest -q
pnpm --dir ui lint
pnpm --dir ui typecheck
pnpm --dir ui test
pnpm --dir ui build
pnpm --dir ui smoke
```

The browser tests need Chromium (`uv run playwright install chromium`). None of the
tests makes a paid model request or submits an application.

## More docs

- [CONTEXT.md](CONTEXT.md): the words the code and the app use.
- [docs/RUNNING.md](docs/RUNNING.md): the full terminal guide.
- [ops/README.md](ops/README.md): running the pipeline on a timer on a Mac.
- [ui/README.md](ui/README.md): how the dashboard and the desktop app are built.
- [ui/ONBOARDING-API.md](ui/ONBOARDING-API.md) and [ui/RUN-API.md](ui/RUN-API.md):
  the dashboard's HTTP routes.
- [coordination/CONTRACTS.md](coordination/CONTRACTS.md): formats shared between
  Python and the dashboard.
- [docs/adr/](docs/adr/): why the data lives where it does.
