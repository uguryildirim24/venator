<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="ui/design/logo/venator-wordmark-dark.svg">
    <img src="ui/design/logo/venator-wordmark-light.svg" alt="Venator" width="302" height="80">
  </picture>
</h1>

Venator is a local job-search and application-preparation app with a Python pipeline, React dashboard and Tauri desktop shell.

## Why it exists

Job boards, application documents and follow-up notes are often separate.
Venator puts them in one local workflow. It fetches selected employer boards,
records filter decisions, prepares documents from confirmed facts and tracks
manual applications. It never presses Submit.

## What it shows

- Discovery adapters for public employer boards, including Greenhouse, Lever,
  Ashby, SmartRecruiters and Workday.
- Configurable Hard Filters with recorded reasons and replayable history.
- A SQLite view and dashboard for inspecting postings, saving or dismissing them,
  reviewing assessments and tracking application actions.
- Optional document drafting through an assistant and reviewed browser handoff.
- Optional keep-model inference and approved retraining on Modal. No model,
  adapter, labels or measured accuracy result is bundled.

`profiles/example/` is a fictional backend engineer. The development dashboard
has a separate fictional fixture for list and assessment states. Its probabilities
and application actions are invented, not research results or Rolf's job search.
The fixture uses example.com links, not live application forms.

[docs/VALIDATION.md](docs/VALIDATION.md) records the offline preview and existing
checks. Discovery was skipped during this review. Live board counts do not measure
matching accuracy, interviews or successful applications.

## Run from a clean clone

Requirements: Python 3.13 or newer, uv, Node.js 24 or newer, and Corepack
(or pnpm 11.22.0). Run these commands in the cloned repository root.
Run pnpm inside `ui/` so Corepack reads its `packageManager` pin and selects
pnpm 11.22.0:

```bash
uv sync --frozen
(cd ui && pnpm install --frozen-lockfile)
mkdir -p build
export VENATOR_HOME="$(mktemp -d "$PWD/build/example-install.XXXXXX")"
export VENATOR_PROFILE=example
uv run python -m venator.discover.run --profile example
uv run python -m venator.match.run --profile example
uv run python -m venator.view.build --profile example --as-of "$(date +%F)"
(cd ui && pnpm dev)
```

Open `http://localhost:5173`. Keep the same shell and environment for all commands.
This uses three public boards and needs internet access. Failed sources are
reported, not replaced with sample jobs. No assistant account or model is needed.
Passing postings wait in **Awaiting Score** because no keep model is configured.
The Install and build output above are ignored by Git. Delete that example Install
when finished. Do not use its résumé to apply.

For an offline UI preview, omit the three Python pipeline commands and run the
same development server with a fresh example Install. `pnpm dev` permits clearly
labelled sample data only when no real view exists. The packaged app does not.

### Set up a real search

Start a fresh Install outside the checkout. Remove the example environment
variables before setup, or set `VENATOR_HOME` to a private folder outside Git.
The default locations are:

- macOS: `~/Library/Application Support/Venator`
- Linux: `$XDG_DATA_HOME/venator` or `~/.local/share/venator`
- Windows: `%APPDATA%\Venator`

The dashboard can connect a Claude Code or Codex CLI assistant, read a résumé,
let you confirm facts and add employers. These steps can be skipped.
**Profile** edits the saved facts, target jobs, filters, employers and settings.
Refresh fetches and filters without inference. Score needs private model settings.

For a hand-written Profile, copy `profiles/example/` into
`<Install>/profiles/<name>/`. Replace every fictional value, remove `scaffold: true`
and run stages with `--profile <name>`. Keep real Profiles, contacts, labels,
résumés and application documents out of this repository.
See [docs/RUNNING.md](docs/RUNNING.md) for all commands.

### Browser and desktop tools

Browser handoff and browser tests need Chromium:

```bash
uv run playwright install chromium
```

Desktop development and packaging also need Rust and the platform's Tauri build
prerequisites. `(cd ui && pnpm desktop)` starts development;
`(cd ui && pnpm desktop:build)` builds packages under
`ui/src-tauri/target/release/bundle/`. Packaging was not checked in this publication
pass. Do not treat a passing web build as an installer verification.

## Privacy and external processing

Persistent Profiles, postings, decisions, answers, contacts, browser sessions and
prepared documents live in the private Install. The default pipeline does not commit.
The CLI retains an explicit `--only commit` stage for stores inside a Git worktree.
Do not use that stage for personal stores or public exports.
Local storage does not mean all processing stays local:

