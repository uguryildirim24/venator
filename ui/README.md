# venator review dashboard

The Owner's window onto the pipeline: a list of Postings, each one read as a page with Venator's
notes in the margin beside it, and a filter inspector that answers "what did this rule
exclude, and why" without leaving the window. `DESIGN.md` is the brief. Local only, offline — no CDNs,
no external assets.

The dashboard itself is read-only and `GET`-only. Two separate surfaces act.
`/api/onboarding` puts three Profile files into this Install's own application data directory
and does nothing else (`ONBOARDING-API.md`). `/api/runs` starts the pipeline stages a person
could otherwise only start from a terminal — Discover, the Hard Filters, and the view rebuild
— and starts nothing else; Node writes no file there, because what a run writes it writes by
running the same stages the CLI runs (`RUN-API.md`). Neither reaches an employer.

## Commands

```
pnpm install        # once
pnpm dev            # browser: API on :5170 + app on :5173, one Ctrl-C
pnpm desktop        # desktop: the same app in a native window (tauri dev)
pnpm desktop:build  # package into src-tauri/target/release/bundle: .app + .dmg on macOS,
                    #   an NSIS installer (.exe) on Windows
pnpm build          # web bundle to ui/dist; `pnpm dev:server` then serves it on :5170
pnpm desktop:stage  # what `pnpm desktop:build` runs first: resolve the target triple, stage the
                    #   payload and the Python runtime for it, refuse a bundle missing one
pnpm sidecar        # rebuild the desktop payload (Node runtime, API bundle)
pnpm sidecar --target x86_64-pc-windows-msvc   # ...for another platform's bundle
pnpm python --target x86_64-pc-windows-msvc    # stage CPython + the runtime wheels + src/venator
pnpm fixture        # rebuild ui/fixtures/venator.fixture.db from scratch
pnpm smoke          # render every route against the fixture, the empty view, and a fixture
                    #   nobody asked for; assert what is and is not on screen
pnpm test           # unit tests: path resolution, Profile validation, the Profile write
pnpm snapshot       # write each route to ui/snapshots/*.html for a look without a browser
pnpm lint           # oxlint, anti-slop plugin
pnpm typecheck      # tsc --noEmit
```

`pnpm dev` is the normal entry point in a browser: open <http://127.0.0.1:5173>.
`pnpm desktop` is the same app in a native window.

## Desktop app

`pnpm desktop` runs the dashboard in a Tauri window; `pnpm desktop:build` packages it. The
window is the same SPA the browser loads — the desktop host's only job is making sure the
read-only API is there to answer it:

- If something is already listening on 5170 (a `pnpm dev` session), the app uses it.
- Otherwise it starts the bundled API: `src-tauri/binaries/node-<triple>` running
  `src-tauri/resources/server.mjs`, both produced by `pnpm sidecar` and both gitignored. A
  packaged app cannot rely on the machine's Node — GUI processes get a minimal PATH and this
  machine's Node sits behind an fnm shim — so the runtime rides along. It costs ~120 MB.
- The window stays hidden until the API answers, then shows. If it never answers, the window
  appears anyway with the dashboard's own error banner rather than never opening.
- The API process is killed when the app exits. Nothing is left listening.

### Packaging for another platform

`bundle.targets` in `src-tauri/tauri.conf.json` names `app` and `dmg` (macOS) and `nsis`
(Windows). Tauri builds only the ones its host platform can, so the list is one list and each
machine takes what applies to it. `nsis` rather than `msi`: it is Tauri's default Windows
installer, and where `msi` needs the WiX toolset installed first, Tauri's bundler fetches what
NSIS needs itself — NSIS and `nsis_tauri_utils.dll`, downloaded from GitHub into a Tauri cache
the first time a bundle is built. That is a smaller ask than WiX, not the absence of one: the
first `tauri build` on a machine needs network access, and an offline or firewalled Windows box
will fail in the bundler rather than in `cargo`.

