# Daily run on macOS

The installed Mac app owns a launchd agent for the daily run. In the app's Profile
settings, choose a time and daily and monthly Score limits and enable the run.
Disabling it removes the agent. Building and testing Venator do not load it.

The agent runs the app's bundled Python, not a checkout. It uses the Install's data
folder, starts at 06:30 by default, and does not run when first loaded. It runs
Discover, Hard Filters, a capped batch of Score, rechecks new picks and saved
Postings, rebuilds View and notifies you. If Score pauses, earlier scores stay;
the other stages still run. A cross-process lock prevents the daily run and a
manual run from writing at the same time. No stage submits an Application.

The app generates the plist at `~/Library/LaunchAgents/dev.venator.loop.plist`.
You can inspect its contents without installing it:

```sh
python -m venator.schedule.agent
python -m venator.schedule.loop --dry-run --profile NAME
```

For a terminal run, use `uv run python` in place of `python`. The standalone
agent command accepts `--interpreter`, `--install`, `--profile`, `--time HH:MM`,
`--daily-usd` and `--monthly-usd` if you need to generate a plist yourself. The
app manages its own agent when you change its settings; don't install a second
copy for the same Install.

To check a loaded agent and its logs:

```sh
launchctl print "gui/$UID/dev.venator.loop"
tail -F "$HOME/Library/Logs/venator/loop.stdout.log" "$HOME/Library/Logs/venator/loop.stderr.log"
```

Run heartbeats are appended to the Install's `data/runs.jsonl`. Each stage writes
`at`, `status` and `stage`; an error row names the failing stage.
