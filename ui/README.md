# Venator dashboard

This is the window you use Venator through. It has a sidebar of lists, each Posting
shown as a page with Venator's notes in the margin, and a filter inspector that shows
what each Hard Filter excluded and why. It runs in a browser or as a desktop app, and
it works offline: no CDNs, no external assets.

It is three pieces:

- a React single-page app (`src/`);
- a small Hono API (`server/`) that listens on `127.0.0.1:5170`;
- a Tauri host (`src-tauri/`) that wraps both into a desktop app.

The UI never imports the Python code. When it needs the pipeline, the API starts a
Python process and reads its output.

## Commands

```
pnpm install        # once
pnpm dev            # browser: API on :5170, app on :5173, one Ctrl-C stops both
pnpm desktop        # the same app in a native window (tauri dev)
pnpm desktop:build  # package it into src-tauri/target/release/bundle:
                    #   .app and .dmg on macOS, an NSIS installer (.exe) on Windows
pnpm build          # web bundle to ui/dist; `pnpm dev:server` then serves it on :5170
pnpm desktop:stage  # the first step of desktop:build: pick the target, stage Node and
                    #   Python for it, refuse to continue if one is missing
pnpm sidecar        # rebuild the desktop payload (Node runtime and API bundle)
pnpm sidecar --target x86_64-pc-windows-msvc   # ...for another platform
pnpm python --target x86_64-pc-windows-msvc    # stage CPython, the wheels and src/venator
pnpm fixture        # rebuild ui/fixtures/venator.fixture.db from scratch
pnpm smoke          # render every route against the fixture and an empty view, and
                    #   check what is and isn't on screen
pnpm test           # unit tests (node --test)
pnpm snapshot       # write each route to ui/snapshots/*.html to look at without a browser
pnpm lint           # oxlint with the anti-slop plugin
pnpm typecheck      # tsc --noEmit
```

Day to day, run `pnpm dev` and open <http://127.0.0.1:5173>.

## The API

The server mounts five routers, in this order (`server/app.ts`):

| Mount | Methods | What it does | Contract |
|---|---|---|---|
| `/api/runs` | `POST`, plus `GET` for run state | starts Refresh or Jev it | [RUN-API.md](RUN-API.md) |
| `/api/applications` | `POST`, plus `GET` for status and files | save, dismiss, restore, prepare, applied, handoff | below |
| `/api/onboarding` | `POST`, plus `GET /settings` and `GET /existing-profile` | setup and editing a saved Profile | [ONBOARDING-API.md](ONBOARDING-API.md) |
| `/api/locations` | `GET`, `POST` | read and save this Install's list location choice | below |
| `/api` | `GET` only | reads the view database | below |

The order matters. Hono's CORS middleware answers a preflight by itself, and a router
at `/api` would match every path below it. If `/api` came first, a preflight for a
write route would get the read-only answer (`GET, OPTIONS`) and the browser would
block the write. That only happens cross-origin, which is the packaged desktop app.
`tests/runs-mounting.test.ts` checks the preflights.

Every action route needs its own header (`X-Venator-Run: 1`,
`X-Venator-Application: 1`, `X-Venator-Onboarding: 1` or `X-Venator-Location: 1`) and a local `Origin` from
the allowlist. The check is shared, in `server/http/local-action-guard.ts`. The
header isn't a password. It forces the browser to send a preflight, which lets the
allowlist refuse a page from some other site.

### Read-only routes

| Endpoint | Returns |
|---|---|
| `/api/summary` | list counts, source health, and which database is open |
| `/api/postings?q=&status=&application=&rule=&sort=&limit=&offset=` | Postings with their latest decision per stage, and whether each is new since the last check |
| `/api/postings/:key` | one Posting: the reading page (sanitised text), the employer HTML for the sandboxed frame, the full decision history including replays, and its TrackEvents |
| `/api/jev-triage?decision=&q=&limit=&offset=` | stale and unavailable Jev results for the selected release, most recently verified first. No probability or score leaves the server |
| `/api/onboarding/state` | whether this Install has a Profile yet, and which one is in use |

`GET /api/locations` returns available location choices and the saved selection.
`POST /api/locations` takes `{ "selected": ["…"] }` and saves the choice in the
Install; it doesn't change a Profile or the view.

### Application routes

`POST /api/applications` takes `{ "action", "key", "provider"?, "coverLetter"? }`.
`action` is one of `prepare`, `save`, `dismiss`, `restore`, `applied` or `handoff`.
`prepare` also needs `provider`: `claude`, `codex` or `api`. The server runs
`python -m venator.applications <action> <key> --profile <name>` and returns its JSON.
While another application action or a run is going, it answers `409`.