**CI runs the bundler, on Windows only.** Two Windows jobs, and they do different things:
`windows — tauri host compiles` runs `cargo build` and `cargo test` on `src-tauri/`, which
compiles and links the host but never packages it, and `windows — the installer a person
installs` runs the real bundler on `windows-latest` and uploads the NSIS `-setup.exe` — see
"Where an installer comes from" below. So `nsis` is a target something has built and inspected.
`app` and `dmg` are not: there is no macOS runner in this workflow, so nothing here builds
them, and they have only ever been made by hand on the Owner's Mac — recorded in
`coordination/status/review-ui.md` on 2026-08-18, and checkable by nobody else. There is no
signing configuration anywhere here for either platform: the installer CI produces is unsigned
and SmartScreen will say so, and no macOS bundle is signed or notarised. That is the honest
state of it.

The Rust half still has to be compiled on (or cross-compiled for) the target. The Node half
does not: `pnpm sidecar --target <rust triple>` — or `$VENATOR_SIDECAR_TARGET` — names the
platform the runtime is for, and any build host can stage any of the six.

**Which runtime a build stages is decided by what it asked for, never by what the build machine
happens to be.** `pnpm sidecar --verified` downloads the pinned Node from nodejs.org for
whatever target is named, this machine's own triple included, and `desktop:stage` passes that
flag on every bundle — so an installer carries a runtime whose bytes were checked, and two
builds of the same commit carry the same one. This is not how it started: the route was chosen
by whether the requested triple equalled the Rust host triple, so `--target x86_64-pc-windows-msvc`
on a Windows host was a no-op, and the only bundles that were ever verified were cross builds —
which neither of the two this repository will ship is. A `.dmg` is built on the Owner's Mac for
the Owner's Mac and a Windows installer on the `windows-latest` runner for Windows; both copied
the build machine's own Node, and the six pinned digests were dead code for every bundle anybody
would install.

Without the flag — `beforeDevCommand`, and a bare `pnpm sidecar` — this machine's own Node is
copied, as it always was. A dev loop is running that Node already and a 53 MB download per
`tauri dev` buys nothing. That route says out loud that what it staged is unverified, and it is
the one that refuses a Node whose major is not the pinned one: the API bundled beside it is
emitted for Node 24, so copying a Node 22 would package an app whose own API may not start. The
verified route stages an exact version and does not ask this machine anything.

Anything downloaded is checked against a **sha256 pinned in `tools/node-runtime.ts`** — in the
repository, in git, changed only by a diff somebody approves. Not against the release's
published `SHASUMS256.txt`: that file and the binary it vouches for come from the same origin,
so whoever can answer as nodejs.org serves both halves and they agree by construction. A target
with no pinned digest refuses rather than falling back to fetching one, and there is no Node
version override — changing the version means editing `NODE_VERSION` and its six digests
together, which is the shape a reviewer can actually check. Downloads are cached under
`node_modules/.cache/`, hashed again on every hit, so the cache can never stand in for the
check.

Then the staged runtime's own header is read back and matched against the target it is *named*
for, and the log says which checks ran rather than leaving a silence to be interpreted. That
last check is the point: a sidecar called `node-x86_64-pc-windows-msvc.exe` holding a Linux ELF
passes every name and size test anyone would run, and fails only on somebody else's machine, as
a window with no API behind it. Every failure on this path removes what it was writing, says
what it was doing, and exits non-zero. **Nothing falls back to copying**: a download that cannot
be reached, or bytes that do not hash to the pin, stops the build, because a fallback would turn
a verified bundle into an unverified one at exactly the moment verification failed.

Both routes and the checks behind them are in `tools/sidecar-staging.ts` rather than in
`tools/prepare-sidecar.ts`, for the reason `python-staging.ts` exists: the script ends in a
top-level `await main()`, so a check living in it is a check no test can reach.

### Staging the pipeline itself

