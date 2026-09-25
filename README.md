<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="ui/design/logo/venator-wordmark-dark.svg">
    <img src="ui/design/logo/venator-wordmark-light.svg" alt="Venator" width="302" height="80">
  </picture>
</h1>

Venator is a local, personal job application app. It runs on your own computer and
works for one person. It pulls job postings from the employer job boards you pick,
throws out the ones you can't or don't want to apply to, and lets Jev, a TypeSafe
classifier, sort the rest into look first, review and skip. You read them on a
dashboard, prepare a résumé and cover letter for the ones you like, and apply
yourself. Venator never presses submit for you.

## What's in it

- **Discover** fetches Postings from Greenhouse, Lever, Ashby, SmartRecruiters,
  Workday and a few other job boards.
- **Hard Filters** apply fixed rules from your Profile (work authorization,
  education, role level, eligibility). Every Posting gets a recorded decision, so
  nothing disappears without a reason.
- **Jev** sorts the Postings that pass. It only runs when you press **Jev it**, and
  it costs a little money per Posting (see [Jev](#jev)).
- **The dashboard** shows the lists, each Posting as a page with notes beside it,
  and buttons to save, dismiss, prepare documents and open the application.
- **Handoff** opens the employer's form in a visible browser, fills what it can
  confirm (Greenhouse forms, for now), and stops. You check it and submit.

Everything personal (your Profile, the Postings, every decision) stays in a folder on
your computer, outside this repository.

Jev ships in shadow mode, without a validated public release. Its labels are an
estimate, not a verdict. Its current vocabulary is written for life-sciences jobs, so
in other fields expect more Postings in review and a less useful look-first list.

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
   connection uses a few words of your plan. Here you can also paste a TypeSafe key
   for Jev. On a Mac it is saved in the Keychain. Elsewhere, set `TYPESAFE_API_KEY`
   before you start the dashboard.
2. **Add your résumé.** Drop in a PDF or paste the text. Reading the PDF on your
   computer is free but only finds your contact details. Reading it with the
   assistant fills in experience, education and skills, and costs one request on your
   plan. Either way you check every value, and only what you tick goes into your
   Profile.
3. **Choose jobs.** Add job titles, level, places, and whether you want remote jobs.
   Then add employers: paste an employer's careers page, press **Look Up**, type the
   employer's name, and add the boards it found. **Find Jobs** saves your Profile and
   starts the first fetch.

You can come back to setup any time from **Profile** in the sidebar. If you skipped
the employers, the empty list offers **Add Employers…**. If you skipped the Jev key,
**Awaiting Jev** offers **Add Key…**.

## Everyday use

- **Refresh** fetches your boards again, runs the Hard Filters and rebuilds the
  lists. New Postings that pass wait in **Awaiting Jev**.
- **Jev it** sends the waiting Postings to Jev, with a $10 limit per press. If Jev
  stops (no key, no credit, the service is down, or the limit is reached), earlier
  results are kept and the rest wait for the next press.
- **For you** is what Jev says to look at first. **Explore** is what it says to
  review. **Excluded** holds Hard Filter kills and Jev exclusions. **Applications**,
  **Saved** and **Dismissed** hold what you've acted on.
- On a Posting, **Save** and **Dismiss** record your choice, and **Restore** undoes
  it. **Prepare Application** drafts a résumé (and a cover letter if you want one)
  from confirmed facts only. **Open Application** hands the prepared files to a
  browser window. **I Applied** records that you applied.

Nothing is scheduled. Venator only fetches when you press Refresh, unless you set up
the optional timer in [ops/README.md](ops/README.md).

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
`sources.names`. Screening answers start empty. Fill in only what's true. The example
files explain every field in comments, and [docs/RUNNING.md](docs/RUNNING.md) covers
registering boards.

## From the terminal

Every button has a command behind it, and the example above shows the pipeline ones.
Discover and the Hard Filters append to history, the view build rebuilds a throwaway
SQLite database, and none of them calls TypeSafe. [docs/RUNNING.md](docs/RUNNING.md)
covers the rest: Jev, preparing and handing off applications, tracking, the stores
and the loop.

## Jev

Jev it sends only Hard Filter passes that don't have a current result, using your
Profile's own Jev policy. Refresh never calls Jev.

To preview what would be sent, without sending anything:

```bash
uv run python -m venator.qualify.jev --profile NAME --as-of YYYY-MM-DD --mode shadow
```

Sending needs a key: `TYPESAFE_API_KEY` in the environment, or one saved in the
Keychain from setup. Never put the key in a Profile or a log. At the current price,
expect about $0.00018 per Posting. Jev it stops at $10 per press. A request already
in flight can go slightly past what's left. From the terminal, `VENATOR_JEV_MAX_USD`
changes that limit.

This repository doesn't include the protected test set or the promotion tool, so
Jev stays in shadow here.

## Environment variables

| Variable | What it does |
|---|---|
| `VENATOR_HOME` | use a different Install folder for Profiles, stores and the view |
| `VENATOR_PROFILE` | the Profile to use when none is named |
| `VENATOR_VIEW_DB` | open this view database in the dashboard |
| `VENATOR_SAMPLE_DATA` | allow the sample data when there's no view (`pnpm dev` sets it) |
| `VENATOR_PYTHON` | the Python the dashboard runs the pipeline with |
| `TYPESAFE_API_KEY` | the Jev key |
| `VENATOR_JEV_MAX_USD` | the spending limit for a pipeline Jev run from the terminal (default 10) |
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
tests makes a paid Jev request or submits an application.

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
