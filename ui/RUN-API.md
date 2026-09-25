# Run API

The HTTP contract for starting a pipeline run from the dashboard. The controls are built
against this file.

The dashboard starts the fetch and filter run. It makes no AI request. Early review
(`#/triage`) orders Postings by Jev's fit estimate.

Everything is on `http://127.0.0.1:5170` (`$VENATOR_API_PORT` moves it), the same loopback
server as the dashboard and onboarding.

---

## Three surfaces, and why this is the third

| Surface | Mounted at | Methods | Spawns | Writes |
|---|---|---|---|---|
| Dashboard API | `/api` | `GET` | nothing | never — the view handle is `readOnly: true` |
| Onboarding | `/api/onboarding` | `POST` | `venator.llm.probe`, `server/onboarding/resolve_board.py` | `<application data directory>/profiles/<name>/`, nothing else |
| Runs | `/api/runs` | `POST`; `GET` only for this process's own memory | `server/runs/plan.py`, `venator.schedule.loop --only …` | only what those stages themselves append |

It is separate from onboarding for the reason onboarding is separate from the dashboard API:
each surface's guarantee is an enumeration short enough to read. Onboarding's is that it
writes three Profile files into one directory and makes exactly one kind of outbound `GET`.
Starting a run appends to `data/`, rebuilds the view, and fetches from every board a Profile
registers — true things that belong in their own enumeration rather than diluting that one.

**`/api/runs` is mounted before `/api`, and that is load-bearing.** Hono's CORS middleware
answers a preflight from inside itself — it returns a 204 without calling `next()` — and a
router mounted at `/api` with `use("*")` matches every sibling path underneath it. With `/api`
first, an `OPTIONS /api/runs` from a desktop origin is answered `Access-Control-Allow-Methods:
GET, OPTIONS`, and the browser refuses the `POST` before it is sent. `POST` survives that
today only because it is a CORS-safelisted method and because the read-only policy echoes
arbitrary request headers back; a perfectly reasonable tightening of that policy would break
every write route under `/api`. Cross-origin is the packaged desktop case — the webview page
is `tauri://localhost` while the API is `http://127.0.0.1:5170` (`ui/.env.desktop`) — and it
never appears in `pnpm dev`, where Vite proxies `/api` and every request is same-origin.
`tests/runs-mounting.test.ts` drives `OPTIONS` at every onboarding and run write path so the
order cannot be quietly undone.

---

## What this surface may never do

- **Reach an employer.** The never-submit invariant is untouched. No route here applies to
  anything, creates an account, touches a CAPTCHA, records a TrackEvent, changes a Posting's
  lifecycle state, or saves or dismisses a Posting. The guards it rests on are not addressed by
  any route here: `install_dry_run_guards` in `src/venator/browser/_shared.py`, the literal
  `never_submit: true` on every FillPlan, and the separate `/api/applications` mount, which
  this surface never calls. Discover `GET`s ATS listing APIs exactly as
  `venator.discover.run` already does from a terminal; the other stages read and write local
  files.
- **Run anything that is not one of the stages a kind maps to.** `discover`, `filters`,
  `view` are what `fetch-and-filter` starts. A request never names a stage — it names a
  *kind*, and the server looks the stage list up in `RUN_KIND_STAGES`. There is no route that
  takes a module name, no argument passthrough, no
  `--postings-dir` or `--decisions-dir` from HTTP, and no way to reach `venator.browser.fill`,
  `venator.browser.recon`, `venator.fill.plan` or `venator.track.record`. Adding a stage
  is an edit somebody makes on purpose and a reviewer sees.
- **Name `commit`.** It is a valid `--only` stage for the CLI and for the launchd job. A
  button in a shipped app must not commit somebody's Postings into whatever repository the app
  happens to be sitting in, and the loop's own work-tree gate only protects the Install that
  has no repository at all — which a developer running the dashboard out of their clone is
  not.
- **Write a file from Node.** What a run writes, it writes by running the same stages the CLI
  runs: appended Postings, appended Filter Decisions, appended heartbeats, and a rebuilt
  disposable view. There is no `fs` call on this surface.
- **Choose an LLM lane.** `VENATOR_LLM_RUNTIME` is never set on anybody's behalf and is not
  forwarded to a child. A lane chosen from a dropdown is the same act as the fallback ladder
  `src/venator/llm/adapter.py` refuses to build.
- **Use any method but `POST` and `GET`.** There is no `PUT`, `PATCH` or `DELETE` anywhere on
  any of this server's three surfaces.

---

## Every run request needs one header