Everything above gives the bundle a dashboard: a Node runtime and a read-only API over a view
database. That is a view of a corpus somebody else's machine built. Every Install runs the whole
pipeline itself — Discover, Match, Tailor, Fill — so on a machine with nothing
installed the app opens, finds no view database, falls back to the sample, and stays there. The
laptop this ships to answers "not recognized" to `python`, `uv`, `node`, `git` and `claude`
alike.

`pnpm python --target <rust triple>` — or `$VENATOR_PYTHON_TARGET` — stages the Python half into
`src-tauri/resources/python/<triple>/`, which `bundle.resources` names so it actually ships:

| | |
|---|---|
| `python.exe`, `python313.dll`, `*.pyd`, `*.dll` | the official embeddable CPython, pinned to one release |
| `Lib/site-packages/` | the runtime wheels, resolved for the target out of `uv.lock` |
| `src/venator/` | the pipeline, with the qualification output schema it reads at import |
| `python313._pth` | rewritten so the interpreter can see the other two |

Windows x86-64 uses python.org's embeddable archive (~154 MB staged). Apple silicon uses
python-build-standalone 3.13.15+20260924 `install_only` (~280 MB staged), pinned to its archive
sha256 in `tools/python-runtime.ts`. Both install the locked production dependencies and
`venator` from the local project at build time (`site-packages/venator` on macOS).
Intel Macs are not yet covered.

The discipline is `prepare-sidecar.ts`'s, for the same reason: this ships somebody else's
binaries to somebody else's machine.

- The archive is checked against a **sha256 pinned in `tools/python-runtime.ts`**. The
  python.org Windows pin was established through its detached OpenPGP signature; the Apple
  silicon pin matches both the published GitHub release asset digest and the downloaded bytes.
  A target with no pin is never downloaded.
- The zip is unpacked by `tools/zip.ts` rather than by whatever `tar` the host has, because GNU
  tar does not read zip at all. Every member is verified against its recorded CRC-32, every
  entry name is contained, and the file count is pinned too — so an archive that unpacks
  *cleanly* and is not the distribution it is filed as is caught.
- The wheels come from `uv export --no-dev` over `uv.lock`, so the bundle ships the versions the
  suite runs against. `--only-binary :all:` refuses a source distribution rather than building
  one for the wrong machine; `--require-hashes` means the index does not get to vouch for
  itself. The staged set is then compared against the resolved set in both directions, and the
  dev group — pytest, and pdfplumber with cryptography and cffi behind it — is refused by name
  as well, because a `--no-dev` gone missing would make both sets agree and both be wrong.
- Every staged file's first bytes are read back and refused if they are an ELF or Mach-O image.
  This is not redundant with the wheel tags: `playwright` ships one build re-tagged per platform
  and records `Tag: py3-none-any` in all of them, so its macOS wheel installs into a Windows tree
  without a complaint from anything that reads metadata, and differs only in whether its bundled
  driver is `node.exe` or a Mach-O `node`.
- The tree is assembled in a scratch directory beside its destination and moved in as the last
  act, so a failure leaves the previous staging untouched rather than half-replaced.

### What a bundle is not allowed to leave out

`pnpm desktop:build` runs `pnpm desktop:stage` first — it is the config's `beforeBuildCommand`,
so `tauri build` cannot be reached without it. It resolves the target triple **once** and hands
it to both staging steps, asks the sidecar step for a verified runtime, and names in its own
summary what that bundle's Node is; `beforeBuildCommand` used to run `pnpm sidecar` with no
target at all, which on a cross build stages *this* machine's Node into a bundle named for
another platform.

`TAURI_ENV_TARGET_TRIPLE` is set by `tauri build` itself and names the triple the bundler is
packaging for, so it is not ranked against anything: if `--target` or `$VENATOR_DESKTOP_TARGET`
names a **different** triple, the build is **refused**, naming both values and where each came
from. Precedence alone cannot catch that — whichever one an order picked, the other would be
discarded in silence and the staging steps would be told a triple the bundler is not packaging
for. When `TAURI_ENV_TARGET_TRIPLE` is set and nothing contradicts it, it is followed. When it
is absent — somebody running `pnpm desktop:stage` by hand — `--target`, then
`$VENATOR_DESKTOP_TARGET`, then the Rust host triple.

