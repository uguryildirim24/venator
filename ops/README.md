# Scheduled pipeline operations

The optional `dev.venator.loop` user agent can run the Venator pipeline every
six hours from a checkout. No agent is installed or loaded by this repository.
When explicitly activated, the loop uses the local `uv` executable, creates
local data commits when needed, and never pushes them.

The agent definition is **generated, not committed**: launchd has no shell, no
`PATH`, and no working directory of its own, so the plist needs absolute paths —
and those are one person's home directory. `venator.schedule.agent` resolves
them from this checkout and the current account at install time.

Activation is an explicit Owner operation — building or testing Venator does not
run `launchctl`.

## Inspect before activation

Run from the repo root:

```sh
uv run python -m venator.schedule.agent            # print the resolved plist
uv run python -m venator.schedule.agent --out /tmp/dev.venator.loop.plist && plutil -lint /tmp/dev.venator.loop.plist
uv run python -m venator.schedule.loop --dry-run
```

Add `--profile <name>` when more than one Profile exists, so the scheduled loop
runs for the one you mean rather than refusing to guess. `--uv`, `--home`, and
`--repository` override what the generator resolved.

## Install and activate

```sh
mkdir -p "$HOME/Library/Logs/venator" "$HOME/Library/LaunchAgents" && uv run python -m venator.schedule.agent --out "$HOME/Library/LaunchAgents/dev.venator.loop.plist" && launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/dev.venator.loop.plist"
```

The plist does not use `RunAtLoad`, so the first automatic invocation is due
after the six-hour `StartInterval`.

Moving the checkout invalidates the installed plist's `WorkingDirectory` —
regenerate and re-bootstrap after a move.

## Uninstall

```sh
launchctl bootout "gui/$UID" "$HOME/Library/LaunchAgents/dev.venator.loop.plist" && rm "$HOME/Library/LaunchAgents/dev.venator.loop.plist"
```

## Logs and health

Tail both launchd streams:

```sh
tail -F "$HOME/Library/Logs/venator/loop.stdout.log" "$HOME/Library/Logs/venator/loop.stderr.log"
```

Inspect the loaded agent and its next-run bookkeeping:

```sh
launchctl print "gui/$UID/dev.venator.loop"
```

Verify heartbeats after a scheduled run, from the repo root:

```sh
tail -n 10 data/runs.jsonl && git log -1 --oneline -- data/
```

Every executed stage writes `at`, `status`, and `stage`. An `error` heartbeat
names the failed stage, and later stages do not run.
