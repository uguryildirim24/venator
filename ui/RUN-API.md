# Run API

The routes behind **Refresh** and **Jev it**. They start the same pipeline stages
you could start from a terminal, and nothing else. The screens are built against
this file.

Everything is on `http://127.0.0.1:5170`, the same server as the dashboard.
`$VENATOR_API_PORT` changes the port. Why this surface is separate from `/api` and
`/api/onboarding`, and why it is mounted before `/api`, is in [README.md](README.md)
and [CONTRACTS.md](../coordination/CONTRACTS.md).

## Two kinds of run

A request names a kind, never a list of stages or a module. The server looks up what
to run.

| Kind | Button | What it runs |
|---|---|---|
| `fetch-and-filter` | Refresh | `python -m venator.schedule.loop --only discover,filters,view --profile <name> --interactive-discover` |
| `jev-it` | Jev it | `python -m venator.schedule.loop --only jev --as-of <plan date> --profile <name>` with `VENATOR_JEV_MAX_USD` set to the plan's limit, then `python -m venator.view.build --profile <name>` if Jev exits cleanly |

Both run with the checkout (or the data directory) as the working directory and an
environment built from an allowlist (`server/runs/environment.ts`), not inherited.
`TYPESAFE_API_KEY` goes only to the Jev child. It comes from the server's own
environment, or from the Mac Keychain entry saved during setup.

What this surface never does:

- **Contact an employer.** No route here applies, saves, dismisses, records a
  TrackEvent or opens a browser. Discover only `GET`s ATS listing APIs, exactly as
  `venator.discover.run` does from a terminal.
- **Run anything else.** There is no way to reach `commit`, `venator.browser.fill`,
  `venator.browser.recon`, `venator.fill.plan` or `venator.track.record`, and no way
  to pass arguments or store paths through. Adding a stage takes a code change
  someone reviews.
- **Write files from Node.** The pipeline writes what it always writes: Postings,
  Filter Decisions, Jev results, heartbeats and the view.
- **Choose a completion runtime for you.** `VENATOR_LLM_RUNTIME` is set only from the
  assistant you picked in setup.
- **Use `PUT`, `PATCH` or `DELETE`.** Only `POST` and `GET`.

## The header

Every run request needs `X-Venator-Run: 1`, plus an `Origin` on the local allowlist
if it sends one. The header isn't a password; loopback is the boundary (ADR-0002).
It makes the browser send a preflight, so a page on another site can't quietly post
to this port. Missing header: `400 bad_request`. Wrong origin: `403 forbidden_origin`.

## `POST /api/runs/plan`

Describes a run before it happens.

```jsonc
// request
{ "kind": "fetch-and-filter" }
```

```jsonc
// 200
{
  "plan": {
    "token": "plan_4f1c…",
    "kind": "fetch-and-filter",
    "profile": "example",
    "profileDirectory": "/…/profiles/example",
    "stages": ["discover", "filters", "view"],
    "storeRoot": "/…/venator",
    "viewPath": "/…/venator/build/venator.db",
    "dashboardViewPath": "/…/venator/build/venator.db",
    "boards": 35,
    "startable": true,
    "notes": []
  }
}
```

A `jev-it` plan also has `jev`:

```jsonc
"jev": {
  "asOf": "2026-01-15",
  "mode": "shadow",
  "postingCount": 120,
  "maximumUsd": 10,
  "selectionHash": "…",
  "summary": "120 Postings passed the Hard Filters, have description text, and have no Jev result under the current release."
}
```

`selectionHash` identifies exactly which Postings were selected, so a plan goes stale
if the selection changes even when the count doesn't.

The kind must match exactly. `"Fetch-and-filter"` or `" fetch-and-filter "` is
refused:

```jsonc
// 400 bad_request
{
  "error": {
    "code": "bad_request",
    "message": "There is no run called “refresh”.",
    "remedy": "This dashboard can describe: fetch-and-filter, jev-it.",
    "field": "kind"
  },
  "run": null
}
```

**Notes are information, not errors.** Each has a `code` and a plain-English
`message`:

| Note | Meaning |
|---|---|
| `no-boards` | the Profile registers no board, so there's nothing to fetch; `startable` is `false` |
| `view-elsewhere` | the run would rebuild a different view database from the one the dashboard is reading |
| `nothing-to-qualify` | no Posting needs Jev right now; `startable` is `false` |
| `jev-key-missing` | no TypeSafe key is in the environment or the Keychain; the plan still counts what is waiting, and the Awaiting Jev list shows "Jev needs a key" |

A plan is safe to ask for on every render. It runs `server/runs/plan.py`, which
writes nothing, creates no store, starts no stage and makes no request. `plan.py` only
puts together answers from the pipeline: which Profile resolves (`venator.profile`),
where the run would write (`venator.paths.install_stores`), and whether this
Profile owns the decisions store (`venator.match.store.verify_decisions_dir`), so a
store that belongs to another Profile is refused before anything is written.

**Which Profile:** `$VENATOR_PROFILE` if it is set, otherwise the only Profile that
isn't a scaffold. That rule lives in `server/onboarding/discovery.ts`. The chosen name
is passed to every child explicitly.

## `POST /api/runs`

Starts the run a plan described.

```jsonc
{ "token": "plan_4f1c…" }
```

The token is the only input, so you can't start a run nobody was shown a plan for.
A token works once, lives only in this server's memory, and expires after ten
minutes. On start the server checks again which Profile applies, where the store is
and whether this Profile may use it. If the plan no longer matches, the answer is
`plan_expired`.

Answers `202` with the starting `RunState`. A run is refused while an application
action is going, and any run is stopped after one hour.