`X-Venator-Run: 1`, on all five routes, plus an `Origin` on the same allowlist the dashboard
uses. It is not authentication and proves nothing about who is asking — the loopback binding
is the boundary (ADR-0002). Its only job is to make the request non-simple, so the browser
sends a preflight the origin allowlist can refuse: a page on some other origin can post a form
or a plain-text body to a loopback port without the browser asking anyone's permission, and
this is what stops that reaching a route that starts a process.

It is a second header name rather than onboarding's for one reason: a run request that claimed
to be an onboarding request would be a small lie in the one place the code should be literal.
The check itself lives once, in `server/http/local-action-guard.ts`, and both surfaces call it
with their own header name and raise their own error type from the answer.

Missing header → `400 bad_request`. Origin not on the allowlist → `403 forbidden_origin`.

---

## `POST /api/runs/plan`

What a run would do before it runs.

```jsonc
// request — a kind, never a stage list
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

`RUN_KINDS` is what may be started. `PLAN_KINDS` is what may be described before a start.
They are separate closed sets and currently contain the same one member: `fetch-and-filter`.
This route takes a `PLAN_KINDS` member. An unrecognised value is `400 bad_request` with the
field and set named. The comparison is literal, so `"Fetch-and-filter"` and
`" fetch-and-filter "` are refused. No plan token is issued.

```jsonc
// 400 bad_request
{
  "error": {
    "code": "bad_request",
    "message": "There is no run called “refresh”.",
    "remedy": "This dashboard can describe: fetch-and-filter.",
    "field": "kind"
  },
  "run": null
}
```

**Notes are not refusals.** A fetch plan with `boards: 0` gives a `no-boards` note and
`startable: false`. `view-elsewhere` says a run would rebuild a view database this dashboard
is not the one reading. A `jev-it` plan gives `nothing-to-qualify` when no Posting needs Jev,
and `jev-key-missing` when neither the environment nor this Install's Keychain entry holds a
TypeSafe key — the dashboard's Awaiting Jev list reads that note to show "Jev needs a key".
The note says only that no key is saved; nothing about a key's value is ever in a plan. A note
carries **prose only**.

**Safe from a render.** It spawns `server/runs/plan.py`, which writes no file, creates no
store, starts no stage and makes no request. That is what separates it from
`POST /api/onboarding/runtime/probe`, which costs a completion and is therefore never called
on a mount.

`plan.py` is an adapter in the exact sense `resolve_board.py` is one: it imports and composes,
and decides nothing. Which Profile resolves comes from `venator.profile`; where a run would
write and which view it would rebuild come from `venator.paths.install_stores`; whether this
Install's Filter Decisions belong to this Profile comes from
`venator.match.store.verify_decisions_dir` — the read a run would do first, so a store claimed
by another Profile is refused before Discover has appended anything.

**Which Profile.** The dashboard's own rule, which `server/onboarding/discovery.ts` keeps
aligned with the pipeline's: the only Profile that is not a scaffold. The chosen name is then
passed to every subprocess explicitly, so the run surface never leaves the Profile implicit —
the plan states it and the screen shows it. A consequence worth knowing: `$VENATOR_PROFILE`
selects a Profile for the CLI and does not select one here, because the dashboard names what
it displayed.

---

## `POST /api/runs`

Start.

```jsonc
{ "token": "plan_4f1c…" }
```

The token is the only input. The Profile and the stages are fixed by the plan the person was
looking at, so **a run nobody was shown a plan for is not expressible**.

A token is opaque, single use, this process's memory only, and valid ten minutes. Everything
the plan stated is checked again here rather than trusted — which Profile is implied, where
the store is, whether this Profile may read it — because the answer must not depend on how
fresh a screen is. A plan that no longer describes what would happen is `plan_expired` rather
than reinterpreted.

What it spawns: `python -m venator.schedule.loop --only <stages> --profile <name>`, with
`cwd = pipelineWorkingDirectory()` and an environment built from a list
(`server/runs/environment.ts`) rather than inherited.

The only argv stage list this surface starts is `discover,filters,view`. It does not contain
`commit`.

**Why the loop rather than a sequencer in TypeScript.** The loop already owns the order, which
module each stage runs, stop-at-first-failure, the per-stage heartbeat that becomes the `runs`
table, and — through `sys.executable` — the propagation of a bundled interpreter to every
stage. A second sequencer would be a second opinion in a second language, and it would
silently produce no heartbeats, so a dashboard-started run would be invisible to the liveness
the dashboard already draws.

`202` with the initial `RunState`.

---

## `GET /api/runs/current` and `GET /api/runs/recent?limit=5`

`{ "run": RunState | null }` and `{ "runs": RunState[] }`, out of this process's memory. No
store is read, no view is opened, nothing is spawned — safe to poll every second while a run
is live.

`recent` is convenience and **is never history**. A restarted server remembers nothing, which
is honest rather than a gap: the durable record of a run is `data/runs.jsonl` and the
`runs` table it is materialised into.

The client follows a live run's id from `current` to `recent` when `current` becomes null. The
server retires a finished child immediately, so this handoff is what keeps a fast terminal
status visible. Once the matching run settles, the client re-reads the view-backed dashboard
data.

---

## `POST /api/runs/current/stop`

`SIGTERM`, then `SIGKILL` after five seconds. It exists so a person can stop a fetch without
quitting the app.

It signals the **whole process tree** — a process group on POSIX, `taskkill /T` on Windows —
because the loop spawns a stage per subprocess and signalling the loop alone would leave the
stage running.

**A stop is about a child, not about a phase.** The recovery pass below is a `venator.view.build`
this server starts *after* the loop has settled, and while one is going a stop reaches that too.
The alternative was a contradiction a person could see: `GET /api/runs/current` reports `running`
for the whole of the recovery, the panel therefore draws Stop, and pressing it answered "there is
no run going" with a process demonstrably going.

`404 no_run` when nothing is going — which means no child of this server's, the recovery pass
included, and not merely no loop.

---

## One run at a time, and what that guard is not

A module-level guard in `server/runs/runner.ts` holds the live run. A second start is
`409 run_in_progress` **with the live `RunState` in the body**, so a double press resolves to
"you are already watching this run" rather than to a failure.

The stages are not independent, which is why the guard is global rather than per stage:
`view.build` rewrites the whole database `openViewDatabase` opens fresh on every request, and
two concurrent `match.run`s read the same undecided Postings and each append their own
decision for every one of them.

**It is one process's memory, not a lock.** A person who also runs `python -m venator.match.run`
in a terminal is outside it, and nothing in `src/venator/` takes a lock —
`venator.profile.claim` guards cross-Profile mixing, not concurrent same-Profile runs. An
advisory lock file under `build/` is the honest version and is deferred; the shipped app is
single-user and single-process.

---

## The window, and the app

**Closing the window does not stop a run.** The child belongs to the API server, not to a
browser tab, so navigating away and coming back shows it again through
`GET /api/runs/current`. A run that takes minutes should not be hostage to a tab.

**Quitting the app does stop it.** `server/main.ts` installs `SIGINT`/`SIGTERM` handlers that
kill the tree before the server closes, and nothing is ever `unref`'d. Without that, quitting
during a run would leave a pipeline process with no window in existence that mentions it.

**A `SIGKILL` of the server does not stop it, and that is stated rather than papered over.**
`kill -9` runs no handler, and the child leads its own process group precisely so a stop can
reach the stage it spawned — so a hard kill of the sidecar leaves the loop running. The two
signals a person's quit actually sends are handled; the third is a hole, and closing it would
mean the child watching its parent, which is a change to `venator.schedule.loop` rather than
to this surface.

**An abandoned request does not stop it either.** A start returns `202` as soon as the child
is spawned, and nothing on this surface listens for a client disconnect: the run is owned by
the process, and a person whose fetch failed mid-flight finds their run at
`GET /api/runs/current` rather than losing it. The one cost is that a token redeemed by a
request that then failed is spent — the plan is re-issued on the next render, so the recovery
is a screen refresh.

**A restarted server remembers nothing.** `GET /api/runs/current` answers `null`, which is
honest; the durable evidence is where it always was.

---

## Progress, and what is deliberately not shown

`venator.schedule.loop` prints three shapes and flushes each as it writes it:

```
<stage>: starting            stdout
<stage>: ok                  stdout
<stage>: ERROR — <message>   stderr
```

Those three lines are a contract, stated with the loop and in `coordination/CONTRACTS.md`.
The reader follows `readProbeDocument`'s discipline: **a line it does not recognise changes no
state**, a stage the run did not plan is ignored rather than invented, and the authority for
what happened stays the exit status plus the heartbeat rows the loop wrote. `<stage>: ERROR
writing heartbeat — …` is a different event and is deliberately not read as a stage failure.

What a person sees is per stage — waiting, running, done — and within a stage, elapsed time
and nothing more, because nothing more is true. **No percentage, no estimated finish, no
per-board line dressed up as progress.** Discover does print a line per board, but those lines
carry board tokens, which `ui/tools/smoke.ts` keeps off screen.

Polling, not streaming: one second while a run is live, ten otherwise. Server-Sent Events add
reconnection semantics, a second response mode in a server that has none, and a class of "the
stream ended but the run did not" bugs, in exchange for saving a loopback `GET` per second
against an object in memory. Deferred until something points at a specific thing polling got
wrong.

---

## A run that half-succeeded

`data/` is append-only, so a run that stopped partway left real rows behind. "The run failed",
said alone, reads as "nothing happened", and the opposite is true.

Per stage, what is actually left:

- **Discover** writes per board, inside the loop over the registry
  (`src/venator/discover/run.py`), so a stage killed halfway has stored the boards it got to.
  Re-running compares each Posting with its latest stored observation and appends changes only.
- **The Hard Filters** build every decision in memory and append once at the end
  (`src/venator/match/run.py`). A killed filters stage has normally written nothing, and a
  re-run appends nothing at all for a Posting whose standing decision still says the same
  thing.
- **The view** is disposable by definition; a failed rebuild is fixed by rebuilding.

So a terminal `RunState` names the stage it stopped in, marks the stages that never ran
`not-reached` rather than `failed`, and lists what each finished stage left. And **if a
store-writing stage finished while the view was never rebuilt, the runner runs
`python -m venator.view.build --profile <name>` once as a recovery pass** and reports it as
its own line — without it, a run that dies in the Hard Filters leaves a person with new
Postings they cannot see and no way to make them appear. It is not started when the view stage
is what failed, and it is never started while the server is shutting down. A stop reaches it
like any other child, and stopping it cannot leave a broken view: `venator.view.build` builds
into a temporary file beside the database and `os.replace`s it into place, so a killed rebuild
leaves the previous view exactly as it was.

`failure.transcript` is the bounded tail (8 KiB) of what the pipeline printed, shown only on a
run that did not succeed, folded away, and captioned as the pipeline's own output rather than
as dashboard prose. It is the one place a board token may reach a screen; the exemption is
deliberate, narrow, and written down beside `FORBIDDEN_ON_SCREEN` in `ui/tools/smoke.ts`.

---

## The error shape

The onboarding surface's body, with one addition:

```jsonc
{
  "error": { "code": "run_in_progress", "message": "…", "remedy": "…", "field": null },
  "run": { /* RunState, on run_in_progress only; null everywhere else */ }
}
```

| Code | Status | When | What it says |
|---|---|---|---|
| `bad_request` | 400 | header missing, body not JSON, unknown kind, no token | Names the field at fault. |
| `forbidden_origin` | 403 | origin not on the allowlist | |
| `no_profile` | 409 | no Profile that is not a scaffold | "There is no Profile to run against." Never invents one. |
| `profile_ambiguous` | 409 | two or more, and nothing says which | Names them, and asks. The pipeline refuses to guess whose search it is running; the dashboard is in no better position. |
| `unknown_profile` | 422 | the pipeline could not load the named Profile | The pipeline's own sentence. |
| `run_in_progress` | 409 | a run is live | Carries the live `RunState`. |
| `no_run` | 404 | stop with nothing going | |
| `plan_expired` | 409 | token unknown, used, older than ten minutes, or what it stated has changed | "What this run was going to do is no longer current." |
| `not_startable` | 409 | starting a plan whose current work count is zero, or a kind outside the startable closed set | The plan's own note and a new-plan remedy. |
| `pipeline_absent` | 503 | `plan.py` exits 3, the interpreter will not start, it timed out, or it answered in an unknown shape | Two honest cases: an Install with no pipeline in it, versus a named interpreter that would not start — with the path, because that is what makes it actionable. |
| `store_unreadable` | 409 | a `StoreRootError` or a store-claim mismatch | The pipeline's own sentence, passed through: it already names the fix. |
| `run_failed_to_start` | 503 | anything unhandled | A fixed sentence. Whatever threw is logged to stderr, never returned — those messages carry paths and environment contents. |

---

## What is not built, and is not being half-built

- **Scheduling from the dashboard.** No "run every morning", no background timer, no run on
  launch. `ops/` and launchd are where that decision lives.
- **A run history of our own.** `data/runs.jsonl` and the `runs` table are the record.
- **An advisory lock file, SSE, and Profile selection in the controls** — deferred, in
  roughly that order.

---

## Vocabulary

`CONTEXT.md`'s, like everything else on screen. **Posting**, not job or listing. **Owner**,
not user. **Hard Filters**, and a **Filter Decision** is recorded for every Posting including
the ones that pass — never "filtered out". The stage identifiers `discover`, `filters`
and `view` belong on a command line; `ui/src/labels.ts` is where each becomes English, and it is
the only place that happens.