`GET /api/applications/:key` returns the application's status.
`GET /api/applications/:key/files/<name>` returns a prepared `resume.pdf`,
`resume.txt` or `letter.txt`.

## Where the data comes from

`server/locations.ts` decides every path the server uses. The Install's data
directory is `$VENATOR_HOME`, or the platform's usual place:
`~/Library/Application Support/Venator` on macOS, `$XDG_DATA_HOME/venator` (else
`~/.local/share/venator`) on Linux, `%APPDATA%\Venator` on Windows.

The server opens the view database named by `$VENATOR_VIEW_DB`, or else
`<data directory>/build/venator.db`. If there isn't one, it serves an empty view: the
right tables with no rows, so every screen shows the first-run state instead of an
error.

It opens the database fresh for each request, because the pipeline rebuilds it
underneath. New data shows up without a restart. When it opens the file it checks the
columns it needs (`server/db.ts`). If they don't match, it answers `503` and names the
problem instead of showing something wrong.

`ui/fixtures/venator.fixture.db` is sample data. It is served only when
`VENATOR_SAMPLE_DATA` is set to something non-blank and there is no real view to
open. `pnpm dev`, `pnpm smoke` and `pnpm snapshot` set it. Nothing a real Install
runs does. A fixture named in `$VENATOR_VIEW_DB` without that variable is treated as
no view at all. When sample data is showing, the sidebar says so, so it can't be
mistaken for your own Postings.

Profiles are looked for in `<data directory>/profiles` first, then in `profiles/`
beside the working directory and the checkout (for the fictional example). They are
only ever written to `<data directory>/profiles`.

## Screens

| Route | Screen |
|---|---|
| `#/queue` | **For you**: passes Jev says to look at first |
| `#/queue?list=needs-review` | **Explore**: passes Jev says to review |
| `#/queue?list=unscored` | **Awaiting Jev**: passes with no current Jev result |
| `#/queue?list=applied` | **Applications**: prepared, handed off, applied, and what came back |
| `#/queue?list=saved`, `?list=dismissed` | **Saved** and **Dismissed** |
| `#/queue?list=filtered` | **Excluded**: Hard Filter kills and Jev exclusions, with the reason |
| `#/triage` | **Jev diagnostics**: stale and unavailable Jev results; those Postings stay in Awaiting Jev |
| `#/postings/<url-encoded key>?from=` | the same window with one Posting open |
| `#/inspector` | filter inspector: every Posting and its Filter Decision, as a table |
| `#/inspector?status=hard-killed&rule=education_fit` | everything one rule excluded |
| `#/employers` | source health, one row per board |
| `#/onboarding` | setup step 1: connect an assistant, and optionally save a Jev key |
| `#/onboarding?step=resume` | setup step 2: add a résumé and tick what's right |
| `#/onboarding?step=targeting` | setup step 3: job titles, level, places, remote, employers; **Find Jobs** saves the Profile and starts a fetch |
| `#/profile` | edit the saved Profile's contact, résumé, targets, filters and employers in a form |

Lists sort by when a Posting was last verified, never by a score. The résumé
assessment shows as a note in the margin, not as a list.

Setup takes over the whole window, with no sidebar. Each step has **Skip for Now**,
and the step lives in the URL so Back works. When the app opens with no route and no
real Profile, it goes to setup once. **Profile** in the sidebar opens the saved
Profile's form instead. Changes are checked before Save; fields the form doesn't
show remain in the Profile. The location choice above the lists filters visible
Postings, page counts and pagination without changing the view database.

Jev it is a separate press in the sidebar footer. Its waiting, running and paused
states stay visible while Refresh remains a fetch-and-filter action. Lists and the
inspector use lighter transitions, with reduced motion respected.

The first-run states (No jobs yet, Jev needs a key, Updating your job list) are in
`src/views/first-run.tsx`. None of them starts anything just by being shown.

## Keyboard

`1` For you, `2` filter inspector, `3` Jev diagnostics, `j`/`k` or `↓`/`↑` next and
previous Posting, `g`/`G` first and last, `enter` open the selected Posting, `/`
search, `esc` leave or clear the search, `r` reload, `?` show the shortcuts.

## Desktop app

`pnpm desktop` runs `pnpm sidecar && pnpm dev` first and loads the Vite dev server in
a native window. It shows exactly what `pnpm dev` shows, sample data included. To see
what a real Install sees, build it with `pnpm desktop:build`.

The packaged app starts its own API:

- If something already listens on port 5170 (say, a `pnpm dev` session), it uses
  that.