Then it decides one of two things about the Python runtime, and says which:

- **A target with a pinned runtime** — `x86_64-pc-windows-msvc` or `aarch64-apple-darwin` — must have one staged.
  `desktop:stage` stages it if it is not there, and **refuses to build** if it still is not,
  naming what is missing. Without that the failure is silent by construction: the `README.txt`
  below means the resource glob always resolves, so a bundle built without ever running `pnpm
  python` succeeds and ships a `python/` directory holding one line of text. That installer is
  the right size for a dashboard, opens onto the sample corpus on a machine with no Python, and
  stays there. `VENATOR_BUNDLE_WITHOUT_PYTHON=1` is the deliberate way past, and it says so.
- **A target with none** — Intel macOS, Linux and `aarch64` Windows — reports that it carries
  no Python. Apple silicon `.dmg` bundles do carry the pinned runtime.

`resources/python/` is created by `pnpm sidecar` on every desktop build whether or not anything
has been staged into it, and always holds at least a `README.txt`. That is not tidiness: Tauri's
build script refuses a resource glob that matches no files — `glob pattern resources/python/**/*
path not found or didn't match any files` — and it fails `cargo build` before a line of the host
is compiled. `pnpm sidecar` also **removes any runtime staged for a different target**, because
`bundle.resources` is a glob rather than a triple: without that, staging a Windows runtime here
and then building a `.dmg` would produce a macOS bundle carrying 154 MB of Windows binaries. It
says which tree it removed and how to stage it again.

### Where an installer comes from

CI builds one. The `windows — the installer a person installs` job in `.github/workflows/ci.yml`
runs the real bundler on `windows-latest` and uploads the NSIS `-setup.exe` as a workflow
artifact, so somebody testing this needs no checkout and no Rust toolchain. It runs on pushes to
`main` and on `workflow_dispatch` — not on pull requests, because it is a release link plus about
250 MB of downloads on the more expensive runner — and it is not a required check.

Nothing about NSIS has to be installed there: on a Windows host `tauri-bundler` fetches the
toolchain itself, and WebView2 is the *installer's* problem on the machine it installs onto, not
the build's. What the job does have to bring is `uv` and a Python, because `beforeBuildCommand`
resolves and installs the wheel set. After the bundle, `.github/scripts/verify_desktop_installer.ps1`
asks what the bundler consumed, whether the produced file is a real PE of a size the payload
accounts for, and — reading the NSIS archive itself with 7-Zip — whether the interpreter, the
wheels, the pipeline and the sidecar are actually inside the installer rather than only in the
staging directory. The installer is unsigned; SmartScreen will say so. The Mac app uses ad hoc signing; distribution
without Gatekeeper warnings requires a Developer ID certificate and Apple notarization.
`hardenedRuntime` is off for the ad hoc Mac build because the bundled Node/V8 cannot reserve
its JIT code range under hardened runtime without JIT entitlements.

**Wired to the app.** `src-tauri/src/lib.rs` resolves `resources/python/<triple>/python.exe`
on Windows or `resources/python/<triple>/python/bin/python3.13` on Apple silicon
under the resource directory — the triple from a compile-time constant `build.rs` takes out of
cargo's `TARGET`, which is the same target `tauri build` hands the staging steps — and hands it
to the API server in `VENATOR_BUNDLED_PYTHON` when the file is there. `pythonInterpreter` then
puts it second: `$VENATOR_PYTHON` still wins, because that is a person naming an interpreter
deliberately. A bundle carrying no runtime names none, and every answer is the answer it was.

Nothing falls back the other way. A bundled interpreter that will not start is reported as an
interpreter that will not start, naming it, rather than as an Install with no pipeline in it —
running a system Python where the bundle shipped its own would be an Install executing code it
did not ship. The `windows — the installer a person installs` job runs the staged interpreter and
makes it import the pipeline and the wheel set out of the bundle before the installer is
uploaded (`.github/scripts/run_staged_interpreter.ps1`).

