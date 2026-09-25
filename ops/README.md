# Running the pipeline on a timer (macOS)

If you want Venator to fetch on its own, you can install a launchd agent,
`dev.venator.loop`, that runs the loop every six hours from your checkout. Nothing in
this repository installs or loads it. Building or testing Venator never runs
`launchctl`.

The agent runs `uv run python -m venator.schedule.loop`, which does Discover, the Hard
Filters and a view rebuild. It never runs Jev and never commits or pushes anything.

The plist is generated, not committed. launchd gives a job no shell, no `PATH` and no
working directory, so the plist needs absolute paths, and those are specific to your
account. `venator.schedule.agent` fills them in from this checkout when you install
it. It only works on macOS.

## Look before you install

From the repository root:

```sh
uv run python -m venator.schedule.agent            # print the plist
uv run python -m venator.schedule.agent --out /tmp/dev.venator.loop.plist && plutil -lint /tmp/dev.venator.loop.plist
uv run python -m venator.schedule.loop --dry-run
```

If you have more than one Profile, add `--profile <name>` so the timer runs the one
you mean instead of refusing to guess. `--uv`, `--home` and `--repository` override
the paths the generator found, and `--interval` changes the six hours (in seconds).

## Install

```sh
mkdir -p "$HOME/Library/Logs/venator" "$HOME/Library/LaunchAgents" && uv run python -m venator.schedule.agent --out "$HOME/Library/LaunchAgents/dev.venator.loop.plist" && launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/dev.venator.loop.plist"
```

The plist doesn't set `RunAtLoad`, so the first run happens six hours after you load
it. If you move the checkout, the plist's `WorkingDirectory` is wrong. Generate it
again and bootstrap it again.

## Uninstall

```sh
launchctl bootout "gui/$UID" "$HOME/Library/LaunchAgents/dev.venator.loop.plist" && rm "$HOME/Library/LaunchAgents/dev.venator.loop.plist"
```

## Logs and health

Follow both logs:

```sh
tail -F "$HOME/Library/Logs/venator/loop.stdout.log" "$HOME/Library/Logs/venator/loop.stderr.log"
```

See the loaded job and when it runs next:

```sh
launchctl print "gui/$UID/dev.venator.loop"
```

Check the heartbeats after a run. They are in the Install's data directory (or in
`$VENATOR_HOME/data/` if you set it):

```sh
tail -n 10 "$HOME/Library/Application Support/Venator/data/runs.jsonl"
```

Each stage that runs writes a line with `at`, `status` and `stage`. If a stage fails,
its line has `status` `error` and names it, and the stages after it don't run.