The loop is used as the sequencer because it already owns the stage order,
stop-at-first-failure and the per-stage heartbeats that become the `runs` table.

## `GET /api/runs/current` and `GET /api/runs/recent?limit=5`

Return `{ "run": RunState | null }` and `{ "runs": RunState[] }` from this server's
memory. Nothing is read from disk and nothing is started, so polling every second is
fine. `limit` goes up to 20.

`recent` is a convenience, not history. A restarted server remembers nothing. The
lasting record is `data/runs.jsonl` and the `runs` table built from it. When
`current` turns `null`, the client follows the run's id into `recent` to show how it
ended, then reloads the dashboard data.

## `POST /api/runs/current/stop`

Sends `SIGTERM`, then `SIGKILL` after five seconds, to the whole process tree (a
process group on macOS and Linux, `taskkill /T` on Windows). The loop starts each stage
as its own process, so stopping only the loop would leave the stage running. A stop
also reaches the recovery view build described below.

`404 no_run` when this server has no child running.

## One run at a time

`server/runs/runner.ts` keeps one live run. Starting a second answers
`409 run_in_progress` with the live `RunState` in the body, so a double press just
shows the run you're already watching. The stages share files: `view.build` replaces
the database the API reads, and two `match.run`s would each append a decision for
the same Postings.

This is one process's memory, not a lock. A `python -m venator.match.run` you start
in a terminal isn't covered. The only lock in `src/venator/` is Jev's, which stops two
Jev passes writing the same qualifications directory. (`venator.profile.claim` stops
two Profiles sharing a store, not two runs of one Profile.)

## The window and the app

- **Closing the window doesn't stop a run.** The server owns the child, so coming
  back shows it again through `GET /api/runs/current`.
- **Quitting the app does.** `server/main.ts` handles `SIGINT` and `SIGTERM` by
  killing the tree before it exits.
- **`kill -9` on the server doesn't.** No handler runs, and the child leads its own
  process group, so it keeps going.
- **A dropped request doesn't.** The run belongs to the server. The token is spent,
  so the screen asks for a new plan.

## Progress

The loop prints three kinds of line and flushes each one:

```
<stage>: starting            stdout
<stage>: ok                  stdout
<stage>: ERROR — <message>   stderr
```

This format is part of the contract (see CONTRACTS.md). Lines the reader doesn't
recognise change nothing, and a stage the plan didn't include is ignored. The exit
status and the heartbeats are what count. `<stage>: ERROR writing heartbeat — …` is a
different event and is not read as a stage failure.

The screen shows each stage as waiting, running or done, and elapsed time within a
stage. No percentages and no time estimates, because there's nothing true to base
them on. Discover prints a line per board, but those contain board tokens, which are
kept off the screen. The client polls once a second while a run is live and every ten
seconds otherwise.

Jev it's output is never captured, so a Jev run has no transcript.

## When a run stops partway

`data/` is append-only, so a run that stopped halfway still left real rows behind:

- **Discover** writes board by board, so a stopped Discover has stored the boards it
  reached. Running it again appends only what changed.
- **Hard Filters** build every decision in memory and append once at the end, so a
  stopped filters stage has usually written nothing.
- **Jev** caches each validated response as it arrives and appends its result rows
  at the end of the pass. The next pass reuses the cache instead of paying again.
- **The view** is disposable. A failed rebuild is fixed by rebuilding.

A finished `RunState` names the stage it stopped in, marks stages that never ran as
`not-reached`, and says whether rows were written (`wroteRows`). If rows were written
but the view wasn't rebuilt, the runner runs `python -m venator.view.build --profile
<name>` once and reports it on its own line (`viewRecovered`). It skips this when the
view stage itself failed or the server is shutting down. `view.build` writes to a
temporary file and swaps it in, so stopping it leaves the previous view intact.

`failure.transcript` is the last 8 KiB the pipeline printed, shown only for a run that
didn't succeed, folded away and labelled as the pipeline's own output. It is the one
place a board token may appear on screen. That exception is written down next to
`FORBIDDEN_ON_SCREEN` in `ui/tools/smoke.ts`.

## Errors

Every error has the same body. `run` is filled in only for `run_in_progress`:

```jsonc
{
  "error": { "code": "run_in_progress", "message": "…", "remedy": "…", "field": null },
  "run": { /* RunState */ }
}
```

| Code | Status | When |
|---|---|---|
| `bad_request` | 400 | header missing, body not JSON, unknown kind, or no token; `field` names the problem |
| `forbidden_origin` | 403 | origin not on the allowlist |
| `no_profile` | 409 | no Profile that isn't a scaffold. It never makes one up |
| `profile_ambiguous` | 409 | more than one Profile and nothing says which. The names are listed |
| `unknown_profile` | 422 | the pipeline couldn't load the named Profile; its own sentence is passed on |
| `run_in_progress` | 409 | a run or an application action is going |
| `no_run` | 404 | stop with nothing running |
| `plan_expired` | 409 | token unknown, already used, older than ten minutes, or what it described has changed |
| `not_startable` | 409 | the plan had nothing to do, or the kind isn't startable |
| `pipeline_absent` | 503 | `plan.py` exited 3, the interpreter wouldn't start, timed out, or answered in an unknown shape. The message says which, with the interpreter path when that's the problem |
| `store_unreadable` | 409 | the store root couldn't be resolved, or the store belongs to another Profile; the pipeline's own sentence is passed on |
| `run_failed_to_start` | 503 | anything unexpected. The detail goes to stderr, never into the response, because it can hold paths and environment values |

## Not built

- Scheduling from the dashboard: no timer, no run on launch. See `ops/`.
- A separate run history. `data/runs.jsonl` and the `runs` table are the record.
- A lock file, streaming progress, and picking a Profile in the run controls.