- Otherwise it runs the bundled Node (`src-tauri/binaries/node-<triple>`) on
  `src-tauri/resources/server.mjs`. Both come from `pnpm sidecar` and are gitignored.
  The app can't rely on a system Node, because GUI apps get a minimal `PATH`. This
  adds about 120 MB. On a Mac, Playwright uses this same Node sidecar instead of
  shipping another one inside Python.
- The window stays hidden until the API answers. If it never does, the window opens
  anyway and shows the dashboard's error banner.
- The API is stopped when the app quits.

The packaged app opens, in order: `$VENATOR_VIEW_DB`; a path written in
`view-path.txt` in the app's config directory
(`~/Library/Application Support/com.venator.dashboard/` on macOS); the Install's own
`build/venator.db`. If none exists, it starts the API with no view and you get the
first-run screen. The header always names the database in use.

The desktop web bundle is built with `vite build --mode desktop`, and `.env.desktop`
sets `VITE_API_BASE` to `http://127.0.0.1:5170/api`, because a page served from
`tauri://localhost` can't reach a relative `/api`. It writes the same `dist/` as the
web build, so run `pnpm build` again before serving the browser version.

Employer links open in your real browser, never inside the app window
(`src/components/external-link.tsx`).

### Packaging

`bundle.targets` in `src-tauri/tauri.conf.json` lists `app` and `dmg` for macOS and
`nsis` for Windows. Each machine builds the ones it can. NSIS is used rather than MSI
because Tauri downloads the NSIS tools itself, so the first Windows build needs
network access but no WiX install.

CI runs on Windows only for the desktop app. `windows — tauri host compiles` builds
and tests the Rust host. `windows — the installer a person installs` runs the real
bundler, checks that the interpreter, wheels, pipeline and sidecar are inside the
installer (`.github/scripts/verify_desktop_installer.ps1`), runs the staged
interpreter (`.github/scripts/run_staged_interpreter.ps1`), and uploads the
`-setup.exe` as an artifact. That job runs on pushes to `main` and on manual
dispatch, not on pull requests, and it isn't a required check. There is no macOS
runner, so the `.app` and `.dmg` are only ever built by hand on a Mac.

Nothing is properly signed. The Windows installer is unsigned and SmartScreen will say
so. The Mac app is ad hoc signed; shipping it without Gatekeeper warnings would need a
Developer ID certificate and notarization. `hardenedRuntime` is off because the
bundled Node can't reserve its JIT memory under the hardened runtime without extra
entitlements.

### The Node runtime

`pnpm sidecar --target <rust triple>` (or `$VENATOR_SIDECAR_TARGET`) stages Node for
any of the six supported targets, from any build machine. The Rust host still has to
be compiled on, or cross-compiled for, the target.

`desktop:stage` always asks for a verified runtime (`pnpm sidecar --verified`). That
downloads Node `NODE_VERSION` from nodejs.org and checks it against a sha256 pinned in
`tools/node-runtime.ts`, not against the release's own `SHASUMS256.txt`, which comes
from the same server. A target with no pinned digest is refused. Downloads are cached
under `node_modules/.cache/` and hashed again on every use. After staging, the
binary's header is read to confirm it really is for the target it's named for. Any
failure removes what was written and exits non-zero. Nothing falls back to copying.

A plain `pnpm sidecar` (what `pnpm desktop` runs) copies this machine's Node instead,
says it is unverified, and refuses a Node whose major version isn't the pinned one,
because the API bundle targets Node 24.

The logic and checks live in `tools/sidecar-staging.ts`, where tests can reach them.

### The Python runtime

The app runs the whole pipeline itself, so it needs Python. `pnpm python --target
<rust triple>` (or `$VENATOR_PYTHON_TARGET`) stages it into
`src-tauri/resources/python/<triple>/`:

- Windows x86-64: python.org's embeddable CPython (about 154 MB staged), with
  `python313._pth` rewritten to find the rest.