Playwright's Chromium is not staged: 120 MB down and 284 MB on disk, marked "Fill stage only" in
`docs/RUNNING.md`, and not part of a first run.

**Which database the desktop app opens**, in order: `VENATOR_VIEW_DB` in the environment →
a path in `~/Library/Application Support/com.venator.dashboard/view-path.txt` → this
Install's own view, `build/venator.db` under the store root `src/venator/paths.py` resolves
(`$VENATOR_HOME`, else `~/Library/Application Support/Venator` on macOS) → a checkout at
`~/projects/venator/build/venator.db`, kept last because it is one machine's layout and must
never outrank a real Install's store → the bundled sample database. `src-tauri/src/install.rs`
is the Rust half of that resolution and mirrors `paths.py` rule for rule; the header always
names the database in use, and sample data is badged, so a fallback is visible rather than
silent. Point the app at a different checkout by writing one line into `view-path.txt`.

Employer postings open in the real browser through the opener plugin, never inside the app
window (`src/components/external-link.tsx`). The dashboard has no business hosting an apply
form.

`build:desktop` is `vite build --mode desktop`, and `.env.desktop` beside this file pins
`VITE_API_BASE` to `http://127.0.0.1:5170/api`, because a webview page is served from
`tauri://localhost` where a relative `/api` would resolve against the app protocol. It writes
the same `dist/` as the web build, so re-run `pnpm build` if you then want to serve the
browser version. The value is a mode file rather than the POSIX `VAR=value command` prefix it
used to be: pnpm runs a script through `cmd.exe` on Windows, which has no such grammar, so
that prefix made `tauri build` fail on the one platform this bundle ships an installer for.

## Where the data comes from

`server/locations.ts` resolves every filesystem location the server uses, and is the only
place that does. An Install's data directory is `$VENATOR_HOME`, else the platform's usual
place: `~/Library/Application Support/Venator`, `$XDG_DATA_HOME/venator` falling back to
`~/.local/share/venator`, or `%APPDATA%\Venator`. A checkout is one place a Profile or a view
may be found, never the default (`docs/adr/0002`).

The server opens the view database named by `$VENATOR_VIEW_DB`, else the first that exists of
`<data directory>/build/venator.db` and `<checkout>/build/venator.db`. When there is none it
serves an **empty view**: the contract's schema with no rows, in memory, with `path` naming
where a view will be written. Every route then renders the truth — nothing discovered, nothing
filtered, nothing queued — and the queue shows the first-run screen.

`ui/fixtures/venator.fixture.db` is served only to a run that asked for it, with
`VENATOR_SAMPLE_DATA` set to anything non-blank; `pnpm dev`, `pnpm smoke` and `pnpm snapshot`
ask, and nothing a shipped Install runs does. The ask is checked against the data rather than
the route it arrived by, so a fixture named outright in `$VENATOR_VIEW_DB` is refused by the
same line — the desktop host used to hand its bundled copy over exactly that way, which is how
a brand new Install came to open onto a review queue of nineteen Postings from real employers
that were not the Owner's. That bundled copy is gone too (`src-tauri/src/lib.rs`). When sample data is
open the sidebar says so, in words that say the Postings are not the Owner's own, so real and
stand-in data never look alike.

`pnpm desktop` shows what `pnpm dev` shows, and for the same reason: `tauri dev` runs
`pnpm dev` as its `beforeDevCommand` (`src-tauri/tauri.conf.json`) and the window loads the
Vite dev server, so `/api` is proxied to the API that command started — the one that asks. It
is the app in a native window, not the shipped app. The **packaged** app is what does not ask:
`start_api` in `src-tauri/src/lib.rs` starts the server itself, sets no `VENATOR_SAMPLE_DATA`
and hands over no view path when this Install has built none, so it opens on the first-run
screen. `pnpm desktop:build` is the way to see that.

