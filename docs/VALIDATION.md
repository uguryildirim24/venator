# Scope-review validation

Rechecked on October 8, 2026, on macOS arm64.
These are command observations, not a matching benchmark or installer verification.
This record describes this review's checks, not the earlier pass's claimed results.

## README run steps

- `uv sync --frozen` passed in the existing worktree environment.
- `(cd ui && pnpm install --frozen-lockfile)` passed with pnpm 11.22.0.
  Dependencies and Corepack were already cached. An empty dependency store or
  completely fresh Python environment was not tested in this review.
- Discovery was skipped. This task forbids data collection and crawling.
  No employer boards were fetched during this review.
- Match and View passed against a fresh example Install with no postings.
  View contained zero postings and zero Filter Decisions. This does not check
  live discovery or filtering accuracy.
- The offline development preview used another fresh Install and the fictional
  fixture. It contained 19 invented postings.
- The frontend at localhost:5173, direct `/api/summary` and frontend proxy
  `/api/summary` returned HTTP 200 for both the empty pipeline and offline preview.
  The development processes were stopped afterward.
- `uv run playwright install chromium` passed with the existing worktree-local
  browser cache. No new browser download was needed.

The committed Vite settings were not changed. Both previews used the README's
localhost address. No runtime binding or default was changed.

## Existing checks

| Check | Observation |
|---|---|
| Python suite with the exclusions below | 2334 passed, 56 subtests passed |
| `(cd ui && pnpm lint)` | Passed |
| `(cd ui && pnpm typecheck)` | Passed |
| `(cd ui && pnpm test)` | 515 passed |
| `(cd ui && pnpm build)` | Passed |
| `(cd ui && pnpm smoke)` | 58 existing checks passed |

The Python command was:

```bash
VENATOR_HANDOFF_HEADLESS=1 uv run pytest -q \
  --basetemp="$PWD/build/final-review/pytest" \
  --ignore=tests/schedule/test_loop.py \
  --ignore=tests/paths/test_store_paths.py \
  --ignore=tests/paths/test_stages_follow_the_install.py \
  --ignore=tests/ci/test_filters_version_guard.py
```

Temporary test storage, caches, HOME and logs were directed into ignored worktree
build storage. Chromium used `build/audit/browsers/`, an existing local cache.
These four excluded files contain Git-writing operations, `.git` marker creation
or indirect Git cleanup. Whole files were excluded, including their safe tests.
No tests were removed to make this command pass. The two-revision CI guard was
not run. No new tests or CI checks were added.

## Publication-tree review

Gitleaks scanned 561 surviving tracked and new publication files individually
with redaction. It reported zero findings and zero scan errors. Default detectors
stayed enabled. The synthetic JWT exception is exact and bound to its existing
test file. This is a tree scan, not a history scan or proof that no private
information exists.

A separate text scan found no real GPA, personal contact details, private machine
paths or candidate visa status. Remaining matches for the former graduation month
were posting expiration or employment-range fixtures, not degree dates.
Candidate fixtures and their assertions use the sample GPA `3.50/4.00`, fictional
degree dates and placeholder authorization status. The dashboard fixture has
invented employers, identifiers, job text, evidence, decisions and probabilities.
Its original schema and historical-score cases remain.

The four design captures remain excluded for privacy. The earlier review
identified job-search context in them; this review did not repeat image OCR.
Runtime GeoNames data and existing fixture evidence were retained. The GeoNames
lookup exceeds 5 MB but is required runtime data, not a generated publication
artifact. Qualification goldens and the compact-input hash assertion passed with
the fictional inputs.

Python executable syntax trees were compared with HEAD for `lanes.py`,
`filters.py` and `secrets.py`, excluding docstrings. Only documentation changed.
The unrelated employment-range date edit in `tests/qualify/test_compile.py` was
reverted to HEAD. The changed candidate degree dates remain fictional.

The Posting page was inspected in source. It renders sanitized text through
`PageBlocks` and `PageBlockText`. Its iframes display prepared PDFs. Incorrect
employer-HTML frame claims were corrected in documentation and comments, without
adding a rendering feature.

Build output, generated databases and logs are ignored. Publication sync rules
are in [PUBLICATION.md](PUBLICATION.md). No commits, staging or pushes were made.
No upstream license fetch was made. Rolf should confirm the supplied lint license
source before the next export.

## Not checked

Live board discovery, paid assistant requests, Modal scoring or training, GPUs,
model downloads, real application submission and desktop packaging were skipped.
Windows and Linux clean-clone behavior were not checked on those platforms.
History scrubbing is separate and still requires Rolf's review.

Earlier validation notes reported an unmocked `git worktree prune` during a prior
check. That command was not run in this review. Its earlier metadata effects were
not verified. Rolf should review worktree registrations separately.