- Apple silicon: python-build-standalone `3.13.15+20260924` `install_only` (about
  280 MB staged before removing Playwright's duplicate Node and build-only pip).

Both archives are checked against a sha256 pinned in `tools/python-runtime.ts`. The
runtime wheels come from `uv export --no-dev` over `uv.lock`, with
`--only-binary :all:` and `--require-hashes`. The staged set is compared with the
resolved set both ways, and dev dependencies are refused by name. Every staged file is
checked for ELF or Mach-O headers that don't belong to the target. Zip archives are
unpacked by `tools/zip.ts`, which checks every CRC, every entry name and the file
count. The tree is built in a scratch directory and moved into place last, so a failed
staging leaves the previous one untouched.

Intel Macs, Linux and Windows on ARM get no bundled Python yet. Playwright's Chromium
isn't bundled either, so browser handoff needs it installed separately.

### What `desktop:stage` checks

`pnpm desktop:build` runs `pnpm desktop:stage` first (it is `beforeBuildCommand`).
It works out the target triple once and passes it to both staging steps.
`TAURI_ENV_TARGET_TRIPLE`, set by `tauri build`, is followed. If `--target` or
`$VENATOR_DESKTOP_TARGET` names a different triple, the build is refused and both
values are shown. Run by hand, it uses `--target`, then `$VENATOR_DESKTOP_TARGET`,
then the Rust host triple.

For a target with a pinned Python (`x86_64-pc-windows-msvc`, `aarch64-apple-darwin`),
it stages Python if needed and refuses to build without it.
`VENATOR_BUNDLE_WITHOUT_PYTHON=1` skips that on purpose, and says so. Other targets
report that they carry no Python.

`resources/python/` always holds at least a `README.txt`, because Tauri refuses a
resource glob that matches nothing. `pnpm sidecar` also removes any Python staged for
a different target, so a Mac bundle never carries Windows binaries.

### How the app finds its Python

`src-tauri/src/lib.rs` looks for `resources/python/<triple>/python.exe` on Windows,
or `resources/python/<triple>/python/bin/python3.13` on Apple silicon, and passes it
to the API as `VENATOR_BUNDLED_PYTHON`. `pythonInterpreter` in `server/locations.ts`
then picks, in order: `$VENATOR_PYTHON`, the bundled Python, the checkout's `.venv`,
then a bare `python3` (`python` on Windows). If the bundled Python won't start, the
error says so. It never falls back to a system Python.

## Layout

```
server/     Hono API
  app.ts              the five mounts, in order
  locations.ts        where everything lives
  db.ts               opening the view and checking its schema
  queries.ts          SQL; rows.ts decodes it; jev.ts decodes Jev rows
  routes.ts           the read-only /api routes
  install-settings.ts the assistant choice and the Jev key
  http/               the header and origin check the action routes share
  onboarding/         setup routes, the Profile writer, the Python adapters
  runs/               the run routes, plans, the runner, progress parsing
  applications/       the application routes
shared/     contracts.ts, onboarding.ts, runs.ts, ports.ts: types both sides compile against
src/        React app
  router.ts, keys.ts, api.ts, lists.ts, labels.ts, margin.ts, application.ts
  components/         sidebar, toolbar, list cells, the Posting page, sheets, help
  views/              workspace (list and page), inspector, employers, first-run states
  views/onboarding/   the three setup steps; copy.ts holds every setup sentence
  onboarding/, runs/  the clients for those routes
  styles/             tokens.css, base.css, window.css, onboarding.css
tests/      node --test units
fixtures/   the sample database builder
src-tauri/  the desktop host
tools/      dev, smoke, snapshot, and the sidecar and Python staging scripts
```

## Environment variables

| Variable | Used for |
|---|---|
| `VENATOR_HOME` | the Install's data directory |
| `VENATOR_VIEW_DB` | an explicit view database |
| `VENATOR_SAMPLE_DATA` | serve the sample fixture when there's no real view |
| `VENATOR_PROFILE` | which Profile the dashboard uses when there's more than one |
| `VENATOR_PYTHON` | the Python to run the pipeline with |
| `VENATOR_BUNDLED_PYTHON` | set by the desktop host; `VENATOR_PYTHON` wins over it |
| `VENATOR_API_PORT` | the API port (default 5170) |
| `TYPESAFE_API_KEY` | the Jev key; passed only to the Jev child |

## Rules for changes

- **One place builds the API URL:** `API_BASE_URL` in `src/api.ts`, from
  `VITE_API_BASE`.
- **Hash routing, no cookies, no origin assumptions.** The same bundle runs from
  Vite, from the Hono server and from a webview loading `dist/` off disk
  (`base: "./"`).
- **Employer HTML is untrusted.** It renders inside a `sandbox=""` iframe. Keep it
  that way.
- **The person submits.** Open Application fills what it can and stops, and "You press
  submit" appears next to it wherever it shows up. A disabled button stays visibly
  disabled, with the reason written beside it.
- **Light and dark** follow `prefers-color-scheme`. `data-theme="light"` or `"dark"`
  on `<html>` overrides it.
- **The window is opaque.** macOS-only window keys (`titleBarStyle: "Overlay"`,
  `hiddenTitle`) live in `src-tauri/tauri.macos.conf.json`. `src/platform.ts` adds
  `app-native` on desktop hosts and leaves room for the traffic lights on a Mac.
- **No raw identifiers on screen.** `src/labels.ts` is the only place a rule id, board
  slug, status or Posting key becomes English. `pnpm smoke` fails if one reaches
  visible text.
- **node:sqlite**, not better-sqlite3: it's in the standard library and needs no
  native build.
