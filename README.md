<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="ui/design/logo/venator-wordmark-dark.svg">
    <img src="ui/design/logo/venator-wordmark-light.svg" alt="Venator" width="302" height="80">
  </picture>
</h1>

Venator is a local, single-person job-application pipeline. It discovers Postings on public ATS boards, records deterministic Hard Filter Decisions, optionally asks TypeSafe Jev to triage eligible Postings, and presents them in a dashboard for review, preparation, and tracking. A Dry Run never submits; a visible handoff leaves submission to the person.

## Status

Discover, Hard Filters, View, Jev qualification, the dashboard, document preparation, Dry Run, and handoff are implemented. Jev ships in **shadow** without the private development cohort or a validated public release; its current-release results still determine the dashboard's triage lists. Nothing is scheduled by installation. Personal Postings and Profile files live only in this Install's application-data directory, outside Git.

## Quick start from scratch

Requirements: Git, Python 3.13+, [uv](https://docs.astral.sh/uv/), Node.js 24+, and pnpm. From a fresh clone, run these commands at the repository root. The example Profile is fictional: do not apply with its résumé.

```bash
git clone https://github.com/uguryildirim24/venator.git venator
cd venator
uv sync
pnpm --dir ui install --frozen-lockfile
export VENATOR_HOME="$(mktemp -d)" # keep this value in the same shell for all commands
export VENATOR_PROFILE=example # selects the example for dashboard subprocesses too
uv run python -m venator.discover.run --profile example
uv run python -m venator.match.run --profile example --as-of "$(date +%F)"
uv run python -m venator.view.build --profile example --as-of "$(date +%F)"
pnpm --dir ui dev
```

Open the local URL printed by `pnpm dev` (normally `http://127.0.0.1:5173`). Keep the server running. The example's three public boards require internet access and can change; a source failure is reported, not silently replaced with demo data. `VENATOR_HOME` selects another Install for **all stores and the View**; the default Install lives outside Git even when commands run inside a checkout. It does not change Profile selection: `--profile example` and `VENATOR_PROFILE=example` read `profiles/example/`. Do not set `VENATOR_SAMPLE_DATA` for this test; that variable opts into a separate stand-in dashboard fixture.

## Make your own Profile

Copy the three YAML files in `profiles/example/` to a new directory under `profiles/<name>/` or `$VENATOR_HOME/profiles/<name>/`. Replace every fictional résumé fact, board, search term, and constraint. Remove `profile.scaffold: true`, give it its own name, and run with `--profile <name>` (a name, never a path). Register both the board token and its employer name. Screening answers start null: supply only facts you can confirm. See [the running guide](docs/RUNNING.md) for board registration and actions. Keep personal Profile data out of a published checkout.

## Pipeline commands

From the repository root, use the same date for both date-taking stages:

```bash
uv run python -m venator.discover.run --profile example
uv run python -m venator.match.run --profile example --as-of YYYY-MM-DD
uv run python -m venator.view.build --profile example --as-of YYYY-MM-DD
uv run python -m venator.schedule.loop --dry-run --profile example
```

Discover and Hard Filters append history; View rebuilds a disposable SQLite database. The loop's dry-run previews stages without executing them. None of the first three commands calls TypeSafe.

## Dashboard

`pnpm --dir ui dev` starts the local API and browser UI on loopback. **For you** holds Jev priorities, **Explore** holds Jev review, **Excluded** holds Hard Filter kills and Jev exclusions, **Awaiting Jev** holds untriaged Postings, and **Applications** holds tracked work. Without Jev results, new Postings wait for qualification. **Refresh** runs Discover → Hard Filters → View; new passing Postings wait in **Awaiting Jev**. **Jev it** sorts passing Postings without a current-release result. Without a key, credit, or service, or when the spend cap is reached, Jev pauses, preserves earlier results, and View still builds. Waiting Postings resume on the next Jev it press. Prepare makes a verified application bundle; Save, Dismiss, Restore and Applied record your actions; Handoff opens a visible browser for your final review and submission. No button submits to an employer.

## Jev (TypeSafe)

A Profile made in setup or copied from `profiles/example/` starts with a conservative Jev policy in shadow. **Jev it** checks Hard Filter passes with that Profile's own binding; **Refresh** does not call Jev. Results are **An estimate, not a verdict**. A local preview uses `uv run python -m venator.qualify.jev --profile NAME --as-of YYYY-MM-DD --mode shadow`; it makes no HTTPS call. **Jev it** or an authorized terminal `--execute` needs `TYPESAFE_API_KEY` (or a key saved in the Mac app's Keychain). Jev it has a $10 per-press spend cap; the CLI Jev pipeline cap can be changed with `VENATOR_JEV_MAX_USD`. One in-flight request may exceed the remaining cap. At the current tariff, budget approximately **$0.00018 per job** (actual usage varies). Never put the key in a Profile or a log. The current release's domain vocabulary describes life-sciences work; for other fields, domain mismatches trigger review rather than automatic exclusion and look-first predictions may be limited. This public copy has no protected cohort or promotion tool. Treat Jev results as estimates, not validated decisions.

## Environment variables

`VENATOR_HOME` (store isolation), `VENATOR_JEV_MAX_USD` (CLI Jev pipeline cap), `VENATOR_PROFILE` (default Profile), `VENATOR_VIEW_DB` (explicit dashboard View), `VENATOR_SAMPLE_DATA` (opt-in stand-in), `VENATOR_PYTHON` (dashboard child interpreter), `TYPESAFE_API_KEY` (authorized Jev), `VENATOR_LLM_RUNTIME` (completion runtime), `VENATOR_LLM_API_URL`, `VENATOR_LLM_API_MODEL`, `VENATOR_LLM_API_KEY_VAR` (key-endpoint completion), and `VENATOR_HANDOFF_HEADLESS` (browser tests). Set values only in your environment; the example needs only `VENATOR_HOME` and `VENATOR_PROFILE`.

## Tests and gates

```bash
VENATOR_HANDOFF_HEADLESS=1 uv run pytest -q
pnpm --dir ui install --frozen-lockfile
pnpm --dir ui lint
pnpm --dir ui typecheck
pnpm --dir ui test
pnpm --dir ui build
pnpm --dir ui smoke
```

Browser tests need Playwright Chromium (`uv run playwright install chromium`). These gates make no paid Jev requests and submit no applications.