- Discovery contacts the configured employer boards and search services.
- Assistant résumé reading and drafting send selected résumé facts, posting text
  and drafting inputs through the chosen runtime. Claude Code and Codex run as
  local CLIs but contact their providers. Requests may use a paid plan.
- Score sends compact inputs and model metadata to Modal. The compact builder
  removes identity fields, but job text and confirmed résumé facts still leave
  the machine. Redaction is not a guarantee of anonymity.
- Training previews payload counts and a hash. Only approval of that exact hash
  permits uploading selected labels, posting excerpts and confirmed facts.
  No training dataset or private model configuration belongs in Git.
- Browser handoff visits employer forms and can fill confirmed answers and upload
  reviewed PDFs. Review every field. Submission remains manual.

Credentials are supplied by CLI sign-in or environment variables, never source
files. To use a key endpoint, explicitly select `VENATOR_LLM_RUNTIME=api` and set
`VENATOR_LLM_API_URL`, `VENATOR_LLM_API_MODEL` and `VENATOR_LLM_API_KEY_VAR`.
The last variable names the environment variable containing the key, not the key
itself. Do not publish endpoints with embedded credentials. No example needs a key.
`ui/.env.desktop` contains only a public loopback URL used by the desktop build.

## Project layout

| Path | Purpose |
|---|---|
| `src/venator/` | Python discovery, filtering, scoring, documents, browser tools and stores |
| `ui/src/` | React dashboard |
| `ui/server/` | Loopback API and pipeline adapters |
| `ui/src-tauri/` | Desktop shell and packaging |
| `ui/fixtures/` | Fictional, generated development data |
| `profiles/example/` | Fictional Profile template |
| `tests/`, `ui/tests/` | Existing Python and UI tests |
| `coordination/CONTRACTS.md` | Shared record and API contracts |
| `docs/`, `ops/` | Terminal guide, data design and macOS scheduling |

`<Install>/data/` is append-only runtime history.
`<Install>/build/venator.db` is disposable and rebuilt from that history.
Job data is fetched by Discover, not bundled. The offline place lookup
`src/venator/place_names.json` is runtime data, retained with
[GeoNames attribution](docs/geonames-attribution.md).

## Limits and known gaps

- This is a single-person local app, not an authenticated hosted service.
- ATS layouts and public board availability change. Review source failures,
  uncertain assessments and form fields yourself.
- The compact qualification policy has a closed life-sciences vocabulary.
  General engineering searches can need manual review.
- No keep model is shipped. Ranking, paid assistant calls, Modal execution,
  model downloads and desktop packaging were not exercised in this cleanup.
- There is no accuracy benchmark, application-success study or included paper.
- Scheduling belongs to the installed macOS app. Development does not install a
  timer. See [ops/README.md](ops/README.md).
- Historical scoring decisions and earlier pipeline-authored submit records still
  load. New scoring uses keep probabilities. New submissions remain manual.

## How this was built

AI coding agents did much of the implementation under Rolf's direction.
Rolf set the project scope and the requirement for human review before publication.
The code implements local persistent storage, recorded decisions and manual
submission. Agents checked the subset of commands and existing suites recorded in
[docs/VALIDATION.md](docs/VALIDATION.md). That is not evidence that Rolf personally
verified every adapter, model or installer. Rolf's final privacy and code review
is still required.

## Existing checks

```bash
VENATOR_HANDOFF_HEADLESS=1 uv run pytest -q
(cd ui && pnpm lint)
(cd ui && pnpm typecheck)
(cd ui && pnpm test)
(cd ui && pnpm build)
(cd ui && pnpm smoke)
```

Install Chromium first. These checks do not make paid model requests or submit
applications. No new tests or CI checks were added for publication. Some existing tests create
Git repositories or `.git` markers. Those tests were skipped during this scope
review. See the exact exclusions in [docs/VALIDATION.md](docs/VALIDATION.md).

## License

Venator is under [MIT](LICENSE). The GeoNames lookup is under CC BY 4.0, with
[attribution and data notes](docs/geonames-attribution.md).
The vendored anti-slop lint rules retain their own [MIT notice](ui/tools/oxlint/anti-slop/LICENSE)
and [source notes](ui/tools/oxlint/anti-slop/UPSTREAM.md).

## More documentation

- [CONTEXT.md](CONTEXT.md): terminology.
- [docs/RUNNING.md](docs/RUNNING.md): terminal commands and optional runtimes.
- [ui/README.md](ui/README.md): dashboard and desktop implementation.
- [ui/ONBOARDING-API.md](ui/ONBOARDING-API.md) and [ui/RUN-API.md](ui/RUN-API.md): HTTP routes.
- [docs/PUBLICATION.md](docs/PUBLICATION.md): changes required in the private export process.