The handle is opened per request rather than held: the view is disposable and gets rebuilt
underneath the dashboard, so the app picks up real data the moment the match engine writes it,
with no restart. Column names are verified against `coordination/CONTRACTS.md` at open, and a
mismatch returns 503 naming the drift instead of rendering something wrong.

## Routes

| Hash route | View |
|---|---|
| `#/queue` | For you — Postings the Hard Filters and the evidence review passed, by day verified |
| `#/queue?list=needs-review` | Explore — Postings whose evidence needs a look |
| `#/queue?list=applied` | Applications — prepared, handed off, applied, and what came back |
| `#/queue?list=saved` / `?list=dismissed` | Saved and Dismissed, by application state |
| `#/queue?list=filtered` | Excluded — every Posting a Hard Filter excluded, with the rule |
| `#/triage` | Early review — the active Jev decision per Posting, with no number |
| `#/postings/<url-encoded posting key>?from=` | The same window with that Posting selected: the page and its margin |
| `#/inspector` | Filter inspector — every Posting and its Filter Decision, as a table |
| `#/inspector?status=hard-killed&rule=education_fit` | Everything one rule excluded, reasons inline |
| `#/employers` | Source health, one row per board |

## API

Bound to 127.0.0.1. `/api` is read-only and `GET` only.

| Endpoint | Returns |
|---|---|
| `/api/summary` | funnel counts, source health, and which database is open |
| `/api/postings?q=&status=&application=&rule=&sort=&limit=&offset=` | Postings with their latest decision per stage, whether each is new since the last check, and how many are |
| `/api/postings/:key` | one Posting: the reading page (a sanitised text extraction), the employer HTML for the sandboxed frame, full decision history including replays, and its TrackEvents |
| `/api/jev-triage?decision=&q=&limit=&offset=` | the active Jev decision per Posting, most recently verified first; current promoted rows win, otherwise current shadow rows. No probability and no score leave the server |
| `/api/onboarding/state` | whether this Install has a Profile yet, and which one is implied |

`/api/runs` is the run surface — `POST plan`, `POST` the start route, `POST current/stop`, and
`GET current` / `GET recent`, which read this server process's own memory and no store. It
spawns `server/runs/plan.py` and `python -m venator.schedule.loop --only …`, with the stage
list looked up from a closed set. A request names a run *kind*, never a stage. `commit` is
never among them. Every request must carry `X-Venator-Run: 1`. See `RUN-API.md`; the limits are
restated at the mount point in `server/runs/routes.ts`.

All three action surfaces are mounted **before** `/api` in `server/app.ts`: Hono's CORS
middleware answers a preflight from inside itself and the first matching router wins, so
`/api`'s read-only policy would otherwise answer `GET, OPTIONS` for every write path beneath
it. It fails only cross-origin, which is the packaged desktop case.
`tests/runs-mounting.test.ts` asserts the onboarding and run preflights.

`/api/onboarding` is the separate `POST` surface — `runtime/probe`, `resume/import`,
`boards/resolve`, `profile`, `employers`. It writes only into
`<application data directory>/profiles/`, no state-changing request of any kind leaves this
process, and every request must carry `X-Venator-Onboarding: 1`. See `ONBOARDING-API.md`; the
limits are restated at the mount point in `server/onboarding/routes.ts`.

**`employers` is the way back**, and the only route here that edits a Profile somebody already
has. Setup writes a Profile whether or not an employer was registered into it — it must, since
resolving a board needs the network — which leaves a person who can be stranded on a dashboard
whose one control is correctly off, with no terminal to rescue themselves with. This registers
an employer into the Profile that is already there: one file, appended to under `sources.names`
and `sources.boards` and nowhere else in it, with every other byte and every comment left as it
was found (`server/onboarding/targeting-edit.ts`). A document written in a shape it does not
recognise is refused rather than rewritten on a guess.

**One route reads off the network, and only reads.** `boards/resolve` runs
`venator.discover.register`, which `GET`s the employer page somebody pasted and the ATS board
that page names, following no redirect. It is how a board becomes registrable at all, it is
what the pipeline's own CLI does, and it is as far as it goes: no `POST`, nothing resembling
an application, no host the pasted URL did not name.

**The runtime probe is never called on render.** Each runtime it checks is a process launch
and a small completion against the person's own subscription.

**And neither is the resume parse.** `resume/import` grew a third path: an uploaded PDF, read
either on this machine for nothing (`--mode text`) or by the connected runtime for **one
completion** (`--mode model`). The paid one is selected by a form field holding one exact word
— no default, no fallback, no retry — its cost is in the copy beside the control before the
press, and what it answers with is a proposal with every section marked unconfirmed that the
person edits and ticks before any of it can reach a Profile. `pnpm smoke` asserts both halves:
that the screen offers all three paths, and that rendering any setup screen posts nothing at
all. See `ONBOARDING-API.md`.

## Setting up a Profile

| Hash route | Screen |
|---|---|
| `#/onboarding` | connect an assistant (Claude or ChatGPT), and an optional Jev key |
| `#/onboarding?step=resume` | add a résumé — a PDF read here, a PDF read by the assistant, or pasted text — and tick what looks right |
| `#/onboarding?step=targeting` | job titles, level, places, remote, and employers to watch; **Find Jobs** writes the Profile and starts a fetch |

The step is in the hash so Back works, a step is a place somebody can be sent to, and every
screen renders on its own under `pnpm smoke`. Every step has **Skip for Now**. The flow owns
the window: no sidebar, no toolbar, no database stamp. It is Figma v6 on the kit — grouped
rows on the control fill, one column — drawn from `window.css` and `onboarding.css`, with no
second component library. `ui/DESIGN.md` lists the boards. A warning from the write opens
as a sheet over the queue; there is no separate "written" step.

The queue's first-run states — No jobs yet, Awaiting Jev with no key, Updating your job list —
live in `src/views/first-run.tsx`. None of them runs anything on render.

An Install with no Profile that is not a scaffold lands here when the app is opened with no
route. It fires once and never off a route somebody asked for, every screen carries an exit,
and the sidebar's Profile stamp is the deliberate way back in for a second Profile.

## Keyboard

`1` For you · `2` inspector · `3` early review · `j`/`k` or `↓`/`↑` next and previous Posting ·
`g`/`G` first/last · `enter` open a row in the inspector · `/` search · `esc` leave or clear
the search · `r` reload · `?` shortcuts. The list moves the selection and the page follows it.

## Layout

```
server/     Hono API: locations.ts (where everything lives), db.ts (open + schema check),
            queries.ts (SQL), rows.ts (decoding), routes.ts, app.ts (the four mounts, in
            order), http/ (the header-and-origin gate all three action surfaces share),
            onboarding/ (writes a Profile), runs/ (starts a pipeline run), applications/
shared/     contracts.ts, onboarding.ts and runs.ts — the domain types server and app both
            compile against
tests/      node --test units for locations, validation, the write, extraction, the probe,
            the board resolver, and the run surface (mounting, plan, progress, lifecycle,
            refusals, and closed run kinds)
src/        React app: views/, components/, router.ts (hash), keys.ts, api.ts, lists.ts (which
            query each sidebar list is), margin.ts (the notes and marks beside a page),
            application.ts (one Posting's application, shared by toolbar and margin)
              components/             sidebar, toolbar, cells (the list), posting-page (the page
                                      and its margin), sheet, help
              onboarding/             api.ts (the POST client), draft.ts (the Profile being written)
              runs/                   api.ts (the run client), use-run.ts (plan + poll + the
                                      two actions), status.tsx (the sidebar's run rows)
              views/                  workspace.tsx (list + page), inspector.tsx, employers.tsx,
                                      empty-list.tsx, first-run.tsx
              views/onboarding/       flow.tsx, connect.tsx, resume.tsx, targeting.tsx,
                                      employers.tsx, parts.tsx, copy.ts (every setup and
                                      first-run sentence)
              styles/                 tokens.css (the kit), base.css, window.css (every surface of
                                      the window), onboarding.css
              styles.css              the import index; that order is the layer order
fixtures/   make-fixture.ts (library) + build.ts (the `pnpm fixture` entry point)
src-tauri/  desktop host: lib.rs starts and supervises the API, tauri.conf.json bundles it
tools/      dev.ts, smoke.ts, snapshot.ts, dom-harness.ts, api-process.ts, prepare-sidecar.ts,
            prepare-python.ts, python-runtime.ts, python-staging.ts, zip.ts,
            node-runtime.ts (which official Node build a target triple takes, and what a
            staged binary actually is), sidecar-staging.ts (which of the two routes a build
            takes, and the checks behind each)
```

**`DESIGN.md` is the arbiter for anything visual** — the kit's tokens, the window's anatomy,
where ember is allowed, and what the margin may say. Read it before adding a screen; a new one
should need nothing past `window.css`.

The API reads five environment variables, all resolved in `server/locations.ts` except the
port: `VENATOR_VIEW_DB` (an explicit view path), `VENATOR_HOME` (the application data
directory), `VENATOR_PYTHON` (the interpreter the runtime probe runs under),
`VENATOR_BUNDLED_PYTHON` (the interpreter the desktop bundle shipped with, set by the Tauri
host and outranked by `VENATOR_PYTHON`) and `VENATOR_API_PORT` (default 5170).

## Notes for the next change

- **One place decides the API origin**: `API_BASE_URL` in `src/api.ts`, from `VITE_API_BASE`.
  A desktop wrap points it at the sidecar; nothing else in the app builds a URL.
- **SPA, hash routing, no cookies, no origin assumptions** — the bundle runs the same from
  Vite, from the Hono server, and from a webview loading `dist/` off disk (`base: "./"`).
- **Employer HTML is untrusted**: Posting descriptions render inside a `sandbox=""` iframe.
  Keep it that way.
- **The person submits.** Save, Dismiss, Restore, Prepare and I Applied go through
  `/api/applications`; Open Application hands prepared documents to a visible browser and
  stops, and "You press submit" sits beside it wherever it appears. Onboarding writes Profile
  files and nothing else, and the run surface writes nothing from Node — it starts the same
  stages the CLI starts and they write what they always write. A disabled control is a
  statement, with the reason in words beside it; never restyle one to look actionable.
- **Light and dark both, on every host**, following `prefers-color-scheme`, with
  `data-theme="light"` or `"dark"` on `<html>` as the override. Nothing pins a scheme.
- **The window is opaque.** The sidebar is the kit's sidebar fill, not a vibrancy material,
  so nothing paints behind the webview on any host. `titleBarStyle: "Overlay"` and
  `hiddenTitle` are macOS-only window keys and live in `src-tauri/tauri.macos.conf.json`.
  `src/platform.ts` is the matching signal on the app's side: every desktop host wears
  `app-native` and drops the drawn window frame, and a Mac reserves the traffic-light gap in
  the sidebar, or in the list's toolbar while the sidebar is hidden.
- **No machine vocabulary on screen.** `src/labels.ts` is the only place an identifier
  becomes English — rule ids, board slugs, statuses, Posting keys. Raw values still live in
  the data, the URL and `title` attributes; they never get rendered as copy. `pnpm smoke`
  fails the build if one reaches visible text, so this holds without anyone remembering it.
  Historical aggregator rows can carry a region rather than an employer slug,
  so those Postings use their stored source label rather than presenting the region as a company.
- **node:sqlite, not better-sqlite3**: stdlib, no native build step, nothing to package.
- **The desktop app ships a Node runtime** (~120 MB of the bundle). The alternative is
  reimplementing the five queries as Rust commands over rusqlite and dropping the HTTP layer
  from the desktop build entirely — smaller and portless, at the cost of two implementations
  of "latest decision per (posting_key, stage)". Worth doing only behind a shared SQL file,
  so the semantics stay in one place.
